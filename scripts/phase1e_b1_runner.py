"""Phase 1E-B1: execute the S1 official orchestration against Beneficial only."""
from __future__ import annotations
import asyncio, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "scripts" / "phase1e_s1_runner.py").read_text(encoding="utf-8")
source = source.replace('OUT=PHASE/"sycophancy"', 'OUT=PHASE/"beneficial"')
source = source.replace('S1=ROOT/"artifacts/phase-1e/sycophancy"', 'S1=ROOT/"artifacts/phase-1e/beneficial"')
source = source.replace('persistbench_sycophancy', 'persistbench_beneficial_memory')
source = source.replace('ds["epochs"]!=3', 'ds["epochs"]!=1')
source = source.replace('range(1,4)', 'range(1,2)')
source = source.replace('or epoch not in (1,2,3)', 'or epoch != 1')
source = source.replace('Sycophancy', 'Beneficial Memory').replace('SYCOPHANCY', 'BENEFICIAL')
source = source.replace('PHASE_1E_S1', 'PHASE_1E_B1').replace('S1_', 'B1_')
source = source.replace('phase-1e-s1', 'phase-1e-b1').replace('PHASE_1E_S1', 'PHASE_1E_B1')
source = source.replace('60_OF_60_OFFICIAL_SCORED', '20_OF_20_OFFICIAL_SCORED').replace('60-slot', '20-slot').replace('!=60', '!=20').replace('len(rows)!=60', 'len(rows)!=20').replace('len(ordered)!=60', 'len(ordered)!=20').replace('len(set(ordered))!=60', 'len(set(ordered))!=20').replace('range(1,4)', 'range(1,2)').replace('!=set(ordered)', '!=set(ordered)')
source = source.replace('SYCOPHANCY_V2_EXECUTION_COMPLETE', 'BENEFICIAL_V2_EXECUTION_COMPLETE').replace('READY_FOR_PRIMARY_METRIC_ACCEPTANCE', 'READY_FOR_BENEFICIAL_GUARDRAIL_ACCEPTANCE')
scope = {'__name__': '_b1_loaded_', '__file__': str(ROOT / 'scripts' / 'phase1e_s1_runner.py')}
exec(compile(source, str(ROOT / 'scripts' / 'phase1e_s1_runner.py'), 'exec'), scope)
print(json.dumps(asyncio.run(scope['run']()), ensure_ascii=False, indent=2))
