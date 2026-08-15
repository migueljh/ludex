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

import asyncio
import json
import logging
import time
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

import httpx

from .graph.provider import (
    ModelRoute,
    load_model_routes,
    model_route,
    _http_status_chain,
    _structured_provider_error_code,
)

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[4]
INVENTORY_DIR = REPO_ROOT / "apps" / "agent" / "evals" / "runs"
DEFAULT_OPERATOR_POLICY_PATH = (
    REPO_ROOT / "apps" / "agent" / "evals" / "operator-policy.json"
)


def load_operator_policy(
    path: str | Path | None = None,
) -> dict[tuple[str, str], str]:
    """Politica declarativa de prohibicion de modelos (I3/MON-20 R2).

    Versionada en `apps/agent/evals/operator-policy.json`: un mapa
    (provider, model) -> accion, SIN condicionales por nombre en src. La
    prohibicion de `open_code_zen/gpt-5.6-luna` es permanente por
    instruccion del usuario; `build_manifest` la marca en manifiestos
    nuevos y `run_matrix_round` la rechaza antes del primer request aunque
    un manifiesto versionado la traiga como `ready`. Archivo ausente =
    error explicito (fail-closed): sin politica no se certifica nada."""
    if path is None:
        path = DEFAULT_OPERATOR_POLICY_PATH
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    policy: dict[tuple[str, str], str] = {}
    for entry in document.get("entries", []):
        policy[(entry["provider"], entry["model"])] = entry["action"]
    return policy

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
    policy = load_operator_policy()
    rows: list[ManifestRow] = []
    for provider in sorted(fresh_models):
        for model in sorted(fresh_models[provider]):
            # I3 (MON-20 R2): politica declarativa versionada ANTES que
            # cualquier otra clasificacion: una prohibicion de operador
            # (p.ej. gpt-5.6-luna, permanente) no se negocia con rutas,
            # precios ni inventario.
            action = policy.get((provider, model))
            if action is not None:
                rows.append(ManifestRow(
                    provider=provider, model=model,
                    protocol=None, endpoint=None, structured_output=None,
                    tier="unknown", status="operator-prohibited",
                    battles=0, concurrency=1, persist=False,
                    pin=(provider, model),
                    estimated_cost_usd=None, estimated_smoke_usd=None,
                    classification_note=(
                        f"operator-prohibited ({action}): prohibido por "
                        "politica declarativa versionada; no se ejecuta"
                    ),
                ))
                continue
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
            # Tier/precio (DIAG-B): la tabla de precios manda; el inventario
            # puede sobreescribir SOLO el tier (p.ej. "unknown" aunque el
            # precio sea 0) o aportar precio cuando la tabla no lo cubre.
            table_hit = tier_prices.get((provider, model))
            inv_tier = prev_row.get("tier_override") if prev_row else None
            if table_hit is not None:
                tier, price_in, price_out, source = table_hit
                if inv_tier is not None:
                    tier = inv_tier
            elif prev_row is not None and prev_row.get("prices"):
                prices = prev_row["prices"]
                price_in = prices.get("input_per_million")
                price_out = prices.get("output_per_million")
                source = prices.get("source_url")
                if inv_tier is not None:
                    tier = inv_tier
                else:
                    # Sin override: free SOLO cuando input y output son
                    # exactamente 0; con precios no cero se infiere paid.
                    zero = (
                        price_in is not None
                        and price_out is not None
                        and Decimal(str(price_in)) == 0
                        and Decimal(str(price_out)) == 0
                    )
                    tier = "free" if zero else "paid"
            else:
                # Sin tabla ni precios se conserva el override explicito
                # (free verificado sin precio sigue free) o queda unknown;
                # jamas se convierte un tier_override: unknown en gratis.
                tier = inv_tier if inv_tier is not None else "unknown"
                price_in, price_out, source = None, None, None
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
    (smoke + 2 batallas) entra en la disponibilidad ANTES de quemar el
    saldo. Si no alcanza: `pending-budget` (nunca
    unsupported/incompatible/externally-limited) preservando
    protocolo/ruta/costo. `free` siempre entra. Rows sin costo estimado se
    clasifican `pending-budget` (no se puede probar que cuesten cero).

    L-02 (MON-20): la disponibilidad es `min(cap_usd, balance_usd -
    leave_usd)` -- el margen de saldo mínimo NO se resta dos veces. Con la
    autorizacion real (Zen 11/10/1 y Kimi 6/5.50/0.50) los limites
    efectivos son exactamente 10.00 y 5.50.
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
        if row.tier == "unknown":
            # DIAG-B: un tier desconocido no puede probar costo cero ni
            # ejecutarse en una fase (free|paid): pending-budget, sin perder
            # protocolo/ruta/costo estimado.
            result.append(_as_status(
                row, "pending-budget",
                "tier desconocido: no se puede probar costo cero ni "
                "ejecutar en una fase",
            ))
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
        allowed = min(spec.cap_usd, spec.balance_usd - spec.leave_usd)
        if used + reserve > allowed:
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
    # L-02 (correccion LATWAN): W/L/T reales de la corrida parcial, cuando
    # existen; None en resultados construidos sin datos de batalla.
    battles_wins: int | None = None
    battles_losses: int | None = None
    battles_ties: int | None = None
    note: str | None = None
    # L-03 (R1A): evidencia durable y sanitizada. `failure_stage` dice si el
    # fallo ocurrio en `smoke` o en `battle`; `http_status` es el status
    # HTTP cuando existe; `provider_error_code` sale SOLO de campos
    # estructurados permitidos (nunca de mensajes, URLs, headers, bodies
    # completos, nombres/valores de variables ni secretos).
    failure_stage: str | None = None
    http_status: int | None = None
    provider_error_code: str | None = None
    # I4 (MON-20 R2): el artefacto atomico es auditable SIN contexto
    # externo: deadline de batalla, identidad de ronda, `generated_at` y
    # referencia + hash del manifiesto que produjo la corrida.
    battle_timeout_seconds: float | None = None
    round: str | None = None
    generated_at: str | None = None
    manifest: str | None = None
    manifest_sha256: str | None = None
    # I5 (MON-20 R2): N=2 nunca es evidencia de calidad: `win_rate` queda
    # null siempre y se publican W/L/T + `comparable=false` +
    # `sample_size` (batallas completadas).
    comparable: bool = False
    sample_size: int | None = None
    # I2 (MON-20 R2): solo los artefactos de stop por interrupcion llevan
    # `compatibility_result=indeterminate-current-run`; las demas filas lo
    # tienen null.
    compatibility_result: str | None = None

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
    round_name: str | None = None,
    manifest_ref: str | None = None,
    manifest_sha256: str | None = None,
) -> list[MatrixModelResult]:
    """Ejecuta una fase de la matriz con fail-closed.

    - selecciona SOLO filas `ready` del tier pedido (nunca mezcla);
    - rechaza ANTES del primer request cualquier fila prohibida por la
      politica declarativa de operador (I3: gpt-5.6-luna permanente), aunque
      el manifiesto la traiga como `ready`;
    - refresca el catalogo antes de la ronda: un modelo que ya no esta en
      /models se clasifica `removed-from-catalog`, no se ejecuta;
    - por modelo: 1 smoke -> si pasa, EXACTAMENTE 2 batallas pinneadas
      (sin chains ni fallback cruzado: el pin lo audita el runner de
      batallas con enforce_pin=True);
    - `previous` permite reanudar: filas con clasificacion final no se
      repiten (`already-finalized`) y las sin clasificacion se reejecutan
      (nunca se salta una sin clasificar);
    - un resultado parcial/abortado nunca publica winrate comparable;
    - I2: si una fila es interrumpida por CancelledError/KeyboardInterrupt/
      SystemExit durante smoke o batalla, emite SINCRONICAMENTE por
      `on_result` un artefacto de stop sanitizado (internal-defect,
      compatibility indeterminate) y RE-LANZA la misma excepcion: la fila
      nunca se pierde entera;
    - I4: cada artefacto lleva contexto de corrida (`battle_timeout_seconds`,
      `round`, `generated_at`, `manifest` + hash) para ser auditable sin
      contexto externo.
    """
    results: list[MatrixModelResult] = []
    previous = previous or {}
    generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

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

    # I3 (MON-20 R2): fail-closed de politica de operador ANTES del refresh
    # de catalogo (que tambien es un request): cero llamadas al proveedor,
    # cero refrescos, cero batallas.
    policy = load_operator_policy()
    policy_offenders = [
        row for row in selected
        if (row.provider, row.model) in policy
    ]
    if policy_offenders:
        action = policy[(policy_offenders[0].provider, policy_offenders[0].model)]
        raise ValueError(
            f"fila(s) {action} por politica de operador (prohibidas): "
            + ", ".join(
                f"{r.provider}/{r.model}" for r in policy_offenders
            )
        )

    fresh: dict[str, list[str]] = {}
    if refresh_catalog is not None:
        fresh = await refresh_catalog() or {}

    def emit(result: MatrixModelResult) -> MatrixModelResult:
        stamped = replace(
            result,
            battle_timeout_seconds=battle_timeout_seconds,
            round=round_name,
            generated_at=generated_at,
            manifest=manifest_ref,
            manifest_sha256=manifest_sha256,
        )
        results.append(stamped)
        if on_result is not None:
            on_result(stamped)
        return stamped

    for row in selected:
        key = f"{row.provider}/{row.model}"
        prior = previous.get(key)
        if prior is not None and prior.final:
            emit(MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="already-finalized",
                smoke_ok=prior.smoke_ok,
                battles_requested=prior.battles_requested,
                battles_completed=prior.battles_completed,
                battles_wins=prior.battles_wins,
                battles_losses=prior.battles_losses,
                battles_ties=prior.battles_ties,
                effective_provider=prior.effective_provider,
                effective_model=prior.effective_model,
                win_rate=None,
                completion_latency_ms=prior.completion_latency_ms,
                decision_latency_ms=prior.decision_latency_ms,
                tokens=prior.tokens, retries=prior.retries,
                rotations=prior.rotations, quarantined=prior.quarantined,
                failure_type=prior.failure_type,
                failure_cause_type=prior.failure_cause_type,
                # T-02 (MON-20 R3): la rama already-finalized conserva la
                # note del resultado previo (p.ej. la del stop por
                # interrupcion); el marcador de no-repeticion solo aplica
                # cuando no habia nota que preservar.
                note=prior.note or "ya finalizado en una corrida anterior: no se repite",
                failure_stage=prior.failure_stage,
                http_status=prior.http_status,
                provider_error_code=prior.provider_error_code,
                comparable=prior.comparable,
                sample_size=prior.sample_size,
                # T-02 (MON-20 R3): la rama already-finalized conserva la
                # marca de indeterminacion del stop al reanudar
                compatibility_result=prior.compatibility_result,
            ))
            continue
        if refresh_catalog is not None and (
            row.model not in fresh.get(row.provider, [])
        ):
            emit(MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="removed-from-catalog",
                smoke_ok=False, battles_requested=0, battles_completed=0,
                battles_wins=0, battles_losses=0, battles_ties=0,
                effective_provider=None, effective_model=None,
                win_rate=None, completion_latency_ms=None,
                decision_latency_ms=None, tokens=None,
                retries=0, rotations=0, quarantined=0,
                failure_type=None, failure_cause_type=None,
                note="ya no esta en /models fresco: no se ejecuta",
            ))
            continue
        # I2: el progreso mutable de la fila dice en que etapa REAL fue
        # interrumpida (smoke o battle) para el artefacto de stop.
        progress = _RowProgress()
        try:
            result = await _run_one(
                row=row, battle_timeout_seconds=battle_timeout_seconds,
                fmt=fmt, opponent=opponent,
                smoke_deadline_seconds=smoke_deadline_seconds,
                build_provider=build_provider, run_battles=run_battles,
                progress=progress,
            )
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit) as exc:
            # I2 (MON-20 R2): artefacto de stop sincronico via on_result y
            # re-lanzamiento EXACTO de la excepcion original (nunca se
            # traga ni se convierte en fallo ordinario). La fila deja
            # evidencia durable clasificada.
            emit(_terminal_stop_result(row, exc, stage=progress.stage))
            raise
        except Exception as exc:  # noqa: BLE001 - fallo interno del runner
            emit(MatrixModelResult(
                provider=row.provider, model=row.model, tier=row.tier,
                protocol=row.protocol, status="internal-defect",
                smoke_ok=False, battles_requested=0, battles_completed=0,
                battles_wins=0, battles_losses=0, battles_ties=0,
                effective_provider=None, effective_model=None,
                win_rate=None, completion_latency_ms=None,
                decision_latency_ms=None, tokens=None,
                retries=0, rotations=0, quarantined=0,
                failure_type=type(exc).__name__,
                failure_cause_type=(
                    type(exc.__cause__).__name__ if exc.__cause__ else None
                ),
                # L-03 (R1A): no es un fallo de smoke ni de batalla, pero la
                # evidencia sanitizada disponible se conserva si existe.
                failure_stage=None,
                http_status=_http_status_chain(exc),
                provider_error_code=_structured_provider_error_code(exc),
                note="fallo del runner de la matriz, no del modelo",
            ))
        else:
            emit(result)
    return results


class _RowProgress:
    """I2: etapa REAL de la fila en vuelo, para clasificar el stop si la
    fila es interrumpida durante smoke o batalla. Mutable a proposito:
    `_run_one` la actualiza en cuanto el smoke pasa."""

    def __init__(self) -> None:
        self.stage = "smoke"


def _fatal_status(exc: BaseException) -> str:
    """T-08 (MON-20 R4): taxonomia structured-only de FatalProviderError,
    compartida con la normalizacion historica de la cobertura:

    - HTTP 400 -> `unsupported-protocol` (rechazo de protocolo/structured
      output, el unico caso que la categoria mide);
    - HTTP 401/403 -> `credential/model unavailable`;
    - HTTP 404/500 o sin status -> `internal-defect` (fail-closed: sin la
      senal exacta del contrato no se afirma un rechazo de protocolo).

    Nunca se infiere de texto libre: solo de la cadena de status."""
    http = _http_status_chain(exc)
    if http in (401, 403):
        return "credential/model unavailable"
    if http == 400:
        return "unsupported-protocol"
    return "internal-defect"


def _terminal_stop_result(
    row: ManifestRow,
    exc: BaseException,
    *,
    stage: str,
) -> MatrixModelResult:
    """I2 (MON-20 R2): artefacto de stop por interrupcion.

    Clasificacion conservadora y sanitizada: `internal-defect` de la
    corrida (NUNCA un veredicto sobre el modelo), `compatibility_result`
    indeterminado, etapa real (`smoke` o `battle`), terminal original
    (`failure_type` = clase de la excepcion) y progreso disponible
    (`battles_requested` = 2 si el smoke ya paso, 0 si no; sin batallas
    completadas durables porque la interrupcion no transporta partial)."""
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status="internal-defect",
        smoke_ok=(stage == "battle"),
        battles_requested=BATTLES_PER_MODEL if stage == "battle" else 0,
        battles_completed=0,
        battles_wins=0, battles_losses=0, battles_ties=0,
        effective_provider=None, effective_model=None,
        win_rate=None, completion_latency_ms=None,
        decision_latency_ms=None, tokens=None,
        retries=0, rotations=0, quarantined=0,
        failure_type=type(exc).__name__,
        failure_cause_type=(
            type(exc.__cause__).__name__ if exc.__cause__ else None
        ),
        failure_stage=stage,
        http_status=_http_status_chain(exc),
        provider_error_code=_structured_provider_error_code(exc),
        comparable=False, sample_size=None,
        compatibility_result="indeterminate-current-run",
        note=(
            f"fila interrumpida por {type(exc).__name__} durante {stage}: "
            "artefacto de stop sanitizado; compatibilidad indeterminada, "
            "no es veredicto sobre el modelo"
        ),
    )


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
    progress: _RowProgress | None = None,
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

    # L-01 (R1A): snapshot ANTES del smoke; el delta (nunca zeros fijos)
    # llega al artefacto tanto en exito como en fallo.
    before = _provider_metrics_snapshot(provider)

    def _fail(status: str, exc: BaseException, *, note: str | None = None,
              smoke_ok: bool = False) -> MatrixModelResult:
        return _smoke_failed(
            row, status, exc, note=note, smoke_ok=smoke_ok,
            metrics=_metrics_delta(before, _provider_metrics_snapshot(provider)),
        )

    try:
        envelope = await provider.complete(
            _SMOKE_PROMPT,
            deadline=time.monotonic() + smoke_deadline_seconds,
            turn_id=f"matrix-smoke:{row.provider}:{row.model}",
        )
    except ProviderPoolExhausted as exc:
        return _fail("credential/model unavailable", exc)
    except CredentialRejected as exc:
        return _fail("credential/model unavailable", exc)
    except ProviderError as exc:
        # L-04 (post-R1B) + T-08 (MON-20 R4): la taxonomia del smoke es
        # structured-only y fail-closed. `unsupported-protocol` queda
        # reservada EXCLUSIVAMENTE al rechazo de protocolo/structured
        # output con HTTP 400 estructurado; 401/403 upstream/model-wide
        # (sin senal key-specific; el provider ya no rota ni cuarentena) es
        # `credential/model unavailable`; y un FatalProviderError con
        # 404/500 o sin status NO autoriza a afirmar un rechazo de
        # protocolo sobre el modelo -> `internal-defect`. Esta misma tabla
        # es la que la cobertura aplica a los historicos
        # (normalize_final_classification), asi runner y evidencia nunca
        # divergen por etapa. Transitorio o deadline: limite externo.
        if isinstance(exc, FatalProviderError):
            status = _fatal_status(exc)
        else:
            status = "externally-limited"
        return _fail(status, exc)
    try:
        DecisionResponse.model_validate(envelope.payload)
    except (ValueError, TypeError) as exc:
        return _fail(
            "invalid-semantic-response", exc,
            note="la respuesta del modelo no valida el contrato DecisionResponse",
        )

    # --- smoke verde: exactamente 2 batallas pinneadas -------------------
    if progress is not None:
        progress.stage = "battle"
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
        return _battle_failed(
            row, exc, status=status,
            metrics=_metrics_delta(
                before, _provider_metrics_snapshot(provider)
            ),
        )
    except Exception as exc:  # noqa: BLE001
        # L-02/L-03 (post-R1B): TODO fallo despues de un smoke verde
        # conserva la fase real (smoke_ok=True, stage=battle,
        # battles_requested=2 objetivo) y la evidencia sanitizada; no cae
        # en el `except Exception` exterior de `run_matrix_round` que
        # reinicia todo a false/0/None. Infraestructura local
        # (ConnectionClosed de Showdown, ShowdownUnavailableError) es
        # `externally-limited`; una excepcion interna genuina sigue siendo
        # `internal-defect` pero con la fase preservada.
        return _battle_failed(
            row, exc, status=_battle_infrastructure_status(exc),
            metrics=_metrics_delta(
                before, _provider_metrics_snapshot(provider)
            ),
        )

    completed = getattr(result, "completed", 0)
    requested = getattr(result, "requested", 0)
    failure = getattr(result, "failure", None)
    failure_type = getattr(result, "failure_type", None)
    if failure_type == "ProviderMixError":
        status = "internal-defect"
    elif failure_type == "BenchmarkDeadlineExceeded":
        status = "externally-limited"
    elif failure_type == "InternalCleanupError":
        # L-01 (correccion LATWAN): sin excepcion primaria, un fallo del
        # cierre de recursos del benchmark (drain/players/calc/contexto/
        # engine) jamas puede quedar `compatible`: internal-defect
        # sanitizado, con el progreso real preservado.
        status = "internal-defect"
    elif failure is not None:
        status = "aborted"
    elif completed == requested:
        status = "compatible"
    else:
        status = "externally-limited"
    wins = getattr(result, "wins", 0)
    losses = getattr(result, "losses", 0)
    ties = getattr(result, "ties", 0)
    # I5 (MON-20 R2): win_rate queda null SIEMPRE. N=2 (2 batallas) prueba
    # compatibilidad funcional, nunca calidad: se publican W/L/T +
    # comparable=false + sample_size.
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status=status, smoke_ok=True,
        battles_requested=requested, battles_completed=completed,
        battles_wins=wins, battles_losses=losses, battles_ties=ties,
        effective_provider=getattr(result, "provider", None),
        effective_model=getattr(result, "model", None),
        win_rate=None,
        completion_latency_ms=_latency_slice(metrics, "completion_latency_ms"),
        decision_latency_ms=_latency_slice(metrics, "decision_latency_ms"),
        tokens={
            "input_tokens": int(metrics.get("input_tokens", 0)),
            "output_tokens": int(metrics.get("output_tokens", 0)),
            "cached_input_tokens": int(metrics.get("cached_input_tokens", 0)),
            "reasoning_tokens": int(metrics.get("reasoning_tokens", 0)),
        },
        # L-01 (R1A): `retries` es el conteo REAL de reintentos ejecutados
        # (`transient_retries_executed`), no el dedupe por turno.
        retries=int(metrics.get("transient_retries_executed", 0)),
        rotations=int(metrics.get("key_rotations", 0)),
        quarantined=int(metrics.get("keys_quarantined", 0)),
        failure_type=getattr(result, "failure_type", None),
        failure_cause_type=getattr(result, "failure_cause_type", None),
        # L-03 (R1A): la fase de batalla y la evidencia sanitizada via
        # BenchmarkResult cuando existen.
        failure_stage="battle" if failure is not None else None,
        http_status=getattr(result, "http_status", None),
        provider_error_code=getattr(result, "provider_error_code", None),
        comparable=False,
        sample_size=completed if completed > 0 else None,
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
    metrics: Mapping[str, int | None] | None = None,
) -> MatrixModelResult:
    """L-01 (R1A): el delta real de DecisionMetrics del provider llega al
    artefacto incluso cuando el smoke falla: retries/rotations/quarantined
    jamas vuelven a fijarse en cero por omision (el artefacto de North decia
    quarantined=0 con el pool agotado por rechazo de credencial, y el de
    Ling retries=0 con el 503 reintentado). Sin `metrics` (proveedor que no
    expone snapshot o error antes de construirlo) no hay delta y quedan en
    cero, que es la verdad: no hubo metricas.

    L-03 (R1A): `failure_stage` (`smoke` o `battle`), `http_status` y
    `provider_error_code` (solo campos estructurados permitidos) se
    persisten de forma sanitizada; el mensaje, la URL, los headers, el body
    completo y los secretos jamas salen del proceso."""
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status=status, smoke_ok=smoke_ok,
        battles_requested=0, battles_completed=0,
        battles_wins=0, battles_losses=0, battles_ties=0,
        effective_provider=None, effective_model=None,
        win_rate=None,
        completion_latency_ms=(
            _latency_slice(metrics, "completion_latency_ms")
            if metrics is not None else None
        ),
        decision_latency_ms=(
            _latency_slice(metrics, "decision_latency_ms")
            if metrics is not None else None
        ),
        tokens=(
            {
                "input_tokens": int(metrics.get("input_tokens", 0)),
                "output_tokens": int(metrics.get("output_tokens", 0)),
                "cached_input_tokens": int(
                    metrics.get("cached_input_tokens", 0)
                ),
                "reasoning_tokens": int(metrics.get("reasoning_tokens", 0)),
            }
            if metrics is not None else None
        ),
        retries=(
            int(metrics.get("transient_retries_executed", 0))
            if metrics is not None else 0
        ),
        rotations=(
            int(metrics.get("key_rotations", 0))
            if metrics is not None else 0
        ),
        quarantined=(
            int(metrics.get("keys_quarantined", 0))
            if metrics is not None else 0
        ),
        failure_type=type(exc).__name__,
        failure_cause_type=(
            type(exc.__cause__).__name__ if exc.__cause__ else None
        ),
        failure_stage="battle" if smoke_ok else "smoke",
        http_status=_http_status_chain(exc),
        provider_error_code=_structured_provider_error_code(exc),
        comparable=False,
        note=note or f"smoke fallido ({type(exc).__name__})",
    )


def _battle_infrastructure_status(exc: BaseException) -> str:
    """L-03 (post-R1B): infraestructura LOCAL no es incompatibilidad del
    modelo:
    - `ConnectionClosedError` de Showdown durante batalla -> externally-limited;
    - `ShowdownUnavailableError` (RuntimeError from OSError del preflight
      local) -> externally-limited;
    - cualquier otra excepcion post-smoke -> internal-defect (con la fase
      preservada, ver `_battle_failed`).

    L-02 (correccion LATWAN): un `BenchmarkFailure` (envuelve la excepcion
    original de la frontera del benchmark) se mira a traves de su `__cause__`:
    la clasificacion sigue el fallo real, no el wrapper."""
    from websockets.exceptions import ConnectionClosedError

    from .benchmark import BenchmarkFailure, ShowdownUnavailableError

    cause = exc.__cause__ if isinstance(exc, BenchmarkFailure) else exc
    if isinstance(cause, (ConnectionClosedError, ShowdownUnavailableError)):
        return "externally-limited"
    return "internal-defect"


def _battle_failed(
    row: ManifestRow,
    exc: BaseException,
    *,
    status: str,
    metrics: Mapping[str, int | None] | None,
) -> MatrixModelResult:
    """L-02 (post-R1B): fallo en la fase de BATTLE tras un smoke verde.

    Conserva la fase real y la evidencia: `smoke_ok=True`,
    `failure_stage="battle"`, las metricas del smoke disponibles (delta del
    provider) y la clase/causa sanitizadas. Nunca pasa por el `except
    Exception` exterior de `run_matrix_round`, que reinicia todo a
    false/0/None.

    L-02 (correccion LATWAN): si `exc` es un `BenchmarkFailure` de la
    frontera real del benchmark, su resultado parcial TIPADO manda:
    `battles_requested`/`battles_completed` reales (jamas ceros inventados),
    W/L/T reales, identidad efectiva (provider/model pinneados, los mismos
    que el smoke ya verifico) y la evidencia sanitizada del fallo. Sin
    partial (fallo directo sin pasar por la frontera, o preflight de
    Showdown antes de crear players) el progreso es 0 — que es la verdad —
    y la identidad efectiva sigue siendo el pin de la fila."""
    from .benchmark import BenchmarkFailure

    partial = exc.result if isinstance(exc, BenchmarkFailure) else None
    if partial is not None:
        battles_requested = partial.requested
        battles_completed = partial.completed
        battles_wins = partial.wins
        battles_losses = partial.losses
        battles_ties = partial.ties
        effective_provider = partial.provider or row.provider
        effective_model = partial.model or row.model
        failure_type = partial.failure_type
        failure_cause_type = partial.failure_cause_type
        http_status = partial.http_status
        provider_error_code = partial.provider_error_code
    else:
        battles_requested = BATTLES_PER_MODEL
        battles_completed = 0
        battles_wins = battles_losses = battles_ties = 0
        effective_provider = row.provider
        effective_model = row.model
        failure_type = type(exc).__name__
        failure_cause_type = (
            type(exc.__cause__).__name__ if exc.__cause__ else None
        )
        http_status = _http_status_chain(exc)
        provider_error_code = _structured_provider_error_code(exc)
    return MatrixModelResult(
        provider=row.provider, model=row.model, tier=row.tier,
        protocol=row.protocol, status=status, smoke_ok=True,
        battles_requested=battles_requested,
        battles_completed=battles_completed,
        battles_wins=battles_wins, battles_losses=battles_losses,
        battles_ties=battles_ties,
        effective_provider=effective_provider, effective_model=effective_model,
        win_rate=None,
        completion_latency_ms=(
            _latency_slice(metrics, "completion_latency_ms")
            if metrics is not None else None
        ),
        decision_latency_ms=(
            _latency_slice(metrics, "decision_latency_ms")
            if metrics is not None else None
        ),
        tokens=(
            {
                "input_tokens": int(metrics.get("input_tokens", 0)),
                "output_tokens": int(metrics.get("output_tokens", 0)),
                "cached_input_tokens": int(
                    metrics.get("cached_input_tokens", 0)
                ),
                "reasoning_tokens": int(metrics.get("reasoning_tokens", 0)),
            }
            if metrics is not None else None
        ),
        retries=(
            int(metrics.get("transient_retries_executed", 0))
            if metrics is not None else 0
        ),
        rotations=(
            int(metrics.get("key_rotations", 0))
            if metrics is not None else 0
        ),
        quarantined=(
            int(metrics.get("keys_quarantined", 0))
            if metrics is not None else 0
        ),
        failure_type=failure_type,
        failure_cause_type=failure_cause_type,
        failure_stage="battle",
        http_status=http_status,
        provider_error_code=provider_error_code,
        comparable=False,
        sample_size=battles_completed if battles_completed > 0 else None,
        note="fallo en fase de batalla tras smoke verde: sin winrate comparable",
    )


def _provider_metrics_snapshot(provider: Any) -> dict[str, int | None] | None:
    """L-01 (R1A): snapshot de las metricas del provider via
    `metrics_snapshot()` (KeyRotatingProvider/ProviderChain). Proveedores
    sin metricas (fakes, no expuestos) devuelven None: no hay delta que
    reportar."""
    snapshot = getattr(provider, "metrics_snapshot", None)
    if not callable(snapshot):
        return None
    return snapshot()


# L-06 (OFFLINE): los percentiles NO se restan nunca (matematicamente
# invalido: p50(after) - p50(before) no es un percentil de nada). Con
# baseline fresco (count=0) los gauges del snapshot posterior SON la unica
# poblacion y se copian; con baseline con muestras los gauges quedan None
# (no comparables) y solo contadores/count/total/tokens se diferencian.
_LATENCY_PREFIXES = ("completion_latency_ms", "decision_latency_ms")
_LATENCY_GAUGE_KEYS = frozenset({
    f"{prefix}_{gauge}"
    for prefix in _LATENCY_PREFIXES
    for gauge in ("p50", "p95", "max")
})


def _metrics_delta(
    before: Mapping[str, int | None] | None,
    after: Mapping[str, int | None] | None,
) -> dict[str, int | None] | None:
    """L-01 (R1A) + L-06 (OFFLINE): delta entre dos snapshots.

    - Contadores, tokens, `count` y `total` se calculan por diferencia.
    - `max/p50/p95` solo se copian del snapshot posterior cuando el baseline
      tiene count=0 (la poblacion posterior es la unica). Con baseline con
      muestras previas los gauges quedan None (no comparables): jamas se
      publican percentiles inventados (p50(after)-p50(before) = 50 en el
      ejemplo de Latwan no significa nada).
    - Sin snapshot inicial o final no hay delta (None).
    """
    if before is None or after is None:
        return None
    dirty = any(
        isinstance(before.get(f"{prefix}_count"), int)
        and before[f"{prefix}_count"] > 0
        for prefix in _LATENCY_PREFIXES
    )
    keys = sorted(set(before) | set(after))
    delta: dict[str, int | None] = {}
    for key in keys:
        start, end = before.get(key), after.get(key)
        if isinstance(start, int) and isinstance(end, int):
            if dirty and key in _LATENCY_GAUGE_KEYS:
                delta[key] = None
            else:
                delta[key] = end - start
        elif end is not None:
            delta[key] = end
        else:
            delta[key] = None
    return delta


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
