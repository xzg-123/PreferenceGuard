"""Phase 1E-B1D one-slot recovery, gated by a non-model connectivity preflight."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import httpx
import json
import os
import socket
import ssl
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
OUT = B1 / "b1d"
CONFIG = PHASE / "phase-1e-v2-treatment-config.json"
B1C_MANIFEST = B1 / "b1c" / "phase-1e-b1c-execution-manifest.json"
B1C_LEDGER = B1 / "b1c" / "phase-1e-b1c-execution-ledger.jsonl"
TARGET = "persistbench_ab0108b8:epoch=1"
V2_SHA = "628dfc7bf07a64ee27093837f6eb790bb482c99efb8bbf784dc75070b27fa994"

def now() -> str: return datetime.now(timezone.utc).isoformat()
def canon(value: Any) -> str: return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def sha(value: str) -> str: return hashlib.sha256(value.encode("utf-8")).hexdigest()
def safe(slot: str) -> str: return slot.replace(":", "_").replace("=", "_")
def write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True); temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n"); handle.flush(); os.fsync(handle.fileno())
    temporary.replace(path)
def append(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canon(value) + "\n"); handle.flush(); os.fsync(handle.fileno())
def exc(error: BaseException, stage: str) -> dict[str, Any]:
    return {"exception_class": type(error).__name__, "exception_message": str(error), "traceback": traceback.format_exc(), "lifecycle_stage": stage, "target_slot": TARGET, "captured_at": now()}
def complete(record: dict[str, Any]) -> bool:
    return bool(record.get("id") and record.get("epoch") and record.get("output", {}).get("completion") and record.get("scores", {}).get("persistbench_judge"))

def non_model_connectivity_preflight() -> dict[str, Any]:
    """Check transport only; /models is an HTTP metadata endpoint, not inference."""
    configured_base_url = os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com"
    parsed = urlparse(configured_base_url)
    host, port = parsed.hostname, parsed.port or 443
    result: dict[str, Any] = {
        "mode": "NON_MODEL_CONNECTIVITY_ONLY",
        "base_url": f"{parsed.scheme}://{host}" + (f":{port}" if port != 443 else ""),
        "base_url_source": "DEEPSEEK_BASE_URL" if os.environ.get("DEEPSEEK_BASE_URL") else "INSPECT_DEEPSEEK_PROVIDER_DEFAULT",
        "api_key_present": bool(os.environ.get("DEEPSEEK_API_KEY")),
        "dotenv_files_present": [str(p.relative_to(ROOT)).replace("\\", "/") for p in ROOT.glob(".env*") if p.is_file()],
        "environment_overrides_present": {name: bool(os.environ.get(name)) for name in ["DEEPSEEK_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"]},
        "checks": {},
    }
    if parsed.scheme != "https" or not host:
        result["checks"]["configuration"] = {"ok": False, "detail": "Base URL is not an HTTPS host."}
        result["healthy"] = False
        return result
    result["checks"]["configuration"] = {"ok": result["api_key_present"], "detail": "DeepSeek key presence only; key value was not read."}
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
        result["checks"]["dns"] = {"ok": bool(addresses), "address_count": len(addresses)}
    except BaseException as error:
        result["checks"]["dns"] = {"ok": False, "exception": f"{type(error).__name__}: {error}"}
    try:
        with socket.create_connection((host, port), timeout=8): pass
        result["checks"]["tcp"] = {"ok": True}
    except BaseException as error:
        result["checks"]["tcp"] = {"ok": False, "exception": f"{type(error).__name__}: {error}"}
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                result["checks"]["tls"] = {"ok": True, "protocol": tls.version()}
    except BaseException as error:
        result["checks"]["tls"] = {"ok": False, "exception": f"{type(error).__name__}: {error}"}
    try:
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            response = client.get(configured_base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY', '')}"})
        result["checks"]["http_non_model"] = {"ok": response.status_code in range(200, 500), "status_code": response.status_code, "endpoint": "/models", "inference_invoked": False}
    except BaseException as error:
        result["checks"]["http_non_model"] = {"ok": False, "exception": f"{type(error).__name__}: {error}", "endpoint": "/models", "inference_invoked": False}
    result["healthy"] = all(check.get("ok") is True for check in result["checks"].values())
    return result

async def run() -> dict[str, Any]:
    preflight_path=OUT/"phase-1e-b1d-connectivity-preflight.json"; manifest_path=OUT/"phase-1e-b1d-execution-manifest.json"; ledger_path=OUT/"phase-1e-b1d-execution-ledger.jsonl"; report_path=OUT/"phase-1e-b1d-integrity-report.json"; markdown_path=OUT/"PHASE_1E_B1D_EXECUTION_REPORT.md"; records_dir=OUT/"records"
    calls={"beneficial_generator":0,"beneficial_judge":0,"cross_domain":0,"sycophancy":0,"reserve":0,"frozen_validation":0}; stage="B1D_PRECALL_INVARIANTS"; failure=None; output_sha=None; generation_latency=None; score=None; record_exists=False
    def marker(event: str, **details: Any) -> None: append(ledger_path,{"event":event,"slot":TARGET,"attempt_number":2,"route":"BENEFICIAL","timestamp":now(),"lifecycle_scope":"APPLICATION_ORCHESTRATION_ONLY",**details})
    try:
        if OUT.exists(): raise AssertionError("B1D output/history already exists; Generation Attempt 3 is forbidden.")
        config=json.loads(CONFIG.read_text(encoding="utf-8")); b1c=json.loads(B1C_MANIFEST.read_text(encoding="utf-8")); events=[json.loads(line) for line in B1C_LEDGER.read_text(encoding="utf-8").splitlines() if line]
        frozen=config["frozen_execution"]; dataset=config["development_main_datasets"]["persistbench_beneficial_memory"]
        if b1c.get("status")!="PHASE_1E_B1C_EXECUTION_INTERRUPTED" or b1c.get("failure",{}).get("lifecycle_stage")!="GENERATOR_RAISED" or b1c.get("failure",{}).get("target_slot")!=TARGET: raise AssertionError("B1C failure provenance differs.")
        if b1c.get("generation_complete_count")!=0 or b1c.get("api_calls",{}).get("beneficial_generator")!=1 or b1c.get("api_calls",{}).get("beneficial_judge")!=0 or b1c.get("completed_slots")!=[]: raise AssertionError("B1C Attempt-1 no-completion topology differs.")
        if sum(e.get("event")=="GENERATOR_CALL_ENTERED" and e.get("slot")==TARGET for e in events)!=1 or not any(e.get("event")=="GENERATOR_RAISED" and e.get("slot")==TARGET for e in events) or any(e.get("event")=="GENERATION_PERSISTED" and e.get("slot")==TARGET for e in events): raise AssertionError("B1C Attempt-1 lifecycle is not a transport failure without completion.")
        if TARGET not in b1c.get("remaining_exact_slot_order",[]) or b1c.get("remaining_exact_slot_order_sha256")!="de04ef3972dc9f3edcf477e92a563d5203c1ae4e61d8e572279110daa7c9d65f": raise AssertionError("Frozen B1C allowlist differs.")
        if config["exact_treatment_instruction_sha256"]!=V2_SHA or frozen["generator_model"]!="deepseek/deepseek-v4-flash" or frozen["judge_model"]!="openrouter/moonshotai/kimi-k2-thinking" or frozen["max_retries"]!=0: raise AssertionError("Frozen component/retry policy differs.")
        if frozen["judge_configuration"]!={"temperature":0,"reasoning_effort":"high","reasoning_enabled":True,"provider_order":["google-vertex"],"allow_fallbacks":False}: raise AssertionError("Frozen Judge configuration differs.")
        if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]: raise AssertionError("Unauthorized scope enabled.")
        samples={str(s.id):s for s in persistbench_dataset(ROOT/dataset["path"])}; sample_id, epoch_text=TARGET.split(":epoch="); epoch=int(epoch_text)
        if sample_id not in samples or epoch!=1: raise AssertionError("Frozen source sample identity differs.")
    except BaseException as error:
        blocked={"phase":"PHASE_1E_B1D","status":"PHASE_1E_B1D_BLOCKED","target_slot":TARGET,"generation_attempt_2_invocations":0,"api_calls":calls,"pre_call_failure":exc(error,stage)}
        if not OUT.exists(): write(report_path,blocked)
        return blocked
    preflight=non_model_connectivity_preflight(); preflight.update({"phase":"PHASE_1E_B1D","target_slot":TARGET,"generation_attempt_2_consumed":False,"max_retries":0,"no_model_inference":True}); write(preflight_path,preflight)
    manifest={"phase":"PHASE_1E_B1D","status":"CONNECTIVITY_PREFLIGHT_COMPLETE","target_slot":TARGET,"started_at":now(),"attempt_1_provenance":"GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION","generation_attempt_2_authorized_if_preflight_healthy":True,"generation_attempt_3_authorized":False,"v2_instruction_sha256":V2_SHA,"max_retries":0,"api_calls":calls,"connectivity_preflight":str(preflight_path.relative_to(ROOT)).replace("\\","/")}; write(manifest_path,manifest)
    if not preflight["healthy"]:
        status="PHASE_1E_B1D_BLOCKED / CONNECTIVITY_PREFLIGHT_NOT_HEALTHY"
        manifest.update({"status":status,"ended_at":now(),"generation_attempt_2_consumed":False,"api_calls":calls}); write(manifest_path,manifest)
        result={"phase":"PHASE_1E_B1D","status":status,"connectivity_preflight_healthy":False,"target_slot":TARGET,"attempt_1_provenance":"GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION","generation_attempt_2_invocations":0,"generation_attempt_2_consumed":False,"generation_attempt_3_authorized":False,"generation_complete":False,"judge_scorer_lifecycle_count":0,"official_score_present":False,"remaining_untouched_count":13,"api_calls":calls,"files":{"connectivity_preflight":str(preflight_path.relative_to(ROOT)).replace("\\","/"),"manifest":str(manifest_path.relative_to(ROOT)).replace("\\","/"),"integrity_report":str(report_path.relative_to(ROOT)).replace("\\","/"),"execution_report":str(markdown_path.relative_to(ROOT)).replace("\\","/")}}; write(report_path,result); markdown_path.write_text(f"# Phase 1E-B1D execution report\n\nStatus: `{status}`\n\nNon-model connectivity preflight was not healthy; Generation Attempt 2 remains unconsumed. No model inference, Judge/scorer call, or Beneficial aggregate metric occurred.\n",encoding="utf-8"); return result
    try:
        marker("GENERATION_ATTEMPT_2_PRE_EXECUTION_PERSISTED",attempt_1_provenance="GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION",max_retries=0)
        sample=samples[sample_id]; state=TaskState(model=ModelName(frozen["generator_model"]),sample_id=sample_id,epoch=epoch,input=sample.input,messages=[],target=Target(sample.target),choices=sample.choices,output=None,completed=False,metadata=dict(sample.metadata),store={}); solver=persistbench_solver(prompt_template=ROOT/config["prompt_template"]["treatment_prompt_template"]); gen_config=GenerateConfig(max_retries=0,max_connections=1,adaptive_connections=False); gen_model=get_model(frozen["generator_model"],config=gen_config)
        async def official_generate(current: TaskState,tool_calls: str="loop",**kwargs: Any)->TaskState: return await task_generate(model=gen_model,state=current,tool_calls=tool_calls,cache=kwargs.get("cache",NOT_GIVEN),config=gen_config.merge(kwargs))
        stage="GENERATOR_CALL_ABOUT_TO_ENTER"; marker(stage); calls["beneficial_generator"]+=1; stage="GENERATOR_CALL_ENTERED"; marker(stage); started=time.perf_counter()
        try: state=await solver(state,official_generate)
        except BaseException as error: stage="GENERATOR_RAISED"; marker(stage,runtime_error=exc(error,stage)); raise
        generation_latency=time.perf_counter()-started
        if state.output.empty or not state.output.completion: raise AssertionError("Generation Attempt 2 completed without a semantic response.")
        output=state.output.model_dump(mode="json"); output_sha=sha(state.output.completion); generation_path=records_dir/f"{safe(TARGET)}.generation.json"; stage="GENERATION_PERSISTENCE"; write(generation_path,{"slot":TARGET,"output":output,"response_sha256":output_sha,"generator_model":output.get("model"),"generator_usage":output.get("usage"),"raw_generation_latency_seconds":generation_latency,"v2_instruction_sha256":V2_SHA,"generation_provenance":"PHASE_1E_B1D_GENERATION_ATTEMPT_2","immutable_after_persistence":True,"downstream_evaluation_state":"PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE"}); marker("GENERATION_PERSISTED",generator_completed=True,response_sha256=output_sha,raw_generation_latency_seconds=generation_latency,generator_usage=output.get("usage"),generation_artifact=str(generation_path.relative_to(ROOT)).replace("\\","/"))
        def frozen_judge()->Any: return get_model(role="grader",default=frozen["judge_model"],config=GenerateConfig(temperature=0,reasoning_effort="high",max_retries=0),provider={"order":["google-vertex"],"allow_fallbacks":False},reasoning_enabled=True)
        stage="JUDGE_SCORER_CALL_ABOUT_TO_ENTER"; marker(stage); calls["beneficial_judge"]+=1; original=official_scorers._get_judge; official_scorers._get_judge=frozen_judge
        try: stage="JUDGE_SCORER_CALL_ENTERED"; marker(stage); score=await persistbench_judge()(state,Target(sample.target))
        except BaseException as error: stage="JUDGE_SCORER_RAISED"; marker(stage,runtime_error=exc(error,stage)); raise
        finally: official_scorers._get_judge=original
        record={"id":sample_id,"epoch":epoch,"input":state.input_text,"target":state.target.target,"messages":[{"role":str(m.role),"content":m.text} for m in state.messages],"output":output,"scores":{"persistbench_judge":score.model_dump(mode="json")},"metadata":dict(state.metadata),"store":dict(state.store),"recovery_provenance":{"recovery_phase":"PHASE_1E_B1D","attempt_number":2,"generation_response_sha256":output_sha,"score_provenance":"PHASE_1E_B1D_OFFICIAL_PERSISTBENCH_SCORER"}}
        if not complete(record): raise AssertionError("Official scorer did not yield a complete record.")
        record_path=records_dir/f"{safe(TARGET)}.recovered.json"; write(record_path,{"slot":TARGET,"record":record,"provenance":record["recovery_provenance"]}); record_exists=True; marker("RECOVERED_RECORD_PERSISTED",official_score_present=True,recovered_artifact=str(record_path.relative_to(ROOT)).replace("\\","/")); status="PHASE_1E_B1D_PASS / GENERATION_ATTEMPT_2_RECOVERED / OFFICIAL_SCORE_RECOVERED / READY_FOR_REMAINING_13_BENEFICIAL_AUTHORIZATION"
    except BaseException as error:
        failure=exc(error,stage); status="PHASE_1E_B1D_EXECUTION_INTERRUPTED / GENERATION_RECOVERED / DOWNSTREAM_JUDGE_INCOMPLETE" if output_sha else "PHASE_1E_B1D_GENERATION_REPLACEMENT_FAILED / SECOND_BENEFICIAL_PERMANENT_UNSCORED_SLOT_CREATED"
    manifest.update({"status":status,"ended_at":now(),"generation_attempt_2_consumed":True,"api_calls":calls,"response_sha256":output_sha,"failure":failure}); write(manifest_path,manifest)
    result={"phase":"PHASE_1E_B1D","status":status,"connectivity_preflight_healthy":True,"target_slot":TARGET,"attempt_1_provenance":"GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION","generation_attempt_2_invocations":calls["beneficial_generator"],"generation_complete":bool(output_sha),"response_sha256":output_sha,"raw_generation_latency_seconds":generation_latency,"judge_scorer_lifecycle_count":calls["beneficial_judge"],"official_score_present":score is not None,"preserved_generation_state":"PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE" if output_sha and score is None else None,"generation_attempt_3_authorized":False,"remaining_untouched_count":13,"api_calls":calls,"failure":failure,"files":{"connectivity_preflight":str(preflight_path.relative_to(ROOT)).replace("\\","/"),"manifest":str(manifest_path.relative_to(ROOT)).replace("\\","/"),"ledger":str(ledger_path.relative_to(ROOT)).replace("\\","/"),"integrity_report":str(report_path.relative_to(ROOT)).replace("\\","/"),"execution_report":str(markdown_path.relative_to(ROOT)).replace("\\","/")}}; write(report_path,result); markdown_path.write_text(f"# Phase 1E-B1D execution report\n\nStatus: `{status}`\n\nExecution integrity only; no Beneficial aggregate metric was computed.\n",encoding="utf-8"); return result

if __name__=="__main__":
    parser=argparse.ArgumentParser(); parser.add_argument("--execute-authorized-b1d",action="store_true"); args=parser.parse_args()
    if not args.execute_authorized_b1d: raise SystemExit("Refusing B1D without the exact authorization switch.")
    print(json.dumps(asyncio.run(run()),ensure_ascii=False,indent=2))
