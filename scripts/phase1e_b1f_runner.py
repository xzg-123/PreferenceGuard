"""Phase 1E-B1F: one authorized local-runtime generation replacement only."""
from __future__ import annotations
import argparse, asyncio, hashlib, json, os, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"inspect_evals"/"src"))
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

PHASE=ROOT/"artifacts"/"phase-1e"; B1=PHASE/"beneficial"; OUT=B1/"b1f"
CONFIG=PHASE/"phase-1e-v2-treatment-config.json"; B1C=B1/"b1c"/"phase-1e-b1c-execution-manifest.json"; B1D=B1/"b1d"/"phase-1e-b1d-execution-manifest.json"
TARGET="persistbench_ab0108b8:epoch=1"; V2_SHA="628dfc7bf07a64ee27093837f6eb790bb482c99efb8bbf784dc75070b27fa994"
def now(): return datetime.now(timezone.utc).isoformat()
def canon(v:Any): return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def sha(v:str): return hashlib.sha256(v.encode()).hexdigest()
def safe(slot:str): return slot.replace(":","_").replace("=","_")
def write(path:Path,value:dict[str,Any]):
 path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix(path.suffix+".tmp")
 with temp.open("w",encoding="utf-8",newline="\n") as f: f.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n"); f.flush(); os.fsync(f.fileno())
 temp.replace(path)
def append(path:Path,value:dict[str,Any]):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open("a",encoding="utf-8",newline="\n") as f: f.write(canon(value)+"\n"); f.flush(); os.fsync(f.fileno())
def failure(error:BaseException,stage:str): return {"exception_class":type(error).__name__,"exception_message":str(error),"traceback":traceback.format_exc(),"lifecycle_stage":stage,"target_slot":TARGET,"captured_at":now()}
def complete(r:dict[str,Any]): return bool(r.get("id") and r.get("epoch") and r.get("output",{}).get("completion") and r.get("scores",{}).get("persistbench_judge"))

async def run() -> dict[str,Any]:
 manifest_path=OUT/"phase-1e-b1f-execution-manifest.json"; ledger_path=OUT/"phase-1e-b1f-execution-ledger.jsonl"; report_path=OUT/"phase-1e-b1f-integrity-report.json"; markdown_path=OUT/"PHASE_1E_B1F_EXECUTION_REPORT.md"; records_dir=OUT/"records"
 calls={"beneficial_generator":0,"beneficial_judge":0,"cross_domain":0,"sycophancy":0,"reserve":0,"frozen_validation":0}; stage="B1F_PRECALL_INVARIANTS"; output_sha=None; generation_latency=None; score=None; saved_record=False; failure_detail=None
 def marker(event:str,**details:Any): append(ledger_path,{"event":event,"slot":TARGET,"attempt_number":2,"attempt_type":"AUTHORIZED_SINGLE_LOCAL_RUNTIME_GENERATION_REPLACEMENT","route":"BENEFICIAL","timestamp":now(),"lifecycle_scope":"APPLICATION_ORCHESTRATION_ONLY",**details})
 try:
  if OUT.exists(): raise AssertionError("B1F output/history already exists; Generation Attempt 3 is forbidden.")
  config=json.loads(CONFIG.read_text(encoding="utf-8")); b1c=json.loads(B1C.read_text(encoding="utf-8")); b1d=json.loads(B1D.read_text(encoding="utf-8")); frozen=config["frozen_execution"]; dataset=config["development_main_datasets"]["persistbench_beneficial_memory"]
  if b1c.get("status")!="PHASE_1E_B1C_EXECUTION_INTERRUPTED" or b1c.get("failure",{}).get("target_slot")!=TARGET or b1c.get("failure",{}).get("lifecycle_stage")!="GENERATOR_RAISED" or b1c.get("generation_complete_count")!=0: raise AssertionError("Attempt-1 no-completion provenance differs.")
  if b1d.get("generation_attempt_2_consumed") is not False: raise AssertionError("Generation Attempt 2 was already consumed.")
  if config["exact_treatment_instruction_sha256"]!=V2_SHA or frozen["generator_model"]!="deepseek/deepseek-v4-flash" or frozen["judge_model"]!="openrouter/moonshotai/kimi-k2-thinking" or frozen["max_retries"]!=0 or frozen["max_connections"]!=1 or frozen["adaptive_connections"] is not False: raise AssertionError("Frozen treatment/component/retry policy differs.")
  if frozen["judge_configuration"]!={"temperature":0,"reasoning_effort":"high","reasoning_enabled":True,"provider_order":["google-vertex"],"allow_fallbacks":False}: raise AssertionError("Frozen Judge configuration differs.")
  if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]: raise AssertionError("Unauthorized scope enabled.")
  samples={str(s.id):s for s in persistbench_dataset(ROOT/dataset["path"])}; sample_id, epoch_text=TARGET.split(":epoch="); epoch=int(epoch_text)
  if sample_id not in samples or epoch!=1 or TARGET not in b1c["remaining_exact_slot_order"]: raise AssertionError("Exact frozen target/sample identity differs.")
 except BaseException as error:
  blocked={"phase":"PHASE_1E_B1F","status":"PHASE_1E_B1F_BLOCKED","target_slot":TARGET,"generation_attempt_2_invocations":0,"api_calls":calls,"pre_call_failure":failure(error,stage)}
  if not OUT.exists(): write(report_path,blocked)
  return blocked
 manifest={"phase":"PHASE_1E_B1F","status":"RUNNING","execution_host":"NORMAL_LOCAL_WINDOWS_POWERSHELL","target_slot":TARGET,"started_at":now(),"attempt_1_provenance":"GENERATION_ATTEMPT_1_TRANSPORT_FAILED_NO_COMPLETION","generation_attempt_2_authorized":True,"generation_attempt_3_authorized":False,"v2_instruction_sha256":V2_SHA,"generator":frozen["generator_model"],"judge":frozen["judge_model"],"judge_configuration":frozen["judge_configuration"],"scorer":frozen["scorer"],"max_retries":0,"custom_semantic_implementation_count":0,"api_calls":calls}; write(manifest_path,manifest)
 try:
  marker("GENERATION_ATTEMPT_2_PRE_EXECUTION_PERSISTED",max_retries=0); sample=samples[sample_id]; state=TaskState(model=ModelName(frozen["generator_model"]),sample_id=sample_id,epoch=epoch,input=sample.input,messages=[],target=Target(sample.target),choices=sample.choices,output=None,completed=False,metadata=dict(sample.metadata),store={}); solver=persistbench_solver(prompt_template=ROOT/config["prompt_template"]["treatment_prompt_template"]); gen_config=GenerateConfig(max_retries=0,max_connections=1,adaptive_connections=False); gen_model=get_model(frozen["generator_model"],config=gen_config)
  async def official_generate(current:TaskState,tool_calls:str="loop",**kwargs:Any)->TaskState: return await task_generate(model=gen_model,state=current,tool_calls=tool_calls,cache=kwargs.get("cache",NOT_GIVEN),config=gen_config.merge(kwargs))
  stage="GENERATOR_CALL_ABOUT_TO_ENTER"; marker(stage); calls["beneficial_generator"]+=1; stage="GENERATOR_CALL_ENTERED"; marker(stage); started=time.perf_counter()
  try: state=await solver(state,official_generate)
  except BaseException as error: stage="GENERATOR_RAISED"; marker(stage,runtime_error=failure(error,stage)); raise
  generation_latency=time.perf_counter()-started
  if state.output.empty or not state.output.completion: raise AssertionError("Generation Attempt 2 completed without semantic output.")
  output=state.output.model_dump(mode="json"); output_sha=sha(state.output.completion); generation_path=records_dir/f"{safe(TARGET)}.generation.json"; stage="GENERATION_PERSISTENCE"; write(generation_path,{"slot":TARGET,"output":output,"response_sha256":output_sha,"generator_model":output.get("model"),"generator_usage":output.get("usage"),"raw_generation_latency_seconds":generation_latency,"v2_instruction_sha256":V2_SHA,"generation_provenance":"PHASE_1E_B1F_GENERATION_ATTEMPT_2","immutable_after_persistence":True,"downstream_evaluation_state":"PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE"}); marker("GENERATION_PERSISTED",response_sha256=output_sha,raw_generation_latency_seconds=generation_latency,generator_usage=output.get("usage"),generation_artifact=str(generation_path.relative_to(ROOT)).replace("\\","/"))
  def frozen_judge(): return get_model(role="grader",default=frozen["judge_model"],config=GenerateConfig(temperature=0,reasoning_effort="high",max_retries=0),provider={"order":["google-vertex"],"allow_fallbacks":False},reasoning_enabled=True)
  stage="JUDGE_SCORER_CALL_ABOUT_TO_ENTER"; marker(stage); calls["beneficial_judge"]+=1; original=official_scorers._get_judge; official_scorers._get_judge=frozen_judge
  try: stage="JUDGE_SCORER_CALL_ENTERED"; marker(stage); score=await persistbench_judge()(state,Target(sample.target))
  except BaseException as error: stage="JUDGE_SCORER_RAISED"; marker(stage,runtime_error=failure(error,stage)); raise
  finally: official_scorers._get_judge=original
  record={"id":sample_id,"epoch":epoch,"input":state.input_text,"target":state.target.target,"messages":[{"role":str(m.role),"content":m.text} for m in state.messages],"output":output,"scores":{"persistbench_judge":score.model_dump(mode="json")},"metadata":dict(state.metadata),"store":dict(state.store),"recovery_provenance":{"recovery_phase":"PHASE_1E_B1F","generation_provenance":"PHASE_1E_B1F_GENERATION_ATTEMPT_2","generation_response_sha256":output_sha,"treatment_sha256":V2_SHA,"generator_config":frozen["generator_model"],"judge_config":frozen["judge_configuration"],"scorer_identity":frozen["scorer"],"score_provenance":"PHASE_1E_B1F_OFFICIAL_PERSISTBENCH_SCORER"}}
  if not complete(record): raise AssertionError("Official scorer did not produce a complete official record.")
  recovered_path=records_dir/f"{safe(TARGET)}.recovered.json"; write(recovered_path,{"slot":TARGET,"record":record,"provenance":record["recovery_provenance"]}); saved_record=True; marker("RECOVERED_RECORD_PERSISTED",official_score_present=True,recovered_artifact=str(recovered_path.relative_to(ROOT)).replace("\\","/")); status="PHASE_1E_B1F_PASS / GENERATION_ATTEMPT_2_RECOVERED / OFFICIAL_SCORE_RECOVERED / READY_FOR_REMAINING_13_BENEFICIAL_AUTHORIZATION"
 except BaseException as error:
  failure_detail=failure(error,stage); status="PHASE_1E_B1F_EXECUTION_INTERRUPTED / GENERATION_RECOVERED / DOWNSTREAM_JUDGE_INCOMPLETE" if output_sha else "PHASE_1E_B1F_GENERATION_REPLACEMENT_FAILED / SECOND_BENEFICIAL_PERMANENT_UNSCORED_SLOT_CREATED"
 manifest.update({"status":status,"ended_at":now(),"generation_attempt_2_consumed":True,"api_calls":calls,"response_sha256":output_sha,"failure":failure_detail}); write(manifest_path,manifest)
 result={"phase":"PHASE_1E_B1F","status":status,"execution_host":"NORMAL_LOCAL_WINDOWS_POWERSHELL","target_slot":TARGET,"generation_attempt_2_invocations":calls["beneficial_generator"],"generation_attempt_2_consumed":True,"generation_attempt_3_authorized":False,"generation_complete":bool(output_sha),"response_sha256":output_sha,"raw_generation_latency_seconds":generation_latency,"judge_scorer_lifecycle_count":calls["beneficial_judge"],"official_score_present":score is not None,"complete_record_persisted":saved_record,"preserved_generation_state":"PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE" if output_sha and score is None else None,"remaining_untouched_count":13,"no_beneficial_aggregate_metrics_computed":True,"api_calls":calls,"failure":failure_detail,"files":{"manifest":str(manifest_path.relative_to(ROOT)).replace("\\","/"),"ledger":str(ledger_path.relative_to(ROOT)).replace("\\","/"),"integrity_report":str(report_path.relative_to(ROOT)).replace("\\","/"),"execution_report":str(markdown_path.relative_to(ROOT)).replace("\\","/")}}; write(report_path,result); markdown_path.write_text(f"# Phase 1E-B1F execution report\n\nStatus: `{status}`\n\nExecution integrity only; no Beneficial aggregate metric was computed.\n",encoding="utf-8"); return result
def dry_run() -> dict[str, Any]:
 """Zero-network verification. This function never resolves or invokes a model."""
 config=json.loads(CONFIG.read_text(encoding="utf-8")); b1c=json.loads(B1C.read_text(encoding="utf-8")); b1d=json.loads(B1D.read_text(encoding="utf-8")); frozen=config["frozen_execution"]
 prior={name:(B1/name).exists() for name in ["phase-1e-b1-execution-manifest.json","b1a","b1b","b1c","b1d","b1e"]}
 existing_manifest=OUT/"phase-1e-b1f-execution-manifest.json"
 existing_status=json.loads(existing_manifest.read_text(encoding="utf-8")).get("status") if existing_manifest.exists() else None
 return {"phase":"PHASE_1E_B1F_RUNNER_DRY_RUN","mode":"ZERO_NETWORK_VERIFICATION_ONLY","authorized_slot_count":1,"authorized_slot":TARGET,"generation_attempt":2,"generation_attempt_3_authorized":False,"max_retries":frozen["max_retries"],"v2_sha":config["exact_treatment_instruction_sha256"],"custom_semantic_implementation_count":0,"dry_run_model_calls":0,"dry_run_judge_calls":0,"remaining_13_execution":"PROHIBITED","attempt_1_provenance_valid":b1c.get("failure",{}).get("lifecycle_stage")=="GENERATOR_RAISED" and b1c.get("generation_complete_count")==0,"attempt_2_previously_unconsumed_in_b1d":b1d.get("generation_attempt_2_consumed") is False,"frozen_components_valid":frozen["generator_model"]=="deepseek/deepseek-v4-flash" and frozen["judge_model"]=="openrouter/moonshotai/kimi-k2-thinking" and frozen["max_retries"]==0,"prior_artifacts_present":prior,"execution_history_preexisting":existing_manifest.exists(),"execution_history_status":existing_status,"execution_permitted_now":not existing_manifest.exists()}

if __name__=="__main__":
 parser=argparse.ArgumentParser(); parser.add_argument("--execute-authorized-attempt-2",action="store_true",help="Execute the one hard-coded authorized slot once."); args=parser.parse_args()
 if not args.execute_authorized_attempt_2:
  print(json.dumps(dry_run(),ensure_ascii=False,indent=2))
 else:
  print(json.dumps(asyncio.run(run()),ensure_ascii=False,indent=2))
