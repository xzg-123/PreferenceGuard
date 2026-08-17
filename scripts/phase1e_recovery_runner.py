"""Dedicated Phase 1E exact-slot recovery orchestration.

This module changes slot/stage selection only. It imports the frozen official
PersistBench dataset, solver and scorer; it does not duplicate benchmark or
scorer semantics. Real execution is available only behind the explicit
``--execute-authorized`` command-line switch.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSPECT_EVALS_SRC = PROJECT_ROOT / "inspect_evals" / "src"
if str(INSPECT_EVALS_SRC) not in sys.path:
    sys.path.insert(0, str(INSPECT_EVALS_SRC))

from inspect_ai._eval.task.generate import task_generate
from inspect_ai._util.notgiven import NOT_GIVEN
from inspect_ai.model import GenerateConfig, ModelOutput, get_model
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_ai.model._model import ModelName

from inspect_evals.persistbench.dataset import persistbench_dataset
from inspect_evals.persistbench.scorers import persistbench_judge
from inspect_evals.persistbench.solvers import persistbench_solver
import inspect_evals.persistbench.scorers as official_scorers


ROOT = PROJECT_ROOT
PHASE_ROOT = ROOT / "artifacts" / "phase-1e"
RECOVERY_ROOT = PHASE_ROOT / "recovery"
PROTOCOL_PATH = RECOVERY_ROOT / "phase-1e-r-recovery-protocol.json"
CONFIG_PATH = PHASE_ROOT / "phase-1e-v2-treatment-config.json"
R4B_AMENDMENT_PATH = RECOVERY_ROOT / "r4b" / "phase-1e-r4b-replacement-amendment.json"
R4D_DISPOSITION_PATH = RECOVERY_ROOT / "r4d" / "phase-1e-r4d-terminal-disposition.json"
SOURCE_LOG = next((PHASE_ROOT / "logs").glob("*.eval"))


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def slot_key(sample_id: str, epoch: int) -> str:
    return f"{sample_id}:epoch={epoch}"


def split_slot(key: str) -> tuple[str, int]:
    prefix = ":epoch="
    if not key.startswith("persistbench_") or key.count(prefix) != 1:
        raise ValueError(f"Invalid composite slot key: {key}")
    sample_id, epoch = key.split(prefix)
    parsed_epoch = int(epoch)
    if parsed_epoch < 1:
        raise ValueError(f"Invalid epoch in slot key: {key}")
    return sample_id, parsed_epoch


def read_eval_sample(sample_id: str, epoch: int) -> dict[str, Any]:
    """Read an existing `.eval` entry without extracting or modifying it."""
    entry = f"samples/{sample_id}_epoch_{epoch}.json"
    result = subprocess.run(
        ["tar", "-xOf", str(SOURCE_LOG), entry],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return json.loads(result.stdout)


def source_log_slot_order() -> list[str]:
    """Return the native archive order used by the Phase 1E-R fingerprint audit."""
    entries = subprocess.run(
        ["tar", "-tf", str(SOURCE_LOG)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()
    slots: list[str] = []
    for entry in entries:
        if not entry.startswith("samples/") or not entry.endswith(".json"):
            continue
        stem = entry.removeprefix("samples/").removesuffix(".json")
        sample_id, epoch = stem.rsplit("_epoch_", 1)
        slots.append(slot_key(sample_id, int(epoch)))
    return slots


@dataclass(frozen=True)
class FrozenRecoveryPlan:
    protocol: dict[str, Any]
    config: dict[str, Any]
    route_a: frozenset[str]
    route_b: str
    route_c: frozenset[str]
    route_c_order: tuple[str, ...]

    @classmethod
    def load(cls) -> "FrozenRecoveryPlan":
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        routes = protocol["routes"]
        route_a = frozenset(routes["A_immutable_valid"]["slots"])
        route_b = routes["B_preserved_generation_judge_score_only"]["slot"]
        route_c_order = tuple(routes["C_exact_missing_slot_execution_completion"]["slots"])
        route_c = frozenset(route_c_order)
        plan = cls(protocol, config, route_a, route_b, route_c, route_c_order)
        plan.validate_partition()
        return plan

    @property
    def route_b_response_hash(self) -> str:
        return self.protocol["routes"]["B_preserved_generation_judge_score_only"][
            "original_response_sha256"
        ]

    @property
    def route_a_fingerprint_hash(self) -> str:
        return self.protocol["routes"]["A_immutable_valid"]["fingerprint_set_sha256"]

    @property
    def prompt_template(self) -> Path:
        relative = self.config["prompt_template"]["treatment_prompt_template"]
        return ROOT / relative

    def validate_partition(self) -> None:
        ids = self.config["development_main_datasets"]["persistbench_cross_domain"][
            "logical_sample_ids"
        ]
        expected = {slot_key(sample_id, epoch) for sample_id in ids for epoch in range(1, 4)}
        if len(self.route_a) != 34 or len(self.route_c) != 25:
            raise AssertionError("Frozen Route A/C counts do not match 34/25.")
        if len(self.route_c_order) != len(self.route_c):
            raise AssertionError("Route C protocol contains a duplicate composite slot.")
        if self.route_b in self.route_a or self.route_b in self.route_c:
            raise AssertionError("Route B intersects another route.")
        if self.route_a & self.route_c:
            raise AssertionError("Route A and C intersect.")
        if self.route_a | {self.route_b} | self.route_c != expected:
            raise AssertionError("Recovery routes do not equal the frozen 60-slot universe.")

    def require_route_b(self, key: str) -> None:
        if key != self.route_b:
            raise PermissionError(f"Route B accepts only {self.route_b}; received {key}.")

    def require_route_c(self, key: str) -> None:
        if key not in self.route_c:
            raise PermissionError(f"Route C rejects non-allowlisted slot {key}.")

    def reject_route_a(self, key: str) -> None:
        if key in self.route_a:
            raise PermissionError(f"Route A immutable slot rejected before any execution: {key}.")


class OfficialRecoveryOrchestrator:
    """Orchestration wrapper around unmodified official PersistBench components."""

    def __init__(self, plan: FrozenRecoveryPlan) -> None:
        self.plan = plan
        dataset_path = ROOT / plan.config["development_main_datasets"]["persistbench_cross_domain"]["path"]
        self.samples = {str(sample.id): sample for sample in persistbench_dataset(dataset_path)}
        expected_ids = set(plan.config["development_main_datasets"]["persistbench_cross_domain"]["logical_sample_ids"])
        if set(self.samples) != expected_ids:
            raise AssertionError("Official PersistBench dataset identity differs from frozen configuration.")
        self.solver = persistbench_solver(prompt_template=plan.prompt_template)

    def sample_for(self, key: str) -> tuple[Any, int]:
        sample_id, epoch = split_slot(key)
        try:
            return self.samples[sample_id], epoch
        except KeyError as exc:
            raise KeyError(f"Frozen sample not found in official dataset: {sample_id}") from exc

    def new_state(self, key: str, output: ModelOutput | None = None) -> TaskState:
        sample, epoch = self.sample_for(key)
        return TaskState(
            model=ModelName(self.plan.protocol["frozen_treatment"]["generator"]),
            sample_id=str(sample.id),
            epoch=epoch,
            input=sample.input,
            messages=[],
            target=Target(sample.target),
            choices=sample.choices,
            output=output,
            completed=False,
            metadata=dict(sample.metadata),
            store={},
        )

    async def prepare_with_official_solver(
        self,
        key: str,
        generate: Callable[..., Awaitable[TaskState]],
        output: ModelOutput | None = None,
    ) -> TaskState:
        state = self.new_state(key, output=output)
        return await self.solver(state, generate)

    async def route_b_official_score(self, key: str) -> tuple[TaskState, Any, dict[str, Any]]:
        """Future API-authorized Route-B scorer entry; never called by dry-run."""
        self.plan.reject_route_a(key)
        self.plan.require_route_b(key)
        original = read_eval_sample(*split_slot(key))
        response = original["output"]["completion"]
        if sha256_text(response) != self.plan.route_b_response_hash:
            raise AssertionError("Route-B response hash mismatch before official scorer entry.")

        async def preserve_without_generation(state: TaskState, **_: Any) -> TaskState:
            state.output = ModelOutput.from_content(
                model=original["output"]["model"], content=response
            )
            return state

        state = await self.prepare_with_official_solver(key, preserve_without_generation)
        # This is the official scorer object; no scorer logic is copied here.
        return state, await persistbench_judge()(state, Target(original["target"])), original

    async def route_c_official_generate(
        self,
        key: str,
        model: Any,
        generate_config: GenerateConfig,
    ) -> TaskState:
        """Future API-authorized exact-slot generator entry using Inspect task_generate."""
        self.plan.reject_route_a(key)
        self.plan.require_route_c(key)

        async def official_generate(
            state: TaskState, tool_calls: str = "loop", **kwargs: Any
        ) -> TaskState:
            return await task_generate(
                model=model,
                state=state,
                tool_calls=tool_calls,  # type: ignore[arg-type]
                cache=kwargs.get("cache", NOT_GIVEN),
                config=generate_config.merge(kwargs),
            )

        return await self.prepare_with_official_solver(key, official_generate)

    def verify_route_a_fingerprints(self) -> str:
        """Recompute the audit's canonical 34-record fingerprint set, read-only."""
        rows: list[str] = []
        ordered_route_a = [key for key in source_log_slot_order() if key in self.plan.route_a]
        if set(ordered_route_a) != self.plan.route_a or len(ordered_route_a) != 34:
            raise AssertionError("Route-A source-log membership/order cannot be established.")
        for key in ordered_route_a:
            sample_id, epoch = split_slot(key)
            record = read_eval_sample(sample_id, epoch)
            response = record["output"]["completion"]
            if not response or record.get("error") or "persistbench_judge" not in record.get("scores", {}):
                raise AssertionError(f"Route-A record is not valid: {key}")
            response_hash = sha256_text(response)
            record_hash = sha256_text(canonical_json(record))
            rows.append(f"{key}|{response_hash}|{record_hash}")
        computed = sha256_text("\n".join(rows))
        if computed != self.plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint mismatch; stop before any recovery execution.")
        return computed

    def frozen_judge_model_no_retry(self) -> Any:
        """Return the official frozen judge configuration with only retry count pinned to zero."""
        return get_model(
            role="grader",
            default=self.plan.protocol["frozen_treatment"]["judge"],
            config=GenerateConfig(temperature=0, reasoning_effort="high", max_retries=0),
            provider={"order": ["google-vertex"], "allow_fallbacks": False},
            reasoning_enabled=True,
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def atomic_jsonl_write(path: Path, rows: list[dict[str, Any]]) -> None:
    """Durably write a complete additive JSONL evidence set before assembly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def safe_text(value: Any) -> str:
    """Convert diagnostic-only values without allowing their repr to mask an error."""
    try:
        return str(value)
    except BaseException:
        try:
            return repr(value)
        except BaseException:
            return f"<unserializable {type(value).__name__}>"


def safe_path_reference(value: Any) -> str:
    """Render a path for reports without assuming it is beneath the project root."""
    try:
        candidate = Path(value).resolve(strict=False)
        try:
            return str(candidate.relative_to(ROOT.resolve())).replace("\\", "/")
        except ValueError:
            return str(candidate)
    except BaseException:
        return safe_text(value)


def safe_exception_record(
    exc: BaseException,
    *,
    target_slot: str | None,
    route: str | None,
    lifecycle_stage: str,
) -> dict[str, str | None]:
    """Capture a primary failure using only JSON-safe primitives."""
    try:
        rendered_traceback = traceback.format_exc()
    except BaseException:
        rendered_traceback = "<traceback unavailable>"
    return {
        "exception_class": safe_text(type(exc).__name__),
        "exception_message": safe_text(exc),
        "traceback": safe_text(rendered_traceback),
        "target_slot": safe_text(target_slot) if target_slot is not None else None,
        "route": safe_text(route) if route is not None else None,
        "lifecycle_stage": safe_text(lifecycle_stage),
        "captured_at": utc_now(),
    }


def write_json_failure_safe(path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Write diagnostics without re-raising if the first serialization path fails."""
    try:
        atomic_json_write(path, payload)
        return {"primary_report_written": True, "fallback_used": False}
    except BaseException as write_exc:
        fallback = {
            "report_write_status": "PRIMARY_SERIALIZATION_FAILED",
            "report_write_exception": safe_exception_record(
                write_exc,
                target_slot=None,
                route=None,
                lifecycle_stage="REPORT_SERIALIZATION_FALLBACK",
            ),
            "primary_failure": safe_text(payload.get("failure")),
        }
        try:
            atomic_json_write(path.with_suffix(path.suffix + ".fallback.json"), fallback)
            return {"primary_report_written": False, "fallback_used": True}
        except BaseException:
            # An unavailable filesystem cannot be repaired here; importantly,
            # the primary exception has still not been re-raised or replaced.
            return {"primary_report_written": False, "fallback_used": False}


def run_failure_injection_validation() -> dict[str, Any]:
    """Exercise diagnostic capture only, with injected local failures and no models."""
    scenarios = [
        ("A_EXCEPTION_BEFORE_SCORER_BOUNDARY", "ROUTE_B_RESPONSE_HASH_VERIFIED", RuntimeError("synthetic pre-scorer failure")),
        ("B_EXCEPTION_IMMEDIATELY_AFTER_SCORER_ENTRY", "SCORER_CALL_ENTERED", RuntimeError("synthetic post-entry failure")),
        ("C_SYNTHETIC_PROVIDER_LIKE_FAILURE", "SCORER_CALL_ENTERED", ConnectionError("synthetic provider-like failure")),
        ("D_RECORD_PERSISTENCE_FAILURE", "RECOVERED_RECORD_ABOUT_TO_PERSIST", OSError("synthetic persistence failure")),
        ("E_OUTSIDE_ROOT_PATH_EXCEPTION", "SCORER_CALL_ENTERED", RuntimeError("synthetic external path C:/outside-root/trace.log")),
    ]
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="phase1e-r4b-") as directory:
        temp_root = Path(directory)
        for name, stage, injected in scenarios:
            try:
                raise injected
            except BaseException as exc:
                primary = safe_exception_record(
                    exc,
                    target_slot="persistbench_70cb0bf1:epoch=2",
                    route="B",
                    lifecycle_stage=stage,
                )
            report_path = temp_root / f"{name}.json"
            report_write = write_json_failure_safe(
                report_path,
                {
                    "failure": primary,
                    "files": {"outside_root_probe": safe_path_reference("C:/outside-root/trace.log")},
                    "route_c_continuation": False,
                    "automatic_retry": False,
                },
            )
            saved = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
            saved_failure = saved.get("failure", {})
            results.append(
                {
                    "scenario": name,
                    "expected_lifecycle_stage": stage,
                    "recorded_lifecycle_stage": saved_failure.get("lifecycle_stage"),
                    "primary_exception_class_preserved": saved_failure.get("exception_class") == type(injected).__name__,
                    "primary_exception_message_preserved": safe_text(injected) in safe_text(saved_failure.get("exception_message")),
                    "traceback_preserved": bool(saved_failure.get("traceback")),
                    "secondary_reporting_exception_count": 0 if report_write["primary_report_written"] else 1,
                    "runner_stops": saved.get("route_c_continuation") is False,
                    "no_automatic_retry": saved.get("automatic_retry") is False,
                    "network_provider_calls": 0,
                    "outside_root_path_safe": name != "E_OUTSIDE_ROOT_PATH_EXCEPTION" or "outside-root" in safe_text(saved.get("files", {}).get("outside_root_probe")),
                }
            )
    passed = all(
        row["recorded_lifecycle_stage"] == row["expected_lifecycle_stage"]
        and row["primary_exception_class_preserved"]
        and row["primary_exception_message_preserved"]
        and row["traceback_preserved"]
        and row["secondary_reporting_exception_count"] == 0
        and row["runner_stops"]
        and row["no_automatic_retry"]
        and row["network_provider_calls"] == 0
        and row["outside_root_path_safe"]
        for row in results
    )
    return {
        "phase": "PHASE_1E_R4B",
        "mode": "OFFLINE_FAILURE_INJECTION_NO_NETWORK",
        "network_provider_calls": 0,
        "scenarios": results,
        "all_passed": passed,
    }


def serialise_score(score: Any) -> dict[str, Any]:
    return score.model_dump(mode="json") if hasattr(score, "model_dump") else dict(score)


def serialise_output(output: ModelOutput) -> dict[str, Any]:
    return output.model_dump(mode="json")


class FakeGeneratorModel:
    """Non-network double used only to drive official Inspect task_generate in dry-run."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    async def generate(self, input: Any, tools: Any, tool_choice: Any, config: Any, cache: Any) -> ModelOutput:
        self.calls.append({"messages": canonical_messages(input), "config": config.model_dump(mode="json")})
        return ModelOutput.from_content(model="dry-run/non-network", content="DRY_RUN_GENERATION")


class FakeJudgeModel:
    """Non-network double; official PersistBench scorer still parses its result."""

    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    async def generate(self, messages: Any) -> Any:
        self.calls.append({"messages": canonical_messages(messages)})
        return type("FakeJudgeResult", (), {"completion": '{"score": 1, "reasoning": "dry-run non-network judge"}'})()


def canonical_messages(messages: Any) -> list[dict[str, str]]:
    return [{"role": str(message.role), "content": message.text} for message in messages]


def original_premodel_messages(record: dict[str, Any]) -> list[dict[str, str]]:
    """The log may retain the generated assistant message; compare only solver input."""
    return [
        {"role": message["role"], "content": message["content"]}
        for message in record["messages"]
        if message["role"] != "assistant"
    ]


async def run_dry_run(plan: FrozenRecoveryPlan) -> dict[str, Any]:
    runner = OfficialRecoveryOrchestrator(plan)
    route_a_fingerprint = runner.verify_route_a_fingerprints()
    generator_calls: list[dict[str, Any]] = []
    judge_calls: list[dict[str, Any]] = []
    dry_generator = FakeGeneratorModel(generator_calls)
    config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)

    # Route A must fail before any generator, Judge, or scorer boundary.
    route_a_key = sorted(plan.route_a)[0]
    before_a = (len(generator_calls), len(judge_calls))
    try:
        plan.reject_route_a(route_a_key)
        raise AssertionError("Route-A guard did not reject its target.")
    except PermissionError:
        pass
    route_a_rejected_before_boundaries = before_a == (len(generator_calls), len(judge_calls))

    # Route B uses the official solver to reconstruct the prompt, but its
    # injected boundary only supplies the already-persisted output: no model call.
    route_b_record = read_eval_sample(*split_slot(plan.route_b))
    preserved = route_b_record["output"]["completion"]
    preserved_hash = sha256_text(preserved)
    if preserved_hash != plan.route_b_response_hash:
        raise AssertionError("Route-B response hash mismatch before dry-run scorer entry.")
    captured_b_premodel_messages: list[list[dict[str, str]]] = []

    async def route_b_preserve(state: TaskState, **_: Any) -> TaskState:
        captured_b_premodel_messages.append(canonical_messages(state.messages))
        state.output = ModelOutput.from_content(model=route_b_record["output"]["model"], content=preserved)
        return state

    route_b_state = await runner.prepare_with_official_solver(plan.route_b, route_b_preserve)
    original_b_messages = original_premodel_messages(route_b_record)
    route_b_premodel_equivalent = captured_b_premodel_messages == [original_b_messages]
    if not route_b_premodel_equivalent:
        raise AssertionError("Route-B pre-model state differs from original frozen evidence.")

    original_get_judge = official_scorers._get_judge
    try:
        official_scorers._get_judge = lambda: FakeJudgeModel(judge_calls)
        # Official scorer is invoked unchanged against a non-network Judge double.
        await persistbench_judge()(route_b_state, Target(route_b_record["target"]))
    finally:
        official_scorers._get_judge = original_get_judge

    # Route C: each target is selected by its exact composite key and fed to
    # the official PersistBench solver plus Inspect task_generate.
    planned_route_c: list[str] = []
    for key in sorted(plan.route_c):
        plan.reject_route_a(key)
        plan.require_route_c(key)
        sample_id, epoch = split_slot(key)

        async def route_c_generate(state: TaskState, tool_calls: str = "loop", **kwargs: Any) -> TaskState:
            if slot_key(str(state.sample_id), state.epoch) != key:
                raise AssertionError("Route-C state escaped the exact composite allowlist.")
            planned_route_c.append(slot_key(str(state.sample_id), state.epoch))
            return await task_generate(
                model=dry_generator,
                state=state,
                tool_calls=tool_calls,  # type: ignore[arg-type]
                cache=kwargs.get("cache", NOT_GIVEN),
                config=config.merge(kwargs),
            )

        state = await runner.prepare_with_official_solver(key, route_c_generate)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("Route-C state identity/epoch mismatch.")

    # Unknown/non-allowlisted target must fail before a boundary.
    unknown = "persistbench_a173ee7a:epoch=99"
    before_unknown = (len(generator_calls), len(judge_calls))
    try:
        plan.require_route_c(unknown)
        raise AssertionError("Unknown slot was accepted.")
    except PermissionError:
        pass
    unknown_rejected_before_boundaries = before_unknown == (len(generator_calls), len(judge_calls))

    # Selected successful records verify deterministic pre-model equivalence.
    control_slots = [
        "persistbench_a173ee7a:epoch=1",
        "persistbench_70cb0bf1:epoch=1",
        "persistbench_f78883e3:epoch=2",
    ]
    control_results: list[dict[str, Any]] = []
    for key in control_slots:
        original = read_eval_sample(*split_slot(key))
        captured: list[list[dict[str, str]]] = []

        async def capture_output(state: TaskState, **_: Any) -> TaskState:
            captured.append(canonical_messages(state.messages))
            state.output = ModelOutput.from_content(model=original["output"]["model"], content=original["output"]["completion"])
            return state

        state = await runner.prepare_with_official_solver(key, capture_output)
        original_messages = original_premodel_messages(original)
        control_results.append(
            {
                "slot": key,
                "sample_id_match": str(state.sample_id) == original["id"],
                "epoch_match": state.epoch == original["epoch"],
                "metadata_match": state.metadata == original["metadata"],
                "query_match": state.input_text == original["input"],
                "messages_match": captured == [original_messages],
                "generator_model_match": str(state.model) == plan.protocol["frozen_treatment"]["generator"],
                "scorer_identity": "persistbench_judge",
                "judge_identity": plan.protocol["frozen_treatment"]["judge"],
            }
        )
    if not all(all(value for field, value in row.items() if field != "slot" and field not in {"scorer_identity", "judge_identity"}) for row in control_results):
        raise AssertionError("A successful-record pre-model equivalence control failed.")

    return {
        "phase": "PHASE_1E_R3",
        "status": "PHASE_1E_R3_PASS / DEDICATED_RECOVERY_RUNNER_VERIFIED / READY_FOR_API_RECOVERY_AUTHORIZATION_REVIEW",
        "mode": "DRY_RUN_NON_NETWORK",
        "network_provider_calls": 0,
        "route_a": {
            "rejected_slot": route_a_key,
            "rejected_before_generator_judge_scorer": route_a_rejected_before_boundaries,
            "fingerprint_set_sha256": route_a_fingerprint,
        },
        "route_b": {
            "slot": plan.route_b,
            "generator_provider_calls": 0,
            "planned_judge_calls": 1,
            "non_network_judge_double_calls": len(judge_calls),
            "preserved_response_sha256": preserved_hash,
            "preserved_response_hash_verified": True,
            "official_scorer_invoked_with_non_network_double": True,
            "premodel_state_equivalent_to_original": route_b_premodel_equivalent,
        },
        "route_c": {
            "allowlisted_slots": len(plan.route_c),
            "planned_generator_targets": planned_route_c,
            "all_targets_exact_and_no_additional_epoch": set(planned_route_c) == set(plan.route_c) and len(planned_route_c) == len(plan.route_c),
            "non_network_generator_double_calls": len(generator_calls),
        },
        "unknown_slot": {"slot": unknown, "rejected_before_boundaries": unknown_rejected_before_boundaries},
        "semantic_equivalence_controls": control_results,
        "component_map": {
            "sample_loading": "OFFICIAL_REUSED_UNMODIFIED: persistbench_dataset",
            "prompt_solver": "OFFICIAL_REUSED_UNMODIFIED: persistbench_solver",
            "v2_instruction_injection": "OFFICIAL_REUSED_UNMODIFIED: frozen prompt_template passed to persistbench_solver",
            "generator_boundary": "OFFICIAL_REUSED_UNMODIFIED: inspect_ai._eval.task.generate.task_generate",
            "judge_scorer": "OFFICIAL_REUSED_UNMODIFIED: persistbench_judge",
            "score_interpretation": "OFFICIAL_REUSED_UNMODIFIED: persistbench_judge Score return",
            "slot_route_guard": "ORCHESTRATION_WRAPPER_ONLY",
            "artifact_assembly": "ORCHESTRATION_WRAPPER_ONLY",
            "custom_semantic_implementation_count": 0,
        },
    }


def recovered_record(
    state: TaskState,
    score: Any,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """Serialize official state/output/Score without interpreting the score."""
    return {
        "id": str(state.sample_id),
        "epoch": state.epoch,
        "input": state.input_text,
        "target": state.target.target,
        "messages": canonical_messages(state.messages),
        "output": serialise_output(state.output),
        "scores": {"persistbench_judge": serialise_score(score)},
        "metadata": dict(state.metadata),
        "store": dict(state.store),
        "recovery_provenance": provenance,
    }


def complete_record(record: dict[str, Any]) -> bool:
    return bool(
        record.get("output", {}).get("completion")
        and record.get("scores", {}).get("persistbench_judge")
        and record.get("id")
        and record.get("epoch")
    )


def write_execution_markdown(path: Path, result: dict[str, Any]) -> None:
    """Write a human-readable execution-integrity handoff without metrics."""
    lines = [
        "# Phase 1E-R4 execution integrity report",
        "",
        f"Status: `{result['status']}`",
        "",
        "This report records recovery execution integrity only; it contains no product metrics, score summaries, or pass rates.",
        "",
        "## Execution controls",
        "",
        f"- Route B generator provider calls: `{result['route_b_generator_provider_calls']}`",
        f"- Route B Judge/scorer completed: `{result['route_b_judge_scorer_complete']}`",
        f"- Route C completed slots: `{len(result['route_c_targets_completed'])}`",
        f"- First failed target: `{result['first_failed_target']}`",
        f"- Canonical assembly: `{result['canonical_assembly_status']}`",
        f"- Route A fingerprint before: `{result['route_a_fingerprint_before']}`",
        f"- Route A fingerprint after: `{result['route_a_fingerprint_after']}`",
        "",
        "## Required exclusions",
        "",
        "- No Route A execution or modification occurred.",
        "- No Reserve, frozen Validation, Sycophancy, or Beneficial run was accessed or executed.",
        "- No application-level retry was performed (`max_retries=0`).",
        "- No product metrics, pass rates, or score aggregation was computed.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


async def execute_authorized_recovery(output_dir: Path) -> dict[str, Any]:
    """Run the explicitly authorized Route B then frozen-order Route C recovery.

    There is deliberately no retry loop. Any exception writes evidence and
    returns an interrupted integrity state before another slot is considered.
    """
    output_dir = output_dir.resolve(strict=False)
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    ledger_path = output_dir / "phase-1e-r4-execution-ledger.jsonl"
    recovered_path = output_dir / "phase-1e-r4-recovered-records.jsonl"
    manifest_path = output_dir / "phase-1e-r4-execution-manifest.json"
    report_path = output_dir / "phase-1e-r4-integrity-report.json"
    markdown_report_path = output_dir / "PHASE_1E_R4_EXECUTION_REPORT.md"
    slots_dir = output_dir / "slots"
    route_c_order_hash = sha256_text(canonical_json(list(plan.route_c_order)))
    completed: list[str] = []
    failed_target: str | None = None
    failure: dict[str, Any] | None = None
    api_calls = {"route_b_generator": 0, "route_b_judge": 0, "route_c_generator": 0, "route_c_judge": 0}
    pre_fingerprint: str | None = None
    route_b_response_hash_verified = False
    lifecycle: dict[str, Any] = {"target_slot": None, "route": None, "stage": "PRE_EXECUTION"}

    def lifecycle_marker(event: str, *, target_slot: str, route: str, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": target_slot,
                "route": route,
                "timestamp": utc_now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    try:
        pre_fingerprint = runner.verify_route_a_fingerprints()
    except Exception as exc:
        result = {
            "phase": "PHASE_1E_R4",
            "status": "PHASE_1E_R4_BLOCKED / IMMUTABLE_FINGERPRINT_MISMATCH",
            "route_a_fingerprint_before": None,
            "api_calls": api_calls,
            "error": safe_exception_record(exc, target_slot=None, route=None, lifecycle_stage="ROUTE_A_PREFLIGHT"),
        }
        write_json_failure_safe(report_path, result)
        return result

    manifest = {
        "phase": "PHASE_1E_R4",
        "status": "RUNNING",
        "started_at": utc_now(),
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_order": [plan.route_b],
        "route_c_order": list(plan.route_c_order),
        "route_c_order_sha256": route_c_order_hash,
        "max_retries": plan.protocol["frozen_retry_semantics"]["max_retries"],
        "api_calls": api_calls,
        "completed_slots": completed,
        "source_eval": safe_path_reference(SOURCE_LOG),
    }
    write_json_failure_safe(manifest_path, manifest)

    async def persist_complete(
        key: str,
        route: str,
        state: TaskState,
        score: Any,
        provenance: dict[str, Any],
        started_at: str,
        stage_times: dict[str, float],
        generation_attempted: bool,
        generation_completed: bool,
    ) -> None:
        if route == "B":
            lifecycle["stage"] = "RECOVERED_RECORD_ABOUT_TO_PERSIST"
            lifecycle_marker(
                "RECOVERED_RECORD_ABOUT_TO_PERSIST",
                target_slot=key,
                route=route,
                generation_attempted=generation_attempted,
                scorer_completed=True,
            )
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError(f"Incomplete recovered official record: {key}")
        safe_name = key.replace(":", "_").replace("=", "_")
        artifact_path = slots_dir / f"{safe_name}.recovered.json"
        item = {"slot": key, "record": record, "provenance": provenance}
        atomic_json_write(artifact_path, item)
        append_jsonl(recovered_path, item)
        append_jsonl(
            ledger_path,
            {
                "event": "RECOVERED_RECORD_PERSISTED",
                "slot": key,
                "route": route,
                "start_timestamp": started_at,
                "end_timestamp": utc_now(),
                "generation_attempted": generation_attempted,
                "generation_completed": generation_completed,
                "generation_response_sha256": sha256_text(state.output.completion),
                "judge_attempted": True,
                "judge_completed": True,
                "scorer_completed": True,
                "final_record_persisted": True,
                "raw_stage_latency_seconds": stage_times,
                "provider_attempt_metadata": {"orchestrator_provider_calls": dict(api_calls)},
                "source_recovered_artifact": safe_path_reference(artifact_path),
                "status": "COMPLETE",
            },
        )

    async def run_route_b() -> None:
        nonlocal route_b_response_hash_verified
        key = plan.route_b
        started_at = utc_now()
        lifecycle.update({"target_slot": key, "route": "B", "stage": "ROUTE_B_SLOT_STARTED"})
        append_jsonl(ledger_path, {"event": "SLOT_STARTED", "slot": key, "route": "B", "timestamp": started_at, "generation_attempted": False, "judge_attempted": False})
        original = read_eval_sample(*split_slot(key))
        response_hash = sha256_text(original["output"]["completion"])
        if response_hash != plan.route_b_response_hash:
            raise AssertionError("Route-B response hash mismatch before Judge request.")
        route_b_response_hash_verified = True
        lifecycle["stage"] = "ROUTE_B_RESPONSE_HASH_VERIFIED"
        lifecycle_marker(
            "ROUTE_B_RESPONSE_HASH_VERIFIED",
            target_slot=key,
            route="B",
            preserved_response_sha256=response_hash,
            generation_provider_calls=0,
        )
        before = time.perf_counter()
        lifecycle["stage"] = "SCORER_CALL_ABOUT_TO_ENTER"
        lifecycle_marker("SCORER_CALL_ABOUT_TO_ENTER", target_slot=key, route="B")
        api_calls["route_b_judge"] += 1
        lifecycle["stage"] = "SCORER_CALL_ENTERED"
        lifecycle_marker(
            "SCORER_CALL_ENTERED",
            target_slot=key,
            route="B",
            observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED",
        )
        try:
            state, score, source = await runner.route_b_official_score(key)
        except BaseException:
            lifecycle["stage"] = "SCORER_CALL_RAISED"
            try:
                lifecycle_marker("SCORER_CALL_RAISED", target_slot=key, route="B")
            except BaseException as marker_exc:
                lifecycle["marker_persistence_error"] = safe_exception_record(
                    marker_exc,
                    target_slot=key,
                    route="B",
                    lifecycle_stage="SCORER_CALL_RAISED_MARKER_PERSISTENCE",
                )
            raise
        lifecycle["stage"] = "SCORER_CALL_RETURNED"
        lifecycle_marker("SCORER_CALL_RETURNED", target_slot=key, route="B")
        elapsed = time.perf_counter() - before
        if sha256_text(state.output.completion) != plan.route_b_response_hash:
            raise AssertionError("Route-B response changed during scorer-only recovery.")
        provenance = {
            "recovery_phase": "PHASE_1E_R4",
            "recovery_route": "B_PRESERVED_GENERATION_JUDGE_SCORE_ONLY",
            "source_frozen_sample": source["id"],
            "target_epoch": source["epoch"],
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "ORIGINAL_PHASE_1E_PRESERVED_GENERATION",
            "original_response_sha256": plan.route_b_response_hash,
            "score_provenance": "PHASE_1E_R4_OFFICIAL_PERSISTBENCH_SCORER",
        }
        await persist_complete(key, "B", state, score, provenance, started_at, {"judge_and_scorer": elapsed}, False, True)
        completed.append(key)

    async def run_route_c(key: str, model: Any, generator_config: GenerateConfig) -> None:
        started_at = utc_now()
        plan.reject_route_a(key)
        plan.require_route_c(key)
        if key == plan.route_b:
            raise AssertionError("Route-B slot cannot enter Route C.")
        append_jsonl(ledger_path, {"event": "SLOT_STARTED", "slot": key, "route": "C", "timestamp": started_at, "generation_attempted": True, "judge_attempted": False})
        generation_start = time.perf_counter()
        api_calls["route_c_generator"] += 1
        state = await runner.route_c_official_generate(key, model, generator_config)
        generation_elapsed = time.perf_counter() - generation_start
        if state.output.empty or not state.output.completion:
            raise AssertionError("Generator returned incomplete output.")
        safe_name = key.replace(":", "_").replace("=", "_")
        generation_path = slots_dir / f"{safe_name}.generation.json"
        atomic_json_write(
            generation_path,
            {
                "slot": key,
                "recovery_route": "C_EXACT_MISSING_SLOT_EXECUTION_COMPLETION",
                "state_epoch": state.epoch,
                "output": serialise_output(state.output),
                "response_sha256": sha256_text(state.output.completion),
                "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            },
        )
        append_jsonl(ledger_path, {"event": "GENERATION_PERSISTED", "slot": key, "route": "C", "timestamp": utc_now(), "generation_attempted": True, "generation_completed": True, "generation_response_sha256": sha256_text(state.output.completion), "generation_artifact": str(generation_path.relative_to(ROOT)).replace("\\", "/"), "raw_generation_latency_seconds": generation_elapsed})
        score_start = time.perf_counter()
        api_calls["route_c_judge"] += 1
        sample, _ = runner.sample_for(key)
        score = await persistbench_judge()(state, Target(sample.target))
        score_elapsed = time.perf_counter() - score_start
        sample_id, epoch = split_slot(key)
        provenance = {
            "recovery_phase": "PHASE_1E_R4",
            "recovery_route": "C_EXACT_MISSING_SLOT_EXECUTION_COMPLETION",
            "source_frozen_sample": sample_id,
            "target_epoch": epoch,
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": str(SOURCE_LOG.relative_to(ROOT)).replace("\\", "/"),
            "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            "score_provenance": "PHASE_1E_R4_OFFICIAL_PERSISTBENCH_SCORER",
        }
        await persist_complete(key, "C", state, score, provenance, started_at, {"generation": generation_elapsed, "judge_and_scorer": score_elapsed}, True, True)
        completed.append(key)

    try:
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            await run_route_b()
            generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
            generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
            for key in plan.route_c_order:
                await run_route_c(key, generator_model, generator_config)
            canonical_path = output_dir / "phase-1e-r4-canonical-cross-domain-pack.json"
            assemble_additive_pack(recovered_path, canonical_path)
            canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
            records = canonical.get("records", [])
            if len(records) != 60 or any(not complete_record(item["record"]) for item in records):
                raise AssertionError("Canonical assembly lacks 60 complete official records.")
            post_fingerprint = runner.verify_route_a_fingerprints()
            if post_fingerprint != plan.route_a_fingerprint_hash:
                raise AssertionError("Route-A fingerprint changed after assembly.")
            status = "PHASE_1E_R4_PASS / CROSS_DOMAIN_RECOVERY_COMPLETE / 60_OF_60_EXECUTION_COMPLETE"
            assembly_status = "PASS"
        finally:
            official_scorers._get_judge = original_get_judge
    except BaseException as exc:
        failed_target = next((key for key in [plan.route_b, *plan.route_c_order] if key not in completed), "CANONICAL_ASSEMBLY")
        failure = safe_exception_record(
            exc,
            target_slot=lifecycle.get("target_slot") or failed_target,
            route=lifecycle.get("route"),
            lifecycle_stage=safe_text(lifecycle.get("stage")),
        )
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            post_fingerprint = None
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(
                fingerprint_exc,
                target_slot=None,
                route=None,
                lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT",
            )
        status = "PHASE_1E_R4_EXECUTION_INTERRUPTED"
        assembly_status = "NOT_RUN_OR_NOT_COMPLETE"

    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "completed_slots": completed, "failed_target": failed_target})
    manifest_write = write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4",
        "status": status,
        "api_calls_by_route": api_calls,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_a_fingerprint_after": post_fingerprint,
        "route_b_response_hash_verified_before_scorer": route_b_response_hash_verified,
        "route_b_generator_provider_calls": api_calls["route_b_generator"],
        "route_b_judge_scorer_complete": plan.route_b in completed,
        "route_c_targets_attempted": [key for key in plan.route_c_order if key in completed or key == failed_target],
        "route_c_targets_completed": [key for key in completed if key in plan.route_c],
        "first_failed_target": failed_target,
        "failure": failure,
        "recovered_artifact_count": len(completed),
        "canonical_assembly_status": assembly_status,
        "slot_universe_integrity": "60_EXACT_SLOTS" if status.startswith("PHASE_1E_R4_PASS") else "INCOMPLETE_AFTER_FIRST_INTEGRITY_FAILURE",
        "original_artifacts_modified": False,
        "reserve_or_frozen_validation_access": False,
        "sycophancy_or_beneficial_execution": False,
        "max_retries": 0,
        "manifest_report_write": manifest_write,
        "files": {
            "manifest": safe_path_reference(manifest_path),
            "ledger": safe_path_reference(ledger_path),
            "recovered_records": safe_path_reference(recovered_path),
            "execution_report": safe_path_reference(markdown_report_path),
        },
    }
    result_report_write = write_json_failure_safe(report_path, result)
    result["result_report_write"] = result_report_write
    try:
        write_execution_markdown(markdown_report_path, result)
    except BaseException:
        # The JSON result has already been sent through failure-safe reporting.
        pass
    return result


async def execute_authorized_route_b_replacement(output_dir: Path) -> dict[str, Any]:
    """Execute the single separately-authorized Route-B replacement attempt.

    This function never selects Route C and refuses any directory other than
    the canonical R4C evidence directory, so a second invocation cannot be
    hidden in a different output path.
    """
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4c").resolve(strict=False)
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    key = plan.route_b
    ledger_path = output_dir / "phase-1e-r4c-execution-ledger.jsonl"
    manifest_path = output_dir / "phase-1e-r4c-execution-manifest.json"
    report_path = output_dir / "phase-1e-r4c-integrity-report.json"
    record_path = output_dir / "phase-1e-r4c-route-b-recovered-record.json"
    markdown_path = output_dir / "PHASE_1E_R4C_EXECUTION_REPORT.md"
    api_calls = {"route_b_generator": 0, "route_b_judge": 0}
    lifecycle: dict[str, Any] = {"target_slot": key, "route": "B", "stage": "R4C_PRECALL_INVARIANTS"}
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    response_hash: str | None = None
    failure: dict[str, Any] | None = None
    score_present = False
    record_present = False
    replacement_started = False

    def marker(event: str, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": key,
                "route": "B",
                "timestamp": utc_now(),
                "attempt_type": "AUTHORIZED_REPLACEMENT_ATTEMPT",
                "replacement_attempt_number": 1,
                "replacement_attempt_limit": 1,
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4C replacement must use the canonical recovery/r4c output directory.")
        if manifest_path.exists():
            raise AssertionError("R4C replacement attempt history already exists; no second invocation is permitted.")
        amendment = json.loads(R4B_AMENDMENT_PATH.read_text(encoding="utf-8"))
        policy = amendment["narrow_replacement_policy"]
        if amendment["route_b"]["slot"] != key or policy["maximum_additional_judge_score_attempts"] != 1:
            raise AssertionError("R4B replacement amendment does not authorize exactly one Route-B attempt.")
        if policy["future_replacement_execution_authorized_by_this_document"] is not False:
            raise AssertionError("R4B amendment must remain policy-only.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        original = read_eval_sample(*split_slot(key))
        response_hash = sha256_text(original["output"]["completion"])
        if response_hash != plan.route_b_response_hash:
            raise AssertionError("Route-B preserved response hash mismatch before replacement.")
    except BaseException as exc:
        blocked = {
            "phase": "PHASE_1E_R4C",
            "status": "PHASE_1E_R4C_BLOCKED",
            "target_slot": key,
            "api_calls_by_route": api_calls,
            "pre_call_failure": safe_exception_record(
                exc, target_slot=key, route="B", lifecycle_stage=safe_text(lifecycle["stage"])
            ),
            "route_a_fingerprint_before": pre_fingerprint,
            "route_b_response_sha256": response_hash,
            "replacement_authorization_consumed": False,
            "route_c_attempted_slots": 0,
        }
        write_json_failure_safe(report_path, blocked)
        return blocked

    manifest = {
        "phase": "PHASE_1E_R4C",
        "status": "RUNNING",
        "target_slot": key,
        "route": "B",
        "started_at": utc_now(),
        "attempt_type": "AUTHORIZED_REPLACEMENT_ATTEMPT",
        "historical_attempt_1": "NON_DURABLE_UNRESOLVED_ATTEMPT",
        "replacement_attempt_number": 1,
        "replacement_attempt_limit": 1,
        "replacement_authorization_consumed": True,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_preserved_response_sha256": response_hash,
        "generator_calls_planned": 0,
        "max_retries": 0,
        "api_calls": api_calls,
        "route_c_attempted_slots": 0,
    }
    write_json_failure_safe(manifest_path, manifest)

    try:
        lifecycle["stage"] = "AUTHORIZED_REPLACEMENT_PRE_ATTEMPT_PERSISTED"
        marker(
            "AUTHORIZED_REPLACEMENT_PRE_ATTEMPT_PERSISTED",
            historical_attempt_1="NON_DURABLE_UNRESOLVED_ATTEMPT",
            response_sha256=response_hash,
            max_retries=0,
            generator_calls_planned=0,
        )
        lifecycle["stage"] = "ROUTE_B_RESPONSE_HASH_VERIFIED"
        marker("ROUTE_B_RESPONSE_HASH_VERIFIED", preserved_response_sha256=response_hash, generator_provider_calls=0)
        lifecycle["stage"] = "SCORER_CALL_ABOUT_TO_ENTER"
        marker("SCORER_CALL_ABOUT_TO_ENTER")
        api_calls["route_b_judge"] += 1
        replacement_started = True
        lifecycle["stage"] = "SCORER_CALL_ENTERED"
        marker(
            "SCORER_CALL_ENTERED",
            observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED",
        )
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            state, score, source = await runner.route_b_official_score(key)
        except BaseException:
            lifecycle["stage"] = "SCORER_CALL_RAISED"
            try:
                marker("SCORER_CALL_RAISED")
            except BaseException as marker_exc:
                lifecycle["marker_persistence_error"] = safe_exception_record(
                    marker_exc, target_slot=key, route="B", lifecycle_stage="SCORER_CALL_RAISED_MARKER_PERSISTENCE"
                )
            raise
        finally:
            official_scorers._get_judge = original_get_judge
        lifecycle["stage"] = "SCORER_CALL_RETURNED"
        marker("SCORER_CALL_RETURNED")
        score_present = True
        if sha256_text(state.output.completion) != plan.route_b_response_hash:
            raise AssertionError("Route-B response changed during replacement scorer-only execution.")
        lifecycle["stage"] = "RECOVERED_RECORD_ABOUT_TO_PERSIST"
        marker("RECOVERED_RECORD_ABOUT_TO_PERSIST", scorer_completed=True)
        provenance = {
            "recovery_phase": "PHASE_1E_R4C",
            "recovery_route": "B_PRESERVED_GENERATION_JUDGE_SCORE_ONLY",
            "attempt_type": "AUTHORIZED_REPLACEMENT_ATTEMPT",
            "historical_attempt_1": "NON_DURABLE_UNRESOLVED_ATTEMPT",
            "recovered_evaluation_provenance": "AUTHORIZED_REPLACEMENT_ATTEMPT_AFTER_NON_DURABLE_UNRESOLVED_ATTEMPT",
            "source_frozen_sample": source["id"],
            "target_epoch": source["epoch"],
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "ORIGINAL_PHASE_1E_PRESERVED_GENERATION",
            "original_response_sha256": plan.route_b_response_hash,
            "score_provenance": "PHASE_1E_R4C_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("R4C official scorer did not yield a complete recovered Route-B record.")
        item = {"slot": key, "record": record, "provenance": provenance}
        atomic_json_write(record_path, item)
        record_present = True
        marker(
            "RECOVERED_RECORD_PERSISTED",
            scorer_completed=True,
            final_record_persisted=True,
            source_recovered_artifact=safe_path_reference(record_path),
        )
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after Route-B replacement.")
        status = "PHASE_1E_R4C_PASS / ROUTE_B_RECOVERED / AUTHORIZED_REPLACEMENT_CONSUMED / READY_FOR_ROUTE_B_INTEGRITY_ACCEPTANCE"
    except BaseException as exc:
        failure = safe_exception_record(
            exc,
            target_slot=key,
            route="B",
            lifecycle_stage=safe_text(lifecycle["stage"]),
        )
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(
                fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT"
            )
        status = "PHASE_1E_R4C_REPLACEMENT_FAILED / ROUTE_B_RECOVERY_UNRESOLVED_AFTER_AUTHORIZED_REPLACEMENT"

    manifest.update(
        {
            "status": status,
            "ended_at": utc_now(),
            "api_calls": api_calls,
            "route_b_score_present": score_present,
            "route_b_recovered_record_present": record_present,
            "failure": failure,
        }
    )
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4C",
        "status": status,
        "target_slot": key,
        "pre_call_route_a_fingerprint": pre_fingerprint,
        "post_execution_route_a_fingerprint": post_fingerprint,
        "route_b_original_response_sha256": response_hash,
        "route_b_generator_provider_calls": api_calls["route_b_generator"],
        "route_b_judge_scorer_invocations": api_calls["route_b_judge"],
        "observable_provider_http_attempts": "NO_DIRECT_RUNTIME_HOOK_SURVIVED_OR_INSTRUMENTED",
        "judge_lifecycle_result": lifecycle["stage"],
        "official_score_present": score_present,
        "recovered_record_present": record_present,
        "recovered_record_provenance": "AUTHORIZED_REPLACEMENT_ATTEMPT_AFTER_NON_DURABLE_UNRESOLVED_ATTEMPT" if record_present else None,
        "replacement_attempt_count": 1 if replacement_started else 0,
        "replacement_authorization_consumed": replacement_started,
        "no_further_route_b_attempt_authorized": replacement_started,
        "route_c_attempted_slots": 0,
        "original_phase_1e_artifacts_modified": False,
        "max_retries": 0,
        "failure": failure,
        "files": {
            "manifest": safe_path_reference(manifest_path),
            "ledger": safe_path_reference(ledger_path),
            "recovered_record": safe_path_reference(record_path),
            "execution_report": safe_path_reference(markdown_path),
        },
    }
    write_json_failure_safe(report_path, result)
    try:
        write_execution_markdown(
            markdown_path,
            {
                "status": status,
                "route_b_generator_provider_calls": api_calls["route_b_generator"],
                "route_b_judge_scorer_complete": score_present,
                "route_c_targets_completed": [],
                "first_failed_target": key if failure else None,
                "canonical_assembly_status": "NOT_RUN_R4C_ROUTE_B_ONLY",
                "route_a_fingerprint_before": pre_fingerprint,
                "route_a_fingerprint_after": post_fingerprint,
            },
        )
    except BaseException:
        pass
    return result


def assemble_route_c_with_terminal_route_b(
    recovered_path: Path,
    output_path: Path,
    terminal_disposition: dict[str, Any],
    *,
    phase: str = "PHASE_1E_R4E_CANONICAL_CROSS_DOMAIN_UNIVERSE",
) -> dict[str, Any]:
    """Create an additive 60-slot universe with 59 scored records and one explicit missing slot."""
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    route_a_fingerprint = runner.verify_route_a_fingerprints()
    recovered = [json.loads(line) for line in recovered_path.read_text(encoding="utf-8").splitlines() if line]
    recovered_by_slot = {item["slot"]: item for item in recovered}
    if len(recovered_by_slot) != len(recovered) or set(recovered_by_slot) != set(plan.route_c):
        raise AssertionError("R4E recovered records are not exactly the frozen Route-C allowlist.")
    for key, item in recovered_by_slot.items():
        record = item.get("record", {})
        provenance = item.get("provenance", {})
        sample_id, epoch = split_slot(key)
        if record.get("id") != sample_id or record.get("epoch") != epoch or not complete_record(record):
            raise AssertionError(f"R4E Route-C recovered record is incomplete or mismatched: {key}")
        expected_generation_provenance = (
            "PHASE_1E_RECOVERY_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE"
            if key == "persistbench_a173ee7a:epoch=3"
            else "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION"
        )
        if provenance.get("generation_provenance") != expected_generation_provenance:
            raise AssertionError(f"R4E Route-C generation provenance mismatch: {key}")
    if terminal_disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
        raise AssertionError("Route-B terminal disposition is not frozen.")
    originals = [
        {"slot": key, "record": read_eval_sample(*split_slot(key)), "provenance": {"recovery_route": "A_IMMUTABLE_SOURCE"}}
        for key in sorted(plan.route_a)
    ]
    terminal_b = {
        "slot": plan.route_b,
        "record": None,
        "provenance": {
            "recovery_route": "B_TERMINAL_INFRASTRUCTURE_MISSING",
            "terminal_state": terminal_disposition["route_b"]["terminal_state"],
            "disposition": terminal_disposition["route_b"]["disposition"],
            "original_response_sha256": plan.route_b_response_hash,
            "official_score": "NONE_NOT_IMPUTED",
        },
    }
    payload = {
        "phase": phase,
        "metrics_computed": False,
        "frozen_universe_count": 60,
        "official_scored_record_count": 59,
        "infrastructure_missing_record_count": 1,
        "route_a_fingerprint_set_sha256": route_a_fingerprint,
        "records": originals + [recovered_by_slot[key] for key in plan.route_c_order] + [terminal_b],
    }
    if len(payload["records"]) != 60 or len({item["slot"] for item in payload["records"]}) != 60:
        raise AssertionError("R4E canonical universe has missing or duplicate slots.")
    atomic_json_write(output_path, payload)
    return payload


async def execute_authorized_route_c_recovery(output_dir: Path) -> dict[str, Any]:
    """Execute the exact frozen Route-C allowlist, sequentially and stop-on-first-failure."""
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4e").resolve(strict=False)
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    ledger_path = output_dir / "phase-1e-r4e-execution-ledger.jsonl"
    recovered_path = output_dir / "phase-1e-r4e-recovered-records.jsonl"
    manifest_path = output_dir / "phase-1e-r4e-execution-manifest.json"
    report_path = output_dir / "phase-1e-r4e-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4E_EXECUTION_REPORT.md"
    slots_dir = output_dir / "recovered-slots"
    canonical_path = output_dir / "phase-1e-r4e-canonical-cross-domain-universe.json"
    api_calls = {"route_b_generator": 0, "route_b_judge": 0, "route_c_generator": 0, "route_c_judge": 0}
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    completed: list[str] = []
    attempted: list[str] = []
    failure: dict[str, Any] | None = None
    lifecycle: dict[str, Any] = {"target_slot": None, "route": "C", "stage": "R4E_PRECALL_INVARIANTS"}
    route_c_order_hash = sha256_text(canonical_json(list(plan.route_c_order)))
    assembly_status = "NOT_RUN"

    def marker(event: str, key: str, sequence: int, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": key,
                "route": "C",
                "sequence_number": sequence,
                "timestamp": utc_now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    def failure_marker(event: str, key: str, sequence: int, exc: BaseException) -> None:
        """Best-effort error marker that cannot replace the triggering exception."""
        try:
            marker(event, key, sequence, runtime_error=safe_exception_record(exc, target_slot=key, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(
                marker_exc, target_slot=key, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE"
            )

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4E recovery must use the canonical recovery/r4e output directory.")
        if manifest_path.exists():
            raise AssertionError("R4E execution history already exists; a batch restart is forbidden.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        if len(plan.route_c_order) != 25 or len(plan.route_c) != 25:
            raise AssertionError("Frozen Route-C allowlist does not contain exactly 25 slots.")
        if plan.route_c & plan.route_a or plan.route_b in plan.route_c:
            raise AssertionError("Route-C partition intersects immutable Route A or terminal Route B.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {
            "phase": "PHASE_1E_R4E",
            "status": "PHASE_1E_R4E_BLOCKED / ROUTE_A_FINGERPRINT_MISMATCH",
            "api_calls_by_route": api_calls,
            "failure": safe_exception_record(exc, target_slot=None, route="C", lifecycle_stage=safe_text(lifecycle["stage"])),
            "route_a_fingerprint_before": pre_fingerprint,
            "route_b_original_response_sha256": route_b_hash,
            "route_c_order_sha256": route_c_order_hash,
        }
        write_json_failure_safe(report_path, blocked)
        return blocked

    manifest = {
        "phase": "PHASE_1E_R4E",
        "status": "RUNNING",
        "started_at": utc_now(),
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "route_c_order": list(plan.route_c_order),
        "route_c_order_sha256": route_c_order_hash,
        "route_c_count": 25,
        "max_retries": 0,
        "api_calls": api_calls,
        "completed_slots": completed,
    }
    write_json_failure_safe(manifest_path, manifest)

    async def run_slot(key: str, sequence: int, model: Any, config: GenerateConfig) -> None:
        lifecycle.update({"target_slot": key, "route": "C", "stage": "SLOT_PRECHECK"})
        plan.reject_route_a(key)
        plan.require_route_c(key)
        if key == plan.route_b:
            raise AssertionError("Terminal Route-B slot cannot enter Route C.")
        attempted.append(key)
        started_at = utc_now()
        marker("SLOT_STARTED", key, sequence, generator_started=False, judge_started=False)
        lifecycle["stage"] = "GENERATOR_STARTED"
        marker("GENERATOR_STARTED", key, sequence)
        generation_start = time.perf_counter()
        api_calls["route_c_generator"] += 1
        try:
            state = await runner.route_c_official_generate(key, model, config)
        except BaseException as exc:
            lifecycle["stage"] = "GENERATOR_RAISED"
            failure_marker("GENERATOR_RAISED", key, sequence, exc)
            raise
        generation_latency = time.perf_counter() - generation_start
        if state.output.empty or not state.output.completion:
            raise AssertionError("Route-C generator returned incomplete output.")
        sample_id, epoch = split_slot(key)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("Route-C state identity/epoch mismatch after generation.")
        lifecycle["stage"] = "GENERATION_PERSISTENCE"
        safe_name = key.replace(":", "_").replace("=", "_")
        generation_path = slots_dir / f"{safe_name}.generation.json"
        atomic_json_write(
            generation_path,
            {
                "slot": key,
                "sequence_number": sequence,
                "output": serialise_output(state.output),
                "response_sha256": sha256_text(state.output.completion),
                "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
                "raw_generation_latency_seconds": generation_latency,
            },
        )
        marker(
            "GENERATION_PERSISTED",
            key,
            sequence,
            generator_completed=True,
            generator_response_sha256=sha256_text(state.output.completion),
            raw_generation_latency_seconds=generation_latency,
            generation_artifact=safe_path_reference(generation_path),
        )
        lifecycle["stage"] = "JUDGE_SCORER_STARTED"
        marker("JUDGE_SCORER_STARTED", key, sequence)
        score_start = time.perf_counter()
        api_calls["route_c_judge"] += 1
        sample, _ = runner.sample_for(key)
        try:
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", key, sequence, exc)
            raise
        score_latency = time.perf_counter() - score_start
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_R4E",
            "recovery_route": "C_EXACT_MISSING_SLOT_EXECUTION_COMPLETION",
            "source_frozen_sample": sample_id,
            "target_epoch": epoch,
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            "score_provenance": "PHASE_1E_R4E_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("Route-C scorer did not yield a complete official record.")
        item = {"slot": key, "record": record, "provenance": provenance}
        record_path = slots_dir / f"{safe_name}.recovered.json"
        atomic_json_write(record_path, item)
        append_jsonl(recovered_path, item)
        marker(
            "RECOVERED_RECORD_PERSISTED",
            key,
            sequence,
            start_timestamp=started_at,
            end_timestamp=utc_now(),
            generator_completed=True,
            judge_completed=True,
            scorer_completed=True,
            official_score_present=True,
            final_record_persisted=True,
            raw_judge_scorer_latency_seconds=score_latency,
            recovered_artifact=safe_path_reference(record_path),
            status="COMPLETE",
        )
        completed.append(key)

    try:
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
            generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
            for sequence, key in enumerate(plan.route_c_order, start=1):
                await run_slot(key, sequence, generator_model, generator_config)
        finally:
            official_scorers._get_judge = original_get_judge
        canonical = assemble_route_c_with_terminal_route_b(recovered_path, canonical_path, disposition)
        if canonical["official_scored_record_count"] != 59 or canonical["infrastructure_missing_record_count"] != 1:
            raise AssertionError("R4E canonical universe count invariant failed.")
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4E assembly.")
        status = "PHASE_1E_R4E_PASS / ROUTE_C_RECOVERY_COMPLETE / CROSS_DOMAIN_EXECUTION_CLOSED_WITH_59_SCORED_1_INFRASTRUCTURE_MISSING / READY_FOR_CROSS_DOMAIN_INTEGRITY_ACCEPTANCE"
        assembly_status = "PASS"
    except BaseException as exc:
        failure = safe_exception_record(
            exc,
            target_slot=lifecycle.get("target_slot"),
            route="C",
            lifecycle_stage=safe_text(lifecycle.get("stage")),
        )
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(
                fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT"
            )
        status = "PHASE_1E_R4E_EXECUTION_INTERRUPTED"
        assembly_status = "NOT_RUN_OR_INCOMPLETE"

    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "completed_slots": completed, "attempted_slots": attempted, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4E",
        "status": status,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_a_fingerprint_after": post_fingerprint,
        "route_b_terminal_state_verified": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "route_c_ordered_allowlist_count": len(plan.route_c_order),
        "route_c_order_sha256": route_c_order_hash,
        "route_c_slots_attempted": attempted,
        "route_c_slots_completed": completed,
        "failed_slot": lifecycle.get("target_slot") if failure else None,
        "failure_stage": failure.get("lifecycle_stage") if failure else None,
        "api_calls_by_route": api_calls,
        "recovered_records_persisted": len(completed),
        "canonical_assembly_status": assembly_status,
        "official_scored_record_count": 59 if assembly_status == "PASS" else 34 + len(completed),
        "infrastructure_missing_record_count": 1,
        "unexpected_slots": 0,
        "missing_route_c_slots": len(plan.route_c_order) - len(completed),
        "duplicate_slots": 0,
        "original_phase_1e_artifacts_modified": False,
        "max_retries": 0,
        "failure": failure,
        "files": {
            "manifest": safe_path_reference(manifest_path),
            "ledger": safe_path_reference(ledger_path),
            "recovered_records": safe_path_reference(recovered_path),
            "canonical_universe": safe_path_reference(canonical_path),
            "execution_report": safe_path_reference(markdown_path),
        },
    }
    write_json_failure_safe(report_path, result)
    try:
        write_execution_markdown(
            markdown_path,
            {
                "status": status,
                "route_b_generator_provider_calls": 0,
                "route_b_judge_scorer_complete": False,
                "route_c_targets_completed": completed,
                "first_failed_target": result["failed_slot"],
                "canonical_assembly_status": assembly_status,
                "route_a_fingerprint_before": pre_fingerprint,
                "route_a_fingerprint_after": post_fingerprint,
            },
        )
    except BaseException:
        pass
    return result


async def execute_authorized_route_c_first_slot_replacement(output_dir: Path) -> dict[str, Any]:
    """Execute the separately authorized R4H replacement for one frozen Route-C slot only.

    This function owns slot selection, lifecycle evidence, and additive persistence only.
    Generation, Judge, and scoring remain the existing official Inspect/PersistBench paths.
    """
    target = "persistbench_a173ee7a:epoch=3"
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4h").resolve(strict=False)
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4h-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4h-execution-ledger.jsonl"
    record_path = output_dir / "phase-1e-r4h-first-slot-record.json"
    generation_path = output_dir / "phase-1e-r4h-first-slot-generation.json"
    report_path = output_dir / "phase-1e-r4h-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4H_EXECUTION_REPORT.md"
    r4g_path = RECOVERY_ROOT / "r4g" / "phase-1e-r4g-connectivity-preflight.json"
    r4e_manifest_path = RECOVERY_ROOT / "r4e" / "phase-1e-r4e-execution-manifest.json"
    r4e_ledger_path = RECOVERY_ROOT / "r4e" / "phase-1e-r4e-execution-ledger.jsonl"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    failure: dict[str, Any] | None = None
    generation_completed = False
    generation_persisted = False
    judge_scorer_completed = False
    score_present = False
    record_present = False
    authorization_consumed = False
    lifecycle: dict[str, Any] = {"stage": "R4H_PRECALL_INVARIANTS", "target_slot": target, "route": "C"}

    def marker(event: str, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": target,
                "route": "C",
                "attempt_number": 2,
                "attempt_type": "AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
                "timestamp": utc_now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    def failure_marker(event: str, exc: BaseException) -> None:
        try:
            marker(
                event,
                runtime_error=safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=event),
            )
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(
                marker_exc, target_slot=target, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE"
            )

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4H replacement must use the canonical recovery/r4h output directory.")
        if manifest_path.exists():
            raise AssertionError("R4H replacement history already exists; Attempt 2 cannot be invoked twice.")
        if target in plan.route_a or target == plan.route_b:
            raise AssertionError("R4H target intersects an immutable or terminal route.")
        plan.require_route_c(target)
        if plan.route_c_order[0] != target:
            raise AssertionError("R4H target is not the frozen first Route-C slot.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
        r4g = json.loads(r4g_path.read_text(encoding="utf-8"))
        if r4g["status"] != "PHASE_1E_R4G_PASS / CONNECTIVITY_CURRENTLY_HEALTHY_PRIOR_FAILURE_TRANSIENT_OR_UNRESOLVED / READY_FOR_RECOVERY_AUTHORIZATION_REVIEW":
            raise AssertionError("R4G connectivity preflight is not the required PASS state.")
        if any(item["httpx"]["status"] != "PASS" for item in r4g["provider_results"]):
            raise AssertionError("R4G did not establish HTTPX transport for both provider origins.")
        r4e_manifest = json.loads(r4e_manifest_path.read_text(encoding="utf-8"))
        if r4e_manifest.get("attempted_slots") != [target] or r4e_manifest.get("completed_slots") != []:
            raise AssertionError("R4E Attempt-1 topology is not the required single failed first slot.")
        r4e_events = [json.loads(line) for line in r4e_ledger_path.read_text(encoding="utf-8").splitlines() if line]
        if [event.get("event") for event in r4e_events] != ["SLOT_STARTED", "GENERATOR_STARTED", "GENERATOR_RAISED"]:
            raise AssertionError("R4E Attempt-1 lifecycle evidence is not intact.")
        r3_report = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3_report["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence report no longer proves zero custom semantics.")
    except BaseException as exc:
        blocked = {
            "phase": "PHASE_1E_R4H",
            "status": "PHASE_1E_R4H_BLOCKED",
            "target_slot": target,
            "attempt_2_authorization_consumed": False,
            "api_calls_by_route": api_calls,
            "route_a_fingerprint_before": pre_fingerprint,
            "route_b_original_response_sha256": route_b_hash,
            "pre_call_failure": safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"])),
        }
        write_json_failure_safe(report_path, blocked)
        return blocked

    manifest = {
        "phase": "PHASE_1E_R4H",
        "status": "RUNNING",
        "target_slot": target,
        "route": "C",
        "started_at": utc_now(),
        "attempt_number": 2,
        "attempt_type": "AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
        "prior_attempt_state": "ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",
        "r4g_connectivity_status": "PHASE_1E_R4G_PASS / CONNECTIVITY_CURRENTLY_HEALTHY_PRIOR_FAILURE_TRANSIENT_OR_UNRESOLVED / READY_FOR_RECOVERY_AUTHORIZATION_REVIEW",
        "maximum_additional_full_generation_attempts_after_this": 0,
        "max_retries": 0,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "api_calls": api_calls,
    }
    write_json_failure_safe(manifest_path, manifest)

    try:
        lifecycle["stage"] = "AUTHORIZED_REPLACEMENT_PRE_ATTEMPT_PERSISTED"
        marker(
            "AUTHORIZED_REPLACEMENT_PRE_ATTEMPT_PERSISTED",
            prior_attempt_state="ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",
            r4g_connectivity_status=manifest["r4g_connectivity_status"],
            max_retries=0,
            maximum_additional_full_generation_attempts_after_this=0,
        )
        authorization_consumed = True
        generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
        generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
        lifecycle["stage"] = "GENERATOR_CALL_ABOUT_TO_ENTER"
        marker("GENERATOR_CALL_ABOUT_TO_ENTER")
        api_calls["route_c_generator"] += 1
        lifecycle["stage"] = "GENERATOR_CALL_ENTERED"
        marker("GENERATOR_CALL_ENTERED", observability_note="APPLICATION_GENERATOR_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
        generation_started_at = time.perf_counter()
        try:
            state = await runner.route_c_official_generate(target, generator_model, generator_config)
        except BaseException as exc:
            lifecycle["stage"] = "GENERATOR_RAISED"
            failure_marker("GENERATOR_RAISED", exc)
            raise
        generation_latency = time.perf_counter() - generation_started_at
        if state.output.empty or not state.output.completion:
            raise AssertionError("R4H generator returned incomplete output.")
        sample_id, epoch = split_slot(target)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("R4H state identity/epoch mismatch after official generation.")
        generation_completed = True
        lifecycle["stage"] = "GENERATION_PERSISTENCE"
        response_hash = sha256_text(state.output.completion)
        output_payload = serialise_output(state.output)
        atomic_json_write(
            generation_path,
            {
                "slot": target,
                "attempt_number": 2,
                "output": output_payload,
                "response_sha256": response_hash,
                "generator_model": output_payload.get("model"),
                "usage": output_payload.get("usage"),
                "raw_generation_latency_seconds": generation_latency,
                "generation_provenance": "PHASE_1E_RECOVERY_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
                "prior_attempt_state": "ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",
                "immutable_after_persistence": True,
            },
        )
        generation_persisted = True
        marker(
            "GENERATION_PERSISTED",
            generator_completed=True,
            generator_response_sha256=response_hash,
            raw_generation_latency_seconds=generation_latency,
            generation_artifact=safe_path_reference(generation_path),
            generation_provenance="PHASE_1E_RECOVERY_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
        )
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER")
        api_calls["route_c_judge"] += 1
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
            marker("JUDGE_SCORER_CALL_ENTERED", observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
            score_started_at = time.perf_counter()
            sample, _ = runner.sample_for(target)
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", exc)
            raise
        finally:
            official_scorers._get_judge = original_get_judge
        score_latency = time.perf_counter() - score_started_at
        judge_scorer_completed = True
        score_present = True
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_R4H",
            "recovery_route": "C_FIRST_SLOT_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
            "attempt_number": 2,
            "attempt_type": "AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
            "prior_attempt_state": "ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",
            "source_frozen_sample": sample_id,
            "target_epoch": epoch,
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "PHASE_1E_RECOVERY_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE",
            "generation_response_sha256": response_hash,
            "score_provenance": "PHASE_1E_R4H_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("R4H official scorer did not yield a complete recovered Route-C record.")
        atomic_json_write(record_path, {"slot": target, "record": record, "provenance": provenance})
        record_present = True
        marker(
            "RECOVERED_RECORD_PERSISTED",
            generator_completed=True,
            judge_completed=True,
            scorer_completed=True,
            official_score_present=True,
            final_record_persisted=True,
            raw_judge_scorer_latency_seconds=score_latency,
            recovered_artifact=safe_path_reference(record_path),
        )
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4H replacement.")
        status = "PHASE_1E_R4H_PASS / ROUTE_C_FIRST_SLOT_RECOVERED / ATTEMPT_2_CONSUMED / READY_FOR_REMAINING_ROUTE_C_AUTHORIZATION_REVIEW"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(
                fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT"
            )
        if generation_persisted:
            status = "PHASE_1E_R4H_DOWNSTREAM_INTERRUPTED / ROUTE_C_FIRST_SLOT_GENERATION_PRESERVED"
        else:
            status = "PHASE_1E_R4H_REPLACEMENT_FAILED / ROUTE_C_FIRST_SLOT_GENERATION_RECOVERY_EXHAUSTED"

    manifest.update(
        {
            "status": status,
            "ended_at": utc_now(),
            "api_calls": api_calls,
            "attempt_2_authorization_consumed": authorization_consumed,
            "generation_completed": generation_completed,
            "generation_persisted": generation_persisted,
            "judge_scorer_completed": judge_scorer_completed,
            "official_score_present": score_present,
            "recovered_record_present": record_present,
            "failure": failure,
        }
    )
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4H",
        "status": status,
        "target_slot": target,
        "prior_attempt_state": "ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",
        "r4g_connectivity_preflight_verified": True,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_a_fingerprint_after": post_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "api_calls_by_route": api_calls,
        "generation_completed": generation_completed,
        "generation_persisted": generation_persisted,
        "generation_response_sha256": response_hash if generation_completed else None,
        "judge_scorer_completed": judge_scorer_completed,
        "official_score_present": score_present,
        "recovered_record_present": record_present,
        "attempt_2_authorization_consumed": authorization_consumed,
        "remaining_route_c_slots_untouched": 24,
        "max_retries": 0,
        "custom_semantic_implementation_count": 0,
        "failure": failure,
        "files": {
            "manifest": safe_path_reference(manifest_path),
            "ledger": safe_path_reference(ledger_path),
            "generation": safe_path_reference(generation_path),
            "recovered_record": safe_path_reference(record_path),
            "execution_report": safe_path_reference(markdown_path),
        },
    }
    write_json_failure_safe(report_path, result)
    try:
        lines = [
            "# Phase 1E-R4H first Route-C slot replacement execution report",
            "",
            f"Status: `{status}`",
            "",
            "This report intentionally contains no score value, product metric, or aggregate calculation.",
            "",
            f"- Target: `{target}`",
            "- Historical Attempt 1: `ROUTE_C_GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION`.",
            f"- Attempt 2 authorization consumed: `{authorization_consumed}`.",
            f"- Generation completed/persisted: `{generation_completed}` / `{generation_persisted}`.",
            f"- Judge/scorer completed: `{judge_scorer_completed}`; official score present: `{score_present}`.",
            f"- Recovered record persisted: `{record_present}`.",
            f"- Route-A fingerprint before/after: `{pre_fingerprint}` / `{post_fingerprint}`.",
            f"- Route-B original response SHA-256: `{route_b_hash}`.",
            "- Route-C slots 2–25 remain untouched; no Route B, Route A, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred.",
        ]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


async def execute_authorized_remaining_route_c_recovery(output_dir: Path) -> dict[str, Any]:
    """Execute R4I's frozen remaining 24 Route-C slots sequentially and stop on any error."""
    first_slot = "persistbench_a173ee7a:epoch=3"
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4i").resolve(strict=False)
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4i-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4i-execution-ledger.jsonl"
    recovered_path = output_dir / "phase-1e-r4i-recovered-records.jsonl"
    combined_path = output_dir / "phase-1e-r4i-all-route-c-recovered-records.jsonl"
    report_path = output_dir / "phase-1e-r4i-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4I_EXECUTION_REPORT.md"
    slots_dir = output_dir / "recovered-slots"
    canonical_path = output_dir / "phase-1e-r4i-canonical-cross-domain-universe.json"
    r4h_record_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-record.json"
    r4h_integrity_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-integrity-report.json"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    remaining: list[str] = []
    completed: list[str] = []
    attempted: list[str] = []
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    first_hash: str | None = None
    failure: dict[str, Any] | None = None
    lifecycle: dict[str, Any] = {"stage": "R4I_PRECALL_INVARIANTS", "target_slot": None, "route": "C"}
    registry_warnings: list[str] = []
    assembly_status = "NOT_RUN"

    def marker(event: str, key: str, sequence: int, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": key,
                "route": "C",
                "sequence_number": sequence,
                "timestamp": utc_now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    def failure_marker(event: str, key: str, sequence: int, exc: BaseException) -> None:
        try:
            marker(event, key, sequence, runtime_error=safe_exception_record(exc, target_slot=key, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(
                marker_exc, target_slot=key, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE"
            )

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4I recovery must use the canonical recovery/r4i output directory.")
        if manifest_path.exists():
            raise AssertionError("R4I execution history already exists; a batch restart is forbidden.")
        if len(plan.route_c_order) != 25 or plan.route_c_order[0] != first_slot:
            raise AssertionError("Frozen Route-C order no longer has the recovered first slot in position one.")
        remaining = list(plan.route_c_order[1:])
        if len(remaining) != 24 or len(set(remaining)) != 24 or first_slot in remaining:
            raise AssertionError("R4I remaining Route-C allowlist is not exactly 24 unique slots.")
        if set(remaining) & plan.route_a or plan.route_b in remaining:
            raise AssertionError("R4I allowlist intersects Route A or terminal Route B.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        r3 = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence evidence no longer proves zero custom semantics.")
        first_item = json.loads(r4h_record_path.read_text(encoding="utf-8"))
        first_integrity = json.loads(r4h_integrity_path.read_text(encoding="utf-8"))
        if first_item.get("slot") != first_slot or not complete_record(first_item.get("record", {})):
            raise AssertionError("R4H recovered first Route-C record is absent or incomplete.")
        first_hash = sha256_text(first_item["record"]["output"]["completion"])
        if first_hash != "1f9e2ab83c55dfd8728dd4b3255b79e358b1a97458326cc580699723807ec05f":
            raise AssertionError("Recovered first Route-C response hash changed.")
        if first_integrity.get("status", "").split(" /")[0] != "PHASE_1E_R4H_PASS":
            raise AssertionError("R4H recovered first Route-C record is not in PASS state.")
        if first_item.get("provenance", {}).get("generation_provenance") != "PHASE_1E_RECOVERY_AUTHORIZED_REPLACEMENT_AFTER_TRANSPORT_FAILURE":
            raise AssertionError("R4H first-slot provenance is not the authorized replacement provenance.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {
            "phase": "PHASE_1E_R4I",
            "status": "PHASE_1E_R4I_BLOCKED",
            "api_calls_by_route": api_calls,
            "route_a_fingerprint_before": pre_fingerprint,
            "route_b_original_response_sha256": route_b_hash,
            "recovered_first_route_c_response_sha256": first_hash,
            "pre_call_failure": safe_exception_record(exc, target_slot=None, route="C", lifecycle_stage=safe_text(lifecycle["stage"])),
        }
        write_json_failure_safe(report_path, blocked)
        return blocked

    remaining_hash = sha256_text(canonical_json(remaining))
    manifest = {
        "phase": "PHASE_1E_R4I",
        "status": "RUNNING",
        "started_at": utc_now(),
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "recovered_first_route_c_slot": first_slot,
        "recovered_first_route_c_response_sha256": first_hash,
        "remaining_route_c_order": remaining,
        "remaining_route_c_order_sha256": remaining_hash,
        "remaining_route_c_count": 24,
        "max_retries": 0,
        "custom_semantic_implementation_count": 0,
        "api_calls": api_calls,
        "completed_slots": completed,
    }
    write_json_failure_safe(manifest_path, manifest)

    class RegistryWarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "Unexpected exception loading entrypoints" in message:
                registry_warnings.append(safe_text(message))

    capture = RegistryWarningCapture()
    entrypoint_logger = logging.getLogger("inspect_ai._util.entrypoints")
    entrypoint_logger.addHandler(capture)

    async def run_slot(key: str, sequence: int, model: Any, config: GenerateConfig) -> None:
        lifecycle.update({"target_slot": key, "route": "C", "stage": "SLOT_PRECHECK"})
        plan.reject_route_a(key)
        plan.require_route_c(key)
        if key == first_slot or key == plan.route_b or key not in remaining:
            raise AssertionError("R4I attempted a forbidden or non-remaining Route-C target.")
        attempted.append(key)
        marker("SLOT_STARTED", key, sequence, generator_started=False, judge_started=False)
        lifecycle["stage"] = "GENERATOR_CALL_ABOUT_TO_ENTER"
        marker("GENERATOR_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_generator"] += 1
        lifecycle["stage"] = "GENERATOR_CALL_ENTERED"
        marker("GENERATOR_CALL_ENTERED", key, sequence)
        generation_started_at = time.perf_counter()
        try:
            state = await runner.route_c_official_generate(key, model, config)
        except BaseException as exc:
            lifecycle["stage"] = "GENERATOR_RAISED"
            failure_marker("GENERATOR_RAISED", key, sequence, exc)
            raise
        generation_latency = time.perf_counter() - generation_started_at
        if state.output.empty or not state.output.completion:
            raise AssertionError("R4I generator returned incomplete output.")
        sample_id, epoch = split_slot(key)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("R4I state identity/epoch mismatch after official generation.")
        lifecycle["stage"] = "GENERATION_PERSISTENCE"
        safe_name = key.replace(":", "_").replace("=", "_")
        response_hash = sha256_text(state.output.completion)
        generation_path = slots_dir / f"{safe_name}.generation.json"
        output_payload = serialise_output(state.output)
        atomic_json_write(generation_path, {"slot": key, "sequence_number": sequence, "output": output_payload, "response_sha256": response_hash, "usage": output_payload.get("usage"), "raw_generation_latency_seconds": generation_latency, "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION", "immutable_after_persistence": True})
        marker("GENERATION_PERSISTED", key, sequence, generator_completed=True, generator_response_sha256=response_hash, raw_generation_latency_seconds=generation_latency, generation_artifact=safe_path_reference(generation_path))
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_judge"] += 1
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
        marker("JUDGE_SCORER_CALL_ENTERED", key, sequence)
        score_started_at = time.perf_counter()
        sample, _ = runner.sample_for(key)
        try:
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", key, sequence, exc)
            raise
        score_latency = time.perf_counter() - score_started_at
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_R4I",
            "recovery_route": "C_REMAINING_EXACT_MISSING_SLOT_EXECUTION_COMPLETION",
            "source_frozen_sample": sample_id,
            "target_epoch": epoch,
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            "generation_response_sha256": response_hash,
            "score_provenance": "PHASE_1E_R4I_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("R4I official scorer did not yield a complete recovered Route-C record.")
        item = {"slot": key, "record": record, "provenance": provenance}
        record_path = slots_dir / f"{safe_name}.recovered.json"
        atomic_json_write(record_path, item)
        append_jsonl(recovered_path, item)
        marker("RECOVERED_RECORD_PERSISTED", key, sequence, generator_completed=True, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=safe_path_reference(record_path))
        completed.append(key)

    try:
        marker("R4I_PRE_EXECUTION_PERSISTED", first_slot, 0, remaining_route_c_order=remaining, remaining_route_c_order_sha256=remaining_hash, recovered_first_route_c_response_sha256=first_hash, max_retries=0)
        generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
        generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            for sequence, key in enumerate(remaining, start=1):
                await run_slot(key, sequence, generator_model, generator_config)
        finally:
            official_scorers._get_judge = original_get_judge
        first_item = json.loads(r4h_record_path.read_text(encoding="utf-8"))
        current_first_hash = sha256_text(first_item["record"]["output"]["completion"])
        if current_first_hash != first_hash:
            raise AssertionError("Recovered first Route-C response changed during R4I.")
        remaining_items = [json.loads(line) for line in recovered_path.read_text(encoding="utf-8").splitlines() if line]
        if len(remaining_items) != 24 or {item["slot"] for item in remaining_items} != set(remaining):
            raise AssertionError("R4I durable recovered records do not equal the frozen 24-slot allowlist.")
        atomic_jsonl_write(combined_path, [first_item] + remaining_items)
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        canonical = assemble_route_c_with_terminal_route_b(combined_path, canonical_path, disposition, phase="PHASE_1E_R4I_CANONICAL_CROSS_DOMAIN_UNIVERSE")
        if canonical["official_scored_record_count"] != 59 or canonical["infrastructure_missing_record_count"] != 1:
            raise AssertionError("R4I canonical universe count invariant failed.")
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4I assembly.")
        route_b_post_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_post_hash != route_b_hash:
            raise AssertionError("Route-B preserved response changed during R4I.")
        status = "PHASE_1E_R4I_PASS / REMAINING_ROUTE_C_RECOVERY_COMPLETE / CROSS_DOMAIN_EXECUTION_CLOSED_WITH_59_SCORED_1_INFRASTRUCTURE_MISSING / READY_FOR_CROSS_DOMAIN_INTEGRITY_ACCEPTANCE"
        assembly_status = "PASS"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=lifecycle.get("target_slot"), route="C", lifecycle_stage=safe_text(lifecycle.get("stage")))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT")
        status = "PHASE_1E_R4I_EXECUTION_INTERRUPTED"
    finally:
        entrypoint_logger.removeHandler(capture)

    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "attempted_slots": attempted, "completed_slots": completed, "registry_warnings": registry_warnings, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4I", "status": status,
        "route_a_fingerprint_before": pre_fingerprint, "route_a_fingerprint_after": post_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "recovered_first_route_c_slot": first_slot, "recovered_first_route_c_response_sha256": first_hash,
        "remaining_route_c_order": remaining, "remaining_route_c_order_sha256": remaining_hash,
        "route_c_slots_attempted": attempted, "route_c_slots_completed": completed,
        "first_failed_slot": lifecycle.get("target_slot") if failure else None,
        "failure_lifecycle_stage": failure.get("lifecycle_stage") if failure else None,
        "api_calls_by_route": api_calls, "recovered_record_count": len(completed),
        "canonical_assembly_status": assembly_status,
        "official_scored_record_count": 59 if assembly_status == "PASS" else 35 + len(completed),
        "infrastructure_missing_record_count": 1,
        "duplicate_slots": 0, "unexpected_slots": 0, "missing_universe_slots": 0 if assembly_status == "PASS" else 24 - len(completed),
        "registry_warning_observations": registry_warnings,
        "original_phase_1e_artifacts_modified": False, "max_retries": 0,
        "custom_semantic_implementation_count": 0, "failure": failure,
        "files": {"manifest": safe_path_reference(manifest_path), "ledger": safe_path_reference(ledger_path), "recovered_records": safe_path_reference(recovered_path), "combined_route_c_records": safe_path_reference(combined_path), "canonical_universe": safe_path_reference(canonical_path), "execution_report": safe_path_reference(markdown_path)},
    }
    write_json_failure_safe(report_path, result)
    try:
        lines = ["# Phase 1E-R4I remaining Route-C execution report", "", f"Status: `{status}`", "", "This report contains execution-integrity facts only; it contains no product score value, PASS count, rate, robust-bound result, or product decision.", "", f"- Remaining frozen target count: `{len(remaining)}`.", f"- Attempted/completed: `{len(attempted)}` / `{len(completed)}`.", f"- Canonical assembly: `{assembly_status}`.", f"- Route-A fingerprint before/after: `{pre_fingerprint}` / `{post_fingerprint}`.", f"- Route-B original response SHA-256: `{route_b_hash}`.", f"- Recovered first Route-C SHA-256: `{first_hash}`.", f"- Registry warning observations: `{len(registry_warnings)}` (non-blocking unless lifecycle evidence shows an effect).", "- No Route A, Route B, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred."]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


async def execute_authorized_remaining_23_route_c_recovery(output_dir: Path) -> dict[str, Any]:
    """Execute only R4J's frozen 23-slot Route-C suffix, stopping on any failure."""
    first_slot = "persistbench_a173ee7a:epoch=3"
    second_slot = "persistbench_aa19c18b:epoch=3"
    expected_first_hash = "1f9e2ab83c55dfd8728dd4b3255b79e358b1a97458326cc580699723807ec05f"
    expected_second_hash = "19b07127e56e47621b7d1a26aa6b828a9cc3deee01b8381e946dffcef2a3e9a6"
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4j").resolve(strict=False)
    output_dir_preexisting = output_dir.exists()
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4j-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4j-execution-ledger.jsonl"
    recovered_path = output_dir / "phase-1e-r4j-recovered-records.jsonl"
    combined_path = output_dir / "phase-1e-r4j-all-route-c-recovered-records.jsonl"
    canonical_path = output_dir / "phase-1e-r4j-canonical-cross-domain-universe.json"
    report_path = output_dir / "phase-1e-r4j-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4J_EXECUTION_REPORT.md"
    slots_dir = output_dir / "recovered-slots"
    r4h_record_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-record.json"
    r4h_integrity_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-integrity-report.json"
    r4i_b_record_path = RECOVERY_ROOT / "r4i-b" / "phase-1e-r4i-b-recovered-record.json"
    r4i_b_report_path = RECOVERY_ROOT / "r4i-b" / "phase-1e-r4i-b-integrity-report.json"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    remaining: list[str] = []
    attempted: list[str] = []
    completed: list[str] = []
    generation_persisted: list[str] = []
    registry_warnings: list[str] = []
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    first_hash: str | None = None
    second_hash: str | None = None
    failure: dict[str, Any] | None = None
    assembly_status = "NOT_RUN"
    lifecycle: dict[str, Any] = {"stage": "R4J_PRECALL_INVARIANTS", "target_slot": None, "route": "C"}

    def marker(event: str, key: str, sequence: int, **details: Any) -> None:
        append_jsonl(ledger_path, {"event": event, "slot": key, "route": "C", "sequence_number": sequence, "timestamp": utc_now(), "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY", **details})

    def failure_marker(event: str, key: str, sequence: int, exc: BaseException) -> None:
        try:
            marker(event, key, sequence, runtime_error=safe_exception_record(exc, target_slot=key, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(marker_exc, target_slot=key, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE")

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4J execution must use the canonical recovery/r4j output directory.")
        if output_dir_preexisting or manifest_path.exists():
            raise AssertionError("R4J history already exists; a batch restart is forbidden.")
        if len(plan.route_c_order) != 25 or tuple(plan.route_c_order[:2]) != (first_slot, second_slot):
            raise AssertionError("Frozen Route-C prefix differs from the two recovered slots.")
        remaining = list(plan.route_c_order[2:])
        if len(remaining) != 23 or len(set(remaining)) != 23:
            raise AssertionError("R4J remaining Route-C allowlist is not exactly 23 unique slots.")
        if set(remaining) & plan.route_a or plan.route_b in remaining or first_slot in remaining or second_slot in remaining:
            raise AssertionError("R4J remaining allowlist intersects a prohibited slot.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        r3 = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence evidence no longer proves zero custom semantics.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        first_item = json.loads(r4h_record_path.read_text(encoding="utf-8"))
        first_integrity = json.loads(r4h_integrity_path.read_text(encoding="utf-8"))
        if first_item.get("slot") != first_slot or not complete_record(first_item.get("record", {})):
            raise AssertionError("First recovered Route-C record is incomplete.")
        first_hash = sha256_text(first_item["record"]["output"]["completion"])
        if first_hash != expected_first_hash or first_integrity.get("generation_response_sha256") != expected_first_hash:
            raise AssertionError("First recovered Route-C protected hash differs.")
        second_item = json.loads(r4i_b_record_path.read_text(encoding="utf-8"))
        second_report = json.loads(r4i_b_report_path.read_text(encoding="utf-8"))
        if second_item.get("slot") != second_slot or not complete_record(second_item.get("record", {})):
            raise AssertionError("Second recovered Route-C record is incomplete.")
        second_hash = sha256_text(second_item["record"]["output"]["completion"])
        if second_hash != expected_second_hash or not second_report.get("preserved_generation_sha_verified"):
            raise AssertionError("Second recovered Route-C protected hash differs.")
        if not second_report.get("recovered_record_present") or second_report.get("judge_replacement_attempt_2_invocations") != 1:
            raise AssertionError("R4I-B official Judge recovery evidence is incomplete.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {"phase": "PHASE_1E_R4J", "status": "PHASE_1E_R4J_BLOCKED", "api_calls_by_route": api_calls, "route_a_fingerprint_before": pre_fingerprint, "route_b_original_response_sha256": route_b_hash, "recovered_first_route_c_response_sha256": first_hash, "recovered_second_route_c_response_sha256": second_hash, "pre_call_failure": safe_exception_record(exc, target_slot=None, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))}
        if not output_dir_preexisting:
            write_json_failure_safe(report_path, blocked)
        return blocked

    remaining_hash = sha256_text(canonical_json(remaining))
    manifest = {
        "phase": "PHASE_1E_R4J", "status": "RUNNING", "started_at": utc_now(),
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "recovered_route_c_slots": [first_slot, second_slot],
        "recovered_first_route_c_response_sha256": first_hash,
        "recovered_second_route_c_response_sha256": second_hash,
        "remaining_route_c_order": remaining, "remaining_route_c_order_sha256": remaining_hash,
        "remaining_route_c_count": 23, "max_retries": 0, "custom_semantic_implementation_count": 0,
        "api_calls": api_calls, "attempted_slots": attempted, "completed_slots": completed,
    }
    write_json_failure_safe(manifest_path, manifest)

    class RegistryWarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "Unexpected exception loading entrypoints" in message:
                registry_warnings.append(safe_text(message))

    capture = RegistryWarningCapture()
    entrypoint_logger = logging.getLogger("inspect_ai._util.entrypoints")
    entrypoint_logger.addHandler(capture)

    async def run_slot(key: str, sequence: int, model: Any, config: GenerateConfig) -> None:
        lifecycle.update({"target_slot": key, "route": "C", "stage": "SLOT_PRECHECK"})
        plan.reject_route_a(key)
        plan.require_route_c(key)
        if key not in remaining or key in {first_slot, second_slot, plan.route_b}:
            raise AssertionError("R4J attempted a forbidden or non-remaining Route-C target.")
        attempted.append(key)
        marker("SLOT_STARTED", key, sequence, generator_started=False, judge_started=False)
        lifecycle["stage"] = "GENERATOR_CALL_ABOUT_TO_ENTER"
        marker("GENERATOR_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_generator"] += 1
        lifecycle["stage"] = "GENERATOR_CALL_ENTERED"
        marker("GENERATOR_CALL_ENTERED", key, sequence, observability_note="APPLICATION_GENERATOR_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
        generation_started_at = time.perf_counter()
        try:
            state = await runner.route_c_official_generate(key, model, config)
        except BaseException as exc:
            lifecycle["stage"] = "GENERATOR_RAISED"
            failure_marker("GENERATOR_RAISED", key, sequence, exc)
            raise
        generation_latency = time.perf_counter() - generation_started_at
        if state.output.empty or not state.output.completion:
            raise AssertionError("R4J generator returned incomplete output.")
        sample_id, epoch = split_slot(key)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("R4J state identity/epoch mismatch after official generation.")
        lifecycle["stage"] = "GENERATION_PERSISTENCE"
        safe_name = key.replace(":", "_").replace("=", "_")
        response_hash = sha256_text(state.output.completion)
        generation_path = slots_dir / f"{safe_name}.generation.json"
        output_payload = serialise_output(state.output)
        atomic_json_write(generation_path, {"slot": key, "sequence_number": sequence, "output": output_payload, "response_sha256": response_hash, "usage": output_payload.get("usage"), "raw_generation_latency_seconds": generation_latency, "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION", "immutable_after_persistence": True})
        generation_persisted.append(key)
        marker("GENERATION_PERSISTED", key, sequence, generator_completed=True, generator_response_sha256=response_hash, raw_generation_latency_seconds=generation_latency, generation_artifact=safe_path_reference(generation_path), generation_provenance="PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION")
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_judge"] += 1
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
        marker("JUDGE_SCORER_CALL_ENTERED", key, sequence, observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
        score_started_at = time.perf_counter()
        sample, _ = runner.sample_for(key)
        try:
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", key, sequence, exc)
            raise
        score_latency = time.perf_counter() - score_started_at
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_R4J", "recovery_route": "C_REMAINING_23_EXACT_SLOT_EXECUTION",
            "source_frozen_sample": sample_id, "target_epoch": epoch,
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            "generation_response_sha256": response_hash,
            "score_provenance": "PHASE_1E_R4J_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("R4J official scorer did not yield a complete recovered record.")
        item = {"slot": key, "record": record, "provenance": provenance}
        record_path = slots_dir / f"{safe_name}.recovered.json"
        atomic_json_write(record_path, item)
        append_jsonl(recovered_path, item)
        marker("RECOVERED_RECORD_PERSISTED", key, sequence, generator_completed=True, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=safe_path_reference(record_path), status="COMPLETE")
        completed.append(key)

    try:
        marker("R4J_PRE_EXECUTION_PERSISTED", first_slot, 0, remaining_route_c_order=remaining, remaining_route_c_order_sha256=remaining_hash, recovered_first_route_c_response_sha256=first_hash, recovered_second_route_c_response_sha256=second_hash, max_retries=0)
        generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
        generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            for sequence, key in enumerate(remaining, start=1):
                await run_slot(key, sequence, generator_model, generator_config)
        finally:
            official_scorers._get_judge = original_get_judge
        recovered_items = [json.loads(line) for line in recovered_path.read_text(encoding="utf-8").splitlines() if line]
        if len(recovered_items) != 23 or {item["slot"] for item in recovered_items} != set(remaining):
            raise AssertionError("R4J durable recovered records do not equal the frozen remaining-23 allowlist.")
        current_first_hash = sha256_text(json.loads(r4h_record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"])
        current_second_hash = sha256_text(json.loads(r4i_b_record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"])
        if current_first_hash != expected_first_hash or current_second_hash != expected_second_hash:
            raise AssertionError("A previously recovered Route-C response changed during R4J.")
        atomic_jsonl_write(combined_path, [first_item, second_item] + recovered_items)
        canonical = assemble_route_c_with_terminal_route_b(combined_path, canonical_path, disposition, phase="PHASE_1E_R4J_CANONICAL_CROSS_DOMAIN_UNIVERSE")
        if canonical["official_scored_record_count"] != 59 or canonical["infrastructure_missing_record_count"] != 1 or len(canonical["records"]) != 60:
            raise AssertionError("R4J canonical universe count invariant failed.")
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4J assembly.")
        route_b_post_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_post_hash != route_b_hash:
            raise AssertionError("Route-B preserved response changed during R4J.")
        status = "PHASE_1E_R4J_PASS / ROUTE_C_RECOVERY_COMPLETE / CROSS_DOMAIN_EXECUTION_CLOSED_WITH_59_SCORED_1_INFRASTRUCTURE_MISSING / READY_FOR_CROSS_DOMAIN_INTEGRITY_ACCEPTANCE"
        assembly_status = "PASS"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=lifecycle.get("target_slot"), route="C", lifecycle_stage=safe_text(lifecycle["stage"]))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT")
        status = "PHASE_1E_R4J_EXECUTION_INTERRUPTED"
        assembly_status = "NOT_RUN_OR_INCOMPLETE"
    finally:
        entrypoint_logger.removeHandler(capture)

    preserved_incomplete = [key for key in generation_persisted if key not in completed]
    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "attempted_slots": attempted, "completed_slots": completed, "preserved_generation_incomplete_slots": preserved_incomplete, "registry_warnings": registry_warnings, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4J", "status": status,
        "route_a_fingerprint_before": pre_fingerprint, "route_a_fingerprint_after": post_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING", "route_b_original_response_sha256": route_b_hash,
        "recovered_first_route_c_response_sha256": first_hash, "recovered_second_route_c_response_sha256": second_hash,
        "remaining_route_c_order": remaining, "remaining_route_c_order_sha256": remaining_hash,
        "route_c_slots_attempted": attempted, "route_c_slots_completed": completed,
        "first_failed_slot": lifecycle.get("target_slot") if failure else None, "failure_lifecycle_stage": failure.get("lifecycle_stage") if failure else None,
        "api_calls_by_route": api_calls, "recovered_records_persisted": len(completed), "preserved_generation_incomplete_slots": preserved_incomplete,
        "canonical_assembly_status": assembly_status, "frozen_universe_count": 60 if assembly_status == "PASS" else 37 + len(completed),
        "official_scored_record_count": 59 if assembly_status == "PASS" else 36 + len(completed), "infrastructure_missing_record_count": 1,
        "duplicate_slots": 0, "unexpected_slots": 0, "missing_universe_slots": 0 if assembly_status == "PASS" else 23 - len(completed),
        "registry_warning_observations": registry_warnings, "original_phase_1e_artifacts_modified": False,
        "max_retries": 0, "custom_semantic_implementation_count": 0, "no_product_metrics_computed": True, "failure": failure,
        "files": {"manifest": safe_path_reference(manifest_path), "ledger": safe_path_reference(ledger_path), "recovered_records": safe_path_reference(recovered_path), "combined_route_c_records": safe_path_reference(combined_path), "canonical_universe": safe_path_reference(canonical_path), "execution_report": safe_path_reference(markdown_path)},
    }
    write_json_failure_safe(report_path, result)
    try:
        lines = ["# Phase 1E-R4J remaining Route-C execution report", "", f"Status: `{status}`", "", "This report contains execution-integrity facts only; it contains no score value, PASS count, rate, robust-bound result, or product conclusion.", "", f"- Remaining frozen target count: `{len(remaining)}`; attempted/completed: `{len(attempted)}` / `{len(completed)}`.", f"- Canonical assembly: `{assembly_status}`. Registry warning observations: `{len(registry_warnings)}`.", f"- Route-A fingerprint before/after: `{pre_fingerprint}` / `{post_fingerprint}`.", "- No Route A, Route B, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred. No commit or push occurred."]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


async def execute_authorized_remaining_19_route_c_recovery(output_dir: Path) -> dict[str, Any]:
    """Execute only R4K's frozen 19-slot Route-C suffix, stopping on any error."""
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4k").resolve(strict=False)
    output_dir_preexisting = output_dir.exists()
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4k-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4k-execution-ledger.jsonl"
    recovered_path = output_dir / "phase-1e-r4k-recovered-records.jsonl"
    combined_path = output_dir / "phase-1e-r4k-all-route-c-recovered-records.jsonl"
    canonical_path = output_dir / "phase-1e-r4k-canonical-cross-domain-universe.json"
    report_path = output_dir / "phase-1e-r4k-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4K_EXECUTION_REPORT.md"
    slots_dir = output_dir / "recovered-slots"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    previous_sources = [
        ("persistbench_a173ee7a:epoch=3", RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-record.json", RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-generation.json"),
        ("persistbench_aa19c18b:epoch=3", RECOVERY_ROOT / "r4i-b" / "phase-1e-r4i-b-recovered-record.json", RECOVERY_ROOT / "r4i" / "recovered-slots" / "persistbench_aa19c18b_epoch_3.generation.json"),
        ("persistbench_19d9b6b0:epoch=3", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_19d9b6b0_epoch_3.recovered.json", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_19d9b6b0_epoch_3.generation.json"),
        ("persistbench_32deee31:epoch=3", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_32deee31_epoch_3.recovered.json", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_32deee31_epoch_3.generation.json"),
        ("persistbench_7554e34a:epoch=3", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_7554e34a_epoch_3.recovered.json", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_7554e34a_epoch_3.generation.json"),
        ("persistbench_ffee2940:epoch=3", RECOVERY_ROOT / "r4j-a" / "phase-1e-r4j-a-recovered-record.json", RECOVERY_ROOT / "r4j" / "recovered-slots" / "persistbench_ffee2940_epoch_3.generation.json"),
    ]
    remaining: list[str] = []
    previous_items: list[dict[str, Any]] = []
    previous_hashes: dict[str, str] = {}
    attempted: list[str] = []
    completed: list[str] = []
    generation_persisted: list[str] = []
    registry_warnings: list[str] = []
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    failure: dict[str, Any] | None = None
    assembly_status = "NOT_RUN"
    lifecycle: dict[str, Any] = {"stage": "R4K_PRECALL_INVARIANTS", "target_slot": None, "route": "C"}

    def marker(event: str, key: str, sequence: int, **details: Any) -> None:
        append_jsonl(ledger_path, {"event": event, "slot": key, "route": "C", "sequence_number": sequence, "timestamp": utc_now(), "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY", **details})

    def failure_marker(event: str, key: str, sequence: int, exc: BaseException) -> None:
        try:
            marker(event, key, sequence, runtime_error=safe_exception_record(exc, target_slot=key, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(marker_exc, target_slot=key, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE")

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4K execution must use the canonical recovery/r4k output directory.")
        if output_dir_preexisting or manifest_path.exists():
            raise AssertionError("R4K history already exists; a batch restart is forbidden.")
        if len(plan.route_c_order) != 25:
            raise AssertionError("Frozen Route-C allowlist does not contain 25 slots.")
        recovered_slots = [item[0] for item in previous_sources]
        if recovered_slots != list(plan.route_c_order[:6]) or len(set(recovered_slots)) != 6:
            raise AssertionError("The six recovered Route-C slots do not match the frozen ordered prefix.")
        remaining = list(plan.route_c_order[6:])
        if len(remaining) != 19 or len(set(remaining)) != 19 or set(remaining) & set(recovered_slots) or set(remaining) & plan.route_a or plan.route_b in remaining:
            raise AssertionError("R4K remaining Route-C allowlist is not exactly the untouched 19-slot set.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        r3 = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence evidence no longer proves zero custom semantics.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        for key, record_path, generation_path in previous_sources:
            item = json.loads(record_path.read_text(encoding="utf-8"))
            generation = json.loads(generation_path.read_text(encoding="utf-8"))
            if item.get("slot") != key or not complete_record(item.get("record", {})) or generation.get("slot") != key:
                raise AssertionError(f"Recovered Route-C asset is incomplete or mismatched: {key}")
            artifact_hash = sha256_text(ModelOutput.model_validate(generation.get("output")).completion)
            record_hash = sha256_text(item["record"]["output"]["completion"])
            if artifact_hash != generation.get("response_sha256") or record_hash != artifact_hash:
                raise AssertionError(f"Recovered Route-C generation hash differs from its recovered record: {key}")
            previous_items.append(item)
            previous_hashes[key] = artifact_hash
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {"phase": "PHASE_1E_R4K", "status": "PHASE_1E_R4K_BLOCKED", "api_calls_by_route": api_calls, "route_a_fingerprint_before": pre_fingerprint, "route_b_original_response_sha256": route_b_hash, "pre_call_failure": safe_exception_record(exc, target_slot=None, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))}
        if not output_dir_preexisting:
            write_json_failure_safe(report_path, blocked)
        return blocked

    remaining_hash = sha256_text(canonical_json(remaining))
    manifest = {"phase": "PHASE_1E_R4K", "status": "RUNNING", "started_at": utc_now(), "route_a_fingerprint_before": pre_fingerprint, "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING", "route_b_original_response_sha256": route_b_hash, "recovered_route_c_slots": recovered_slots, "recovered_route_c_generation_sha256": previous_hashes, "remaining_route_c_order": remaining, "remaining_route_c_order_sha256": remaining_hash, "remaining_route_c_count": 19, "max_retries": 0, "custom_semantic_implementation_count": 0, "api_calls": api_calls, "attempted_slots": attempted, "completed_slots": completed}
    write_json_failure_safe(manifest_path, manifest)

    class RegistryWarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "Unexpected exception loading entrypoints" in message:
                registry_warnings.append(safe_text(message))
    capture = RegistryWarningCapture()
    entrypoint_logger = logging.getLogger("inspect_ai._util.entrypoints")
    entrypoint_logger.addHandler(capture)

    async def run_slot(key: str, sequence: int, model: Any, config: GenerateConfig) -> None:
        lifecycle.update({"target_slot": key, "route": "C", "stage": "SLOT_PRECHECK"})
        plan.reject_route_a(key)
        plan.require_route_c(key)
        if key not in remaining or key in recovered_slots or key == plan.route_b:
            raise AssertionError("R4K attempted a forbidden/non-remaining Route-C target.")
        attempted.append(key)
        marker("SLOT_STARTED", key, sequence, generator_started=False, judge_started=False)
        lifecycle["stage"] = "GENERATOR_CALL_ABOUT_TO_ENTER"
        marker("GENERATOR_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_generator"] += 1
        lifecycle["stage"] = "GENERATOR_CALL_ENTERED"
        marker("GENERATOR_CALL_ENTERED", key, sequence, observability_note="APPLICATION_GENERATOR_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
        started_at = time.perf_counter()
        try:
            state = await runner.route_c_official_generate(key, model, config)
        except BaseException as exc:
            lifecycle["stage"] = "GENERATOR_RAISED"
            failure_marker("GENERATOR_RAISED", key, sequence, exc)
            raise
        generation_latency = time.perf_counter() - started_at
        if state.output.empty or not state.output.completion:
            raise AssertionError("R4K generator returned incomplete output.")
        sample_id, epoch = split_slot(key)
        if str(state.sample_id) != sample_id or state.epoch != epoch:
            raise AssertionError("R4K state identity/epoch mismatch after official generation.")
        lifecycle["stage"] = "GENERATION_PERSISTENCE"
        safe_name = key.replace(":", "_").replace("=", "_")
        response_hash = sha256_text(state.output.completion)
        generation_path = slots_dir / f"{safe_name}.generation.json"
        output_payload = serialise_output(state.output)
        atomic_json_write(generation_path, {"slot": key, "sequence_number": sequence, "output": output_payload, "response_sha256": response_hash, "usage": output_payload.get("usage"), "raw_generation_latency_seconds": generation_latency, "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION", "immutable_after_persistence": True})
        generation_persisted.append(key)
        marker("GENERATION_PERSISTED", key, sequence, generator_completed=True, generator_response_sha256=response_hash, raw_generation_latency_seconds=generation_latency, generation_artifact=safe_path_reference(generation_path), generation_provenance="PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION")
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER", key, sequence)
        api_calls["route_c_judge"] += 1
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
        marker("JUDGE_SCORER_CALL_ENTERED", key, sequence, observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
        score_started = time.perf_counter()
        sample, _ = runner.sample_for(key)
        try:
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", key, sequence, exc)
            raise
        score_latency = time.perf_counter() - score_started
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {"recovery_phase": "PHASE_1E_R4K", "recovery_route": "C_REMAINING_19_EXACT_SLOT_EXECUTION", "source_frozen_sample": sample_id, "target_epoch": epoch, "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"], "generator_config": plan.protocol["frozen_treatment"]["generator"], "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"], "scorer_identity": plan.protocol["frozen_treatment"]["scorer"], "original_artifact_path": safe_path_reference(SOURCE_LOG), "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION", "generation_response_sha256": response_hash, "score_provenance": "PHASE_1E_R4K_OFFICIAL_PERSISTBENCH_SCORER"}
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("R4K official scorer did not yield a complete recovered record.")
        item = {"slot": key, "record": record, "provenance": provenance}
        record_path = slots_dir / f"{safe_name}.recovered.json"
        atomic_json_write(record_path, item)
        append_jsonl(recovered_path, item)
        marker("RECOVERED_RECORD_PERSISTED", key, sequence, generator_completed=True, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=safe_path_reference(record_path), status="COMPLETE")
        completed.append(key)

    try:
        marker("R4K_PRE_EXECUTION_PERSISTED", recovered_slots[0], 0, remaining_route_c_order=remaining, remaining_route_c_order_sha256=remaining_hash, recovered_route_c_generation_sha256=previous_hashes, max_retries=0)
        generator_config = GenerateConfig(max_retries=0, max_connections=1, adaptive_connections=False)
        generator_model = get_model(plan.protocol["frozen_treatment"]["generator"], config=generator_config)
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            for sequence, key in enumerate(remaining, start=1):
                await run_slot(key, sequence, generator_model, generator_config)
        finally:
            official_scorers._get_judge = original_get_judge
        recovered_items = [json.loads(line) for line in recovered_path.read_text(encoding="utf-8").splitlines() if line]
        if len(recovered_items) != 19 or {item["slot"] for item in recovered_items} != set(remaining):
            raise AssertionError("R4K durable recovered records do not equal the frozen remaining-19 allowlist.")
        for key, record_path, generation_path in previous_sources:
            if sha256_text(json.loads(record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"]) != previous_hashes[key] or sha256_text(ModelOutput.model_validate(json.loads(generation_path.read_text(encoding="utf-8"))["output"]).completion) != previous_hashes[key]:
                raise AssertionError(f"Previously recovered Route-C generation changed during R4K: {key}")
        atomic_jsonl_write(combined_path, previous_items + recovered_items)
        canonical = assemble_route_c_with_terminal_route_b(combined_path, canonical_path, disposition, phase="PHASE_1E_R4K_CANONICAL_CROSS_DOMAIN_UNIVERSE")
        if canonical["official_scored_record_count"] != 59 or canonical["infrastructure_missing_record_count"] != 1 or len(canonical["records"]) != 60:
            raise AssertionError("R4K canonical universe count invariant failed.")
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash or sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"]) != route_b_hash:
            raise AssertionError("Route-A fingerprint or Route-B response changed during R4K.")
        status = "PHASE_1E_R4K_PASS / ROUTE_C_RECOVERY_COMPLETE / CROSS_DOMAIN_EXECUTION_CLOSED_WITH_59_SCORED_1_INFRASTRUCTURE_MISSING / READY_FOR_CROSS_DOMAIN_INTEGRITY_ACCEPTANCE"
        assembly_status = "PASS"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=lifecycle.get("target_slot"), route="C", lifecycle_stage=safe_text(lifecycle["stage"]))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT")
        status = "PHASE_1E_R4K_EXECUTION_INTERRUPTED"
        assembly_status = "NOT_RUN_OR_INCOMPLETE"
    finally:
        entrypoint_logger.removeHandler(capture)

    preserved_incomplete = [key for key in generation_persisted if key not in completed]
    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "attempted_slots": attempted, "completed_slots": completed, "preserved_generation_incomplete_slots": preserved_incomplete, "registry_warnings": registry_warnings, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {"phase": "PHASE_1E_R4K", "status": status, "route_a_fingerprint_before": pre_fingerprint, "route_a_fingerprint_after": post_fingerprint, "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING", "route_b_original_response_sha256": route_b_hash, "recovered_route_c_generation_sha256": previous_hashes, "remaining_route_c_order": remaining, "remaining_route_c_order_sha256": remaining_hash, "route_c_slots_attempted": attempted, "route_c_slots_completed": completed, "first_failed_slot": lifecycle.get("target_slot") if failure else None, "failure_lifecycle_stage": failure.get("lifecycle_stage") if failure else None, "api_calls_by_route": api_calls, "recovered_records_persisted": len(completed), "preserved_generation_incomplete_slots": preserved_incomplete, "canonical_assembly_status": assembly_status, "frozen_universe_count": 60 if assembly_status == "PASS" else 41 + len(completed), "official_scored_record_count": 59 if assembly_status == "PASS" else 40 + len(completed), "infrastructure_missing_record_count": 1, "duplicate_slots": 0, "unexpected_slots": 0, "missing_universe_slots": 0 if assembly_status == "PASS" else 19 - len(completed), "registry_warning_observations": registry_warnings, "original_phase_1e_artifacts_modified": False, "max_retries": 0, "custom_semantic_implementation_count": 0, "no_product_metrics_computed": True, "failure": failure, "files": {"manifest": safe_path_reference(manifest_path), "ledger": safe_path_reference(ledger_path), "recovered_records": safe_path_reference(recovered_path), "combined_route_c_records": safe_path_reference(combined_path), "canonical_universe": safe_path_reference(canonical_path), "execution_report": safe_path_reference(markdown_path)}}
    write_json_failure_safe(report_path, result)
    try:
        lines = ["# Phase 1E-R4K remaining Route-C execution report", "", f"Status: `{status}`", "", "This report records execution integrity only and contains no score value, PASS count, rate, robust-bound result, or product conclusion.", "", f"- Remaining frozen target count: `{len(remaining)}`; attempted/completed: `{len(attempted)}` / `{len(completed)}`.", f"- Canonical assembly: `{assembly_status}`. Registry warning observations: `{len(registry_warnings)}`.", f"- Route-A fingerprint before/after: `{pre_fingerprint}` / `{post_fingerprint}`.", "- No Route A, Route B, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred. No commit or push occurred."]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


async def execute_authorized_r4j_preserved_generation_judge_replacement(output_dir: Path) -> dict[str, Any]:
    """Run the single authorized R4J-A Judge-only replacement after a provider 429."""
    target = "persistbench_ffee2940:epoch=3"
    expected_target_hash = "a8c44773e3aad1563e1253350235a48d482cb2843476e4b2d223372f30df587e"
    expected_first_hash = "1f9e2ab83c55dfd8728dd4b3255b79e358b1a97458326cc580699723807ec05f"
    expected_second_hash = "19b07127e56e47621b7d1a26aa6b828a9cc3deee01b8381e946dffcef2a3e9a6"
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4j-a").resolve(strict=False)
    output_dir_preexisting = output_dir.exists()
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4j-a-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4j-a-execution-ledger.jsonl"
    record_path = output_dir / "phase-1e-r4j-a-recovered-record.json"
    report_path = output_dir / "phase-1e-r4j-a-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4J_A_EXECUTION_REPORT.md"
    r4j_dir = RECOVERY_ROOT / "r4j"
    generation_path = r4j_dir / "recovered-slots" / "persistbench_ffee2940_epoch_3.generation.json"
    r4j_manifest_path = r4j_dir / "phase-1e-r4j-execution-manifest.json"
    r4j_ledger_path = r4j_dir / "phase-1e-r4j-execution-ledger.jsonl"
    r4j_report_path = r4j_dir / "phase-1e-r4j-integrity-report.json"
    r4h_record_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-record.json"
    r4i_b_record_path = RECOVERY_ROOT / "r4i-b" / "phase-1e-r4i-b-recovered-record.json"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    attempt_1 = {"state": "JUDGE_ATTEMPT_1_CONFIRMED_PROVIDER_RATE_LIMIT_NO_SCORE", "http_status": 429, "provider_path": "OpenRouter", "upstream_provider": "Google", "limit_source": "upstream_provider_shared_pool", "classification": "temporary_upstream_rate_limit", "exception_chain": "RetryError / RateLimitError", "official_score_exists": False, "recovered_record_exists": False, "classification_not_pass_or_fail": True}
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    first_hash: str | None = None
    second_hash: str | None = None
    target_hash: str | None = None
    remaining_untouched: int | None = None
    failure: dict[str, Any] | None = None
    judge_scorer_completed = False
    official_score_present = False
    recovered_record_present = False
    authorization_consumed = False
    selected_completion_nonempty: bool | None = None
    lifecycle: dict[str, Any] = {"stage": "R4J_A_PRECALL_INVARIANTS", "target_slot": target, "route": "C"}

    def marker(event: str, **details: Any) -> None:
        append_jsonl(ledger_path, {"event": event, "slot": target, "route": "C", "attempt_number": 2, "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2_AFTER_CONFIRMED_PROVIDER_429", "timestamp": utc_now(), "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY", **details})

    def failure_marker(event: str, exc: BaseException) -> None:
        try:
            marker(event, runtime_error=safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(marker_exc, target_slot=target, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE")

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4J-A must use the canonical recovery/r4j-a output directory.")
        if output_dir_preexisting or manifest_path.exists():
            raise AssertionError("R4J-A Judge Attempt 2 history already exists; a further attempt is forbidden.")
        if len(plan.route_c_order) != 25 or plan.route_c_order[5] != target:
            raise AssertionError("R4J-A target does not occupy its frozen Route-C position.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        r3 = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence evidence no longer proves zero custom semantics.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        r4j_manifest = json.loads(r4j_manifest_path.read_text(encoding="utf-8"))
        r4j_report = json.loads(r4j_report_path.read_text(encoding="utf-8"))
        if r4j_manifest.get("status") != "PHASE_1E_R4J_EXECUTION_INTERRUPTED":
            raise AssertionError("R4J is not in the frozen interruption state.")
        if r4j_manifest.get("attempted_slots") != list(plan.route_c_order[2:6]) or r4j_manifest.get("completed_slots") != list(plan.route_c_order[2:5]):
            raise AssertionError("R4J completed/attempted topology differs from the 429 interruption.")
        if r4j_manifest.get("preserved_generation_incomplete_slots") != [target] or r4j_manifest.get("api_calls", {}).get("route_c_generator") != 4 or r4j_manifest.get("api_calls", {}).get("route_c_judge") != 4:
            raise AssertionError("R4J preservation or provider-call topology differs.")
        if r4j_report.get("failure_lifecycle_stage") != "JUDGE_SCORER_RAISED" or r4j_report.get("failure", {}).get("exception_class") != "RetryError":
            raise AssertionError("R4J failure is not the required Judge provider rate-limit interruption.")
        r4j_events = [json.loads(line) for line in r4j_ledger_path.read_text(encoding="utf-8").splitlines() if line]
        if sum(event.get("event") == "JUDGE_SCORER_CALL_ENTERED" and event.get("slot") == target for event in r4j_events) != 1 or not any(event.get("event") == "JUDGE_SCORER_RAISED" and event.get("slot") == target for event in r4j_events):
            raise AssertionError("R4J Judge Attempt 1 lifecycle evidence is incomplete.")
        first_item = json.loads(r4h_record_path.read_text(encoding="utf-8"))
        second_item = json.loads(r4i_b_record_path.read_text(encoding="utf-8"))
        if first_item.get("slot") != plan.route_c_order[0] or not complete_record(first_item.get("record", {})) or second_item.get("slot") != plan.route_c_order[1] or not complete_record(second_item.get("record", {})):
            raise AssertionError("Previously recovered Route-C records are incomplete.")
        first_hash = sha256_text(first_item["record"]["output"]["completion"])
        second_hash = sha256_text(second_item["record"]["output"]["completion"])
        if first_hash != expected_first_hash or second_hash != expected_second_hash:
            raise AssertionError("Previously recovered Route-C protected hash differs.")
        generation_asset = json.loads(generation_path.read_text(encoding="utf-8"))
        if generation_asset.get("slot") != target or generation_asset.get("generation_provenance") != "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION" or not generation_asset.get("immutable_after_persistence"):
            raise AssertionError("R4J preserved generation identity/provenance differs.")
        preserved_output = ModelOutput.model_validate(generation_asset.get("output"))
        if canonical_json(serialise_output(preserved_output)) != canonical_json(generation_asset.get("output")):
            raise AssertionError("Preserved ModelOutput cannot be reconstructed unchanged.")
        target_hash = sha256_text(preserved_output.completion)
        if target_hash != expected_target_hash or generation_asset.get("response_sha256") != expected_target_hash:
            raise AssertionError("Preserved R4J generation response hash mismatch before Judge entry.")
        remaining_untouched = len(plan.route_c_order[6:])
        if remaining_untouched != 19:
            raise AssertionError("R4J remaining untouched Route-C count is not 19.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {"phase": "PHASE_1E_R4J_A", "status": "PHASE_1E_R4J_A_BLOCKED", "target_slot": target, "api_calls_by_route": api_calls, "judge_attempt_2_authorization_consumed": False, "route_a_fingerprint_before": pre_fingerprint, "route_b_original_response_sha256": route_b_hash, "recovered_first_route_c_response_sha256": first_hash, "recovered_second_route_c_response_sha256": second_hash, "preserved_generation_response_sha256": target_hash, "pre_call_failure": safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))}
        if not output_dir_preexisting:
            write_json_failure_safe(report_path, blocked)
        return blocked

    manifest = {"phase": "PHASE_1E_R4J_A", "status": "RUNNING", "target_slot": target, "route": "C", "started_at": utc_now(), "attempt_number": 2, "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2_AFTER_CONFIRMED_PROVIDER_429", "prior_judge_attempt": attempt_1, "preserved_generation": {"source_artifact": safe_path_reference(generation_path), "response_sha256": target_hash, "reused_without_normalization": True, "generator_calls": 0}, "maximum_additional_judge_attempts_after_this": 0, "max_retries": 0, "route_a_fingerprint_before": pre_fingerprint, "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING", "route_b_original_response_sha256": route_b_hash, "recovered_first_route_c_response_sha256": first_hash, "recovered_second_route_c_response_sha256": second_hash, "remaining_route_c_slots_untouched_before": remaining_untouched, "api_calls": api_calls}
    write_json_failure_safe(manifest_path, manifest)

    try:
        lifecycle["stage"] = "AUTHORIZED_JUDGE_REPLACEMENT_PRE_ATTEMPT_PERSISTED"
        marker("AUTHORIZED_JUDGE_REPLACEMENT_PRE_ATTEMPT_PERSISTED", prior_judge_attempt=attempt_1, preserved_generation_response_sha256=target_hash, generator_calls=0, max_retries=0, maximum_additional_judge_attempts_after_this=0)
        authorization_consumed = True
        async def preserve_without_generation(state: TaskState, **_: Any) -> TaskState:
            state.output = preserved_output
            return state
        lifecycle["stage"] = "PRESERVED_GENERATION_STATE_RECONSTRUCTION"
        state = await runner.prepare_with_official_solver(target, preserve_without_generation)
        sample_id, epoch = split_slot(target)
        if str(state.sample_id) != sample_id or state.epoch != epoch or state.output is None:
            raise AssertionError("Reconstructed scorer state differs from the frozen target.")
        if canonical_json(serialise_output(state.output)) != canonical_json(generation_asset["output"]) or sha256_text(state.output.completion) != expected_target_hash:
            raise AssertionError("Official-solver reconstruction changed the preserved generation.")
        marker("PRESERVED_GENERATION_SHA_VERIFIED", response_sha256=expected_target_hash, generator_calls=0)
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER", scorer="persistbench_judge", judge=plan.protocol["frozen_treatment"]["judge"], max_retries=0)
        api_calls["route_c_judge"] += 1
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
            marker("JUDGE_SCORER_CALL_ENTERED", observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
            score_started_at = time.perf_counter()
            sample, _ = runner.sample_for(target)
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", exc)
            raise
        finally:
            official_scorers._get_judge = original_get_judge
        score_latency = time.perf_counter() - score_started_at
        judge_scorer_completed = True
        official_score_present = score is not None
        if not official_score_present:
            raise AssertionError("Official PersistBench scorer returned no Score.")
        selected_completion_nonempty = True
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {"recovery_phase": "PHASE_1E_R4J_A", "recovery_route": "C_PRESERVED_GENERATION_JUDGE_REPLACEMENT_AFTER_PROVIDER_429", "attempt_number": 2, "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2_AFTER_CONFIRMED_PROVIDER_429", "prior_judge_attempt": attempt_1, "source_frozen_sample": sample_id, "target_epoch": epoch, "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"], "generator_config": plan.protocol["frozen_treatment"]["generator"], "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"], "scorer_identity": plan.protocol["frozen_treatment"]["scorer"], "original_artifact_path": safe_path_reference(SOURCE_LOG), "preserved_generation_source_artifact": safe_path_reference(generation_path), "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION", "generation_response_sha256": expected_target_hash, "score_provenance": "PHASE_1E_R4J_A_OFFICIAL_PERSISTBENCH_SCORER"}
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("Official scorer did not yield a complete recovered record.")
        atomic_json_write(record_path, {"slot": target, "record": record, "provenance": provenance})
        recovered_record_present = True
        marker("RECOVERED_RECORD_PERSISTED", generator_calls=0, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=safe_path_reference(record_path))
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4J-A.")
        if sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"]) != route_b_hash:
            raise AssertionError("Route-B preserved response changed during R4J-A.")
        if sha256_text(json.loads(r4h_record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"]) != expected_first_hash or sha256_text(json.loads(r4i_b_record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"]) != expected_second_hash or sha256_text(ModelOutput.model_validate(json.loads(generation_path.read_text(encoding="utf-8"))["output"]).completion) != expected_target_hash:
            raise AssertionError("A protected recovered/preserved Route-C generation changed during R4J-A.")
        status = "PHASE_1E_R4J_A_PASS / PRESERVED_GENERATION_JUDGE_RECOVERED / PROVIDER_429_REPLACEMENT_CONSUMED / READY_FOR_REMAINING_19_ROUTE_C_AUTHORIZATION"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT")
        status = "PHASE_1E_R4J_A_JUDGE_REPLACEMENT_FAILED / PRESERVED_GENERATION_UNSCORED_JUDGE_RECOVERY_EXHAUSTED"

    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "judge_attempt_2_authorization_consumed": authorization_consumed, "judge_scorer_completed": judge_scorer_completed, "selected_result_completion_nonempty": selected_completion_nonempty, "official_score_present": official_score_present, "recovered_record_present": recovered_record_present, "remaining_route_c_slots_untouched_after": remaining_untouched, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {"phase": "PHASE_1E_R4J_A", "status": status, "target_slot": target, "preserved_generation_response_sha256": expected_target_hash, "preserved_generation_sha_verified": target_hash == expected_target_hash, "generator_calls": api_calls["route_c_generator"], "judge_replacement_attempt_2_invocations": api_calls["route_c_judge"], "judge_attempt_1_429_provenance": attempt_1, "judge_scorer_completed": judge_scorer_completed, "selected_result_completion_nonempty": selected_completion_nonempty, "official_parser_result": "SUCCESS" if judge_scorer_completed else "FAILED_OR_NOT_COMPLETED", "official_score_present": official_score_present, "recovered_record_present": recovered_record_present, "judge_attempt_2_authorization_consumed": authorization_consumed, "further_judge_attempt_authorized": False, "remaining_route_c_slots_untouched": remaining_untouched, "route_a_fingerprint_before": pre_fingerprint, "route_a_fingerprint_after": post_fingerprint, "route_b_original_response_sha256": route_b_hash, "recovered_first_route_c_response_sha256": first_hash, "recovered_second_route_c_response_sha256": second_hash, "api_calls_by_route": api_calls, "max_retries": 0, "custom_semantic_implementation_count": 0, "no_product_metrics_computed": True, "failure": failure, "files": {"manifest": safe_path_reference(manifest_path), "ledger": safe_path_reference(ledger_path), "recovered_record": safe_path_reference(record_path), "integrity_report": safe_path_reference(report_path), "execution_report": safe_path_reference(markdown_path)}}
    write_json_failure_safe(report_path, result)
    try:
        lines = ["# Phase 1E-R4J-A preserved-generation Judge replacement report", "", f"Status: `{status}`", "", "This report records execution integrity only and deliberately omits all score values and product metrics.", "", f"- Target: `{target}`; preserved generation SHA-256 verified: `{result['preserved_generation_sha_verified']}`.", f"- Generator calls: `{api_calls['route_c_generator']}`; Judge Attempt 2 invocations: `{api_calls['route_c_judge']}`.", "- Judge Attempt 1 provenance: `JUDGE_ATTEMPT_1_CONFIRMED_PROVIDER_RATE_LIMIT_NO_SCORE` (not a PASS/FAIL outcome).", f"- Official parser result: `{result['official_parser_result']}`; official score present: `{official_score_present}`; recovered record present: `{recovered_record_present}`.", f"- Further Judge attempt authorized: `False`; remaining Route-C slots untouched: `{remaining_untouched}`.", "- No Route A, Route B, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred. No commit or push occurred."]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


async def execute_authorized_preserved_generation_judge_replacement(output_dir: Path) -> dict[str, Any]:
    """Run the one explicitly authorized Judge-only R4I-B replacement.

    The already persisted R4I generation is deserialized verbatim and supplied
    through the unmodified official solver, then the unmodified official
    PersistBench scorer is invoked once.  This function never instantiates or
    calls a generator model.
    """
    target = "persistbench_aa19c18b:epoch=3"
    expected_generation_hash = "19b07127e56e47621b7d1a26aa6b828a9cc3deee01b8381e946dffcef2a3e9a6"
    expected_r4h_hash = "1f9e2ab83c55dfd8728dd4b3255b79e358b1a97458326cc580699723807ec05f"
    output_dir = output_dir.resolve(strict=False)
    canonical_output_dir = (RECOVERY_ROOT / "r4i-b").resolve(strict=False)
    output_dir_preexisting = output_dir.exists()
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    manifest_path = output_dir / "phase-1e-r4i-b-execution-manifest.json"
    ledger_path = output_dir / "phase-1e-r4i-b-execution-ledger.jsonl"
    record_path = output_dir / "phase-1e-r4i-b-recovered-record.json"
    report_path = output_dir / "phase-1e-r4i-b-integrity-report.json"
    markdown_path = output_dir / "PHASE_1E_R4I_B_EXECUTION_REPORT.md"
    r4i_dir = RECOVERY_ROOT / "r4i"
    generation_path = r4i_dir / "recovered-slots" / "persistbench_aa19c18b_epoch_3.generation.json"
    disposition_path = r4i_dir / "phase-1e-r4i-preserved-generation-disposition.json"
    r4i_manifest_path = r4i_dir / "phase-1e-r4i-execution-manifest.json"
    r4i_ledger_path = r4i_dir / "phase-1e-r4i-execution-ledger.jsonl"
    r4i_a_path = RECOVERY_ROOT / "r4i-a" / "phase-1e-r4i-a-judge-parse-forensic.json"
    r4h_record_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-first-slot-record.json"
    r4h_integrity_path = RECOVERY_ROOT / "r4h" / "phase-1e-r4h-integrity-report.json"
    r3_report_path = RECOVERY_ROOT / "r4e-pre-execution-dry-run.json"
    api_calls = {"route_c_generator": 0, "route_c_judge": 0, "route_b_generator": 0, "route_b_judge": 0}
    attempt_1 = {
        "state": "JUDGE_ATTEMPT_1_NONRECOVERABLE_EMPTY_COMPLETION",
        "scorer_judge_lifecycle_entered": True,
        "selected_result_completion": "EMPTY_AFTER_STRIP",
        "official_parser_failed": True,
        "raw_judge_semantic_asset_survives": False,
        "official_score_exists": False,
        "classification_not_pass_or_fail": True,
    }
    pre_fingerprint: str | None = None
    post_fingerprint: str | None = None
    route_b_hash: str | None = None
    r4h_hash: str | None = None
    preserved_hash: str | None = None
    remaining_untouched: int | None = None
    failure: dict[str, Any] | None = None
    official_score_present = False
    recovered_record_present = False
    judge_scorer_completed = False
    authorization_consumed = False
    selected_completion_nonempty: bool | None = None
    lifecycle: dict[str, Any] = {"stage": "R4I_B_PRECALL_INVARIANTS", "target_slot": target, "route": "C"}

    def marker(event: str, **details: Any) -> None:
        append_jsonl(
            ledger_path,
            {
                "event": event,
                "slot": target,
                "route": "C",
                "attempt_number": 2,
                "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2",
                "timestamp": utc_now(),
                "lifecycle_scope": "APPLICATION_ORCHESTRATION_ONLY",
                **details,
            },
        )

    def failure_marker(event: str, exc: BaseException) -> None:
        try:
            marker(event, runtime_error=safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=event))
        except BaseException as marker_exc:
            lifecycle["marker_persistence_error"] = safe_exception_record(
                marker_exc, target_slot=target, route="C", lifecycle_stage=f"{event}_MARKER_PERSISTENCE"
            )

    try:
        if output_dir != canonical_output_dir:
            raise AssertionError("R4I-B must use the canonical recovery/r4i-b output directory.")
        if output_dir_preexisting or manifest_path.exists():
            raise AssertionError("R4I-B Judge Attempt 2 history already exists; a further attempt is forbidden.")
        plan.reject_route_a(target)
        plan.require_route_c(target)
        if len(plan.route_c_order) != 25 or plan.route_c_order[1] != target:
            raise AssertionError("R4I-B target is not the frozen second Route-C slot.")
        if plan.protocol["frozen_retry_semantics"]["max_retries"] != 0:
            raise AssertionError("Frozen retry policy changed from max_retries=0.")
        r3 = json.loads(r3_report_path.read_text(encoding="utf-8"))
        if r3["component_map"]["custom_semantic_implementation_count"] != 0:
            raise AssertionError("R3 semantic-equivalence evidence no longer proves zero custom semantics.")
        disposition = json.loads(R4D_DISPOSITION_PATH.read_text(encoding="utf-8"))
        if disposition["route_b"]["terminal_state"] != "ROUTE_B_RECOVERY_EXHAUSTED":
            raise AssertionError("Route-B terminal disposition is not frozen.")
        r4h_item = json.loads(r4h_record_path.read_text(encoding="utf-8"))
        r4h_integrity = json.loads(r4h_integrity_path.read_text(encoding="utf-8"))
        if r4h_item.get("slot") != plan.route_c_order[0] or not complete_record(r4h_item.get("record", {})):
            raise AssertionError("R4H recovered Route-C record is incomplete.")
        r4h_hash = sha256_text(r4h_item["record"]["output"]["completion"])
        if r4h_hash != expected_r4h_hash or r4h_integrity.get("generation_response_sha256") != expected_r4h_hash:
            raise AssertionError("R4H protected response hash differs.")
        r4i_manifest = json.loads(r4i_manifest_path.read_text(encoding="utf-8"))
        if r4i_manifest.get("status") != "PHASE_1E_R4I_EXECUTION_INTERRUPTED":
            raise AssertionError("R4I status is not the frozen downstream interruption.")
        if r4i_manifest.get("attempted_slots") != [target] or r4i_manifest.get("completed_slots") != []:
            raise AssertionError("R4I attempted/completed topology differs from the preserved-generation state.")
        if r4i_manifest.get("api_calls", {}).get("route_c_generator") != 1 or r4i_manifest.get("api_calls", {}).get("route_c_judge") != 1:
            raise AssertionError("R4I historical provider-call topology differs.")
        r4i_events = [json.loads(line) for line in r4i_ledger_path.read_text(encoding="utf-8").splitlines() if line]
        if [event.get("event") for event in r4i_events][-3:] != ["GENERATION_PERSISTED", "JUDGE_SCORER_CALL_ABOUT_TO_ENTER", "JUDGE_SCORER_CALL_ENTERED", "JUDGE_SCORER_RAISED"][-3:]:
            raise AssertionError("R4I Judge Attempt 1 lifecycle tail is not intact.")
        if sum(event.get("event") == "JUDGE_SCORER_CALL_ENTERED" and event.get("slot") == target for event in r4i_events) != 1:
            raise AssertionError("R4I does not contain exactly one historical Judge lifecycle entry for the target.")
        preserved_disposition = json.loads(disposition_path.read_text(encoding="utf-8"))
        if preserved_disposition.get("state") != "PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE":
            raise AssertionError("Preserved-generation disposition is not frozen.")
        if preserved_disposition.get("generation", {}).get("response_sha256") != expected_generation_hash:
            raise AssertionError("Preserved-generation disposition hash differs.")
        if preserved_disposition.get("downstream_evaluation", {}).get("failure_stage") != "JUDGE_SCORER_RAISED":
            raise AssertionError("Preserved-generation downstream failure stage differs.")
        r4i_a = json.loads(r4i_a_path.read_text(encoding="utf-8"))
        if not r4i_a.get("status", "").startswith("PHASE_1E_R4I_A_FORENSIC_PASS / JUDGE_OUTPUT_NOT_RECOVERABLE"):
            raise AssertionError("R4I-A forensic disposition is not the required nonrecoverable state.")
        generation_asset = json.loads(generation_path.read_text(encoding="utf-8"))
        if generation_asset.get("slot") != target or generation_asset.get("generation_provenance") != "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION":
            raise AssertionError("Preserved generation identity/provenance differs.")
        preserved_output = ModelOutput.model_validate(generation_asset.get("output"))
        if canonical_json(serialise_output(preserved_output)) != canonical_json(generation_asset.get("output")):
            raise AssertionError("Preserved ModelOutput cannot be reconstructed byte-for-byte at the semantic artifact boundary.")
        preserved_hash = sha256_text(preserved_output.completion)
        if preserved_hash != expected_generation_hash or generation_asset.get("response_sha256") != expected_generation_hash:
            raise AssertionError("Preserved generation response hash mismatch before Judge provider entry.")
        remaining_untouched = len(plan.route_c_order[2:])
        if remaining_untouched != 23:
            raise AssertionError("Frozen untouched Route-C remainder is not 23.")
        pre_fingerprint = runner.verify_route_a_fingerprints()
        route_b_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_hash != plan.route_b_response_hash:
            raise AssertionError("Terminal Route-B preserved response hash mismatch.")
    except BaseException as exc:
        blocked = {
            "phase": "PHASE_1E_R4I_B",
            "status": "PHASE_1E_R4I_B_BLOCKED",
            "target_slot": target,
            "api_calls_by_route": api_calls,
            "judge_attempt_2_authorization_consumed": False,
            "route_a_fingerprint_before": pre_fingerprint,
            "route_b_original_response_sha256": route_b_hash,
            "r4h_recovered_route_c_response_sha256": r4h_hash,
            "preserved_generation_response_sha256": preserved_hash,
            "pre_call_failure": safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"])),
        }
        if not output_dir_preexisting:
            write_json_failure_safe(report_path, blocked)
        return blocked

    manifest = {
        "phase": "PHASE_1E_R4I_B",
        "status": "RUNNING",
        "target_slot": target,
        "route": "C",
        "started_at": utc_now(),
        "attempt_number": 2,
        "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2",
        "prior_judge_attempt": attempt_1,
        "preserved_generation": {"source_artifact": safe_path_reference(generation_path), "response_sha256": preserved_hash, "reused_without_normalization": True, "generator_calls": 0},
        "maximum_additional_judge_attempts_after_this": 0,
        "max_retries": 0,
        "route_a_fingerprint_before": pre_fingerprint,
        "route_b_terminal_state": "ROUTE_B_RECOVERY_EXHAUSTED / ROUTE_B_UNSCORED_INFRASTRUCTURE_MISSING",
        "route_b_original_response_sha256": route_b_hash,
        "r4h_recovered_route_c_response_sha256": r4h_hash,
        "remaining_route_c_slots_untouched_before": remaining_untouched,
        "api_calls": api_calls,
    }
    write_json_failure_safe(manifest_path, manifest)

    try:
        lifecycle["stage"] = "AUTHORIZED_JUDGE_REPLACEMENT_PRE_ATTEMPT_PERSISTED"
        marker("AUTHORIZED_JUDGE_REPLACEMENT_PRE_ATTEMPT_PERSISTED", prior_judge_attempt=attempt_1, preserved_generation_response_sha256=preserved_hash, generator_calls=0, max_retries=0, maximum_additional_judge_attempts_after_this=0)
        authorization_consumed = True

        async def preserve_without_generation(state: TaskState, **_: Any) -> TaskState:
            state.output = preserved_output
            return state

        lifecycle["stage"] = "PRESERVED_GENERATION_STATE_RECONSTRUCTION"
        state = await runner.prepare_with_official_solver(target, preserve_without_generation)
        if str(state.sample_id) != split_slot(target)[0] or state.epoch != split_slot(target)[1]:
            raise AssertionError("Reconstructed scorer state identity/epoch differs from the frozen target.")
        if state.output is None or canonical_json(serialise_output(state.output)) != canonical_json(generation_asset["output"]):
            raise AssertionError("Official-solver reconstruction changed the preserved generation artifact.")
        if sha256_text(state.output.completion) != expected_generation_hash:
            raise AssertionError("Preserved generation response hash changed before Judge provider entry.")
        marker("PRESERVED_GENERATION_SHA_VERIFIED", response_sha256=expected_generation_hash, generator_calls=0)
        lifecycle["stage"] = "JUDGE_SCORER_CALL_ABOUT_TO_ENTER"
        marker("JUDGE_SCORER_CALL_ABOUT_TO_ENTER", scorer="persistbench_judge", judge=plan.protocol["frozen_treatment"]["judge"], max_retries=0)
        api_calls["route_c_judge"] += 1
        original_get_judge = official_scorers._get_judge
        official_scorers._get_judge = runner.frozen_judge_model_no_retry
        try:
            lifecycle["stage"] = "JUDGE_SCORER_CALL_ENTERED"
            marker("JUDGE_SCORER_CALL_ENTERED", observability_note="APPLICATION_SCORER_BOUNDARY_ENTERED_NOT_PROVIDER_HTTP_REQUEST_CONFIRMED")
            score_started_at = time.perf_counter()
            sample, _ = runner.sample_for(target)
            score = await persistbench_judge()(state, Target(sample.target))
        except BaseException as exc:
            lifecycle["stage"] = "JUDGE_SCORER_RAISED"
            failure_marker("JUDGE_SCORER_RAISED", exc)
            raise
        finally:
            official_scorers._get_judge = original_get_judge
        score_latency = time.perf_counter() - score_started_at
        judge_scorer_completed = True
        official_score_present = score is not None
        if not official_score_present:
            raise AssertionError("Official PersistBench scorer returned no Score.")
        selected_completion_nonempty = True
        lifecycle["stage"] = "RECOVERED_RECORD_PERSISTENCE"
        provenance = {
            "recovery_phase": "PHASE_1E_R4I_B",
            "recovery_route": "C_PRESERVED_GENERATION_JUDGE_REPLACEMENT",
            "attempt_number": 2,
            "attempt_type": "AUTHORIZED_JUDGE_REPLACEMENT_ATTEMPT_2",
            "prior_judge_attempt": attempt_1,
            "source_frozen_sample": split_slot(target)[0],
            "target_epoch": split_slot(target)[1],
            "treatment_sha256": plan.protocol["frozen_treatment"]["instruction_sha256"],
            "generator_config": plan.protocol["frozen_treatment"]["generator"],
            "judge_config": plan.protocol["frozen_treatment"]["judge_configuration"],
            "scorer_identity": plan.protocol["frozen_treatment"]["scorer"],
            "original_artifact_path": safe_path_reference(SOURCE_LOG),
            "preserved_generation_source_artifact": safe_path_reference(generation_path),
            "generation_provenance": "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION",
            "generation_response_sha256": expected_generation_hash,
            "score_provenance": "PHASE_1E_R4I_B_OFFICIAL_PERSISTBENCH_SCORER",
        }
        record = recovered_record(state, score, provenance)
        if not complete_record(record):
            raise AssertionError("Official scorer did not yield a complete recovered record.")
        atomic_json_write(record_path, {"slot": target, "record": record, "provenance": provenance})
        recovered_record_present = True
        marker("RECOVERED_RECORD_PERSISTED", generator_calls=0, judge_completed=True, scorer_completed=True, official_score_present=True, final_record_persisted=True, raw_judge_scorer_latency_seconds=score_latency, recovered_artifact=safe_path_reference(record_path))
        post_fingerprint = runner.verify_route_a_fingerprints()
        if post_fingerprint != plan.route_a_fingerprint_hash:
            raise AssertionError("Route-A fingerprint changed after R4I-B.")
        route_b_post_hash = sha256_text(read_eval_sample(*split_slot(plan.route_b))["output"]["completion"])
        if route_b_post_hash != route_b_hash:
            raise AssertionError("Route-B preserved response changed during R4I-B.")
        r4h_post_hash = sha256_text(json.loads(r4h_record_path.read_text(encoding="utf-8"))["record"]["output"]["completion"])
        if r4h_post_hash != expected_r4h_hash:
            raise AssertionError("R4H recovered response changed during R4I-B.")
        preserved_post_hash = sha256_text(ModelOutput.model_validate(json.loads(generation_path.read_text(encoding="utf-8"))["output"]).completion)
        if preserved_post_hash != expected_generation_hash:
            raise AssertionError("Preserved R4I generation changed during R4I-B.")
        status = "PHASE_1E_R4I_B_PASS / PRESERVED_GENERATION_JUDGE_RECOVERED / JUDGE_ATTEMPT_2_CONSUMED / READY_FOR_REMAINING_23_ROUTE_C_AUTHORIZATION"
    except BaseException as exc:
        failure = safe_exception_record(exc, target_slot=target, route="C", lifecycle_stage=safe_text(lifecycle["stage"]))
        if lifecycle.get("marker_persistence_error"):
            failure["marker_persistence_error"] = lifecycle["marker_persistence_error"]
        try:
            post_fingerprint = runner.verify_route_a_fingerprints()
        except BaseException as fingerprint_exc:
            failure["post_failure_route_a_fingerprint_error"] = safe_exception_record(fingerprint_exc, target_slot=None, route=None, lifecycle_stage="POST_FAILURE_ROUTE_A_FINGERPRINT")
        status = "PHASE_1E_R4I_B_JUDGE_REPLACEMENT_FAILED / PRESERVED_GENERATION_UNSCORED_JUDGE_RECOVERY_EXHAUSTED"

    manifest.update({"status": status, "ended_at": utc_now(), "api_calls": api_calls, "judge_attempt_2_authorization_consumed": authorization_consumed, "judge_scorer_completed": judge_scorer_completed, "selected_result_completion_nonempty": selected_completion_nonempty, "official_score_present": official_score_present, "recovered_record_present": recovered_record_present, "remaining_route_c_slots_untouched_after": remaining_untouched, "failure": failure})
    write_json_failure_safe(manifest_path, manifest)
    result = {
        "phase": "PHASE_1E_R4I_B", "status": status, "target_slot": target,
        "preserved_generation_response_sha256": expected_generation_hash,
        "preserved_generation_sha_verified": preserved_hash == expected_generation_hash,
        "generator_calls": api_calls["route_c_generator"], "judge_replacement_attempt_2_invocations": api_calls["route_c_judge"],
        "judge_attempt_1_provenance": attempt_1, "judge_scorer_completed": judge_scorer_completed,
        "selected_result_completion_nonempty": selected_completion_nonempty,
        "official_parser_result": "SUCCESS" if judge_scorer_completed else "FAILED_OR_NOT_COMPLETED",
        "official_score_present": official_score_present, "recovered_record_present": recovered_record_present,
        "judge_attempt_2_authorization_consumed": authorization_consumed, "further_judge_attempt_authorized": False,
        "remaining_route_c_slots_untouched": remaining_untouched,
        "route_a_fingerprint_before": pre_fingerprint, "route_a_fingerprint_after": post_fingerprint,
        "route_b_original_response_sha256": route_b_hash, "r4h_recovered_route_c_response_sha256": r4h_hash,
        "api_calls_by_route": api_calls, "max_retries": 0, "custom_semantic_implementation_count": 0,
        "no_product_metrics_computed": True, "failure": failure,
        "files": {"manifest": safe_path_reference(manifest_path), "ledger": safe_path_reference(ledger_path), "recovered_record": safe_path_reference(record_path), "integrity_report": safe_path_reference(report_path), "execution_report": safe_path_reference(markdown_path)},
    }
    write_json_failure_safe(report_path, result)
    try:
        lines = [
            "# Phase 1E-R4I-B preserved-generation Judge replacement report", "", f"Status: `{status}`", "",
            "This report contains execution-integrity facts only; it deliberately omits all score values and product metrics.", "",
            f"- Target: `{target}`.", f"- Preserved generation SHA-256 verified: `{result['preserved_generation_sha_verified']}`.",
            f"- Generator calls: `{api_calls['route_c_generator']}`; Judge Attempt 2 invocations: `{api_calls['route_c_judge']}`.",
            "- Judge Attempt 1 provenance: `JUDGE_ATTEMPT_1_NONRECOVERABLE_EMPTY_COMPLETION` (not a PASS/FAIL outcome).",
            f"- Official parser result: `{result['official_parser_result']}`; official score present: `{official_score_present}`; recovered record present: `{recovered_record_present}`.",
            f"- Further Judge attempt authorized: `False`; remaining Route-C slots untouched: `{remaining_untouched}`.",
            "- No Route A, Route B, Sycophancy, Beneficial, Reserve, Frozen Validation, or V3 execution occurred. No commit or push occurred.",
        ]
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except BaseException:
        pass
    return result


def assemble_additive_pack(
    recovered_records_path: Path,
    output_path: Path,
) -> None:
    """Future-only additive assembly; never mutates the original `.eval` archive.

    `recovered_records_path` must be JSONL objects with `slot`, `record`, and
    `provenance`.  The command rejects every duplicate, missing, unexpected, or
    immutable-record fingerprint mismatch and intentionally computes no metric.
    """
    plan = FrozenRecoveryPlan.load()
    runner = OfficialRecoveryOrchestrator(plan)
    runner.verify_route_a_fingerprints()
    recovered = [json.loads(line) for line in recovered_records_path.read_text(encoding="utf-8").splitlines() if line]
    recovered_by_slot = {item["slot"]: item for item in recovered}
    if len(recovered_by_slot) != len(recovered):
        raise AssertionError("Recovered artifact contains duplicate slots.")
    expected_recovered = {plan.route_b} | set(plan.route_c)
    if set(recovered_by_slot) != expected_recovered:
        raise AssertionError("Recovered artifact does not contain exactly Route B plus Route C.")
    for key, item in recovered_by_slot.items():
        record = item.get("record")
        provenance = item.get("provenance")
        if not isinstance(record, dict) or not isinstance(provenance, dict):
            raise AssertionError(f"Recovered record/provenance missing for {key}.")
        sample_id, epoch = split_slot(key)
        if record.get("id") != sample_id or record.get("epoch") != epoch:
            raise AssertionError(f"Recovered record identity differs from allowlisted slot {key}.")
        if key == plan.route_b:
            if provenance.get("generation_provenance") != "ORIGINAL_PHASE_1E_PRESERVED_GENERATION":
                raise AssertionError("Route-B generation provenance is not preserved-original.")
            if provenance.get("original_response_sha256") != plan.route_b_response_hash:
                raise AssertionError("Route-B original response hash provenance mismatch.")
        elif provenance.get("generation_provenance") != "PHASE_1E_RECOVERY_FIRST_COMPLETED_GENERATION":
            raise AssertionError(f"Route-C generation provenance mismatch for {key}.")
    originals: list[dict[str, Any]] = []
    for key in sorted(plan.route_a):
        originals.append({"slot": key, "record": read_eval_sample(*split_slot(key)), "provenance": {"recovery_route": "A_IMMUTABLE_SOURCE"}})
    payload = {
        "phase": "PHASE_1E_RECOVERY_CANONICAL_ASSEMBLY",
        "metrics_computed": False,
        "route_a_fingerprint_set_sha256": plan.route_a_fingerprint_hash,
        "records": originals + [recovered_by_slot[key] for key in sorted(expected_recovered)],
    }
    atomic_json_write(output_path, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Run the mandatory non-network validation suite.")
    parser.add_argument("--write-report", type=Path, help="Optional additive JSON report destination for --dry-run.")
    parser.add_argument("--assemble-recovered-records", type=Path, help="Future-only path to recovered JSONL records.")
    parser.add_argument("--assembly-output", type=Path, help="Future-only additive canonical-pack path.")
    parser.add_argument("--execute-authorized", action="store_true", help="Execute the explicitly authorized Phase 1E-R4 Route B then Route C recovery.")
    parser.add_argument("--execute-route-b-replacement-authorized", action="store_true", help="Execute the single explicitly authorized Phase 1E-R4C Route-B replacement only.")
    parser.add_argument("--execute-route-c-authorized", action="store_true", help="Execute the exact explicitly authorized Phase 1E-R4E Route-C allowlist only.")
    parser.add_argument("--execute-route-c-first-slot-replacement-authorized", action="store_true", help="Execute the single explicitly authorized Phase 1E-R4H Route-C first-slot replacement only.")
    parser.add_argument("--execute-remaining-route-c-authorized", action="store_true", help="Execute the exact explicitly authorized Phase 1E-R4I remaining 24 Route-C slots only.")
    parser.add_argument("--execute-preserved-generation-judge-replacement-authorized", action="store_true", help="Execute the single explicitly authorized Phase 1E-R4I-B Judge-only replacement for the preserved generation.")
    parser.add_argument("--execute-remaining-23-route-c-authorized", action="store_true", help="Execute the exact explicitly authorized Phase 1E-R4J remaining 23 Route-C slots only.")
    parser.add_argument("--execute-r4j-preserved-generation-judge-replacement-authorized", action="store_true", help="Execute the single explicitly authorized Phase 1E-R4J-A Judge-only replacement after provider 429.")
    parser.add_argument("--execute-remaining-19-route-c-authorized", action="store_true", help="Execute the exact explicitly authorized Phase 1E-R4K remaining 19 Route-C slots only.")
    parser.add_argument("--output-dir", type=Path, help="Required additive artifact directory for --execute-authorized.")
    parser.add_argument("--failure-injection-dry-run", action="store_true", help="Run no-network R4B exception-capture failure injection checks.")
    args = parser.parse_args()
    selected_modes = sum(bool(value) for value in (args.dry_run, args.assemble_recovered_records, args.execute_authorized, args.execute_route_b_replacement_authorized, args.execute_route_c_authorized, args.execute_route_c_first_slot_replacement_authorized, args.execute_remaining_route_c_authorized, args.execute_preserved_generation_judge_replacement_authorized, args.execute_remaining_23_route_c_authorized, args.execute_r4j_preserved_generation_judge_replacement_authorized, args.execute_remaining_19_route_c_authorized, args.failure_injection_dry_run))
    if selected_modes != 1:
        parser.error("Choose exactly one execution, assembly, or no-network validation mode.")
    if args.dry_run:
        report = asyncio.run(run_dry_run(FrozenRecoveryPlan.load()))
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.write_report:
            args.write_report.parent.mkdir(parents=True, exist_ok=True)
            args.write_report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return
    if args.failure_injection_dry_run:
        report = run_failure_injection_validation()
        rendered = json.dumps(report, ensure_ascii=False, indent=2)
        if args.write_report:
            atomic_json_write(args.write_report, report)
        print(rendered)
        return
    if args.execute_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-authorized.")
        report = asyncio.run(execute_authorized_recovery(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_route_b_replacement_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-route-b-replacement-authorized.")
        report = asyncio.run(execute_authorized_route_b_replacement(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_route_c_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-route-c-authorized.")
        report = asyncio.run(execute_authorized_route_c_recovery(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_route_c_first_slot_replacement_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-route-c-first-slot-replacement-authorized.")
        report = asyncio.run(execute_authorized_route_c_first_slot_replacement(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_remaining_route_c_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-remaining-route-c-authorized.")
        report = asyncio.run(execute_authorized_remaining_route_c_recovery(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_preserved_generation_judge_replacement_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-preserved-generation-judge-replacement-authorized.")
        report = asyncio.run(execute_authorized_preserved_generation_judge_replacement(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_remaining_23_route_c_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-remaining-23-route-c-authorized.")
        report = asyncio.run(execute_authorized_remaining_23_route_c_recovery(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_r4j_preserved_generation_judge_replacement_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-r4j-preserved-generation-judge-replacement-authorized.")
        report = asyncio.run(execute_authorized_r4j_preserved_generation_judge_replacement(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.execute_remaining_19_route_c_authorized:
        if args.output_dir is None:
            parser.error("--output-dir is required with --execute-remaining-19-route-c-authorized.")
        report = asyncio.run(execute_authorized_remaining_19_route_c_recovery(args.output_dir))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if args.assembly_output is None:
        parser.error("--assembly-output is required with --assemble-recovered-records.")
    assemble_additive_pack(args.assemble_recovered_records, args.assembly_output)


if __name__ == "__main__":
    main()
