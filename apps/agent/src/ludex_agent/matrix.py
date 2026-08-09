"""Runner/matriz dinamica de compatibilidad de proveedores (F2-10B/MON-20).

Refresca los catalogos /models antes de cada ronda, compara altas/bajas
contra el inventario commiteado, construye un manifiesto reproducible con
una fila por provider/model (pin estricto, concurrency=1, persist=false,
dos batallas solo si el smoke pasa) y planifica el presupuesto por
provider con hard caps.

Reglas de presupuesto (addendum MON-20): si el saldo no alcanza para
reservar smoke + dos batallas de un modelo, su fila queda en
`pending-budget` / `not-run` — NUNCA `unsupported`, `incompatible` ni
`externally-limited` — preservando protocolo, ruta y costo estimado, y
sin publicar winrate.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import httpx

from .graph.provider import (
    ModelRoute,
    load_model_routes,
    model_route,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
INVENTORY_DIR = REPO_ROOT / "apps" / "agent" / "evals" / "runs"

# Anclas de tokens por batalla medida (BENCHMARKS.md 2026-07-28/2026-08-08):
# deepseek-v4-flash: ~89k input / 60k output por batalla; gemini-2.5-flash:
# ~40k input por llamada y una batalla ronda las 30-50 llamadas. La ancla de
# presupuesto usa 1.5M input / 60k output por batalla (conservador, cota
# superior) y 40k input / 2k output por smoke (una completion).
ANCHOR_TOKENS_PER_BATTLE = (Decimal("1500000"), Decimal("60000"))
ANCHOR_TOKENS_PER_SMOKE = (Decimal("40000"), Decimal("2000"))
BATTLES_PER_MODEL = 2


@dataclass(frozen=True)
class CatalogEntry:
    provider: str
    model: str
    protocol: str | None
    endpoint: str | None
    structured_output: str | None
    tier: str
    price_input_usd: Decimal | None
    price_output_usd: Decimal | None
    price_source: str | None
    deprecated: bool = False
    exclusion_reason: str | None = None
    in_scope: bool = True
    route_present: bool = True


@dataclass(frozen=True)
class ManifestRow:
    provider: str
    model: str
    protocol: str | None
    endpoint: str | None
    structured_output: str | None
    tier: str
    status: str
    battles: int
    concurrency: int
    persist: bool
    pin: tuple[str, str]
    estimated_cost_usd: Decimal | None
    estimated_smoke_usd: Decimal | None
    cumulative_cost_usd: Decimal | None = None
    classification_note: str | None = None


async def refresh_models(
    provider: str,
    *,
    base_url: str | None,
    api_key: str,
    environ: Mapping[str, str],
    client: httpx.AsyncClient | None = None,
) -> list[str]:
    """Catalogo fresco de /models (metadata, sin cuota). Google filtra por
    `generateContent`; los demas devuelven el shape OpenAI
    `{"data": [{"id": ...}]}`."""
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        if provider == "google":
            keys = environ.get("GEMINI_API_KEY", "").split(",")
            key = next((k.strip() for k in keys if k.strip()), api_key)
            response = await client.request(
                "GET",
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"key": key},
                timeout=30,
            )
            response.raise_for_status()
            document = response.json()
            models = document.get("models", [])
            return [
                entry["name"].removeprefix("models/")
                for entry in models
                if isinstance(entry, dict)
                and "generateContent" in entry.get(
                    "supportedGenerationMethods", []
                )
            ]
        response = await client.request(
            "GET",
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30,
        )
        response.raise_for_status()
        document = response.json()
        data = document.get("data") if isinstance(document, dict) else None
        if not isinstance(data, list):
            raise RuntimeError(
                f"{provider} /models sin forma {{'data': [...]}}"
            )
        return [
            entry["id"]
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("id"), str)
        ]
    finally:
        if owns_client:
            await client.aclose()


def delta_catalog(
    previous: Mapping[str, Sequence[str]],
    fresh: Mapping[str, Sequence[str]],
) -> dict[str, dict[str, list[str]]]:
    """Altas/bajas de catalogos entre el inventario commiteado y el fresh."""
    delta: dict[str, dict[str, list[str]]] = {}
    providers = set(previous) | set(fresh)
    for provider in sorted(providers):
        old = set(previous.get(provider, []))
        new = set(fresh.get(provider, []))
        delta[provider] = {
            "added": sorted(new - old),
            "removed": sorted(old - new),
        }
    return delta


def _route_for(routes: Mapping[tuple[str, str], ModelRoute], provider: str, model: str) -> ModelRoute | None:
    try:
        return model_route(routes, provider, model)
    except ValueError:
        return None


def build_manifest(
    fresh_models: Mapping[str, Sequence[str]],
    *,
    routes: Mapping[tuple[str, str], ModelRoute] | None = None,
    previous_inventory: Mapping[str, Any] | None = None,
    tier_prices: Mapping[tuple[str, str], tuple[str, str, str, str]] | None = None,
) -> list[ManifestRow]:
    """Una fila por provider/model del catalogo fresco.

    Clasificaciones explicitas (nada desaparece en silencio):
    - `excluded:<razon>`: fuera de scope con razon de capacidad (inventario);
    - `missing-route`: modelo descubierto sin ruta declarativa -> no se
      inventa un protocolo ni se ensaya pagando;
    - `ready`: smoke pendiente; si pasa, `battles` = 2 batallas pinneadas.
    """
    if routes is None:
        routes = load_model_routes()
    if previous_inventory is None:
        previous_inventory = {}
    if tier_prices is None:
        tier_prices = {}
    previous_models = previous_inventory.get("models", {})
    rows: list[ManifestRow] = []
    for provider in sorted(fresh_models):
        for model in sorted(fresh_models[provider]):
            prev_row = None
            for entry in previous_models.get(provider, []):
                if entry.get("id") == model:
                    prev_row = entry
                    break
            exclusion = (
                prev_row.get("exclusion_reason")
                if prev_row is not None and not prev_row.get("in_scope", True)
                else None
            )
            route = _route_for(routes, provider, model)
            if exclusion is not None:
                rows.append(ManifestRow(
                    provider=provider, model=model,
                    protocol=None, endpoint=None, structured_output=None,
                    tier="unknown", status=f"excluded:{exclusion}",
                    battles=0, concurrency=1, persist=False,
                    pin=(provider, model),
                    estimated_cost_usd=None, estimated_smoke_usd=None,
                    classification_note="fuera de scope (razon de capacidad)",
                ))
                continue
            if route is None and provider != "google":
                rows.append(ManifestRow(
                    provider=provider, model=model,
                    protocol=None, endpoint=None, structured_output=None,
                    tier="unknown", status="missing-route",
                    battles=0, concurrency=1, persist=False,
                    pin=(provider, model),
                    estimated_cost_usd=None, estimated_smoke_usd=None,
                    classification_note=(
                        "sin ruta declarativa: no se ensaya protocolo ni se "
                        "gasta cuota; clasificacion explicita"
                    ),
                ))
                continue
            if route is None:
                # Proveedor nativo sin capa de rutas (google): el protocolo
                # es el del kind, no se declara por modelo.
                route = ModelRoute(protocol="google")
            # Tier/precio: la tabla de precios manda; el inventario puede
            # sobreescribir el tier (p.ej. "unknown" aunque el precio sea 0)
            # o aportar precio cuando la tabla no lo cubre.
            table_hit = tier_prices.get((provider, model))
            inv_tier = prev_row.get("tier_override") if prev_row else None
            if table_hit is not None:
                tier, price_in, price_out, source = table_hit
                if inv_tier is not None:
                    tier = inv_tier
            elif prev_row is not None and prev_row.get("prices"):
                tier = prev_row.get("tier", "unknown")
                prices = prev_row["prices"]
                price_in = prices.get("input_per_million")
                price_out = prices.get("output_per_million")
                source = prices.get("source_url")
            else:
                tier, price_in, price_out, source = (
                    "unknown", None, None, None
                )
            price_in_d = Decimal(price_in) if price_in is not None else None
            price_out_d = Decimal(price_out) if price_out is not None else None
            cost = _estimate_cost(route, tier, price_in_d, price_out_d)
            rows.append(ManifestRow(
                provider=provider, model=model,
                protocol=route.protocol, endpoint=route.endpoint,
                structured_output=route.structured_output,
                tier=tier, status="ready",
                battles=BATTLES_PER_MODEL, concurrency=1, persist=False,
                pin=(provider, model),
                estimated_cost_usd=cost[0], estimated_smoke_usd=cost[1],
                classification_note=f"precios: {source or 'sin fuente'}",
            ))
    return rows


def _estimate_cost(
    route: ModelRoute,
    tier: str,
    price_in: Decimal | None,
    price_out: Decimal | None,
) -> tuple[Decimal | None, Decimal | None]:
    if tier == "free":
        return Decimal("0"), Decimal("0")
    if price_in is None or price_out is None:
        return None, None
    battles_in, battles_out = ANCHOR_TOKENS_PER_BATTLE
    smoke_in, smoke_out = ANCHOR_TOKENS_PER_SMOKE
    per_battle = (
        battles_in * price_in + battles_out * price_out
    ) / Decimal("1000000")
    smoke = (smoke_in * price_in + smoke_out * price_out) / Decimal("1000000")
    return per_battle * BATTLES_PER_MODEL, smoke


@dataclass(frozen=True)
class BudgetSpec:
    balance_usd: Decimal
    cap_usd: Decimal
    leave_usd: Decimal


def plan_budget(
    rows: Sequence[ManifestRow],
    budgets: Mapping[str, BudgetSpec],
) -> list[ManifestRow]:
    """Ordena por costo estimado ascendente y aplica hard-stop por provider.

    Reglas (addendum MON-20): un modelo pago solo se inicia si su reserva
    (smoke + 2 batallas) entra en el cap ANTES de quemar el saldo. Si no
    alcanza: `pending-budget` (nunca unsupported/incompatible/externally-
    limited) preservando protocolo/ruta/costo. `free` siempre entra. Rows
    sin costo estimado se clasifican `pending-budget` (no se puede probar
    que cuesten cero).
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            row.estimated_cost_usd is not None,
            row.estimated_cost_usd if row.estimated_cost_usd is not None
            else Decimal(0),
            row.provider,
            row.model,
        ),
    )
    spent: dict[str, Decimal] = {}
    result: list[ManifestRow] = []
    for row in ordered:
        if row.status not in {"ready"}:
            result.append(row)
            continue
        spec = budgets.get(row.provider)
        if row.tier == "free":
            result.append(row)
            continue
        if spec is None or row.estimated_cost_usd is None:
            result.append(_as_status(
                row, "pending-budget",
                "sin presupuesto configurado o costo no estimable: no se "
                "puede probar costo cero",
            ))
            continue
        used = spent.get(row.provider, Decimal("0"))
        reserve = (row.estimated_smoke_usd or Decimal(0)) + row.estimated_cost_usd
        allowed = spec.cap_usd - spec.leave_usd
        if used + reserve > allowed or used + reserve > spec.balance_usd:
            result.append(_as_status(
                row, "pending-budget",
                f"no alcanza el presupuesto: reserva {reserve} vs "
                f"disponible {allowed - used}",
            ))
            continue
        spent[row.provider] = used + reserve
        result.append(_as_status(
            row, "ready",
            row.classification_note,
            cumulative=used + reserve,
        ))
    return result


def _as_status(
    row: ManifestRow, status: str, note: str | None, cumulative: Decimal | None = None
) -> ManifestRow:
    return ManifestRow(
        provider=row.provider, model=row.model,
        protocol=row.protocol, endpoint=row.endpoint,
        structured_output=row.structured_output,
        tier=row.tier, status=status, battles=row.battles,
        concurrency=row.concurrency, persist=row.persist, pin=row.pin,
        estimated_cost_usd=row.estimated_cost_usd,
        estimated_smoke_usd=row.estimated_smoke_usd,
        cumulative_cost_usd=cumulative,
        classification_note=note or row.classification_note,
    )


def tier_prices_from_pricing_table(
    pricing: Any,
) -> dict[tuple[str, str], tuple[str, str, str, str]]:
    """Convierte una PricingTable en el mapa (provider, model) ->
    (tier, input, output, source). Precio 0/0 -> free; con precio -> paid."""
    result: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for (provider, model), entry in pricing.entries.items():
        zero = (
            entry.input_per_million == 0 and entry.output_per_million == 0
        )
        tier = "free" if zero else "paid"
        result[(provider, model)] = (
            tier,
            str(entry.input_per_million),
            str(entry.output_per_million),
            entry.source_url,
        )
    return result


def manifest_to_dict(rows: Sequence[ManifestRow]) -> dict[str, Any]:
    return {
        "rows": [asdict(row) for row in rows],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
