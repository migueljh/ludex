"""Taxonomia estructurada unica de fallos de provider (MON-20 T-13/D59).

Modulo stdlib-only (sin imports de SDK/DB/httpx): lo importan el runner
(`ludex_agent.matrix`) y el generador de cobertura
(`evals/build_matrix_coverage.py`, via bootstrap de sys.path) para que los
SIETE sitios de clasificacion del inventario formal nunca diverjan. Es la
reconciliacion de D43.2 (pool rechazado por credencial) con D54 R1
(transitorio/deadline/pool -> limite externo), decidida por Latwan en R6.

Firma minima: `provider_failure_class(failure_type, http_status,
failure_cause_type)`. Solo se usan strings de clase y status HTTP; jamas
texto libre, mensajes, notes ni URLs.

R8 (F-A): la membresia de la tabla se DERIVA de la tabla, no se declara.
`explicit_failure_class` es la tabla: cada rama explicita devuelve su
categoria y la ausencia de rama devuelve `None` (fail-closed). Solo
`provider_failure_class` mapea `None` a `internal-defect`. El invariante
1b introspecciona las subclases y le exige a la tabla una rama explicita
para cada una; ya no existe ningun frozenset de membresia que mantener a
mano.
"""

from __future__ import annotations


def explicit_failure_class(
    failure_type: str | None,
    http_status: int | None,
    failure_cause_type: str | None = None,
) -> str | None:
    """Taxonomia unica de ProviderError (T-08/T-11/T-13) — LA TABLA.

    Fuente unica de clasificacion Y de membresia (R8, F-A): una clase
    tiene rama explicita aca o cae al fail-closed devolviendo `None`.
    Ningun otro objeto declara que clases existen.

    - `FatalProviderError` + HTTP 400 -> `unsupported-protocol` (rechazo de
      protocolo/structured output, el unico caso que la categoria mide);
    - `FatalProviderError` + 401/403 -> `credential/model unavailable`;
    - `FatalProviderError` + 404/500/None u otro status -> `internal-defect`
      (veredicto conservador: sin la senal exacta del contrato no se
      afirma un rechazo de protocolo sobre el modelo);
    - `CredentialRejected` -> `credential/model unavailable` (D43.2);
    - `ProviderPoolExhausted` CON `failure_cause_type == "CredentialRejected"`
      (pool totalmente en cuarentena por 401/403 credential-specific) ->
      `credential/model unavailable` (D43.2);
    - `ProviderPoolExhausted` sin causa CredentialRejected (cooldown/cuota/
      pool transitorio) -> `externally-limited` (D54 R1);
    - `ProviderMixError`/`InternalCleanupError` -> `internal-defect`;
    - `TransientProviderError`, `BenchmarkDeadlineExceeded`,
      `DecisionDeadlineExceeded`, `ProviderError` generico y `QuotaExceeded`
      -> `externally-limited` (F5: cuota agotada es limite externo por
      definicion);
    - `ProviderSelectionError` (construccion del provider) ->
      `credential/model unavailable`;
    - clase desconocida o None -> `None` (fail-closed; solo el wrapper
      publico lo mapea a `internal-defect`).

    Nunca se infiere de texto libre: solo de la clase, la cadena de status
    y la causa estructurada."""
    if failure_type == "FatalProviderError":
        if http_status in (401, 403):
            return "credential/model unavailable"
        if http_status == 400:
            return "unsupported-protocol"
        return "internal-defect"
    if failure_type in {"CredentialRejected", "ProviderSelectionError"}:
        return "credential/model unavailable"
    if failure_type == "ProviderPoolExhausted":
        if failure_cause_type == "CredentialRejected":
            return "credential/model unavailable"
        return "externally-limited"
    if failure_type in {"ProviderMixError", "InternalCleanupError"}:
        return "internal-defect"
    if failure_type in {
        "TransientProviderError", "BenchmarkDeadlineExceeded",
        "DecisionDeadlineExceeded", "ProviderError", "QuotaExceeded",
    }:
        return "externally-limited"
    return None


def provider_failure_class(
    failure_type: str | None,
    http_status: int | None,
    failure_cause_type: str | None = None,
) -> str:
    """Wrapper publico de `explicit_failure_class` (R8, F-A).

    Ninguna rama vive aca: la tabla es una sola. Unica responsabilidad de
    este wrapper: mapear el fail-closed (`None`) a `internal-defect`, el
    veredicto conservador que el runner y el generador persisten."""
    category = explicit_failure_class(
        failure_type, http_status, failure_cause_type
    )
    return "internal-defect" if category is None else category
