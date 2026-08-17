"""Phase 1E-B1G: sequential exact execution of the 13 remaining Beneficial slots."""
from __future__ import annotations
import argparse, asyncio, hashlib, json, logging, os, sys, time, traceback
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

PHASE=ROOT/"artifacts"/"phase-1e"; B1=PHASE/"beneficial"; OUT=B1/"b1g"
CONFIG=PHASE/"phase-1e-v2-treatment-config.json"; B1_MANIFEST=B1/"phase-1e-b1-execution-manifest.json"; B1_RECORDS=B1/"phase-1e-b1-completed-records.jsonl"; B1C=B1/"b1c"/"phase-1e-b1c-execution-manifest.json"; B1F=B1/"b1f"/"phase-1e-b1f-execution-manifest.json"; B1F_RECORD=B1/"b1f"/"records"/"persistbench_ab0108b8_epoch_1.recovered.json"; MISSING="persistbench_7c438f64:epoch=1"; B1F_SLOT="persistbench_ab0108b8:epoch=1"; V2_SHA="628dfc7bf07a64ee27093837f6eb790bb482c99efb8bbf784dc75070b27fa994"
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
def exc(e:BaseException,stage:str,slot:str|None): return {"exception_class":type(e).__name__,"exception_message":str(e),"traceback":traceback.format_exc(),"lifecycle_stage":stage,"target_slot":slot,"captured_at":now()}
def complete(r:dict[str,Any]): return bool(r.get("id") and r.get("epoch") and r.get("output",{}).get("completion") and r.get("scores",{}).get("persistbench_judge"))
def fingerprint():
 files=[]
 for name in ["records","b1a","b1b","b1c","b1d","b1e","b1f"]:
  root=B1/name
  if root.exists(): files += [p for p in root.rglob("*") if p.is_file()]
 files += [B1_MANIFEST,B1_RECORDS,B1/"phase-1e-b1-integrity-report.json",B1/"PHASE_1E_B1_EXECUTION_REPORT.md",PHASE/"sycophancy"/"PHASE_1E_S1_EXECUTION_REPORT.md",PHASE/"recovery"/"r4l"/"phase-1e-r4l-cross-domain-integrity-acceptance.json"]
 entries={str(p.relative_to(ROOT)).replace("\\","/"):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(p for p in files if p.exists()))}
 return {"entries":entries,"sha256":sha(canon(entries))}
def inputs():
 config=json.loads(CONFIG.read_text(encoding="utf-8")); b1=json.loads(B1_MANIFEST.read_text(encoding="utf-8")); b1c=json.loads(B1C.read_text(encoding="utf-8")); b1f=json.loads(B1F.read_text(encoding="utf-8")); frozen=config["frozen_execution"]; dataset=config["development_main_datasets"]["persistbench_beneficial_memory"]
 original=list(b1["frozen_slot_order"]); previous=list(b1["completed_slots"]); b1f_item=json.loads(B1F_RECORD.read_text(encoding="utf-8"))
 expected=[f"{sample_id}:epoch=1" for sample_id in dataset["logical_sample_ids"]]
 if original!=expected or len(original)!=20 or len(set(original))!=20: raise AssertionError("Frozen original 20-slot manifest differs.")
 if previous!=original[:5] or len(previous)!=5: raise AssertionError("B1 prior completion boundary differs.")
 if b1c.get("remaining_exact_slot_order_sha256")!="de04ef3972dc9f3edcf477e92a563d5203c1ae4e61d8e572279110daa7c9d65f" or B1F_SLOT not in b1c.get("remaining_exact_slot_order",[]): raise AssertionError("B1C frozen remaining universe differs.")
 if not b1f.get("status","").startswith("PHASE_1E_B1F_PASS / GENERATION_ATTEMPT_2_RECOVERED / OFFICIAL_SCORE_RECOVERED") or b1f_item.get("slot")!=B1F_SLOT or not complete(b1f_item.get("record",{})): raise AssertionError("B1F completed-record integrity differs.")
 if config["exact_treatment_instruction_sha256"]!=V2_SHA or frozen["generator_model"]!="deepseek/deepseek-v4-flash" or frozen["judge_model"]!="openrouter/moonshotai/kimi-k2-thinking" or frozen["max_retries"]!=0 or frozen["max_connections"]!=1 or frozen["adaptive_connections"] is not False: raise AssertionError("Frozen treatment/model/retry policy differs.")
 if frozen["judge_configuration"]!={"temperature":0,"reasoning_effort":"high","reasoning_enabled":True,"provider_order":["google-vertex"],"allow_fallbacks":False}: raise AssertionError("Frozen Judge configuration differs.")
 if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]: raise AssertionError("Unauthorized scope enabled.")
 completed=set(previous)|{B1F_SLOT}; remaining=[slot for slot in original if slot not in completed and slot!=MISSING]
 if len(remaining)!=13 or len(set(remaining))!=13 or MISSING in remaining or B1F_SLOT in remaining or set(remaining)&set(previous): raise AssertionError("Exact remaining-13 allowlist invalid.")
 samples={str(s.id):s for s in persistbench_dataset(ROOT/dataset["path"])}
 if set(samples)!=set(dataset["logical_sample_ids"]): raise AssertionError("Official source dataset identity differs.")
 return config,frozen,original,previous,remaining,samples

def dry_run():
 config,frozen,original,previous,remaining,samples=inputs()
 return {"phase":"PHASE_1E_B1G_RUNNER_DRY_RUN","mode":"ZERO_NETWORK_VERIFICATION_ONLY","authorized_slot_count":len(remaining),"authorized_slot_order":remaining,"authorized_slot_order_sha256":sha(canon(remaining)),"excluded_completed_slots":previous+[B1F_SLOT],"permanent_unscored_slot":MISSING,"v2_sha":config["exact_treatment_instruction_sha256"],"generator":frozen["generator_model"],"judge":frozen["judge_model"],"max_retries":frozen["max_retries"],"custom_semantic_implementation_count":0,"dry_run_model_calls":0,"dry_run_judge_calls":0,"sequential_execution":True,"first_error_stops":True,"remaining_13_execution_authorized":True,"reserve_frozen_validation_v3_prohibited":True,"output_history_preexisting":OUT.exists()}

async def run():
 manifest_path=OUT/"phase-1e-b1g-execution-manifest.json"; ledger_path=OUT/"phase-1e-b1g-execution-ledger.jsonl"; records_path=OUT/"phase-1e-b1g-completed-records.jsonl"; report_path=OUT/"phase-1e-b1g-integrity-report.json"; markdown_path=OUT/"PHASE_1E_B1G_EXECUTION_REPORT.md"; records_dir=OUT/"records"
 calls={"beneficial_generator":0,"beneficial_judge":0,"cross_domain":0,"sycophancy":0,"reserve":0,"frozen_validation":0}; attempted=[]; completed=[]; persisted=[]; warnings=[]; failure=None; stage="B1G_PRECALL_INVARIANTS"; target=None; pre=None; post=None
 def marker(event:str,slot:str,seq:int,**details:Any): append(ledger_path,{"event":event,"slot":slot,"sequence_number":seq,"route":"BENEFICIAL","timestamp":now(),"lifecycle_scope":"APPLICATION_ORCHESTRATION_ONLY",**details})
 try:
  if OUT.exists(): raise AssertionError("B1G output/history already exists; restart is forbidden.")
  config,frozen,original,previous,remaining,samples=inputs(); pre=fingerprint()
 except BaseException as e:
  blocked={"phase":"PHASE_1E_B1G","status":"PHASE_1E_B1G_BLOCKED","api_calls":calls,"pre_call_failure":exc(e,stage,target)}
  if not OUT.exists(): write(report_path,blocked)
  return blocked
 order_sha=sha(canon(remaining)); manifest={"phase":"PHASE_1E_B1G","status":"RUNNING","started_at":now(),"original_frozen_order":original,"previous_completed_slots":previous+[B1F_SLOT],"permanent_unscored_slot":MISSING,"remaining_exact_slot_order":remaining,"remaining_exact_slot_order_sha256":order_sha,"remaining_count":13,"v2_instruction_sha256":V2_SHA,"generator":frozen["generator_model"],"judge":frozen["judge_model"],"judge_configuration":frozen["judge_configuration"],"scorer":frozen["scorer"],"max_retries":0,"custom_semantic_implementation_count":0,"protected_assets_before":pre,"api_calls":calls,"attempted_slots":attempted,"completed_slots":completed}; write(manifest_path,manifest)
 class Capture(logging.Handler):
  def emit(self,record):
   if "Unexpected exception loading entrypoints" in record.getMessage(): warnings.append(record.getMessage())
 capture=Capture(); logger=logging.getLogger("inspect_ai._util.entrypoints"); logger.addHandler(capture)
 solver=persistbench_solver(prompt_template=ROOT/config["prompt_template"]["treatment_prompt_template"]); gen_config=GenerateConfig(max_retries=0,max_connections=1,adaptive_connections=False); gen_model=get_model(frozen["generator_model"],config=gen_config)
 def frozen_judge(): return get_model(role="grader",default=frozen["judge_model"],config=GenerateConfig(temperature=0,reasoning_effort="high",max_retries=0),provider={"order":["google-vertex"],"allow_fallbacks":False},reasoning_enabled=True)
 async def one(slot:str,seq:int):
  nonlocal stage,target
  target=slot; stage="SLOT_PRECHECK"
  if slot not in remaining: raise AssertionError("Slot escaped B1G allowlist.")
  sample_id,epoch_text=slot.split(":epoch="); epoch=int(epoch_text); sample=samples[sample_id]
  if epoch!=1 or str(sample.id)!=sample_id: raise AssertionError("Sample identity/epoch differs.")
  state=TaskState(model=ModelName(frozen["generator_model"]),sample_id=sample_id,epoch=epoch,input=sample.input,messages=[],target=Target(sample.target),choices=sample.choices,output=None,completed=False,metadata=dict(sample.metadata),store={}); attempted.append(slot); marker("SLOT_STARTED",slot,seq,generator_started=False,judge_started=False)
  async def official_generate(current:TaskState,tool_calls:str="loop",**kwargs:Any)->TaskState: return await task_generate(model=gen_model,state=current,tool_calls=tool_calls,cache=kwargs.get("cache",NOT_GIVEN),config=gen_config.merge(kwargs))
  stage="GENERATOR_CALL_ABOUT_TO_ENTER"; marker(stage,slot,seq); calls["beneficial_generator"]+=1; stage="GENERATOR_CALL_ENTERED"; marker(stage,slot,seq); start=time.perf_counter()
  try: state=await solver(state,official_generate)
  except BaseException as e: stage="GENERATOR_RAISED"; marker(stage,slot,seq,runtime_error=exc(e,stage,slot)); raise
  gen_latency=time.perf_counter()-start
  if state.output.empty or not state.output.completion: raise AssertionError("Generation/state incomplete.")
  output=state.output.model_dump(mode="json"); response_sha=sha(state.output.completion); generation_path=records_dir/f"{safe(slot)}.generation.json"; stage="GENERATION_PERSISTENCE"; write(generation_path,{"slot":slot,"sequence_number":seq,"output":output,"response_sha256":response_sha,"generator_model":output.get("model"),"generator_usage":output.get("usage"),"raw_generation_latency_seconds":gen_latency,"v2_instruction_sha256":V2_SHA,"generation_provenance":"PHASE_1E_B1G_FIRST_COMPLETED_GENERATION","immutable_after_persistence":True,"downstream_evaluation_state":"PRESERVED_GENERATION_DOWNSTREAM_EVALUATION_INCOMPLETE"}); persisted.append(slot); marker("GENERATION_PERSISTED",slot,seq,response_sha256=response_sha,raw_generation_latency_seconds=gen_latency,generator_usage=output.get("usage"),generation_artifact=str(generation_path.relative_to(ROOT)).replace("\\","/"))
  stage="JUDGE_SCORER_CALL_ABOUT_TO_ENTER"; marker(stage,slot,seq); calls["beneficial_judge"]+=1; original_get=official_scorers._get_judge; official_scorers._get_judge=frozen_judge
  try: stage="JUDGE_SCORER_CALL_ENTERED"; marker(stage,slot,seq); judge_start=time.perf_counter(); score=await persistbench_judge()(state,Target(sample.target)); judge_latency=time.perf_counter()-judge_start
  except BaseException as e: stage="JUDGE_SCORER_RAISED"; marker(stage,slot,seq,runtime_error=exc(e,stage,slot)); raise
  finally: official_scorers._get_judge=original_get
  record={"id":sample_id,"epoch":epoch,"input":state.input_text,"target":state.target.target,"messages":[{"role":str(m.role),"content":m.text} for m in state.messages],"output":output,"scores":{"persistbench_judge":score.model_dump(mode="json")},"metadata":dict(state.metadata),"store":dict(state.store),"recovery_provenance":{"recovery_phase":"PHASE_1E_B1G","source_frozen_sample":sample_id,"target_epoch":epoch,"treatment_sha256":V2_SHA,"generator_config":frozen["generator_model"],"judge_config":frozen["judge_configuration"],"scorer_identity":frozen["scorer"],"generation_provenance":"PHASE_1E_B1G_FIRST_COMPLETED_GENERATION","generation_response_sha256":response_sha,"score_provenance":"PHASE_1E_B1G_OFFICIAL_PERSISTBENCH_SCORER","raw_generation_latency_seconds":gen_latency,"raw_judge_latency_seconds":judge_latency,"generator_usage":output.get("usage")}}
  if not complete(record): raise AssertionError("Official scorer did not produce a complete record.")
  recovered_path=records_dir/f"{safe(slot)}.recovered.json"; item={"slot":slot,"record":record,"provenance":record["recovery_provenance"]}; write(recovered_path,item); append(records_path,item); marker("RECOVERED_RECORD_PERSISTED",slot,seq,official_score_present=True,recovered_artifact=str(recovered_path.relative_to(ROOT)).replace("\\","/")); completed.append(slot)
 try:
  marker("B1G_PRE_EXECUTION_PERSISTED",remaining[0],0,ordered_slots_sha256=order_sha,remaining_count=13,max_retries=0)
  for seq,slot in enumerate(remaining,1): await one(slot,seq)
  rows=[json.loads(line) for line in records_path.read_text(encoding="utf-8").splitlines() if line]
  if len(rows)!=13 or [r.get("slot") for r in rows]!=remaining or any(not complete(r.get("record",{})) for r in rows): raise AssertionError("Durable B1G records differ from frozen 13-slot order.")
  post=fingerprint()
  if pre!=post: raise AssertionError("Prior B1/B1A/B1B/B1C/B1D/B1E/B1F or guardrail assets changed.")
  status="PHASE_1E_B1G_PASS / REMAINING_13_BENEFICIAL_EXECUTION_COMPLETE / READY_FOR_BENEFICIAL_GUARDRAIL_ACCEPTANCE"
 except BaseException as e:
  failure=exc(e,stage,target)
  try: post=fingerprint()
  except BaseException as f: failure["post_failure_fingerprint_error"]=exc(f,"POST_FAILURE_FINGERPRINT",None)
  status="PHASE_1E_B1G_EXECUTION_INTERRUPTED"
 finally: logger.removeHandler(capture)
 incomplete=[slot for slot in persisted if slot not in completed]; manifest.update({"status":status,"ended_at":now(),"api_calls":calls,"attempted_slots":attempted,"completed_slots":completed,"generation_complete_count":len(persisted),"preserved_generation_incomplete_slots":incomplete,"protected_assets_after":post,"prior_assets_unchanged":pre==post,"registry_warnings":warnings,"failure":failure}); write(manifest_path,manifest)
 result={"phase":"PHASE_1E_B1G","status":status,"remaining_13_ordered_list_sha256":order_sha,"attempted_count":len(attempted),"completed_count":len(completed),"first_failed_slot":target if failure else None,"failure_lifecycle":failure.get("lifecycle_stage") if failure else None,"generator_invocation_count":calls["beneficial_generator"],"generation_complete_count":len(persisted),"judge_scorer_lifecycle_count":calls["beneficial_judge"],"official_score_presence_count":len(completed),"preserved_generation_incomplete_slots":incomplete,"v2_sha_verified":config["exact_treatment_instruction_sha256"]==V2_SHA,"max_retries_verified":frozen["max_retries"]==0,"custom_semantic_implementation_count":0,"prior_assets_unchanged":pre==post,"no_beneficial_aggregate_metrics_computed":True,"no_reserve_or_frozen_validation_access":True,"no_v3_created":True,"api_calls":calls,"failure":failure,"files":{"manifest":str(manifest_path.relative_to(ROOT)).replace("\\","/"),"ledger":str(ledger_path.relative_to(ROOT)).replace("\\","/"),"records":str(records_path.relative_to(ROOT)).replace("\\","/"),"integrity_report":str(report_path.relative_to(ROOT)).replace("\\","/"),"execution_report":str(markdown_path.relative_to(ROOT)).replace("\\","/")}}; write(report_path,result); markdown_path.write_text(f"# Phase 1E-B1G execution report\n\nStatus: `{status}`\n\nExecution integrity only; no Beneficial aggregate PASS metrics were computed.\n",encoding="utf-8"); return result
if __name__=="__main__":
 parser=argparse.ArgumentParser(); parser.add_argument("--execute-authorized-remaining-13",action="store_true"); args=parser.parse_args()
 print(json.dumps(asyncio.run(run()) if args.execute_authorized_remaining_13 else dry_run(),ensure_ascii=False,indent=2))
