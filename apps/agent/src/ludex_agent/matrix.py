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


def select_rows_for_tier(
    rows: Sequence[ManifestRow], tier: str
) -> list[ManifestRow]:
    """Filas ejecutables de una fase/tier (fail-closed).

    Solo filas `ready` del tier pedido: un manifiesto con filas paid y una
    fase `free` ejecuta unicamente free y NUNCA toca una fila paid (canario
    del ejecutor R1 de MON-20). El runner ademas revalida el tier justo
    antes de construir el provider de cada fila seleccionada."""
    return [row for row in rows if row.status == "ready" and row.tier == tier]


@dataclass(frozen=True)
class MatrixModelResult:
    provider: str
    model: str
    tier: str
    protocol: str | None
    status: str
    smoke_ok: bool
    battles_requested: int
    battles_completed: int
    effective_provider: str | None
    effective_model: str | None
    win_rate: str | None
    completion_latency_ms: dict[str, int | None] | None
    decision_latency_ms: dict[str, int | None] | None
    tokens: dict[str, int] | None
    retries: int
    rotations: int
    quarantined: int
    failure_type: str | None
    failure_cause_type: str | None
    note: str | None = None

    @property
    def final(self) -> bool:
        """Clasificaciones terminales: un modelo finalizado no se repite en
        una reanudacion ni se salta sin clasificacion."""
        return self.status not in {"running", "pending"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


FINAL_STATUSES = frozenset({
    "compatible", "invalid-semantic-response", "credential/model unavailable",
    "unsupported-protocol", "externally-limited", "internal-defect",
    "aborted", "missing-route", "not-in-tier", "removed-from-catalog",
    "already-finalized", "smoke-failed",
})


async def run_matrix_round(
    *,
    rows: Sequence[ManifestRow],
    tier: str,
    battle_timeout_seconds: float,
    fmt: str,
    opponent: str,
    smoke_deadline_seconds: float,
    build_provider: Callable[[str, str], Any],
    run_battles: Callable[..., Awaitable[
        tuple[Any, Mapping[str, int | None]]
    ]],
    refresh_catalog: Callable[[], Awaitable[dict[str, list[str]]]] | None,
    previous: Mapping[str, MatrixModelResult] | None = None,
    on_result: Callable[[MatrixModelResult], None] | None = None,
) -> list[MatrixModelResult]:
    """Ejecuta una fase de la matriz con fail-closed.

    - selecciona SOLO filas `ready` del tier pedido (nunca mezcla);
    - refresca el catalogo antes de la ronda: un modelo que ya no esta en
      /models se clasifica `removed-from-catalog`, no se ejecuta;
    - por modelo: 1 smoke -> si pasa, EXACTAMENTE 2 batallas pinneadas
      (sin chains ni fallback cruzado: el pin lo audita el runner de
      batallas con enforce_pin=True);
    - `previous` permite reanudar: filas con clasificacion final no se
      repiten (`already-finalized`) y las sin clasificacion se reejecutan
      (nunca se salta una sin clasificar);
    - un resultado parcial/abortado nunca publica winrate comparable.
    """
    results: list[MatrixModelResult] = []
    previous = previous or {}

    selected = select_rows_for_tier(rows, tier)
    offenders = [row for row in selected if row.tier != tier]
    if offenders:
        # Fail-closed ANTES del primer request: aunque el filtro de
        # seleccion se rompa, ninguna fila fuera de la fase llega al
        # provider (canario: quitar el filtro -> rojo sin llamadas).
        raise ValueError(
            "fila(s) fuera de la fase "
            + ", ".join(f"{r.provider}/{r.model}" for r in offenders)
        )

    fresh: dict[str, list[str]] = {}
    if refresh_catalog is not None:
        fresh = await refresh_catalog() or {}

    for row in selected:
        key = f"{row.provider}/{row.model}"
        prior = previous.get(key)
        if prior is not None and prior.final:
            results.append(MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="already-finalized",
                smoke_ok=prior.smoke_ok,
                battles_requested=prior.battles_requested,
                battles_completed=prior.battles_completed,
                effective_provider=prior.effective_provider,
                effective_model=prior.effective_model,
                win_rate=prior.win_rate,
                completion_latency_ms=prior.completion_latency_ms,
                decision_latency_ms=prior.decision_latency_ms,
                tokens=prior.tokens, retries=prior.retries,
                rotations=prior.rotations, quarantined=prior.quarantined,
                failure_type=prior.failure_type,
                failure_cause_type=prior.failure_cause_type,
                note="ya finalizado en una corrida anterior: no se repite",
            ))
            if on_result is not None:
                on_result(results[-1])
            continue
        if refresh_catalog is not None and (
            row.model not in fresh.get(row.provider, [])
        ):
            result = MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="removed-from-catalog",
                smoke_ok=False, battles_requested=0, battles_completed=0,
                effective_provider=None, effective_model=None,
                win_rate=None, completion_latency_ms=None,
                decision_latency_ms=None, tokens=None,
                retries=0, rotations=0, quarantined=0,
                failure_type=None, failure_cause_type=None,
                note="ya no esta en /models fresco: no se ejecuta",
            )
            results.append(result)
            if on_result is not None:
                on_result(result)
            continue
        try:
            result = await _run_one(
                row=row, battle_timeout_seconds=battle_timeout_seconds,
                fmt=fmt, opponent=opponent,
                smoke_deadline_seconds=smoke_deadline_seconds,
                build_provider=build_provider, run_battles=run_battles,
            )
        except Exception as exc:  # noqa: BLE001 - fallo interno del runner
            result = MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="internal-defect",
                smoke_ok=False, battles_requested=0, battles_completed=0,
                effective_provider=None, effective_model=None,
                win_rate=None, completion_latency_ms=None,
                decision_latency_ms=None, tokens=None,
                retries=0, rotations=0, quarantined=0,
                failure_type=type(exc).__name__,
                failure_cause_type=(
                    type(exc.__cause__).__name__ if exc.__cause__ else None
                ),
                note="fallo del runner de la matriz, no del modelo",
            )
        results.append(result)
        if on_result is not None:
            on_result(result)
    return results


async def _run_one(
    *,
    row: ManifestRow,
    battle_timeout_seconds: float,
    fmt: str,
    opponent: str,
    smoke_deadline_seconds: float,
    build_provider: Callable[[str, str], Any],
    run_battles: Callable[..., Awaitable[
        tuple[Any, Mapping[str, int | None]]
    ]],
) -> MatrixModelResult:
    from .graph.decision import DecisionResponse
    from .graph.provider import (
        CredentialRejected,
        DecisionDeadlineExceeded,
        FatalProviderError,
        ProviderError,
        ProviderPoolExhausted,
        TransientProviderError,
    )

    # --- smoke: una completion estructurada, sin Showdown ---------------
    try:
        provider = build_provider(row.provider, row.model)
    except ProviderError as exc:
        return _smoke_failed(
            row, "credential/model unavailable", exc
        )
    except ValueError as exc:
        return _smoke_failed(
            row, "missing-route", exc
        )
    try:
        envelope = await provider.complete(
            _SMOKE_PROMPT,
            deadline=time.monotonic() + smoke_deadline_seconds,
            turn_id=f"matrix-smoke:{row.provider}:{row.model}",
        )
    except ProviderPoolExhausted as exc:
        return _smoke_failed(row, "credential/model unavailable", exc)
    except CredentialRejected as exc:
        return _smoke_failed(row, "credential/model unavailable", exc)
    except ProviderError as exc:
        # FatalProviderError: endpoint/protocolo/modelo rechazado (la clase
        # exacta se preserva en failure_type). Transitorio o deadline:
        # limite externo.
        status = (
            "unsupported-protocol"
            if isinstance(exc, FatalProviderError)
            else "externally-limited"
        )
        return _smoke_failed(row, status, exc)
    try:
        DecisionResponse.model_validate(envelope.payload)
    except (ValueError, TypeError) as exc:
        return _smoke_failed(
            row, "invalid-semantic-response", exc,
            note="la respuesta del modelo no valida el contrato DecisionResponse",
        )

    # --- smoke verde: exactamente 2 batallas pinneadas -------------------
    try:
        result, metrics = await run_battles(
            row.provider, row.model,
            n=BATTLES_PER_MODEL, battle_timeout_seconds=battle_timeout_seconds,
            fmt=fmt, opponent=opponent,
        )
    except ProviderError as exc:
        # ProviderMixError (mezcla efectiva) aborta la corrida del modelo.
        status = "internal-defect" if type(exc).__name__ == "ProviderMixError" \
            else "externally-limited"
        return _smoke_failed(row, status, exc, smoke_ok=True)

    completed = getattr(result, "completed", 0)
    requested = getattr(result, "requested", 0)
    failure = getattr(result, "failure", None)
    failure_type = getattr(result, "failure_type", None)
    if failure_type == "ProviderMixError":
        status = "internal-defect"
    elif failure_type == "BenchmarkDeadlineExceeded":
        status = "externally-limited"
    elif failure is not None:
        status = "aborted"
    elif completed == requested:
        status = "compatible"
    else:
        status = "externally-limited"
    win_rate = None
    if status == "compatible" and completed:
        wins = getattr(result, "wins", 0)
        win_rate = f"{wins / completed:.4f}"
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status=status, smoke_ok=True,
        battles_requested=requested, battles_completed=completed,
        effective_provider=getattr(result, "provider", None),
        effective_model=getattr(result, "model", None),
        win_rate=win_rate,
        completion_latency_ms=_latency_slice(metrics, "completion_latency_ms"),
        decision_latency_ms=_latency_slice(metrics, "decision_latency_ms"),
        tokens={
            "input_tokens": int(metrics.get("input_tokens", 0)),
            "output_tokens": int(metrics.get("output_tokens", 0)),
            "cached_input_tokens": int(metrics.get("cached_input_tokens", 0)),
            "reasoning_tokens": int(metrics.get("reasoning_tokens", 0)),
        },
        retries=int(metrics.get("turns_transient_affected", 0)),
        rotations=int(metrics.get("key_rotations", 0)),
        quarantined=int(metrics.get("keys_quarantined", 0)),
        failure_type=getattr(result, "failure_type", None),
        failure_cause_type=getattr(result, "failure_cause_type", None),
        note=None if status == "compatible" else (
            "batallas parciales o abortadas: sin winrate comparable"
        ),
    )


_SMOKE_PROMPT = (
    "Elegí exactamente una acción legal y respondé con action, un "
    "rationale breve, confidence en [0,1] y alternatives (puede ser []). "
    "legal_actions="
    '[{"kind":"move","id":"tackle"},{"kind":"switch","species":"pikachu"}]'
)


def _smoke_failed(
    row: ManifestRow,
    status: str,
    exc: BaseException,
    *,
    note: str | None = None,
    smoke_ok: bool = False,
) -> MatrixModelResult:
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status=status, smoke_ok=smoke_ok,
        battles_requested=0, battles_completed=0,
        effective_provider=None, effective_model=None,
        win_rate=None, completion_latency_ms=None,
        decision_latency_ms=None, tokens=None,
        retries=0, rotations=0, quarantined=0,
        failure_type=type(exc).__name__,
        failure_cause_type=(
            type(exc.__cause__).__name__ if exc.__cause__ else None
        ),
        note=note or f"smoke fallido ({type(exc).__name__})",
    )


def _latency_slice(
    metrics: Mapping[str, int | None], prefix: str
) -> dict[str, int | None]:
    return {
        "count": metrics.get(f"{prefix}_count"),
        "total": metrics.get(f"{prefix}_total"),
        "p50": metrics.get(f"{prefix}_p50"),
        "p95": metrics.get(f"{prefix}_p95"),
        "max": metrics.get(f"{prefix}_max"),
    }


def manifest_to_dict(rows: Sequence[ManifestRow]) -> dict[str, Any]:
    return {
        "rows": [asdict(row) for row in rows],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
