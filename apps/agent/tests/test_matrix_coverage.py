"""MON-20 R2: generador versionado de la cobertura final y sus invariantes
(C1/C2/I1/I3/M2).

La cobertura `20260814-provider-matrix-coverage.json` debe reconstruirse
byte a byte desde fuentes versionadas (manifiesto + artefactos atómicos +
ledger de stops) con el generador `evals/build_matrix_coverage.py`. Si alguien
reintroduce cualquiera de los dos mapeos defectuosos (pending-budget ->
externally-limited, o aborted -> externally-limited sin evidencia), la
reconstruccion difiere del archivo commiteado y estos tests se ponen rojos.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[1] / "evals"
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

import build_matrix_coverage as bmc  # noqa: E402

RUNS = EVALS_DIR / "runs"
COVERAGE_PATH = RUNS / "20260814-provider-matrix-coverage.json"
MANIFEST_PATH = RUNS / "20260814t183716z-matrix-manifest.json"
LEDGER_PATH = RUNS / "20260814-paid-diagnostic-stops.json"


def _committed() -> dict:
    return json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))


def _rebuilt(generated_at: str | None = None) -> dict:
    if generated_at is None:
        generated_at = _committed()["generated_at"]
    return bmc.build_coverage(
        manifest_path=MANIFEST_PATH,
        runs_dir=RUNS,
        ledger_path=LEDGER_PATH,
        generated_at=generated_at,
    )


def test_coverage_commiteada_se_reconstruye_identica_desde_fuentes():
    """I1: el archivo commiteado es la salida exacta del generador
    versionado; cualquier deriva (incluidos los dos mapeos defectuosos) deja
    de ser reproducible y este test se pone rojo."""
    rebuilt = _rebuilt()
    assert rebuilt == _committed()


def test_invariantes_coverage_commiteado():
    """I1: invariantes commiteadas recomputadas desde fuentes: 112 pares
    unicos, cobertura exacta del manifiesto, 22 ready con evidencia 18+4,
    0 comparables, 0 persistidos, counts fieles."""
    doc = _rebuilt()
    rows = doc["rows"]
    keys = {(r["provider"], r["model"]) for r in rows}
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    manifest_keys = {
        (r["provider"], r["model"]) for r in manifest["rows"]
    }
    assert len(keys) == 112
    assert keys == manifest_keys  # cobertura exacta del manifiesto

    ready = [r for r in rows if r["manifest_status"] == "ready"]
    atomic = [r for r in rows if r["evidence_kind"] == "atomic-runtime-artifact"]
    stops = [r for r in rows if r["evidence_kind"] == "sanitized-diagnostic-stop"]
    assert len(ready) == 22
    assert len(atomic) == 18
    assert len(stops) == 4
    assert all(r["evidence_kind"] != "manifest-classification" for r in ready)

    assert doc["counts"]["by_evidence_kind"]["atomic-runtime-artifact"] == 18
    assert doc["counts"]["by_evidence_kind"]["sanitized-diagnostic-stop"] == 4
    assert doc["invariants"]["comparable_rows"] == 0
    assert doc["invariants"]["persisted_rows"] == 0
    assert doc["invariants"]["unique_provider_model_rows"] is True
    assert doc["invariants"]["coverage_matches_manifest"] is True


def test_ninguna_fila_no_ejecutada_esta_en_un_bucket_medido():
    """C1: pending-budget (y toda fila nunca contactada) queda explicitamente
    no intentada: final_classification null + disposition not-attempted, fuera
    de los buckets medidos. Reintroducir `pending-budget ->
    externally-limited` rompe este test."""
    doc = _rebuilt()
    rows = doc["rows"]
    not_attempted = [
        r for r in rows if r["disposition"] == "not-attempted"
    ]
    measured = [r for r in rows if r["disposition"] == "measured"]
    assert len(not_attempted) == 90  # 75 pending-budget + 15 excluidas
    assert len(measured) == 22
    for row in not_attempted:
        assert row["final_classification"] is None, (
            f"{row['provider']}/{row['model']} no contactada no puede "
            f"tener clasificacion medida: {row['final_classification']}"
        )
        assert row["manifest_status"] != "ready"
    for row in measured:
        assert row["final_classification"] is not None
    # los buckets medidos no contienen filas nunca contactadas
    assert doc["invariants"]["not_attempted_rows_in_measured_bucket"] == 0
    by_class = doc["counts"]["by_final_classification"]
    assert sum(by_class.values()) == 22
    assert "externally-limited" in by_class
    assert by_class["externally-limited"] == 10  # solo aborted transitorios


def test_aborted_con_400_fatal_no_es_limite_externo():
    """C2: las filas Kimi aborted con FatalProviderError + HTTP 400 se
    normalizan a unsupported-protocol (la taxonomia del runner), preservando
    runtime_status, failure_type, failure_cause_type, stage y http_status
    originales. Colapsar todo `aborted -> externally-limited` rompe esto."""
    doc = _rebuilt()
    rows = doc["rows"]
    kimi_400 = [
        r for r in rows
        if r["provider"] == "kimi"
        and r["runtime_status"] == "aborted"
        and r.get("http_status") == 400
    ]
    assert len(kimi_400) == 2
    for row in kimi_400:
        assert row["final_classification"] == "unsupported-protocol", row
        # evidencia original preservada, nunca inferida de texto
        assert row["failure_type"] == "FatalProviderError"
        assert row["failure_cause_type"] == "BadRequestError"
        assert row["failure_stage"] == "battle"
        assert row["http_status"] == 400
        assert row["runtime_status"] == "aborted"
        assert row["smoke_result"] == "passed"
    assert doc["invariants"]["fatal_400_aborted_classified_unsupported"] == 2


def test_normalizacion_aborted_usa_la_tabla_del_runner():
    """C2: la tabla de normalizacion de `aborted` es exactamente la del
    runner (smoke y batalla): FatalProviderError 400 -> unsupported-protocol;
    401/403 -> credential/model unavailable; transitorio/deadline/pool ->
    externally-limited; defecto interno -> internal-defect."""
    n = bmc.normalize_final_classification
    # passthrough de clases ya fieles
    assert n("compatible", None, None) == "compatible"
    assert n("unsupported-protocol", None, None) == "unsupported-protocol"
    assert n("invalid-semantic-response", None, None) == "invalid-semantic-response"
    assert n("credential/model unavailable", None, None) == "credential/model unavailable"
    assert n("internal-defect", None, None) == "internal-defect"
    # FatalProviderError: 400 -> protocolo; 401/403 -> credencial
    assert n("aborted", "FatalProviderError", 400) == "unsupported-protocol"
    assert n("aborted", "FatalProviderError", 401) == "credential/model unavailable"
    assert n("aborted", "FatalProviderError", 403) == "credential/model unavailable"
    # transitorio / deadline / pool -> limite externo
    assert n("aborted", "TransientProviderError", None) == "externally-limited"
    assert n("aborted", "ProviderPoolExhausted", None) == "externally-limited"
    assert n("aborted", "BenchmarkDeadlineExceeded", None) == "externally-limited"
    # defecto interno -> internal-defect
    assert n("aborted", "ProviderMixError", None) == "internal-defect"
    assert n("aborted", "InternalCleanupError", None) == "internal-defect"
    # sin evidencia estructurada atribuible al proveedor: nunca limite externo
    assert n("aborted", None, None) == "internal-defect"
    # T-03 (MON-20 R3): FatalProviderError SOLO con HTTP 400 estructurado es
    # rechazo de protocolo; 404/500/None no autorizan a afirmar
    # unsupported-protocol sobre un modelo
    assert n("aborted", "FatalProviderError", 404) == "internal-defect"
    assert n("aborted", "FatalProviderError", 500) == "internal-defect"
    assert n("aborted", "FatalProviderError", None) == "internal-defect"


def test_stop_semantico_desde_artefacto_conserva_marca_y_campos(tmp_path):
    """T-01 (MON-20 R3): el artefacto de stop que I2 escribe por on_result
    (status internal-defect + compatibility_result indeterminate-current-run
    + contexto I4) DEBE conservar la marca de indeterminacion y sus campos en
    la cobertura, end-to-end por `scan_executed_artifacts` + `build_coverage`.
    Sin el arreglo, `_atomic_row` lo publicaba como un internal-defect medido
    cualquiera, indistinguible y sin la marca que I2 existe para producir."""
    runs = tmp_path / "runs"
    runs.mkdir()
    manifest = tmp_path / "manifest.json"
    ledger = tmp_path / "ledger.json"
    manifest.write_text(json.dumps({
        "generated_at": "2026-08-15T00:00:00Z",
        "baseline_inventory": "evals/runs/inventory.json",
        "delta": {}, "pricing": {"path": "apps/agent/evals/pricing-x.json"},
        "rows": [{
            "provider": "kimi", "model": "kimi-k2.6",
            "protocol": "chat_completions", "endpoint": None,
            "structured_output": "json_schema", "tier": "paid",
            "status": "ready", "battles": 2, "concurrency": 1,
            "persist": False, "pin": ["kimi", "kimi-k2.6"],
            "estimated_cost_usd": "1", "estimated_smoke_usd": "0.01",
            "classification_note": "fixture offline",
        }],
    }))
    ledger.write_text(json.dumps({"rows": []}))
    (runs / "r9-kimi-kimi-k2.6-matrix.json").write_text(json.dumps({
        "provider": "kimi", "model": "kimi-k2.6", "tier": "paid",
        "protocol": "chat_completions", "status": "internal-defect",
        "smoke_ok": True, "battles_requested": 2, "battles_completed": 0,
        "effective_provider": None, "effective_model": None, "win_rate": None,
        "completion_latency_ms": None, "decision_latency_ms": None,
        "tokens": None, "retries": 0, "rotations": 0, "quarantined": 0,
        "failure_type": "CancelledError", "failure_cause_type": None,
        "failure_stage": "battle", "http_status": None,
        "provider_error_code": None, "comparable": False, "sample_size": None,
        "compatibility_result": "indeterminate-current-run",
        "battle_timeout_seconds": 1800.0, "round": "r9",
        "generated_at": "2026-08-15T00:00:00Z", "manifest": "m.json",
        "manifest_sha256": "deadbeef",
        "note": "fila interrumpida por CancelledError durante battle: "
                "artefacto de stop sanitizado",
    }))
    doc = bmc.build_coverage(
        manifest_path=manifest, runs_dir=runs, ledger_path=ledger,
        generated_at="2026-08-15T00:00:00Z",
    )
    row = doc["rows"][0]
    # stop semantico: NUNCA se publica como internal-defect medido cualquiera
    assert row["evidence_kind"] == "sanitized-diagnostic-stop", row
    assert row["compatibility_result"] == "indeterminate-current-run"
    assert row["final_classification"] == "internal-defect"
    assert row["failure_type"] == "CancelledError"
    assert row["failure_stage"] == "battle"
    # contexto I4 conservado en la fila
    assert row["sample_size"] is None
    assert row["round"] == "r9"
    assert row["manifest"] == "m.json"
    assert row["manifest_sha256"] == "deadbeef"
    assert row["generated_at"] == "2026-08-15T00:00:00Z"
    assert row["battle_timeout_seconds"] == 1800.0
    assert row["source_artifact"] == "r9-kimi-kimi-k2.6-matrix.json"
    # la invariante lo cuenta como stop, no como artefacto atomico medido
    assert doc["counts"]["by_evidence_kind"]["sanitized-diagnostic-stop"] == 1
    assert doc["counts"]["by_evidence_kind"].get(
        "atomic-runtime-artifact", 0
    ) == 0


def test_atomic_row_lee_comparable_winrate_y_sample_del_artefacto(tmp_path):
    """T-04 (MON-20 R3): comparable/win_rate/sample_size salen del artefacto,
    no de literales: la invariante `comparable_rows == 0` deja de ser
    tautologica. Un artefacto que declare comparable=true se refleja en la
    fila y en la invariante."""
    plan = {
        "provider": "open_code_zen", "model": "hy3-free",
        "protocol": "chat_completions", "endpoint": None,
        "structured_output": None, "tier": "free", "status": "ready",
        "battles": 2, "concurrency": 1, "persist": False,
        "pin": ["open_code_zen", "hy3-free"],
        "estimated_cost_usd": "0", "estimated_smoke_usd": "0",
        "classification_note": "fixture",
    }
    # historico sin campo comparable -> default False, win_rate null (I5)
    row = bmc._atomic_row(plan, {
        "provider": "open_code_zen", "model": "hy3-free",
        "status": "compatible", "smoke_ok": True,
        "battles_requested": 2, "battles_completed": 2,
        "failure_type": None, "http_status": None,
    }, "r2-zen-free-open_code_zen-hy3-free-matrix.json")
    assert row["comparable"] is False
    assert row["win_rate"] is None
    assert row["sample_size"] is None
    # artefacto nuevo (runner I5) con la declaracion explicita
    row2 = bmc._atomic_row(plan, {
        "provider": "open_code_zen", "model": "hy3-free",
        "status": "compatible", "smoke_ok": True,
        "battles_requested": 2, "battles_completed": 2,
        "comparable": True, "win_rate": "0.5000", "sample_size": 2,
        "failure_type": None, "http_status": None,
    }, "r2-zen-free-open_code_zen-hy3-free-matrix.json")
    assert row2["comparable"] is True
    assert row2["win_rate"] == "0.5000"
    assert row2["sample_size"] == 2


def test_scan_prefiere_artefacto_reciente_por_generated_at(tmp_path):
    """T-07 (MON-20 R3): `scan_executed_artifacts` ordena por el
    `generated_at` que I4 persiste en el artefacto, con el nombre como
    desempate; para historicos sin fecha usa el fallback lexicografico
    documentado (prefijos de ronda cronologicos)."""
    runs = tmp_path / "runs"
    runs.mkdir()

    def artifact(generated_at=None):
        base = {"provider": "kimi", "model": "kimi-k2.6",
                "status": "compatible"}
        if generated_at is not None:
            base["generated_at"] = generated_at
        return json.dumps(base)

    # r10 es lexicograficamente MENOR que r9 (la premisa del fallback), pero
    # su generated_at es MAS RECIENTE: el generated_at manda
    (runs / "r10-kimi-kimi-k2.6-matrix.json").write_text(
        artifact("2026-08-15T10:00:00Z"))
    (runs / "r9-kimi-kimi-k2.6-matrix.json").write_text(
        artifact("2026-08-15T09:00:00Z"))
    (runs / "r8-kimi-kimi-k2.6-matrix.json").write_text(
        artifact())  # historico sin fecha

    scanned = bmc.scan_executed_artifacts(runs)
    filename, document = scanned[("kimi", "kimi-k2.6")]
    assert filename == "r10-kimi-kimi-k2.6-matrix.json"
    assert document["generated_at"] == "2026-08-15T10:00:00Z"

    # fallback determinista: solo historicos sin fecha -> mayor nombre
    runs2 = tmp_path / "runs2"
    runs2.mkdir()
    (runs2 / "r9-kimi-kimi-k2.6-matrix.json").write_text(artifact())
    (runs2 / "r10-kimi-kimi-k2.6-matrix.json").write_text(artifact())
    scanned2 = bmc.scan_executed_artifacts(runs2)
    assert scanned2[("kimi", "kimi-k2.6")][0] == \
        "r9-kimi-kimi-k2.6-matrix.json"


def test_ledger_commiteado_estructura_e_invariantes():
    """I1/I3: el ledger de stops es la fuente versionada de las 4 filas
    interrumpidas: clasificacion internal-defect, compatibilidad
    indeterminada, referencia de log relativa (M2), hash y eventos
    saneados presentes."""
    ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    assert len(ledger["rows"]) == 4
    expected_keys = {
        ("open_code_zen", "deepseek-v4-flash"),
        ("open_code_zen", "gpt-5.6-luna"),
        ("open_code_zen", "minimax-m2.5"),
        ("kimi", "kimi-k2.6"),
    }
    for row in ledger["rows"]:
        key = (row["provider"], row["model"])
        assert key in expected_keys
        assert row["final_classification"] == "internal-defect"
        assert row["compatibility_result"] == "indeterminate-current-run"
        assert row["comparable"] is False
        assert row["win_rate"] is None
        assert row["failure_stage"] == "battle"
        evidence = row["log_evidence"]
        assert evidence["sha256"]
        assert isinstance(evidence["bytes"], int)
        assert evidence["sanitized_events"]
        # M2: referencia relativa, nunca ruta absoluta del operador
        assert "/" not in evidence["log_reference"], evidence
        assert "tmp" not in evidence["log_reference"], evidence
    # T-06 (MON-20 R3): gpt-5.6-luna nunca se reintenta — asercion sobre el
    # campo EXACTO `future_execution`, no sobre cualquier aparicion de texto
    luna = next(r for r in ledger["rows"] if r["model"] == "gpt-5.6-luna")
    assert luna["future_execution"] == "operator-prohibited-never-retry"


def test_evidencia_versionada_sin_rutas_absolutas():
    """M2: la evidencia versionada (coverage, ledger, manifiesto) no filtra
    el layout local del operador: ninguna ruta absoluta /Users/... ni
    /tmp/ludex-coordination/..."""
    for path in (COVERAGE_PATH, LEDGER_PATH, MANIFEST_PATH):
        text = path.read_text(encoding="utf-8")
        assert "/Users/" not in text, path
        assert "/tmp/" not in text, path
    # el pricing del coverage y del manifiesto es una referencia relativa
    doc = _committed()
    assert doc["discovery"]["pricing"]["path"].startswith("apps/agent/evals/")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["pricing"]["path"].startswith("apps/agent/evals/")


def test_manifiesto_final_marca_luna_operator_prohibited():
    """I3: el manifiesto final versionado marca gpt-5.6-luna como no
    ejecutable (politica declarativa), sin borrar su historial (sigue
    ready/paid con evidencia de stop)."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    luna = next(r for r in manifest["rows"] if r["model"] == "gpt-5.6-luna")
    assert luna["provider"] == "open_code_zen"
    assert luna["status"] == "ready"  # historial del plan preservado
    assert luna.get("operator_prohibited") == "operator-prohibited-never-retry"
    policy = json.loads(
        (EVALS_DIR / "operator-policy.json").read_text(encoding="utf-8")
    )
    entries = {
        (e["provider"], e["model"]): e["action"]
        for e in policy["entries"]
    }
    assert entries[("open_code_zen", "gpt-5.6-luna")] == \
        "operator-prohibited-never-retry"
    # la politica es declarativa y no menciona condicionales de src
    assert policy["scope"]["live_game_execution"] == \
        "Chinese models and Gemini free tier only"
