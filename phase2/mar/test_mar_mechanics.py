"""Offline synthetic mechanical tests; no PersistBench files or providers are read."""
from __future__ import annotations
import json
from mar_runtime import allowed_memories, memory_off, parse_router_response, router_input

def response(mode, decisions): return json.dumps({"schema_version":"mar_router_v1","task_mode":mode,"decisions":decisions})
def decision(identifier, authority, reason): return {"memory_id":identifier,"authority":authority,"reason_code":reason}

def main():
    ids=["m1","m2"]
    belief=parse_router_response(response("OBJECTIVE_JUDGMENT",[decision("m1","CONTEXT_ONLY","BELIEF_NOT_EVIDENCE"),decision("m2","BLOCK","IRRELEVANT")]),ids)
    assert belief.decisions[0]["authority"] == "CONTEXT_ONLY" and allowed_memories(["belief","noise"],belief) == []
    preference=parse_router_response(response("PERSONALIZED_DECISION",[decision("m1","ALLOW","RELEVANT_USER_PREFERENCE"),decision("m2","BLOCK","IRRELEVANT")]),ids)
    assert allowed_memories(["diet preference","irrelevant"],preference) == ["diet preference"]
    stale=parse_router_response(response("PERSONALIZED_DECISION",[decision("m1","BLOCK","STALE_OR_CONFLICTING"),decision("m2","ALLOW","RELEVANT_USER_PREFERENCE")]),ids)
    assert allowed_memories(["old preference","current preference"],stale) == ["current preference"]
    self_context=parse_router_response(response("SELF_CONTEXT_REQUEST",[decision("m1","ALLOW","RELEVANT_PERSONAL_FACT"),decision("m2","BLOCK","IRRELEVANT")]),ids)
    assert allowed_memories(["historical belief","noise"],self_context) == ["historical belief"]
    malformed=parse_router_response("not json",ids); assert malformed.router_degraded and allowed_memories(["a","b"],memory_off(ids,"MALFORMED_JSON")) == []
    duplicate=parse_router_response(response("OBJECTIVE_JUDGMENT",[decision("m1","BLOCK","IRRELEVANT"),decision("m1","BLOCK","IRRELEVANT")]),ids); assert duplicate.router_degraded and duplicate.failure_reason == "DUPLICATE_ID"
    missing=parse_router_response(response("OBJECTIVE_JUDGMENT",[decision("m1","BLOCK","IRRELEVANT")]),ids); assert not missing.router_degraded and missing.missing_id_count == 1 and missing.decisions[1]["authority"] == "BLOCK"
    assert router_input("q",[])["memories"] == []
    print(json.dumps({"status":"MAR_MECHANICAL_SANITY_PASS","cases":["belief_objective_context_only","preference_allow","irrelevant_block","stale_block","self_context_allow","malformed_memory_off","duplicate_memory_off","zero_allow_empty_context"]},indent=2))
if __name__ == "__main__": main()
