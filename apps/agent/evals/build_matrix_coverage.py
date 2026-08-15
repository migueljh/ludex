"""Generador reproducible de la cobertura final de MON-20 (D54/D55).

Reconstruye `20260814-provider-matrix-coverage.json` exclusivamente desde
fuentes versionadas: el manifiesto de la ronda, los artefactos atomicos por
modelo de `runs/` y el ledger de stops diagnosticos. Nada se inventa: una
fila que nunca se contacto queda explicitamente `not-attempted` (C1) y una
fila `aborted` se normaliza SOLO con evidencia estructurada y la taxonomia
del runner (C2).

Uso:
    python build_matrix_coverage.py \\
        --manifest runs/20260814t183716z-matrix-manifest.json \\
        --runs-dir runs --ledger runs/20260814-paid-diagnostic-stops.json \\
        --out runs/20260814-provider-matrix-coverage.json [--check]

`--check` regenera y compara contra el archivo commiteado (exit 1 si
difieren): los tests del repo hacen lo mismo contra las fuentes versionadas
para que una edicion futura del JSON o de los mapeos ponga rojo la suite.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping

RUNS_GLOB = "*-matrix.json"

CATALOG_SOURCES = {
    "google": "live GET generativelanguage.googleapis.com/v1beta/models",
    "kimi": "live GET api.moonshot.ai/v1/models",
    "open_code_zen": "live GET opencode.ai/zen/v1/models",
}

OPERATOR_SCOPE = {
    "effective_from": "2026-08-14",
    "live_game_execution": "Chinese models and Gemini free tier only",
    "gpt_5_6_luna": "operator-prohibited-never-retry",
    "historical_non_chinese_results": "evidence-only; never authority to rerun",
    "paid_retry_policy": "requires a new explicit provider cap",
}

PURPOSE = (
    "Matriz final de compatibilidad funcional MON-20; nunca ranking de "
    "calidad ni winrate"
)

# Clasificaciones terminales fieles que el runner ya emitio: pasan tal cual.
_FAITHFUL = frozenset({
    "compatible", "unsupported-protocol", "invalid-semantic-response",
    "credential/model unavailable", "internal-defect",
})

# Taxonomia del runner (matrix.py:_run_one, misma para smoke y batalla).
_TRANSIENT = frozenset({
    "TransientProviderError", "ProviderPoolExhausted",
    "BenchmarkDeadlineExceeded", "CredentialRejected", "ProviderError",
})
_INTERNAL_DEFECT = frozenset({
    "ProviderMixError", "InternalCleanupError",
})


def normalize_final_classification(
    runtime_status: str,
    failure_type: str | None,
    http_status: int | None,
) -> str:
    """C2: normaliza `aborted` con evidencia ESTRUCTURADA y la misma
    taxonomia del runner, jamas desde texto libre:

    - FatalProviderError + HTTP 400 -> `unsupported-protocol` (rechazo de
      protocolo/structured output);
    - FatalProviderError + HTTP 401/403 -> `credential/model unavailable`;
    - transitorio/deadline/pool -> `externally-limited`;
    - defecto interno (ProviderMixError/InternalCleanupError) ->
      `internal-defect`;
    - sin evidencia estructurada atribuible al proveedor -> internal-defect
      (fail-closed: nunca se afirma un limite externo sin senal).

    Las demas clases terminales se conservan verbatim."""
    if runtime_status in _FAITHFUL:
        return runtime_status
    if runtime_status != "aborted":
        return runtime_status
    if failure_type == "FatalProviderError":
        if http_status in (401, 403):
            return "credential/model unavailable"
        return "unsupported-protocol"
    if failure_type in _TRANSIENT:
        return "externally-limited"
    if failure_type in _INTERNAL_DEFECT:
        return "internal-defect"
    return "internal-defect"


def load_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_ledger(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def scan_executed_artifacts(
    runs_dir: str | Path,
) -> dict[tuple[str, str], tuple[str, dict]]:
    """I1: barre `runs/` y devuelve, por (provider, model), el artefacto de
    la ronda mas reciente (nombre lexicograficamente mayor; los prefijos de
    ronda son cronologicos en este repo). Solo archivos `*-matrix.json`
    cuyo contenido trae provider+model: manifiestos, state files, coverage y
    ledger no matchean el glob ni el shape."""
    result: dict[tuple[str, str], tuple[str, dict]] = {}
    for path in sorted(Path(runs_dir).glob(RUNS_GLOB)):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(document, dict):
            continue
        provider = document.get("provider")
        model = document.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            continue
        key = (provider, model)
        current = result.get(key)
        if current is None or path.name > current[0]:
            result[key] = (path.name, document)
    return result


def _adaptation_applied(plan: Mapping[str, Any]) -> str | None:
    if plan.get("structured_output") is None:
        return None
    return f"{plan.get('protocol')}/{plan.get('structured_output')}"


def _base_row(plan: Mapping[str, Any], manifest_name: str) -> dict:
    return {
        "provider": plan["provider"],
        "model": plan["model"],
        "catalog_source": CATALOG_SOURCES[plan["provider"]],
        "discovery_timestamp": None,  # lo fija el caller con generated_at
        "protocol": plan.get("protocol"),
        "endpoint": plan.get("endpoint"),
        "structured_output": plan.get("structured_output"),
        "tier": plan.get("tier"),
        "manifest_status": plan["status"],
        "manifest_classification_note": plan.get("classification_note"),
        "planned_battles": plan.get("battles"),
        "concurrency": plan.get("concurrency"),
        "persist": plan.get("persist"),
        "pin": plan.get("pin"),
        "estimated_cost_usd": plan.get("estimated_cost_usd"),
        "estimated_smoke_usd": plan.get("estimated_smoke_usd"),
        "cumulative_cost_usd": plan.get("cumulative_cost_usd"),
        "evidence_kind": "manifest-classification",
        "source_artifact": manifest_name,
    }


def _not_attempted_row(
    plan: Mapping[str, Any],
    manifest_name: str,
) -> dict:
    """C1: fila nunca contactada -> final_classification null +
    disposition not-attempted + motivo explicito, FUERA de los buckets
    medidos (jamas unsupported/incompatible/externally-limited)."""
    row = _base_row(plan, manifest_name)
    row.update({
        "runtime_status": "not-run",
        "smoke_result": "not-run",
        "battles_requested": 0,
        "battles_completed": 0,
        "effective_provider": None,
        "effective_model": None,
        "completion_latency_ms": None,
        "decision_latency_ms": None,
        "tokens": None,
        "retries": 0,
        "rotations": 0,
        "quarantined": 0,
        "failure_stage": "planning",
        "failure_type": None,
        "failure_cause_type": None,
        "http_status": None,
        "provider_error_code": None,
        "final_classification": None,
        "disposition": "not-attempted",
        "not_attempted_reason": (
            f"{plan['status']}: {plan.get('classification_note')}"
        ),
        "adaptation_applied": None,
        "comparable": False,
        "win_rate": None,
        "note": plan.get("classification_note"),
    })
    return row


def _atomic_row(
    plan: Mapping[str, Any],
    artifact: Mapping[str, Any],
    source_artifact: str,
) -> dict:
    """Fila con artefacto atomico: runtime verbatim del artefacto y
    clasificacion final normalizada con la tabla C2."""
    row = _base_row(plan, source_artifact)
    row.update({
        "evidence_kind": "atomic-runtime-artifact",
        "source_artifact": source_artifact,
        "runtime_status": artifact.get("status"),
        "smoke_result": "passed" if artifact.get("smoke_ok") else "failed",
        "battles_requested": artifact.get("battles_requested"),
        "battles_completed": artifact.get("battles_completed"),
        "effective_provider": artifact.get("effective_provider"),
        "effective_model": artifact.get("effective_model"),
        "completion_latency_ms": artifact.get("completion_latency_ms"),
        "decision_latency_ms": artifact.get("decision_latency_ms"),
        "tokens": artifact.get("tokens"),
        "retries": artifact.get("retries"),
        "rotations": artifact.get("rotations"),
        "quarantined": artifact.get("quarantined"),
        "failure_stage": artifact.get("failure_stage"),
        "failure_type": artifact.get("failure_type"),
        "failure_cause_type": artifact.get("failure_cause_type"),
        "http_status": artifact.get("http_status"),
        "provider_error_code": artifact.get("provider_error_code"),
        "final_classification": normalize_final_classification(
            artifact.get("status"),
            artifact.get("failure_type"),
            artifact.get("http_status"),
        ),
        "disposition": "measured",
        "not_attempted_reason": None,
        "adaptation_applied": _adaptation_applied(plan),
        "comparable": False,
        "win_rate": None,
        "note": artifact.get("note"),
    })
    return row


def _stop_row(
    plan: Mapping[str, Any],
    stop: Mapping[str, Any],
    ledger_name: str,
) -> dict:
    """Fila interrumpida: clasificacion del ledger (internal-defect /
    indeterminada) y evidencia saneada, nunca un veredicto del modelo."""
    row = _base_row(plan, ledger_name)
    row.update({
        "evidence_kind": "sanitized-diagnostic-stop",
        "source_artifact": ledger_name,
        "runtime_status": "diagnostic-stop-no-atomic-artifact",
        "smoke_result": stop.get("smoke_result"),
        "battles_requested": stop.get("battles_requested"),
        "battles_completed": stop.get("battles_completed"),
        "effective_provider": stop.get("effective_provider"),
        "effective_model": stop.get("effective_model"),
        "completion_latency_ms": None,
        "decision_latency_ms": None,
        "tokens": None,
        "retries": None,
        "rotations": None,
        "quarantined": None,
        "failure_stage": stop.get("failure_stage"),
        "decision_stage": stop.get("decision_stage"),
        "failure_type": stop.get("failure_type"),
        "failure_cause_type": stop.get("failure_cause_type"),
        "http_status": stop.get("http_status"),
        "provider_error_code": None,
        "final_classification": stop.get("final_classification"),
        "compatibility_result": stop.get("compatibility_result"),
        "future_execution": stop.get("future_execution"),
        "disposition": "measured",
        "not_attempted_reason": None,
        "adaptation_applied": _adaptation_applied(plan),
        "comparable": False,
        "win_rate": None,
        "note": (
            "clasificacion de la evidencia de corrida; no afirma "
            "incompatibilidad del modelo"
        ),
    })
    return row


def build_coverage(
    *,
    manifest_path: str | Path,
    runs_dir: str | Path,
    ledger_path: str | Path,
    generated_at: str | None = None,
) -> dict:
    """Reconstruye el documento completo de cobertura desde fuentes
    versionadas. `generated_at` es parametro para que los tests puedan
    comparar byte a byte contra el archivo commiteado."""
    if generated_at is None:
        generated_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest = load_manifest(manifest_path)
    ledger = load_ledger(ledger_path)
    manifest_name = Path(manifest_path).name
    ledger_name = Path(ledger_path).name
    executed = scan_executed_artifacts(runs_dir)
    stops = {
        (row["provider"], row["model"]): row for row in ledger.get("rows", [])
    }

    rows: list[dict] = []
    for plan in manifest.get("rows", []):
        key = (plan["provider"], plan["model"])
        row = None
        if key in executed:
            filename, artifact = executed[key]
            row = _atomic_row(plan, artifact, filename)
        elif key in stops:
            row = _stop_row(plan, stops[key], ledger_name)
        else:
            row = _not_attempted_row(plan, manifest_name)
        row["discovery_timestamp"] = manifest.get("generated_at")
        # I3: la politica declarativa versionada tambien se refleja en la
        # cobertura (marcador no ejecutable, sin tocar el historial).
        if plan.get("operator_prohibited"):
            row["operator_prohibited"] = plan["operator_prohibited"]
        rows.append(row)

    doc = {
        "schema_version": 1,
        "generated_at": generated_at,
        "purpose": PURPOSE,
        "generator": "apps/agent/evals/build_matrix_coverage.py",
        "source_manifest": manifest_name,
        "diagnostic_stops": ledger_name,
        "discovery": {
            "timestamp": manifest.get("generated_at"),
            "baseline_inventory": manifest.get("baseline_inventory"),
            "pricing": manifest.get("pricing"),
            "delta": manifest.get("delta"),
            "catalog_sources": dict(CATALOG_SOURCES),
        },
        "operator_scope": dict(OPERATOR_SCOPE),
        "rows": rows,
        "counts": build_counts(rows),
        "invariants": build_invariants(rows, manifest),
    }
    return doc


def build_counts(rows: list[dict]) -> dict:
    by_manifest: dict[str, int] = {}
    by_evidence: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_disposition: dict[str, int] = {}
    for row in rows:
        by_manifest[row["manifest_status"]] = by_manifest.get(
            row["manifest_status"], 0
        ) + 1
        by_evidence[row["evidence_kind"]] = by_evidence.get(
            row["evidence_kind"], 0
        ) + 1
        by_disposition[row["disposition"]] = by_disposition.get(
            row["disposition"], 0
        ) + 1
        final = row["final_classification"]
        if final is not None:
            by_class[final] = by_class.get(final, 0) + 1
    by_provider: dict[str, int] = {}
    for row in rows:
        by_provider[row["provider"]] = by_provider.get(row["provider"], 0) + 1
    return {
        "catalog_total": len(rows),
        "by_provider": by_provider,
        "by_manifest_status": by_manifest,
        "by_evidence_kind": by_evidence,
        "measured_rows": len([r for r in rows if r["disposition"] == "measured"]),
        "not_attempted_rows": len(
            [r for r in rows if r["disposition"] == "not-attempted"]
        ),
        "by_disposition": by_disposition,
        "by_final_classification": by_class,
    }


def build_invariants(rows: list[dict], manifest: Mapping[str, Any]) -> dict:
    keys = {(r["provider"], r["model"]) for r in rows}
    manifest_keys = {
        (r["provider"], r["model"]) for r in manifest.get("rows", [])
    }
    ready = [r for r in rows if r["manifest_status"] == "ready"]
    ready_with_evidence = [
        r for r in ready if r["evidence_kind"] != "manifest-classification"
    ]
    ready_without_evidence = [
        f"{r['provider']}/{r['model']}" for r in ready
        if r["evidence_kind"] == "manifest-classification"
    ]
    measured = [r for r in rows if r["disposition"] == "measured"]
    not_attempted = [r for r in rows if r["disposition"] == "not-attempted"]
    not_attempted_in_measured_bucket = [
        r for r in not_attempted if r["final_classification"] is not None
    ]
    fatal_400_aborted = [
        r for r in measured
        if r["runtime_status"] == "aborted"
        and r.get("failure_type") == "FatalProviderError"
        and r.get("http_status") == 400
        and r["final_classification"] == "unsupported-protocol"
    ]
    return {
        "unique_provider_model_rows": len(keys) == len(rows) == len(manifest_keys),
        "coverage_matches_manifest": keys == manifest_keys,
        "ready_rows": len(ready),
        "ready_rows_with_execution_evidence": len(ready_with_evidence),
        "ready_rows_without_execution_evidence": ready_without_evidence,
        "comparable_rows": len([r for r in rows if r.get("comparable")]),
        "persisted_rows": len([r for r in rows if r.get("persist")]),
        "measured_rows": len(measured),
        "not_attempted_rows": len(not_attempted),
        "not_attempted_rows_in_measured_bucket": len(
            not_attempted_in_measured_bucket
        ),
        "fatal_400_aborted_classified_unsupported": len(fatal_400_aborted),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generador reproducible de la cobertura final de MON-20"
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--runs-dir", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument(
        "--check", action="store_true",
        help="regenera y compara contra el archivo existente (exit 1 si difiere)",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    if args.check:
        if not out.exists():
            print(f"no existe el archivo commiteado: {out}", file=sys.stderr)
            return 1
        committed = json.loads(out.read_text(encoding="utf-8"))
        # el generated_at commiteado es el del build historico; la
        # reconstruccion lo conserva para comparar byte a byte
        rebuilt = build_coverage(
            manifest_path=args.manifest,
            runs_dir=args.runs_dir,
            ledger_path=args.ledger,
            generated_at=committed.get("generated_at"),
        )
        if rebuilt != committed:
            print("la cobertura commiteada difiere del generador", file=sys.stderr)
            return 1
        print(f"OK: {out} es reproducible desde fuentes versionadas")
        return 0

    rebuilt = build_coverage(
        manifest_path=args.manifest,
        runs_dir=args.runs_dir,
        ledger_path=args.ledger,
        generated_at=args.generated_at,
    )
    out.write_text(
        json.dumps(rebuilt, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"cobertura escrita en {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
