"""Phase 1E-S1 exact-slot Sycophancy execution orchestration only."""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, sys, time, traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"inspect_evals"/"src"))
from inspect_ai.model import GenerateConfig, get_model
from inspect_ai.model._model import ModelName
from inspect_ai._eval.task.generate import task_generate
from inspect_ai._util.notgiven import NOT_GIVEN
from inspect_ai.scorer import Target
from inspect_ai.solver import TaskState
from inspect_evals.persistbench.dataset import persistbench_dataset
from inspect_evals.persistbench.solvers import persistbench_solver
from inspect_evals.persistbench.scorers import persistbench_judge
import inspect_evals.persistbench.scorers as official_scorers

PHASE=ROOT/"artifacts"/"phase-1e"; OUT=PHASE/"sycophancy"; CONFIG=PHASE/"phase-1e-v2-treatment-config.json"; R4L=PHASE/"recovery"/"r4l"/"phase-1e-r4l-cross-domain-integrity-acceptance.json"
def now(): return datetime.now(timezone.utc).isoformat()
def canon(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def sha(x): return hashlib.sha256(x.encode()).hexdigest()
def write(path:Path,payload:dict[str,Any]):
 path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
 with tmp.open("w",encoding="utf-8",newline="\n") as f: f.write(json.dumps(payload,ensure_ascii=False,indent=2)+"\n"); f.flush(); os.fsync(f.fileno())
 tmp.replace(path)
def append(path:Path,payload:dict[str,Any]):
 path.parent.mkdir(parents=True,exist_ok=True)
 with path.open("a",encoding="utf-8",newline="\n") as f: f.write(canon(payload)+"\n"); f.flush(); os.fsync(f.fileno())
def exc(e:BaseException,stage:str,slot:str|None): return {"exception_class":type(e).__name__,"exception_message":str(e),"traceback":traceback.format_exc(),"lifecycle_stage":stage,"target_slot":slot,"captured_at":now()}
def output_json(output:Any): return output.model_dump(mode="json")
def score_json(score:Any): return score.model_dump(mode="json")
def complete(record:dict[str,Any]): return bool(record.get("output",{}).get("completion") and record.get("scores",{}).get("persistbench_judge") and record.get("id") and record.get("epoch"))

async def run() -> dict[str,Any]:
 manifest_path=OUT/"phase-1e-s1-execution-manifest.json"; ledger=OUT/"phase-1e-s1-execution-ledger.jsonl"; records_jsonl=OUT/"phase-1e-s1-completed-records.jsonl"; report_path=OUT/"phase-1e-s1-integrity-report.json"; slots_dir=OUT/"records"; markdown=OUT/"PHASE_1E_S1_EXECUTION_REPORT.md"
 calls={"sycophancy_generator":0,"sycophancy_judge":0,"cross_domain":0,"beneficial":0,"reserve":0,"frozen_validation":0}; attempted=[]; completed=[]; persisted=[]; warnings=[]; failure=None; stage="S1_PRECALL"; target=None
 try:
  if OUT.exists() or manifest_path.exists(): raise AssertionError("S1 output/history already exists; batch restart forbidden.")
  config=json.loads(CONFIG.read_text(encoding="utf-8")); r4l=json.loads(R4L.read_text(encoding="utf-8"))
  if not r4l["status"].startswith("PHASE_1E_R4L_PASS / CROSS_DOMAIN_RECOVERY_INTEGRITY_ACCEPTED"): raise AssertionError("Cross-domain R4L acceptance is not frozen PASS.")
  ds=config["development_main_datasets"]["persistbench_sycophancy"]; ids=ds["logical_sample_ids"]
  if len(ids)!=20 or len(set(ids))!=20 or ds["logical_sample_count"]!=20 or ds["epochs"]!=3: raise AssertionError("Frozen Sycophancy dataset cardinality differs.")
  if config["frozen_execution"]["max_retries"]!=0 or config["frozen_execution"]["max_connections"]!=1 or config["frozen_execution"]["adaptive_connections"] is not False: raise AssertionError("Frozen execution policy differs.")
  if config["authorization"]["development_reserve"] or config["authorization"]["frozen_validation"] or config["authorization"]["v3_or_higher"]: raise AssertionError("Unauthorized dataset/V3 authorization state.")
  dataset_path=ROOT/ds["path"]; samples={str(s.id):s for s in persistbench_dataset(dataset_path)}
  if set(samples)!=set(ids) or len(samples)!=20: raise AssertionError("Official Sycophancy dataset identity differs from frozen config.")
  ordered=[f"{sid}:epoch={epoch}" for sid in ids for epoch in range(1,4)]
  if len(ordered)!=60 or len(set(ordered))!=60: raise AssertionError("Frozen 60-slot epoch universe invalid.")
 except BaseException as e:
  blocked={"phase":"PHASE_1E_S1","status":"PHASE_1E_S1_BLOCKED","api_calls":calls,"failure":exc(e,stage,target)}; write(report_path,blocked); return blocked
 order_hash=sha(canon(ordered)); manifest={"phase":"PHASE_1E_S1","status":"RUNNING","started_at":now(),"frozen_slot_order":ordered,"frozen_slot_order_sha256":order_hash,"frozen_universe_count":60,"v2_instruction_sha256":config["exact_treatment_instruction_sha256"],"generator":config["frozen_execution"]["generator_model"],"judge":config["frozen_execution"]["judge_model"],"judge_configuration":config["frozen_execution"]["judge_configuration"],"max_retries":0,"custom_semantic_implementation_count":0,"cross_domain_r4l_status":r4l["status"],"api_calls":calls,"attempted_slots":attempted,"completed_slots":completed}; write(manifest_path,manifest)
 def mark(event:str,key:str,seq:int,**extra): append(ledger,{"event":event,"slot":key,"sequence_number":seq,"route":"SYCOPHANCY","timestamp":now(),"lifecycle_scope":"APPLICATION_ORCHESTRATION_ONLY",**extra})
 class Capture(logging.Handler):
  def emit(self,r):
   if "Unexpected exception loading entrypoints" in r.getMessage(): warnings.append(r.getMessage())
 capture=Capture(); entrylogger=logging.getLogger("inspect_ai._util.entrypoints"); entrylogger.addHandler(capture)
 solver=persistbench_solver(prompt_template=ROOT/config["prompt_template"]["treatment_prompt_template"])
 gen_config=GenerateConfig(max_retries=0,max_connections=1,adaptive_connections=False); gen_model=get_model(config["frozen_execution"]["generator_model"],config=gen_config)
 def frozen_judge(): return get_model(role="grader",default=config["frozen_execution"]["judge_model"],config=GenerateConfig(temperature=0,reasoning_effort="high",max_retries=0),provider={"order":["google-vertex"],"allow_fallbacks":False},reasoning_enabled=True)
 async def one(key:str,seq:int):
  nonlocal stage,target
  target=key; stage="SLOT_PRECHECK"; sid,epoch_text=key.split(":epoch="); epoch=int(epoch_text)
  if key not in ordered or sid not in samples or epoch not in (1,2,3): raise AssertionError("Sycophancy slot/epoch escaped frozen allowlist.")
  sample=samples[sid]; state=TaskState(model=ModelName(config["frozen_execution"]["generator_model"]),sample_id=sid,epoch=epoch,input=sample.input,messages=[],target=Target(sample.target),choices=sample.choices,output=None,completed=False,metadata=dict(sample.metadata),store={})
  attempted.append(key); mark("SLOT_STARTED",key,seq,generator_started=False,judge_started=False)
  async def official_generate(s:TaskState,tool_calls:str="loop",**kwargs:Any): return await task_generate(model=gen_model,state=s,tool_calls=tool_calls,cache=kwargs.get("cache",NOT_GIVEN),config=gen_config.merge(kwargs))
  stage="GENERATOR_CALL_ABOUT_TO_ENTER"; mark("GENERATOR_CALL_ABOUT_TO_ENTER",key,seq); calls["sycophancy_generator"]+=1; stage="GENERATOR_CALL_ENTERED"; mark("GENERATOR_CALL_ENTERED",key,seq)
  started=time.perf_counter()
  try: state=await solver(state,official_generate)
  except BaseException as e: stage="GENERATOR_RAISED"; mark("GENERATOR_RAISED",key,seq,runtime_error=exc(e,stage,key)); raise
  raw_latency=time.perf_counter()-started
  if state.output.empty or not state.output.completion or str(state.sample_id)!=sid or state.epoch!=epoch: raise AssertionError("Sycophancy generation/state incomplete.")
  stage="GENERATION_PERSISTENCE"; safe=key.replace(":","_").replace("=","_"); generation_path=slots_dir/f"{safe}.generation.json"; out=output_json(state.output); response_hash=sha(state.output.completion)
  write(generation_path,{"slot":key,"sequence_number":seq,"output":out,"response_sha256":response_hash,"generator_model":out.get("model"),"usage":out.get("usage"),"raw_generation_latency_seconds":raw_latency,"v2_instruction_sha256":config["exact_treatment_instruction_sha256"],"generation_provenance":"PHASE_1E_S1_FIRST_COMPLETED_GENERATION","immutable_after_persistence":True}); persisted.append(key); mark("GENERATION_PERSISTED",key,seq,generator_completed=True,generator_response_sha256=response_hash,raw_generation_latency_seconds=raw_latency,generation_artifact=str(generation_path.relative_to(ROOT)).replace("\\","/"))
  stage="JUDGE_SCORER_CALL_ABOUT_TO_ENTER"; mark("JUDGE_SCORER_CALL_ABOUT_TO_ENTER",key,seq); calls["sycophancy_judge"]+=1; stage="JUDGE_SCORER_CALL_ENTERED"; mark("JUDGE_SCORER_CALL_ENTERED",key,seq); jstart=time.perf_counter()
  try: score=await persistbench_judge()(state,Target(sample.target))
  except BaseException as e: stage="JUDGE_SCORER_RAISED"; mark("JUDGE_SCORER_RAISED",key,seq,runtime_error=exc(e,stage,key)); raise
  judge_latency=time.perf_counter()-jstart; stage="RECOVERED_RECORD_PERSISTENCE"; provenance={"recovery_phase":"PHASE_1E_S1","recovery_route":"SYCOPHANCY_FROZEN_DEVELOPMENT_EXECUTION","source_frozen_sample":sid,"target_epoch":epoch,"treatment_sha256":config["exact_treatment_instruction_sha256"],"generator_config":config["frozen_execution"]["generator_model"],"judge_config":config["frozen_execution"]["judge_configuration"],"scorer_identity":config["frozen_execution"]["scorer"],"generation_provenance":"PHASE_1E_S1_FIRST_COMPLETED_GENERATION","generation_response_sha256":response_hash,"score_provenance":"PHASE_1E_S1_OFFICIAL_PERSISTBENCH_SCORER"}; record={"id":sid,"epoch":epoch,"input":state.input_text,"target":state.target.target,"messages":[{"role":str(m.role),"content":m.text} for m in state.messages],"output":out,"scores":{"persistbench_judge":score_json(score)},"metadata":dict(state.metadata),"store":dict(state.store),"recovery_provenance":provenance};
  if not complete(record): raise AssertionError("Official scorer did not yield a complete Sycophancy record.")
  item={"slot":key,"record":record,"provenance":provenance}; record_path=slots_dir/f"{safe}.recovered.json"; write(record_path,item); append(records_jsonl,item); mark("RECOVERED_RECORD_PERSISTED",key,seq,generator_completed=True,judge_completed=True,scorer_completed=True,official_score_present=True,final_record_persisted=True,raw_judge_scorer_latency_seconds=judge_latency,recovered_artifact=str(record_path.relative_to(ROOT)).replace("\\","/")); completed.append(key)
 try:
  mark("S1_PRE_EXECUTION_PERSISTED",ordered[0],0,ordered_slots_sha256=order_hash,max_retries=0)
  original=official_scorers._get_judge; official_scorers._get_judge=frozen_judge
  try:
   for sequence,key in enumerate(ordered,1): await one(key,sequence)
  finally: official_scorers._get_judge=original
  rows=[json.loads(line) for line in records_jsonl.read_text(encoding="utf-8").splitlines() if line]
  if len(rows)!=60 or {x["slot"] for x in rows}!=set(ordered) or any(not complete(x["record"]) for x in rows): raise AssertionError("S1 durable completed records do not equal frozen universe.")
  status="PHASE_1E_S1_PASS / SYCOPHANCY_V2_EXECUTION_COMPLETE / 60_OF_60_OFFICIAL_SCORED / READY_FOR_PRIMARY_METRIC_ACCEPTANCE"; assembly="COMPLETE"
 except BaseException as e:
  failure=exc(e,stage,target); status="PHASE_1E_S1_EXECUTION_INTERRUPTED"; assembly="INCOMPLETE"
 finally: entrylogger.removeHandler(capture)
 incomplete=[k for k in persisted if k not in completed]; manifest.update({"status":status,"ended_at":now(),"api_calls":calls,"attempted_slots":attempted,"completed_slots":completed,"preserved_generation_incomplete_slots":incomplete,"registry_warnings":warnings,"failure":failure}); write(manifest_path,manifest)
 result={"phase":"PHASE_1E_S1","status":status,"frozen_universe_count":60,"ordered_allowlist_sha256":order_hash,"slots_attempted":attempted,"slots_completed":completed,"first_failed_slot":target if failure else None,"failure_stage":failure.get("lifecycle_stage") if failure else None,"api_calls":calls,"generation_complete_count":len(persisted),"judge_scorer_lifecycle_count":calls["sycophancy_judge"],"official_score_presence_count":len(completed),"preserved_generation_incomplete_slots":incomplete,"duplicate_slots":0,"unexpected_slots":0,"missing_slots":0 if assembly=="COMPLETE" else 60-len(completed),"v2_instruction_sha256":config["exact_treatment_instruction_sha256"],"max_retries":0,"custom_semantic_implementation_count":0,"cross_domain_closed_unchanged":True,"registry_warning_observations":warnings,"no_product_metrics_computed":True,"failure":failure,"files":{"manifest":str(manifest_path.relative_to(ROOT)).replace("\\","/"),"ledger":str(ledger.relative_to(ROOT)).replace("\\","/"),"records":str(records_jsonl.relative_to(ROOT)).replace("\\","/"),"report":str(markdown.relative_to(ROOT)).replace("\\","/")}}; write(report_path,result)
 atomic_text=lambda p,t: (p.parent.mkdir(parents=True,exist_ok=True),p.write_text(t,encoding="utf-8")); atomic_text(markdown,"# Phase 1E-S1 execution report\n\nStatus: `"+status+"`\n\nExecution integrity only; no score/PASS aggregate or product decision was computed.\n")
 return result
if __name__=="__main__": print(json.dumps(asyncio.run(run()),ensure_ascii=False,indent=2))
