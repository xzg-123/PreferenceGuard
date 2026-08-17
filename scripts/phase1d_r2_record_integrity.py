"""Metric-blind integrity checks for the Phase 1D-R2 exact-slot recovery."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log


ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_LOG = ROOT / "artifacts/phase-1d/logs/2026-08-15T20-23-46-00-00_persistbench-cross-domain_nFDaEr33DAZCB6fcYqirBP.eval"
FINGERPRINT_PATH = ROOT / "artifacts/phase-1d/recovery/phase-1d-cross-domain-original-52-fingerprints.json"
EXPECTED_MISSING = {
    ("persistbench_380f234d", 3),
    ("persistbench_3a410733", 3),
    ("persistbench_70cb0bf1", 3),
    ("persistbench_788eb782", 3),
    ("persistbench_aebb0255", 3),
    ("persistbench_c51c3c8b", 3),
    ("persistbench_ee1bf6af", 3),
    ("persistbench_f78883e3", 3),
}


def normalize(value: Any) -> Any:
    """Produce a deterministic semantic record without run-local trace fields."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def record_payload(sample: Any) -> dict[str, Any]:
    data = sample.model_dump(mode="json")
    return {
        "id": data["id"],
        "epoch": data["epoch"],
        "input": normalize(data["input"]),
        "target": normalize(data["target"]),
        "messages": normalize(data["messages"]),
        "output": normalize(data["output"]),
        "scores": normalize(data["scores"]),
        "metadata": normalize(data["metadata"]),
        "store": normalize(data["store"]),
        "error": normalize(data["error"]),
    }


def digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_records(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    log = read_eval_log(str(path))
    records: dict[tuple[str, int], dict[str, Any]] = {}
    for sample in log.samples:
        payload = record_payload(sample)
        key = (str(payload["id"]), int(payload["epoch"]))
        if key in records:
            raise RuntimeError(f"Duplicate record in {path}: {key}")
        if payload["error"] is not None or not payload["scores"]:
            raise RuntimeError(f"Non-valid scored record in {path}: {key}")
        records[key] = {"fingerprint": digest(payload), "payload": payload}
    return records


def prepare() -> None:
    records = load_records(ORIGINAL_LOG)
    if len(records) != 52:
        raise RuntimeError(f"Expected 52 original records; found {len(records)}")
    missing = {(f"persistbench_{sample_id}", epoch) for sample_id, epoch in []}
    actual_missing = {(sample_id, epoch) for sample_id in {key[0] for key in EXPECTED_MISSING} for epoch in range(1, 4)} - set(records)
    if actual_missing != EXPECTED_MISSING:
        raise RuntimeError(f"Original missing-slot set mismatch: {sorted(actual_missing)}")
    output = {
        "phase": "PHASE_1D_R2",
        "purpose": "METRIC_BLIND_IMMUTABILITY_FINGERPRINT_OF_EXISTING_VALID_CROSS_DOMAIN_RECORDS",
        "original_log": str(ORIGINAL_LOG.relative_to(ROOT)).replace("\\", "/"),
        "original_log_sha256": hashlib.sha256(ORIGINAL_LOG.read_bytes()).hexdigest(),
        "record_count": len(records),
        "fingerprint_definition": "SHA256 of canonical id, epoch, input, target, messages, output, scores, metadata, store, and error fields; excludes run-local timestamps, UUIDs, events, timelines, and usage accounting.",
        "records": [
            {"logical_sample_id": key[0], "epoch": key[1], "fingerprint_sha256": value["fingerprint"]}
            for key, value in sorted(records.items())
        ],
        "formal_partial_metrics_inspected": False,
    }
    FINGERPRINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINGERPRINT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PREPARED", "record_count": len(records), "artifact": str(FINGERPRINT_PATH.relative_to(ROOT))}))


def verify(recovery_log_arg: str) -> None:
    baseline = json.loads(FINGERPRINT_PATH.read_text(encoding="utf-8"))
    baseline_fingerprints = {
        (entry["logical_sample_id"], int(entry["epoch"])): entry["fingerprint_sha256"]
        for entry in baseline["records"]
    }
    recovered_path = Path(recovery_log_arg).resolve()
    recovered = load_records(recovered_path)
    expected_keys = set(baseline_fingerprints) | EXPECTED_MISSING
    if set(recovered) != expected_keys:
        raise RuntimeError(f"Recovered key set mismatch: missing={sorted(expected_keys - set(recovered))}, unexpected={sorted(set(recovered) - expected_keys)}")
    changed = [key for key, old in baseline_fingerprints.items() if recovered[key]["fingerprint"] != old]
    if changed:
        raise RuntimeError(f"Original record fingerprint changed: {changed}")
    if len(recovered) != 60:
        raise RuntimeError(f"Expected 60 valid scored records; found {len(recovered)}")
    print(json.dumps({"status": "PASS", "original_records_preserved": 52, "recovered_records": 8, "total_records": 60, "recovery_log": str(recovered_path.relative_to(ROOT))}))


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in {"prepare", "verify"}:
        raise SystemExit("Usage: phase1d_r2_record_integrity.py prepare | verify <recovery-log>")
    if sys.argv[1] == "prepare":
        prepare()
    else:
        if len(sys.argv) != 3:
            raise SystemExit("verify requires a recovery log path")
        verify(sys.argv[2])
