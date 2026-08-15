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
"""

from __future__ import annotations


def provider_failure_class(
    failure_type: str | None,
    http_status: int | None,
    failure_cause_type: str | None = None,
) -> str:
    """Taxonomia unica de ProviderError (T-08/T-11/T-13).

    - `FatalProviderError` + HTTP 400 -> `unsupported-protocol` (rechazo de
      protocolo/structured output, el unico caso que la categoria mide);
    - `FatalProviderError` + 401/403 -> `credential/model unavailable`;
    - `FatalProviderError` + 404/500/None u otro status -> `internal-defect`
      (fail-closed: sin la senal exacta del contrato no se afirma un rechazo
      de protocolo sobre el modelo);
    - `CredentialRejected` -> `credential/model unavailable` (D43.2);
    - `ProviderPoolExhausted` CON `failure_cause_type == "CredentialRejected"`
      (pool totalmente en cuarentena por 401/403 credential-specific) ->
      `credential/model unavailable` (D43.2);
    - `ProviderPoolExhausted` sin causa CredentialRejected (cooldown/cuota/
      pool transitorio) -> `externally-limited` (D54 R1);
    - `ProviderMixError`/`InternalCleanupError` -> `internal-defect`;
    - `TransientProviderError`, `BenchmarkDeadlineExceeded`,
      `DecisionDeadlineExceeded` y `ProviderError` generico ->
      `externally-limited`;
    - `ProviderSelectionError` (construccion del provider) ->
      `credential/model unavailable`;
    - clase desconocida o None -> `internal-defect` fail-closed.

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
        "DecisionDeadlineExceeded", "ProviderError",
    }:
        return "externally-limited"
    return "internal-defect"
