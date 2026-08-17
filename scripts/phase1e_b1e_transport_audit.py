"""Phase 1E-B1E transport-environment audit.  It never imports a model client."""
from __future__ import annotations
import hashlib, http.client, json, os, socket, ssl, sys, traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/"artifacts"/"phase-1e"/"beneficial"/"b1e"
CONFIG=ROOT/"artifacts"/"phase-1e"/"phase-1e-v2-treatment-config.json"
B1D=ROOT/"artifacts"/"phase-1e"/"beneficial"/"b1d"/"phase-1e-b1d-execution-manifest.json"
TARGET="persistbench_ab0108b8:epoch=1"
V2_SHA="628dfc7bf07a64ee27093837f6eb790bb482c99efb8bbf784dc75070b27fa994"
def now(): return datetime.now(timezone.utc).isoformat()
def canon(value: Any): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"))
def sha(value: str): return hashlib.sha256(value.encode()).hexdigest()
def write(path: Path, value: dict[str,Any]):
 path.parent.mkdir(parents=True,exist_ok=True); temp=path.with_suffix(path.suffix+".tmp")
 with temp.open("w",encoding="utf-8",newline="\n") as f: f.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n"); f.flush(); os.fsync(f.fileno())
 temp.replace(path)
def check_endpoint(name: str, base_url: str, api_key_present: bool) -> dict[str,Any]:
 parsed=urlparse(base_url); host=parsed.hostname; port=parsed.port or 443
 out={"service":name,"base_url":f"{parsed.scheme}://{host}"+(f":{port}" if port!=443 else ""),"non_model_http_endpoint":"/models","model_inference_invoked":False,"checks":{}}
 if parsed.scheme!="https" or not host:
  out["checks"]["configuration"]={"ok":False,"detail":"Expected HTTPS base URL with hostname."}; out["healthy"]=False; return out
 out["checks"]["configuration"]={"ok":api_key_present,"detail":"API-key presence only; value not read or recorded."}
 try: addresses={i[4][0] for i in socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)}; out["checks"]["dns"]={"ok":bool(addresses),"address_count":len(addresses)}
 except BaseException as e: out["checks"]["dns"]={"ok":False,"exception":f"{type(e).__name__}: {e}"}
 try:
  with socket.create_connection((host,port),timeout=8): pass
  out["checks"]["tcp_443"]={"ok":True}
 except BaseException as e: out["checks"]["tcp_443"]={"ok":False,"exception":f"{type(e).__name__}: {e}"}
 try:
  with socket.create_connection((host,port),timeout=8) as raw:
   with ssl.create_default_context().wrap_socket(raw,server_hostname=host) as conn: out["checks"]["tls"]={"ok":True,"protocol":conn.version()}
 except BaseException as e: out["checks"]["tls"]={"ok":False,"exception":f"{type(e).__name__}: {e}"}
 try:
  conn=http.client.HTTPSConnection(host,port,timeout=10,context=ssl.create_default_context()); conn.request("GET",(parsed.path.rstrip("/")+"/models") or "/models",headers={"Authorization":"Bearer [present-but-redacted]"}); response=conn.getresponse(); response.read(0); conn.close(); out["checks"]["http_non_model"]={"ok":200<=response.status<500,"status_code":response.status,"inference_invoked":False}
 except BaseException as e: out["checks"]["http_non_model"]={"ok":False,"exception":f"{type(e).__name__}: {e}","inference_invoked":False}
 out["healthy"]=all(v.get("ok") is True for v in out["checks"].values()); return out
def run():
 report=OUT/"phase-1e-b1e-transport-audit.json"; markdown=OUT/"PHASE_1E_B1E_TRANSPORT_AUDIT.md"
 if report.exists(): raise SystemExit("B1E output/history already exists; audit rerun is forbidden.")
 config=json.loads(CONFIG.read_text(encoding="utf-8")); b1d=json.loads(B1D.read_text(encoding="utf-8")); frozen=config["frozen_execution"]
 if b1d.get("generation_attempt_2_consumed") is not False: raise AssertionError("Generation Attempt 2 is not unconsumed.")
 if config["exact_treatment_instruction_sha256"]!=V2_SHA or frozen["max_retries"]!=0: raise AssertionError("Frozen treatment/retry parity differs.")
 deepseek=check_endpoint("DeepSeek",os.environ.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",bool(os.environ.get("DEEPSEEK_API_KEY")))
 openrouter=check_endpoint("OpenRouter",os.environ.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",bool(os.environ.get("OPENROUTER_API_KEY")))
 env_names=["DEEPSEEK_API_KEY","DEEPSEEK_BASE_URL","OPENROUTER_API_KEY","OPENROUTER_BASE_URL","HTTP_PROXY","HTTPS_PROXY","NO_PROXY","REQUESTS_CA_BUNDLE","SSL_CERT_FILE","SSL_CERT_DIR","CURL_CA_BUNDLE","PYTHONHTTPSVERIFY"]
 parity={"same_repository":str(ROOT)==r"C:\Users\夏泽国\Documents\ChatGPT\PreferenceGuard","same_dataset":config["development_main_datasets"]["persistbench_beneficial_memory"]["path"],"same_v2_sha":config["exact_treatment_instruction_sha256"]==V2_SHA,"v2_sha":V2_SHA,"same_generator":frozen["generator_model"]=="deepseek/deepseek-v4-flash","generator":frozen["generator_model"],"same_judge":frozen["judge_model"]=="openrouter/moonshotai/kimi-k2-thinking","judge":frozen["judge_model"],"same_judge_configuration":frozen["judge_configuration"]=={"temperature":0,"reasoning_effort":"high","reasoning_enabled":True,"provider_order":["google-vertex"],"allow_fallbacks":False},"judge_configuration":frozen["judge_configuration"],"same_official_scorer":frozen["scorer"]=="inspect_evals/src/inspect_evals/persistbench/scorers.py@persistbench_judge","scorer":frozen["scorer"],"max_retries_zero":frozen["max_retries"]==0,"custom_semantic_implementation_count":0}
 healthy=deepseek["healthy"] and openrouter["healthy"]
 status="PHASE_1E_B1E_PASS / TRANSPORT_HEALTHY_IN_VERIFIED_RUNTIME / GENERATION_ATTEMPT_2_STILL_UNCONSUMED / READY_FOR_SINGLE_REPLACEMENT_EXECUTION_REVIEW" if healthy else "PHASE_1E_B1E_BLOCKED / TRANSPORT_REMEDIATION_UNRESOLVED / GENERATION_ATTEMPT_2_STILL_UNCONSUMED"
 result={"phase":"PHASE_1E_B1E","status":status,"mode":"TRANSPORT_ENVIRONMENT_REMEDIATION_AUDIT_ONLY","timestamp":now(),"model_inference_calls":0,"judge_calls":0,"scoring_calls":0,"generation_calls":0,"target_slot":TARGET,"generation_attempt_2_consumed":False,"generation_attempt_3_authorized":False,"current_runtime":{"execution_context":"CODEX_MANAGED_WORKSPACE_RUNTIME","python_executable":sys.executable,"virtualenv_root":str(Path(sys.executable).parents[1]),"current_working_directory":str(Path.cwd()),"parent_process_context":"UNAVAILABLE__WIN32_PROCESS_ACCESS_DENIED_IN_CURRENT_RUNTIME"},"environment":{"variables_present":{name:bool(os.environ.get(name)) for name in env_names},"dotenv_files_present":[str(p.relative_to(ROOT)).replace("\\","/") for p in ROOT.glob(".env*") if p.is_file()]},"transport":{"deepseek":deepseek,"openrouter":openrouter},"normal_local_runtime_comparison":{"result":"NOT_ESTABLISHED_FROM_CODEX_MANAGED_RUNTIME","reason":"Current runtime socket attempts are denied with WinError 10013; no external normal-local PowerShell process is observable or executable from this sandbox.","manual_check_script":"artifacts/phase-1e/beneficial/b1e/PHASE_1E_B1E_LOCAL_TRANSPORT_CHECKS.ps1"},"semantic_evaluation_parity":parity,"no_beneficial_aggregate_metrics_computed":True,"no_reserve_or_frozen_validation_access":True,"no_v3_created":True}
 write(report,result); markdown.write_text("# Phase 1E-B1E transport environment remediation audit\n\nStatus: `"+status+"`\n\nThis audit made zero model-inference, generation, Judge, and scoring calls. Generation Attempt 2 remains unconsumed. Normal local-runtime transport health is not established until the supplied local PowerShell checks have been run and their results provided.\n",encoding="utf-8")
 return result
if __name__=="__main__": print(json.dumps(run(),ensure_ascii=False,indent=2))
