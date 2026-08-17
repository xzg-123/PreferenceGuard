"""Phase 1E-B1C: exact sequential execution of the 14 remaining Beneficial slots.

This is an orchestration-only runner. It imports the frozen official PersistBench
solver and scorer and deliberately contains no score aggregation or product
decision logic.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inspect_evals" / "src"))

from inspect_ai._eval.task.generate import task_generate
from inspect_ai._util.notgiven import NOT_GIVEN
from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.model._model import ModelName
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_evals.persistbench.dataset import persistbench_dataset
from inspect_evals.persistbench.scorers import persistbench_judge
from inspect_evals.persistbench.solvers import persistbench_solver
import inspect_evals.persistbench.scorers as official_scorers


PHASE = ROOT / "artifacts" / "phase-1e"
B1 = PHASE / "beneficial"
OUT = B1 / "b1c"
CONFIG_PATH = PHASE / "phase-1e-v2-treatment-config.json"
B1_MANIFEST = B1 / "phase-1e-b1-execution-manifest.json"
B1_LEDGER = B1 / "phase-1e-b1-execution-ledger.jsonl"
B1_RECORDS = B1 / "phase-1e-b1-completed-records.jsonl"
B1B_POLICY = B1 / "b1b" / "phase-1e-b1b-missing-slot-policy.json"
MISSING_SLOT = "persistbench_7c438f64:epoch=1"
V2_SHA = "628dfc7bf07a64ee27093837f6eb790bb482c99efb8bbf784dc75070b27fa994"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canon(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def exception_record(error: BaseException, stage: str, slot: str | None) -> dict[str, Any]:
    return {
        "exception_class": type(error).__name__,
        "exception_message": str(error),
        "traceback": traceback.format_exc(),
        "lifecycle_stage": stage,
        "target_slot": slot,
        "captured_at": now(),
    }


def record_is_complete(record: dict[str, Any]) -> bool:
    """Check only record structure; never inspect a score value."""
    return bool(
        record.get("id")
        and record.get("epoch")
        and record.get("output", {}).get("completion")
        and record.get("scores", {}).get("persistbench_judge")
    )


def safe_slot_name(slot: str) -> str:
    return slot.replace(":", "_").replace("=", "_")


def protected_assets_fingerprint() -> dict[str, Any]:
    """Hash prior assets without interpreting their score outcomes."""
    roots = [
        PHASE / "recovery" / "r4l",
        PHASE / "sycophancy",
        B1 / "records",
        B1 / "b1a",
        B1 / "b1b",
    ]
    files = [B1_MANIFEST, B1_LEDGER, B1_RECORDS, B1 / "phase-1e-b1-integrity-report.json", B1 / "PHASE_1E_B1_EXECUTION_REPORT.md"]
    for root in roots:
        if root.exists():
            files.extend(path for path in root.rglob("*") if path.is_file())
    entries = {
        str(path.relative_to(ROOT)).replace("\\", "/"): sha_file(path)
        for path in sorted(set(files))
    }
    return {"entries": entries, "sha256": sha_text(canon(entries))}


class JudgeUsageCapture:
    """Transparent official-model proxy; records raw judge usage only."""

    def __init__(self, model: Any, capture: dict[str, Any]) -> None:
        self.model = model
        self.capture = capture

    async def generate(self, *args: Any, **kwargs: Any) -> Any:
        result = await self.model.generate(*args, **kwargs)
        dumped = result.model_dump(mode="json") if hasattr(result, "model_dump") else {}
        self.capture["usage"] = dumped.get("usage")
        self.capture["output_model"] = dumped.get("model")
        return result


async def run() -> dict[str, Any]:
    manifest_path = OUT / "phase-1e-b1c-execution-manifest.json"
    ledger_path = OUT / "phase-1e-b1c-execution-ledger.jsonl"
    records_path = OUT / "phase-1e-b1c-completed-records.jsonl"
    report_path = OUT / "phase-1e-b1c-integrity-report.json"
    markdown_path = OUT / "PHASE_1E_B1C_EXECUTION_REPORT.md"
    canonical_path = OUT / "phase-1e-b1c-canonical-beneficial-universe.json"
    records_dir = OUT / "records"
    calls = {
        "beneficial_generator": 0,
        "beneficial_judge": 0,
        "cross_domain": 0,
        "sycophancy": 0,
        "reserve": 0,
        "frozen_validation": 0,
    }
    attempted: list[str] = []
    completed: list[str] = []
    persisted_generation: list[str] = []
    warnings: list[str] = []
    failure: dict[str, Any] | None = None
    stage = "B1C_PRECALL_INVARIANTS"
    target: str | None = None
    pre_assets: dict[str, Any] | None = None
    post_assets: dict[str, Any] | None = None
    canonical_count = 0

    def marker(event: str, slot: str, sequence: int, **details: Any) -> None:
        append(
            ledger_path,
            {
                "event": event,
                "slot": slot,
                "sequence_number": sequence,
                "route": "BENEFICIAL",
                "timestamp": now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    try:
        if OUT.exists() or manifest_path.exists():
            raise AssertionError("B1C output/history already exists; restart is forbidden.")
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        b1 = json.loads(B1_MANIFEST.read_text(encoding="utf-8"))
        b1b = json.loads(B1B_POLICY.read_text(encoding="utf-8"))
        previous_items = [json.loads(line) for line in B1_RECORDS.read_text(encoding="utf-8").splitlines() if line]
        dataset = config["development_main_datasets"]["persistbench_beneficial_memory"]
        original_order = list(b1["frozen_slot_order"])
        expected_order = [f"{sample_id}:epoch=1" for sample_id in dataset["logical_sample_ids"]]
        previous_completed = list(b1["completed_slots"])
        if original_order != expected_order or len(original_order) != 20 or len(set(original_order)) != 20:
            raise AssertionError("Original frozen Beneficial 20-slot manifest differs from the frozen config.")
        if b1.get("status") != "PHASE_1E_B1_EXECUTION_INTERRUPTED" or previous_completed != original_order[:5]:
            raise AssertionError("B1 prior completion topology differs from the authorized state.")
        if b1b.get("affected_slot", {}).get("slot") != MISSING_SLOT or b1b.get("affected_slot", {}).get("terminal_state") != "PRESERVED_GENERATION_UNSCORED_JUDGE_RECOVERY_EXHAUSTED":
            raise AssertionError("B1B permanent missing-slot policy differs.")
        if b1b.get("api_calls", {}).get("model") != 0 or b1b.get("no_beneficial_aggregate_metrics_computed") is not True:
            raise AssertionError("B1B policy-only integrity state differs.")
        if len(previous_items) != 5 or {item.get("slot") for item in previous_items} != set(previous_completed) or any(not record_is_complete(item.get("record", {})) for item in previous_items):
            raise AssertionError("Previously completed Beneficial records are not structurally complete.")
        completed_set = set(previous_completed)
        remaining = [slot for slot in original_order if slot not in completed_set and slot != MISSING_SLOT]
        if len(remaining) != 14 or len(set(remaining)) != 14 or set(remaining) & completed_set or MISSING_SLOT in remaining:
            raise AssertionError("Derived remaining-14 allowlist is invalid.")
        if config["exact_treatment_instruction_sha256"] != V2_SHA:
            raise AssertionError("Frozen V2 instruction SHA differs.")
        frozen = config["frozen_execution"]
        if frozen["generator_model"] != "deepseek/deepseek-v4-flash" or frozen["judge_model"] != "openrouter/moonshotai/kimi-k2-thinking" or frozen["max_retries"] != 0 or frozen["max_connections"] != 1 or frozen["adaptive_connections"] is not False:
            raise AssertionError("Frozen model/retry/connection policy differs.")
        if frozen["judge_configuration"] != {"temperature": 0, "reasoning_effort": "high", "reasoning_enabled": True, "provider_order": ["google-vertex"], "allow_fallbacks": False}:
            raise AssertionError("Frozen Judge configuration differs.")
        if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]:
            raise AssertionError("Unauthorized Reserve/Frozen Validation/V3 scope is enabled.")
        samples = {str(sample.id): sample for sample in persistbench_dataset(ROOT / dataset["path"])}
        if set(samples) != set(dataset["logical_sample_ids"]):
            raise AssertionError("Official Beneficial source identity differs from frozen config.")
        pre_assets = protected_assets_fingerprint()
    except BaseException as error:
        blocked = {
            "phase": "PHASE_1E_B1C",
            "status": "PHASE_1E_B1C_BLOCKED",
            "api_calls": calls,
            "pre_call_failure": exception_record(error, stage, target),
        }
        if not OUT.exists():
            write(report_path, blocked)
        return blocked

    order_sha = sha_text(canon(remaining))
    manifest = {
        "phase": "PHASE_1E_B1C",
        "status": "RUNNING",
        "started_at": now(),
        "original_frozen_beneficial_order": original_order,
        "original_frozen_beneficial_order_sha256": sha_text(canon(original_order)),
        "previous_complete_slots": previous_completed,
        "permanent_unscored_slot": MISSING_SLOT,
        "remaining_exact_slot_order": remaining,
        "remaining_exact_slot_order_sha256": order_sha,
        "remaining_count": 14,
        "v2_instruction_sha256": V2_SHA,
        "generator": frozen["generator_model"],
        "judge": frozen["judge_model"],
        "judge_configuration": frozen["judge_configuration"],
        "scorer": frozen["scorer"],
        "max_retries": 0,
        "custom_semantic_implementation_count": 0,
        "protected_assets_before": pre_assets,
        "api_calls": calls,
        "attempted_slots": attempted,
        "completed_slots": completed,
    }
    write(manifest_path, manifest)

    class EntryCapture(logging.Handler):
        def emit(self, record: Any) -> None:
            if "Unexpected exception loading entrypoints" in record.getMessage():
                warnings.append(record.getMessage())

    capture = EntryCapture()
    entry_logger = logging.getLogger("inspect_ai._util.entrypoints")
    entry_logger.addHandler(capture)  # type: ignore[arg-type]
    solver = persistbench_solver(prompt_template=ROOT / config["prompt_template"]["treatment_prompt_template"])
    generation_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
    generator_model = get_model(frozen["generator_model"], config=generation_config)

    def frozen_judge(capture_state: dict[str, Any]) -> Any:
        model = get_model(
            role="grader",
            default=frozen["judge_model"],
            config=GenerateConfig(temperature=0, reasoning_effort="high", max_retries=0),
            provider={"order": ["google-vertex"], "allow_fallbacks": False},
            reasoning_enabled=True,
        )
        return JudgeUsageCapture(model, capture_state)

    async def one(slot: str, sequence: int) -> None:
        nonlocal stage, target
        target = slot
        stage = "SLOT_PRECHECK"
        if slot not in remaining or slot in completed_set or slot == MISSING_SLOT:
            raise AssertionError("Slot escaped the frozen B1C allowlist.")
        sample_id, epoch_text = slot.split(":epoch=")
        epoch = int(epoch_text)
        sample = samples[sample_id]
        if epoch != 1 or str(sample.id) != sample_id:
            raise AssertionError("Frozen source sample identity/epoch differs.")
        slot_started = time.perf_counter()
        state = TaskState(model=ModelName(frozen["generator_model"]), sample_id=sample_id, epoch=epoch, input=sample.input, messages=[], target=Target(sample.target), choices=sample.choices, output=None, completed=False, metadata=dict(sample.metadata), store={})
        attempted.append(slot)
        marker("SLOT_STARTED", slot, sequence, generator_started=False, judge_started=False)

        async def official_generate(current: TaskState, tool_calls: str = "loop", **kwargs: Any) -> TaskState:
            return await task_generate(model=generator_model, state=current, tool_calls=tool_calls, cache=kwargs.get("cache", NOT_GIVEN), config=generation_config.merge(kwargs))

        stage = "GENERATOR_CALL_ABOUT_TO_ENTER"
        marker(stage, slot, sequence)
        calls["beneficial_generator"] += 1
        stage = "GENERATOR_CALL_ENTERED"
        marker(stage, slot, sequence)
        generation_started = time.perf_counter()
        try:
            state = await solver(state, official_generate)
        except BaseException as error:
            stage = "GENERATOR_RAISED"
            marker(stage, slot, sequence, runtime_error=exception_record(error, stage, slot))
            raise
        generation_latency = time.perf_counter() - generation_started
        if state.output.empty or not state.output.completion or str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("Generation/state is incomplete after official solver execution.")
        output = state.output.model_dump(mode="json")
        response_sha = sha_text(state.output.completion)
        generation_path = records_dir / f"{safe_slot_name(slot)}.generation.json"
        stage = "GENERATION_PERSISTENCE"
        write(
            generation_path,
            {
                "slot": slot,
                "sequence_number": sequence,
                "output": output,
                "response_sha256": response_sha,
                "generator_model": output.get("model"),
                "generator_usage": output.get("usage"),
                "raw_generation_latency_seconds": generation_latency,
                "raw_judge_latency_seconds": None,
                "total_slot_latency_seconds": None,
                "orchestration_latency_seconds": None,
                "v2_instruction_sha256": V2_SHA,
                "generation_provenance": "PHASE_1E_B1C_FIRST_COMPLETED_GENERATION",
                "downstream_evaluation_state": "PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE",
                "immutable_after_persistence": True,
            },
        )
        persisted_generation.append(slot)
        marker("GENERATION_PERSISTED", slot, sequence, generator_completed=True, generator_response_sha256=response_sha, raw_generation_latency_seconds=generation_latency, generator_usage=output.get("usage"), generation_artifact=str(generation_path.relative_to(ROOT)).replace("\\", "/"), downstream_evaluation_state="PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE")

        judge_capture: dict[str, Any] = {}
        stage = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker(stage, slot, sequence, scorer=frozen["scorer"], judge=frozen["judge_model"], max_retries=0)
        calls["beneficial_judge"] += 1
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = lambda: frozen_judge(judge_capture)
        try:
            stage = "JUDGE_SCORER_CALL_ENTERED"
            marker(stage, slot, sequence, observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
            judge_started = time.perf_counter()
            score = await persistbench_judge()(state, Target(sample.target))
            judge_latency = time.perf_counter() - judge_started
        except BaseException as error:
            stage = "JUDGE_SCORER_RAISED"
            marker(stage, slot, sequence, runtime_error=exception_record(error, stage, slot), raw_judge_latency_seconds=time.perf_counter() - judge_started if "judge_started" in locals() else None)
            raise
        finally:
            official_scorers._get_judge = original_get_judge

        stage = "RECOVERED_RECORD_PERSISTENCE"
        total_slot_latency = time.perf_counter() - slot_started
        record = {
            "id": sample_id,
            "epoch": epoch,
            "input": state.input_text,
            "target": state.target.target,
            "messages": [{"role": str(message.role), "content": message.text} for message in state.messages],
            "output": output,
            "scores": {"persistbench_judge": score.model_dump(mode="json")},
            "metadata": dict(state.metadata),
            "store": dict(state.store),
            "recovery_provenance": {
                "recovery_phase": "PHASE_1E_B1C",
                "recovery_route": "BENEFICIAL_EXACT_REMAINING_SLOT_EXECUTION",
                "source_frozen_sample": sample_id,
                "target_epoch": epoch,
                "treatment_sha256": V2_SHA,
                "generator_config": frozen["generator_model"],
                "judge_config": frozen["judge_configuration"],
                "scorer_identity": frozen["scorer"],
                "generation_provenance": "PHASE_1E_B1C_FIRST_COMPLETED_GENERATION",
                "generation_response_sha256": response_sha,
                "score_provenance": "PHASE_1E_B1C_OFFICIAL_PERSISTBENCH_SCORER",
                "raw_generation_latency_seconds": generation_latency,
                "raw_judge_latency_seconds": judge_latency,
                "total_slot_latency_seconds": total_slot_latency,
                "orchestration_latency_seconds": total_slot_latency - generation_latency - judge_latency,
                "generator_usage": output.get("usage"),
                "judge_usage": judge_capture.get("usage"),
            },
        }
        if not record_is_complete(record):
            raise AssertionError("Official scorer did not yield a complete official record.")
        recovered_path = records_dir / f"{safe_slot_name(slot)}.recovered.json"
        item = {"slot": slot, "record": record, "provenance": record["recovery_provenance"]}
        write(recovered_path, item)
        append(records_path, item)
        marker("RECOVERED_RECORD_PERSISTED", slot, sequence, generator_completed=True, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_generation_latency_seconds=generation_latency, raw_judge_latency_seconds=judge_latency, total_slot_latency_seconds=total_slot_latency, orchestration_latency_seconds=total_slot_latency - generation_latency - judge_latency, generator_usage=output.get("usage"), judge_usage=judge_capture.get("usage"), recovered_artifact=str(recovered_path.relative_to(ROOT)).replace("\\", "/"))
        completed.append(slot)

    try:
        marker("B1C_PRE_EXECUTION_PERSISTED", remaining[0], 0, ordered_slots_sha256=order_sha, remaining_count=14, max_retries=0, existing_complete_count=5, permanent_unscored_slot=MISSING_SLOT)
        for sequence, slot in enumerate(remaining, 1):
            await one(slot, sequence)
        rows = [json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
        if len(rows) != 14 or [row.get("slot") for row in rows] != remaining or any(not record_is_complete(row.get("record", {})) for row in rows):
            raise AssertionError("B1C durable records do not exactly equal the frozen remaining-14 allowlist.")
        all_entries = []
        previous_by_slot = {item["slot"]: item for item in previous_items}
        new_by_slot = {item["slot"]: item for item in rows}
        for slot in original_order:
            if slot in previous_by_slot:
                all_entries.append({"slot": slot, "outcome_state": "OFFICIAL_SCORED", "provenance": "EXISTING_B1_COMPLETE_OFFICIAL_RECORD", "record_artifact": "artifacts/phase-1e/beneficial/phase-1e-b1-completed-records.jsonl"})
            elif slot in new_by_slot:
                all_entries.append({"slot": slot, "outcome_state": "OFFICIAL_SCORED", "provenance": "B1C_COMPLETE_OFFICIAL_RECORD", "record_artifact": str((records_dir / f"{safe_slot_name(slot)}.recovered.json").relative_to(ROOT)).replace("\\", "/")})
            elif slot == MISSING_SLOT:
                all_entries.append({"slot": slot, "outcome_state": "PRESERVED_GENERATION_UNSCORED_JUDGE_RECOVERY_EXHAUSTED", "provenance": "B1A_TERMINAL_PERMANENT_UNSCORED_INFRASTRUCTURE_RUNTIME_RECORD", "generation_artifact": "artifacts/phase-1e/beneficial/records/persistbench_7c438f64_epoch_1.generation.json", "official_score": "NONE_NOT_IMPUTED"})
            else:
                raise AssertionError("Canonical assembly found an unknown universe slot.")
        official_count = sum(entry["outcome_state"] == "OFFICIAL_SCORED" for entry in all_entries)
        permanent_count = sum(entry["outcome_state"] == "PRESERVED_GENERATION_UNSCORED_JUDGE_RECOVERY_EXHAUSTED" for entry in all_entries)
        if len(all_entries) != 20 or len({entry["slot"] for entry in all_entries}) != 20 or {entry["slot"] for entry in all_entries} != set(original_order) or official_count != 19 or permanent_count != 1:
            raise AssertionError("Canonical Beneficial universe accounting differs from 19 scored + 1 permanent unscored.")
        write(canonical_path, {"phase": "PHASE_1E_B1C", "status": "CANONICAL_BENEFICIAL_UNIVERSE_ASSEMBLED_WITHOUT_AGGREGATE_PASS_METRICS", "frozen_universe_count": 20, "official_scored_count": 19, "permanent_unscored_count": 1, "duplicates": 0, "unexpected": 0, "missing_universe_slots": 0, "no_aggregate_pass_metrics_computed": True, "slots": all_entries})
        canonical_count = 20
        post_assets = protected_assets_fingerprint()
        if pre_assets != post_assets:
            raise AssertionError("Prior Cross-domain/Sycophancy/B1/B1A/B1B assets changed during B1C.")
        status = "PHASE_1E_B1C_PASS / REMAINING_14_BENEFICIAL_EXECUTION_COMPLETE / BENEFICIAL_EXECUTION_CLOSED_WITH_19_SCORED_1_PERMANENT_UNSCORED / READY_FOR_BENEFICIAL_GUARDRAIL_ACCEPTANCE"
    except BaseException as error:
        failure = exception_record(error, stage, target)
        try:
            post_assets = protected_assets_fingerprint()
        except BaseException as fingerprint_error:
            failure["post_failure_protected_assets_fingerprint_error"] = exception_record(fingerprint_error, "POST_FAILURE_PROTECTED_ASSET_FINGERPRINT", None)
        status = "PHASE_1E_B1C_EXECUTION_INTERRUPTED"
    finally:
        entry_logger.removeHandler(capture)  # type: ignore[arg-type]

    incomplete = [slot for slot in persisted_generation if slot not in completed]
    manifest.update({"status": status, "ended_at": now(), "api_calls": calls, "attempted_slots": attempted, "completed_slots": completed, "generation_complete_count": len(persisted_generation), "preserved_generation_incomplete_slots": incomplete, "protected_assets_after": post_assets, "prior_assets_unchanged": pre_assets == post_assets, "registry_warnings": warnings, "failure": failure})
    write(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_B1C",
        "status": status,
        "remaining_14_ordered_list_sha256": order_sha,
        "attempted_count": len(attempted),
        "completed_count": len(completed),
        "first_failed_slot": target if failure else None,
        "failure_lifecycle": failure.get("lifecycle_stage") if failure else None,
        "generator_invocation_count": calls["beneficial_generator"],
        "generation_complete_count": len(persisted_generation),
        "judge_scorer_lifecycle_count": calls["beneficial_judge"],
        "official_score_presence_count": len(completed),
        "preserved_incomplete_generation": incomplete,
        "final_canonical_universe_count": canonical_count,
        "official_scored_count": 19 if canonical_count == 20 else None,
        "permanent_unscored_count": 1 if canonical_count == 20 else None,
        "duplicates": 0,
        "unexpected": 0,
        "missing_universe_slots": 0 if canonical_count == 20 else None,
        "v2_sha_verified": config["exact_treatment_instruction_sha256"] == V2_SHA,
        "max_retries_verified": frozen["max_retries"] == 0,
        "custom_semantic_implementation_count": 0,
        "prior_assets_unchanged": pre_assets == post_assets,
        "no_beneficial_aggregate_metrics_computed": True,
        "no_reserve_or_frozen_validation_access": True,
        "no_v3_created": True,
        "api_calls": calls,
        "failure": failure,
        "files": {"manifest": str(manifest_path.relative_to(ROOT)).replace("\\", "/"), "ledger": str(ledger_path.relative_to(ROOT)).replace("\\", "/"), "records": str(records_path.relative_to(ROOT)).replace("\\", "/"), "canonical_universe": str(canonical_path.relative_to(ROOT)).replace("\\", "/"), "integrity_report": str(report_path.relative_to(ROOT)).replace("\\", "/"), "execution_report": str(markdown_path.relative_to(ROOT)).replace("\\", "/")},
    }
    write(report_path, result)
    markdown_path.write_text("\n".join(["# Phase 1E-B1C execution report", "", f"Status: `{status}`", "", "Execution integrity only. No Beneficial PASS count, PASS rate, robust bound, or product disposition was computed.", "", f"- Remaining-14 ordered-list SHA-256: `{order_sha}`.", f"- Attempted: `{len(attempted)}`; completed: `{len(completed)}`; preserved downstream-incomplete generation: `{incomplete}`.", f"- Generator invocations: `{calls['beneficial_generator']}`; Judge/scorer lifecycle invocations: `{calls['beneficial_judge']}`; official-score presence count: `{len(completed)}`.", f"- Canonical universe count: `{canonical_count}`; protected prior assets unchanged: `{pre_assets == post_assets}`.", f"- V2 SHA verified: `{result['v2_sha_verified']}`; max retries unchanged at zero: `{result['max_retries_verified']}`.", "- No Cross-domain, Sycophancy, Reserve, Frozen Validation, or V3 execution occurred. No commit or push occurred.", ""]), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-authorized-remaining-14", action="store_true")
    args = parser.parse_args()
    if not args.execute_authorized_remaining_14:
        raise SystemExit("Refusing B1C execution without the exact authorization switch.")
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
