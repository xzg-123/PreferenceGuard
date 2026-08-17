"""Build the Phase 1A exclusion and deterministic split freeze without model calls."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from inspect_ai.log import read_eval_log
from inspect_evals.persistbench.dataset import persistbench_dataset


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "phase-1a"
NAMESPACE = "PreferenceGuard_Phase1A_v1"

DATASETS = {
    "persistbench_cross_domain": {
        "file": ROOT / "inspect_evals/src/inspect_evals/persistbench/benchmark_samples/cross_domain.jsonl",
        "expected_hash": "883fbf9e5733cfb2f577e8cd18ffe9a7ec4ea96f0195449fabe8b3fa2b6687b4",
        "epochs": 3,
    },
    "persistbench_sycophancy": {
        "file": ROOT / "inspect_evals/src/inspect_evals/persistbench/benchmark_samples/sycophancy.jsonl",
        "expected_hash": "634f5a533b4a3a7af55052b2c4bd55b4d30634e862ea4a48fb8e4dbbab412858",
        "epochs": 3,
    },
    "persistbench_beneficial_memory": {
        "file": ROOT / "inspect_evals/src/inspect_evals/persistbench/benchmark_samples/beneficial_samples.jsonl",
        "expected_hash": "26a951bee5da72049616674ffacfc9c725cd65f88355bee579831e4f51a044d3",
        "epochs": 1,
    },
}

SPLIT_COUNTS = {
    "development_main": 20,
    "development_reserve": 10,
    "frozen_validation": 20,
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, data: Any) -> str:
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(rendered, encoding="utf-8")
    # Hash the persisted bytes, not the in-memory LF rendering. This keeps the
    # recorded freeze hash correct on Windows text-mode newline translation.
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def add_exposure(
    exposures: dict[tuple[str, str], dict[str, set[str]]],
    task: str,
    sample_id: str,
    reason: str,
    source_phase: str,
    source: str,
) -> None:
    key = (task, sample_id)
    entry = exposures.setdefault(
        key,
        {"reasons": {"PHASE_0_EXPOSED_ONLY"}, "phases": set(), "sources": set()},
    )
    entry["reasons"].add(reason)
    entry["phases"].add(source_phase)
    entry["sources"].add(source)


def frozen_exposures() -> dict[tuple[str, str], dict[str, set[str]]]:
    exposures: dict[tuple[str, str], dict[str, set[str]]] = {}

    # Phase 0D: every logical sample present in a real smoke/gate eval artifact.
    for path in sorted((ROOT / "artifacts/phase-0d-cli-smoke/logs").glob("*.eval")):
        log = read_eval_log(path)
        for sample in log.samples:
            add_exposure(
                exposures,
                log.eval.task,
                sample.id,
                "PHASE_0D_SMOKE_OR_GATE",
                "PHASE_0D",
                path.relative_to(ROOT).as_posix(),
            )

    # The calibration manifest is the frozen primary mapping between blind IDs and samples.
    calibration = load_json(ROOT / "artifacts/phase-0e/judge-calibration-manifest.json")
    by_blind: dict[str, tuple[str, str]] = {}
    for record in calibration["records"]:
        task = record["source_task"]
        sample_id = record["stable_sample_id"]
        by_blind[record["blind_id"]] = (task, sample_id)
        add_exposure(
            exposures,
            task,
            sample_id,
            "JUDGE_CALIBRATION_ONLY",
            "PHASE_0E",
            "artifacts/phase-0e/judge-calibration-manifest.json",
        )

    # Blind reviewer B has one result for each frozen calibration record.
    reviewer_b = load_json(ROOT / "artifacts/phase-0e/blind-reviewer-b-results.json")
    for record in reviewer_b:
        if record["blind_id"] not in by_blind:
            raise RuntimeError(f"Blind reviewer result lacks calibration mapping: {record['blind_id']}")
        task, sample_id = by_blind[record["blind_id"]]
        if task != record["task"]:
            raise RuntimeError(f"Blind reviewer task mismatch: {record['blind_id']}")
        add_exposure(
            exposures,
            task,
            sample_id,
            "JUDGE_CALIBRATION_ONLY",
            "PHASE_0E",
            "artifacts/phase-0e/blind-reviewer-b-results.json",
        )

    # The completed human audit maps HUM IDs back to frozen CAL IDs.
    human_map = load_json(ROOT / "artifacts/phase-0e/human-audit-hidden-map.json")["mapping"]
    for record in human_map:
        blind_id = record["original_blind_id"]
        if blind_id not in by_blind:
            raise RuntimeError(f"Human audit mapping lacks calibration mapping: {blind_id}")
        task, sample_id = by_blind[blind_id]
        add_exposure(
            exposures,
            task,
            sample_id,
            "JUDGE_CALIBRATION_ONLY",
            "PHASE_0E",
            "artifacts/phase-0e/human-audit-hidden-map.json",
        )

    # Phase 0F records each Kimi rescore against the same frozen logical sample.
    kimi_rows = [
        json.loads(line)
        for line in (ROOT / "artifacts/phase-0f/official-kimi-judge-results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for record in kimi_rows:
        blind_id = record["record_id"]
        if blind_id not in by_blind:
            raise RuntimeError(f"Kimi rescore lacks calibration mapping: {blind_id}")
        task, sample_id = by_blind[blind_id]
        if task != record["task"] or sample_id != record["logical_sample_id"]:
            raise RuntimeError(f"Kimi rescore mapping mismatch: {blind_id}")
        add_exposure(
            exposures,
            task,
            sample_id,
            "JUDGE_CALIBRATION_ONLY",
            "PHASE_0F",
            "artifacts/phase-0f/official-kimi-judge-results.jsonl",
        )

    return exposures


def load_universe() -> tuple[dict[str, list[dict[str, str]]], dict[str, dict[str, Any]]]:
    universes: dict[str, list[dict[str, str]]] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for task, config in DATASETS.items():
        path = config["file"]
        actual_hash = sha256_bytes(path.read_bytes())
        if actual_hash != config["expected_hash"]:
            raise RuntimeError(f"Dataset hash mismatch for {task}: {actual_hash}")
        dataset = persistbench_dataset(path)
        rows = [{"logical_sample_id": sample.id, "task": task} for sample in dataset]
        if len(rows) != len({row["logical_sample_id"] for row in rows}):
            raise RuntimeError(f"Duplicate logical sample IDs within {task}")
        universes[task] = rows
        provenance[task] = {
            "source_dataset": path.relative_to(ROOT).as_posix(),
            "source_dataset_sha256": actual_hash,
            "bundled_logical_sample_count": len(rows),
            "official_epochs_per_logical_sample": config["epochs"],
        }
    all_keys = [(row["task"], row["logical_sample_id"]) for rows in universes.values() for row in rows]
    if len(all_keys) != len(set(all_keys)):
        raise RuntimeError("Duplicate task/sample identities across the bundled universe")
    return universes, provenance


def ranked(task: str, rows: list[dict[str, str]]) -> list[dict[str, str]]:
    output = []
    for row in rows:
        rank = sha256_bytes(f"{NAMESPACE}|{task}|{row['logical_sample_id']}".encode("utf-8"))
        output.append({**row, "deterministic_rank_sha256": rank})
    return sorted(output, key=lambda row: (row["deterministic_rank_sha256"], row["logical_sample_id"]))


def split_outputs(
    universes: dict[str, list[dict[str, str]]],
    exposures: dict[tuple[str, str], dict[str, set[str]]],
    provenance: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    exclusion_records = []
    for (task, sample_id), entry in sorted(exposures.items()):
        if task not in universes or sample_id not in {row["logical_sample_id"] for row in universes[task]}:
            raise RuntimeError(f"Exposed record cannot map to a unique frozen dataset sample: {task}/{sample_id}")
        exclusion_records.append(
            {
                "logical_sample_id": sample_id,
                "task": task,
                "exposure_reason": sorted(entry["reasons"]),
                "source_phase": sorted(entry["phases"]),
                "source_artifacts": sorted(entry["sources"]),
                "exclusion_status": "EXCLUDED_FROM_DEVELOPMENT_MAIN_DEVELOPMENT_RESERVE_FROZEN_VALIDATION",
            }
        )

    exclusion = {
        "schema_version": "PREFERENCEGUARD_PHASE_1A_EXCLUSION_V1",
        "policy": "All PHASE_0_EXPOSED_ONLY logical samples are excluded from every Phase 1 split.",
        "records": exclusion_records,
        "counts_by_task": {
            task: sum(record["task"] == task for record in exclusion_records) for task in DATASETS
        },
    }

    selected: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(dict)
    untouched: dict[str, list[dict[str, str]]] = {}
    for task, rows in universes.items():
        eligible = [row for row in rows if (task, row["logical_sample_id"]) not in exposures]
        ordered = ranked(task, eligible)
        need = sum(SPLIT_COUNTS.values())
        if len(ordered) < need:
            raise RuntimeError(f"Insufficient eligible samples for {task}: {len(ordered)} < {need}")
        main_end = SPLIT_COUNTS["development_main"]
        reserve_end = main_end + SPLIT_COUNTS["development_reserve"]
        validation_end = reserve_end + SPLIT_COUNTS["frozen_validation"]
        selected[task]["development_main"] = ordered[:main_end]
        selected[task]["development_reserve"] = ordered[main_end:reserve_end]
        selected[task]["frozen_validation"] = ordered[reserve_end:validation_end]
        untouched[task] = ordered[validation_end:]

    def make_manifest(split: str) -> dict[str, Any]:
        records = [record for task in DATASETS for record in selected[task][split]]
        return {
            "schema_version": "PREFERENCEGUARD_PHASE_1A_SPLIT_V1",
            "split": split,
            "split_namespace": NAMESPACE,
            "selection_policy": "Sort eligible samples by SHA256(namespace|task|logical_sample_id), ascending; dataset order is not used.",
            "records": records,
            "counts_by_task": {task: len(selected[task][split]) for task in DATASETS},
            "source_provenance": provenance,
        }

    main = make_manifest("development_main")
    reserve = make_manifest("development_reserve")
    validation = make_manifest("frozen_validation")
    untouched_summary = {
        "schema_version": "PREFERENCEGUARD_PHASE_1A_UNTOUCHED_POOL_V1",
        "split_namespace": NAMESPACE,
        "policy": "Untouched pool must not be run in Phase 1A or Phase 1B unless separately authorized.",
        "counts_by_task": {task: len(untouched[task]) for task in DATASETS},
        "total_count": sum(len(rows) for rows in untouched.values()),
        "source_provenance": provenance,
    }
    return exclusion, main, reserve, validation, untouched_summary


def generator_baseline() -> dict[str, Any]:
    logs = sorted((ROOT / "artifacts/phase-0d-cli-smoke/logs").glob("*.eval")) + sorted(
        (ROOT / "artifacts/phase-0e/logs").glob("*.eval")
    )
    model = "deepseek/deepseek-v4-flash"
    total_tokens = 0
    epochs = 0
    by_task: dict[str, dict[str, int]] = defaultdict(lambda: {"epoch_records": 0, "total_tokens": 0})
    for path in logs:
        log = read_eval_log(path)
        usage = log.stats.model_usage.get(model)
        if usage is None or usage.total_tokens is None:
            raise RuntimeError(f"Generator-only usage missing from frozen artifact: {path}")
        count = len(log.samples)
        total_tokens += usage.total_tokens
        epochs += count
        by_task[log.eval.task]["epoch_records"] += count
        by_task[log.eval.task]["total_tokens"] += usage.total_tokens
    if epochs == 0:
        raise RuntimeError("No frozen generator epoch records available")
    return {
        "source_artifacts": [path.relative_to(ROOT).as_posix() for path in logs],
        "model": model,
        "epoch_records": epochs,
        "total_tokens": total_tokens,
        "total_tokens_per_epoch": total_tokens / epochs,
        "by_task": dict(by_task),
    }


def validate_and_report(
    exclusion: dict[str, Any],
    main: dict[str, Any],
    reserve: dict[str, Any],
    validation: dict[str, Any],
    untouched: dict[str, Any],
    hashes: dict[str, str],
) -> dict[str, Any]:
    split_sets = {
        "development_main": {(r["task"], r["logical_sample_id"]) for r in main["records"]},
        "development_reserve": {(r["task"], r["logical_sample_id"]) for r in reserve["records"]},
        "frozen_validation": {(r["task"], r["logical_sample_id"]) for r in validation["records"]},
    }
    excluded = {(r["task"], r["logical_sample_id"]) for r in exclusion["records"]}
    checks = {
        "development_main_x_reserve_overlap": len(split_sets["development_main"] & split_sets["development_reserve"]),
        "development_main_x_validation_overlap": len(split_sets["development_main"] & split_sets["frozen_validation"]),
        "reserve_x_validation_overlap": len(split_sets["development_reserve"] & split_sets["frozen_validation"]),
        "exclusion_x_development_main_overlap": len(excluded & split_sets["development_main"]),
        "exclusion_x_development_reserve_overlap": len(excluded & split_sets["development_reserve"]),
        "exclusion_x_frozen_validation_overlap": len(excluded & split_sets["frozen_validation"]),
        "split_logical_sample_duplicate_count": sum(
            len(records) - len({r["logical_sample_id"] for r in records})
            for records in (main["records"], reserve["records"], validation["records"])
        ),
        "task_count_exact": all(
            manifest["counts_by_task"] == {task: expected for task in DATASETS}
            for manifest, expected in ((main, 20), (reserve, 10), (validation, 20))
        ),
        "dataset_hashes_exact": True,
        "deterministic_rerun_identical": True,
    }
    if any(value != 0 for key, value in checks.items() if key.endswith("overlap") or key.endswith("duplicate_count")):
        raise RuntimeError(f"Split contamination or duplication: {checks}")
    if not checks["task_count_exact"]:
        raise RuntimeError(f"Task counts are not exact: {checks}")

    epochs = {task: DATASETS[task]["epochs"] for task in DATASETS}
    planned_epoch_records = {task: 20 * epochs[task] for task in DATASETS}
    planned_total = sum(planned_epoch_records.values())
    kimi_tokens_per_call = 230669 / 24
    kimi_latency_per_call = 285.234 / 24
    kimi_expected_tokens = kimi_tokens_per_call * planned_total
    kimi_alert_minimum = 1.5 * kimi_expected_tokens
    generator = generator_baseline()
    generator_expected_tokens = generator["total_tokens_per_epoch"] * planned_total
    generator_alert_minimum = 1.5 * generator_expected_tokens
    report = {
        "schema_version": "PREFERENCEGUARD_PHASE_1A_INTEGRITY_V1",
        "status": "PASS",
        "ambiguities": [],
        "source_dataset_hashes": {task: DATASETS[task]["expected_hash"] for task in DATASETS},
        "exclusion_count_by_task": exclusion["counts_by_task"],
        "eligible_count_by_task": {
            task: DATASETS[task]["bundled_count"] - exclusion["counts_by_task"][task] for task in DATASETS
        },
        "checks": checks,
        "manifest_sha256": hashes,
        "official_epochs_per_logical_sample": epochs,
        "phase_1b_development_main_projection": {
            "logical_samples": 60,
            "epoch_records": planned_epoch_records,
            "total_epoch_records": planned_total,
            "planned_deepseek_generation_calls": planned_total,
            "planned_kimi_judge_calls": planned_total,
        },
        "kimi_budget": {
            "empirical_source": "Phase 0F official Kimi 24-call rescore",
            "empirical_total_tokens_per_call": kimi_tokens_per_call,
            "empirical_aggregate_latency_seconds_per_call": kimi_latency_per_call,
            "expected_total_tokens": kimi_expected_tokens,
            "expected_aggregate_latency_seconds": kimi_latency_per_call * planned_total,
            "hard_alert_minimum_tokens": kimi_alert_minimum,
            "hard_alert_tokens_with_rounding_buffer": 2100000,
        },
        "generator_budget": {
            "status": "NUMERICALLY_FROZEN_FROM_PHASE_0",
            "generator_only_historical": generator,
            "expected_total_tokens": generator_expected_tokens,
            "hard_alert_minimum_tokens": generator_alert_minimum,
            "hard_alert_tokens_with_rounding_buffer": 1600000,
        },
        "untouched_pool": untouched["counts_by_task"],
    }
    return report


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    universes, provenance = load_universe()
    for task in DATASETS:
        DATASETS[task]["bundled_count"] = len(universes[task])
    exposures = frozen_exposures()
    first = split_outputs(universes, exposures, provenance)
    second = split_outputs(universes, exposures, provenance)
    if json.dumps(first, sort_keys=True) != json.dumps(second, sort_keys=True):
        raise RuntimeError("Deterministic split rerun was not identical")
    exclusion, main_manifest, reserve_manifest, validation_manifest, untouched = first
    hashes = {
        "phase-0-exclusion-manifest.json": write_json(OUT / "phase-0-exclusion-manifest.json", exclusion),
        "development-main-manifest.json": write_json(OUT / "development-main-manifest.json", main_manifest),
        "development-reserve-manifest.json": write_json(OUT / "development-reserve-manifest.json", reserve_manifest),
        "frozen-validation-manifest.json": write_json(OUT / "frozen-validation-manifest.json", validation_manifest),
    }
    write_json(OUT / "untouched-pool-summary.json", untouched)
    report = validate_and_report(exclusion, main_manifest, reserve_manifest, validation_manifest, untouched, hashes)
    write_json(OUT / "phase-1a-integrity-report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
