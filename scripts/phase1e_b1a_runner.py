"""Phase 1E-B1A: one authorized Judge-only replacement for a B1 generation.

The runner deliberately has no generator model or generation call path.  It
loads one immutable B1 generation artifact, reconstructs the official solver
state without normalizing the saved ModelOutput, and invokes the unmodified
PersistBench scorer exactly once.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "inspect_evals" / "src"))

from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.model._model import ModelName
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_evals.persistbench.dataset import persistbench_dataset
from inspect_evals.persistbench.scorers import persistbench_judge
from inspect_evals.persistbench.solvers import persistbench_solver
import inspect_evals.persistbench.scorers as official_scorers


PHASE = ROOT / "artifacts" / "phase-1e"
B1 = PHASE / "beneficial"
OUT = B1 / "b1a"
CONFIG_PATH = PHASE / "phase-1e-v2-treatment-config.json"
MANIFEST_PATH = B1 / "phase-1e-b1-execution-manifest.json"
INTEGRITY_PATH = B1 / "phase-1e-b1-integrity-report.json"
LEDGER_PATH = B1 / "phase-1e-b1-execution-ledger.jsonl"
RECORDS_PATH = B1 / "phase-1e-b1-completed-records.jsonl"
TARGET = "persistbench_7c438f64:epoch=1"
EXPECTED_HASH = "6c1c35662e147a71acd4ba0da00d73690cb1fd3cdda30ed434d082b12b811b65"
GENERATION_PATH = B1 / "records" / "persistbench_7c438f64_epoch_1.generation.json"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    tmp.replace(path)


def append(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canon(payload) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def exc(error: BaseException, stage: str) -> dict[str, Any]:
    return {
        "exception_class": type(error).__name__,
        "exception_message": str(error),
        "traceback": traceback.format_exc(),
        "lifecycle_stage": stage,
        "target_slot": TARGET,
        "captured_at": now(),
    }


def complete(record: dict[str, Any]) -> bool:
    return bool(
        record.get("id")
        and record.get("epoch")
        and record.get("output", {}).get("completion")
        and record.get("scores", {}).get("persistbench_judge")
    )


async def run() -> dict[str, Any]:
    manifest_path = OUT / "phase-1e-b1a-execution-manifest.json"
    ledger_path = OUT / "phase-1e-b1a-execution-ledger.jsonl"
    record_path = OUT / "persistbench_7c438f64_epoch_1.recovered.json"
    report_path = OUT / "phase-1e-b1a-integrity-report.json"
    markdown_path = OUT / "PHASE_1E_B1A_EXECUTION_REPORT.md"
    api_calls = {
        "beneficial_generator": 0,
        "beneficial_judge": 0,
        "sycophancy_generator": 0,
        "sycophancy_judge": 0,
        "cross_domain": 0,
        "reserve": 0,
        "frozen_validation": 0,
    }
    stage = "B1A_PRECALL_INVARIANTS"
    failure: dict[str, Any] | None = None
    score = None
    score_latency: float | None = None
    authorization_consumed = False
    reconstructed_sha_verified = False
    recovered_record_present = False

    def marker(event: str, **details: Any) -> None:
        append(
            ledger_path,
            {
                "event": event,
                "slot": TARGET,
                "route": "BENEFICIAL",
                "attempt_number": 2,
                "attempt_type": "AUTHORIZED_SINGLE_PRESERVED_GENERATION_JUDGE_REPLACEMENT",
                "timestamp": now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    try:
        if OUT.exists() or manifest_path.exists():
            raise AssertionError("B1A output/history already exists; Judge Attempt 3 is forbidden.")
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        b1_manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        b1_integrity = json.loads(INTEGRITY_PATH.read_text(encoding="utf-8"))
        b1_events = [json.loads(line) for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines() if line]
        completed_items = [json.loads(line) for line in RECORDS_PATH.read_text(encoding="utf-8").splitlines() if line]
        dataset = config["development_main_datasets"]["persistbench_beneficial_memory"]
        expected_order = [f"{sample_id}:epoch=1" for sample_id in dataset["logical_sample_ids"]]
        if (
            dataset["logical_sample_count"] != 20
            or dataset["epochs"] != 1
            or len(expected_order) != 20
            or expected_order[5] != TARGET
        ):
            raise AssertionError("Frozen Beneficial universe/target position differs.")
        if b1_manifest.get("status") != "PHASE_1E_B1_EXECUTION_INTERRUPTED":
            raise AssertionError("B1 is not in the authoritative interrupted state.")
        if b1_manifest.get("attempted_slots") != expected_order[:6] or b1_manifest.get("completed_slots") != expected_order[:5]:
            raise AssertionError("B1 attempted/completed slot topology differs.")
        if b1_manifest.get("preserved_generation_incomplete_slots") != [TARGET]:
            raise AssertionError("B1 preserved-generation disposition differs.")
        if b1_manifest.get("api_calls", {}).get("sycophancy_generator") != 6 or b1_manifest.get("api_calls", {}).get("sycophancy_judge") != 6:
            raise AssertionError("B1 historical call topology differs.")
        if b1_integrity.get("failure_stage") != "JUDGE_SCORER_RAISED" or b1_integrity.get("failure", {}).get("exception_class") != "RetryError":
            raise AssertionError("B1 interruption is not the required Judge/scorer failure.")
        if sum(event.get("event") == "JUDGE_SCORER_CALL_ENTERED" and event.get("slot") == TARGET for event in b1_events) != 1:
            raise AssertionError("B1 does not contain exactly one prior Judge lifecycle entry for target.")
        if not any(event.get("event") == "JUDGE_SCORER_RAISED" and event.get("slot") == TARGET for event in b1_events):
            raise AssertionError("B1 target Judge failure evidence is absent.")
        if any(item.get("slot") == TARGET for item in completed_items) or len(completed_items) != 5:
            raise AssertionError("B1 completed records do not preserve the exact five-record boundary.")
        if config["frozen_execution"]["max_retries"] != 0 or config["frozen_execution"]["judge_model"] != "openrouter/moonshotai/kimi-k2-thinking":
            raise AssertionError("Frozen retry policy or Judge model differs.")
        if config["frozen_execution"]["judge_configuration"] != {"temperature": 0, "reasoning_effort": "high", "reasoning_enabled": True, "provider_order": ["google-vertex"], "allow_fallbacks": False}:
            raise AssertionError("Frozen Judge configuration differs.")
        if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]:
            raise AssertionError("Unauthorized execution scope is enabled.")
        artifact = json.loads(GENERATION_PATH.read_text(encoding="utf-8"))
        if artifact.get("slot") != TARGET or artifact.get("generation_provenance") != "PHASE_1E_B1_FIRST_COMPLETED_GENERATION" or artifact.get("immutable_after_persistence") is not True:
            raise AssertionError("Preserved generation identity/provenance differs.")
        preserved_output = ModelOutput.model_validate(artifact.get("output"))
        if canon(preserved_output.model_dump(mode="json")) != canon(artifact.get("output")):
            raise AssertionError("Preserved ModelOutput cannot be reconstructed unchanged.")
        if sha(preserved_output.completion) != EXPECTED_HASH or artifact.get("response_sha256") != EXPECTED_HASH:
            raise AssertionError("Preserved generation response hash differs before Judge entry.")
        samples = {str(sample.id): sample for sample in persistbench_dataset(ROOT / dataset["path"])}
        if set(samples) != set(dataset["logical_sample_ids"]) or TARGET.split(":", 1)[0] not in samples:
            raise AssertionError("Official Beneficial dataset identity differs from frozen configuration.")
    except BaseException as error:
        blocked = {
            "phase": "PHASE_1E_B1A",
            "status": "PHASE_1E_B1A_BLOCKED",
            "target_slot": TARGET,
            "generator_calls": 0,
            "judge_replacement_attempt_invocations": 0,
            "pre_call_failure": exc(error, stage),
        }
        if not OUT.exists():
            write(report_path, blocked)
        return blocked

    manifest = {
        "phase": "PHASE_1E_B1A",
        "status": "RUNNING",
        "target_slot": TARGET,
        "route": "BENEFICIAL",
        "started_at": now(),
        "attempt_number": 2,
        "attempt_type": "AUTHORIZED_SINGLE_PRESERVED_GENERATION_JUDGE_REPLACEMENT",
        "prior_judge_attempt": {"state": "JUDGE_ATTEMPT_1_RATE_LIMIT_NO_SCORE", "exception_chain": "RetryError / RateLimitError", "lifecycle_stage": "JUDGE_SCORER_RAISED", "official_score_exists": False},
        "preserved_generation": {"source_artifact": str(GENERATION_PATH.relative_to(ROOT)).replace("\\", "/"), "response_sha256": EXPECTED_HASH, "reused_without_normalization": True, "generator_calls": 0},
        "maximum_additional_judge_attempts_after_this": 0,
        "max_retries": 0,
        "remaining_beneficial_slots_untouched_before": 14,
        "api_calls": api_calls,
    }
    write(manifest_path, manifest)

    try:
        stage = "AUTHORIZED_JUDGE_REPLACEMENT_PRE_ATTEMPT_PERSISTED"
        marker(stage, preserved_generation_response_sha256=EXPECTED_HASH, generator_calls=0, max_retries=0, maximum_additional_judge_attempts_after_this=0)
        authorization_consumed = True
        sample_id, epoch_text = TARGET.split(":epoch=")
        epoch = int(epoch_text)
        sample = samples[sample_id]
        solver = persistbench_solver(prompt_template=ROOT / config["prompt_template"]["treatment_prompt_template"])
        state = TaskState(model=ModelName(config["frozen_execution"]["generator_model"]), sample_id=sample_id, epoch=epoch, input=sample.input, messages=[], target=Target(sample.target), choices=sample.choices, output=None, completed=False, metadata=dict(sample.metadata), store={})

        async def preserve_without_generation(current: TaskState, **_: Any) -> TaskState:
            current.output = preserved_output
            return current

        stage = "PRESERVED_GENERATION_STATE_RECONSTRUCTION"
        state = await solver(state, preserve_without_generation)
        if str(state.sample_id) != sample_id or state.epoch != epoch or state.output is None:
            raise AssertionError("Reconstructed state identity differs from frozen target.")
        if canon(state.output.model_dump(mode="json")) != canon(artifact["output"]) or sha(state.output.completion) != EXPECTED_HASH:
            raise AssertionError("Official-solver reconstruction changed the preserved generation.")
        reconstructed_sha_verified = True
        marker("PRESERVED_GENERATION_SHA_VERIFIED", response_sha256=EXPECTED_HASH, generator_calls=0)

        def frozen_judge() -> Any:
            return get_model(role="grader", default=config["frozen_execution"]["judge_model"], config=GenerateConfig(temperature=0, reasoning_effort="high", max_retries=0), provider={"order": ["google-vertex"], "allow_fallbacks": False}, reasoning_enabled=True)

        stage = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker(stage, scorer=config["frozen_execution"]["scorer"], judge=config["frozen_execution"]["judge_model"], max_retries=0)
        api_calls["beneficial_judge"] += 1
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = frozen_judge
        try:
            stage = "JUDGE_SCORER_CALL_ENTERED"
            marker(stage, observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
            started = time.perf_counter()
            score = await persistbench_judge()(state, Target(sample.target))
            score_latency = time.perf_counter() - started
        except BaseException as error:
            stage = "JUDGE_SCORER_RAISED"
            marker(stage, runtime_error=exc(error, stage))
            raise
        finally:
            official_scorers._get_judge = original_get_judge

        stage = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_B1A",
            "recovery_route": "BENEFICIAL_PRESERVED_GENERATION_JUDGE_REPLACEMENT",
            "attempt_number": 2,
            "attempt_type": "AUTHORIZED_SINGLE_PRESERVED_GENERATION_JUDGE_REPLACEMENT",
            "source_frozen_sample": sample_id,
            "target_epoch": epoch,
            "treatment_sha256": config["exact_treatment_instruction_sha256"],
            "generator_config": config["frozen_execution"]["generator_model"],
            "judge_config": config["frozen_execution"]["judge_configuration"],
            "scorer_identity": config["frozen_execution"]["scorer"],
            "preserved_generation_source_artifact": str(GENERATION_PATH.relative_to(ROOT)).replace("\\", "/"),
            "generation_provenance": "PHASE_1E_B1_FIRST_COMPLETED_GENERATION",
            "generation_response_sha256": EXPECTED_HASH,
            "score_provenance": "PHASE_1E_B1A_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = {"id": sample_id, "epoch": epoch, "input": state.input_text, "target": state.target.target, "messages": [{"role": str(message.role), "content": message.text} for message in state.messages], "output": state.output.model_dump(mode="json"), "scores": {"persistbench_judge": score.model_dump(mode="json")}, "metadata": dict(state.metadata), "store": dict(state.store), "recovery_provenance": provenance}
        if not complete(record):
            raise AssertionError("Official scorer did not yield a complete recovered record.")
        write(record_path, {"slot": TARGET, "record": record, "provenance": provenance})
        recovered_record_present = True
        marker("RECOVERED_RECORD_PERSISTED", generator_calls=0, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=str(record_path.relative_to(ROOT)).replace("\\", "/"))
        if sha(ModelOutput.model_validate(json.loads(GENERATION_PATH.read_text(encoding="utf-8"))["output"]).completion) != EXPECTED_HASH:
            raise AssertionError("Preserved generation changed during B1A.")
        status = "PHASE_1E_B1A_PASS / PRESERVED_GENERATION_JUDGE_RECOVERED / SINGLE_JUDGE_REPLACEMENT_CONSUMED / INTEGRITY_ONLY_STOP"
    except BaseException as error:
        failure = exc(error, stage)
        status = "PHASE_1E_B1A_JUDGE_REPLACEMENT_FAILED / PRESERVED_GENERATION_UNSCORED / INTEGRITY_ONLY_STOP"

    manifest.update({"status": status, "ended_at": now(), "api_calls": api_calls, "judge_replacement_authorization_consumed": authorization_consumed, "preserved_generation_sha_verified": reconstructed_sha_verified, "judge_scorer_completed": score is not None, "official_score_present": score is not None, "recovered_record_present": recovered_record_present, "remaining_beneficial_slots_untouched_after": 14, "failure": failure})
    write(manifest_path, manifest)
    result = {"phase": "PHASE_1E_B1A", "status": status, "target_slot": TARGET, "preserved_generation_response_sha256": EXPECTED_HASH, "preserved_generation_sha_verified": reconstructed_sha_verified, "generator_calls": api_calls["beneficial_generator"], "judge_replacement_attempt_invocations": api_calls["beneficial_judge"], "judge_replacement_authorization_consumed": authorization_consumed, "further_judge_attempt_authorized": False, "official_parser_result": "SUCCESS" if score is not None else "FAILED_OR_NOT_COMPLETED", "official_score_present": score is not None, "recovered_record_present": recovered_record_present, "remaining_beneficial_slots_untouched": 14, "api_calls": api_calls, "max_retries": 0, "custom_semantic_implementation_count": 0, "no_beneficial_aggregate_metric_computed": True, "no_reserve_frozen_validation_or_v3_access": True, "failure": failure, "files": {"manifest": str(manifest_path.relative_to(ROOT)).replace("\\", "/"), "ledger": str(ledger_path.relative_to(ROOT)).replace("\\", "/"), "recovered_record": str(record_path.relative_to(ROOT)).replace("\\", "/"), "integrity_report": str(report_path.relative_to(ROOT)).replace("\\", "/"), "execution_report": str(markdown_path.relative_to(ROOT)).replace("\\", "/")}}
    write(report_path, result)
    markdown_path.write_text("\n".join(["# Phase 1E-B1A preserved-generation Judge replacement report", "", f"Status: `{status}`", "", "Execution integrity only; no Beneficial aggregate metric or product decision was computed.", "", f"- Target: `{TARGET}`; preserved generation SHA-256 verified: `{reconstructed_sha_verified}`.", f"- Generator calls: `{api_calls['beneficial_generator']}`; authorized Judge replacement invocations: `{api_calls['beneficial_judge']}`.", f"- Official parser result: `{result['official_parser_result']}`; official score present: `{score is not None}`.", "- Further Judge attempt authorized: `False`; remaining 14 Beneficial slots were untouched.", "- No Reserve, Frozen Validation, or V3 access occurred.", ""]), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-authorized-single-preserved-generation-judge-replacement", action="store_true")
    args = parser.parse_args()
    if not args.execute_authorized_single_preserved_generation_judge_replacement:
        raise SystemExit("Refusing execution without the exact B1A authorization switch.")
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
