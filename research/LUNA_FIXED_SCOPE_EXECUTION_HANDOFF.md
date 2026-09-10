# Luna Handoff: R16/S4 Fixed-Scope Search Semantics

## Boundary

The scientific design and implementation are frozen. Do not redesign the
treatments, scopes, order, repeats, seed, workers, budgets, hints, telemetry,
thresholds, validation, or statistics. The 72-cell grid was **not run** during
preparation.

The authoritative machine contract is:

```text
research/contracts/r16_fixed_scope_search_semantics_v1.json
```

The implementation is research-only. Do not wire it into production and do
not commit or migrate as part of execution.

## Frozen design

- 6 exact source/scope states: IA13, IA32, IA51, IA52, TOP60, TOP27.
- 4 treatments: control first-qualifying, minimum coordination, iterative
  strict-bound refinement, direct exact-v2 optimization.
- 3 independent clean eight-worker repeats per source/treatment.
- 72 cells, each reset to its copied immutable source.
- seed 101; R16/S4; 300 cumulative CP-SAT search seconds; 180 seconds for
  one-worker candidate validation; unchanged incumbent-derived hints.
- no hybrid in this initial grid.

## Stage 0: preflight

From the repository root, run exactly:

```powershell
python -m scheduling_engine.benchmark_r16_fixed_scope_search_semantics `
  --contract research/contracts/r16_fixed_scope_search_semantics_v1.json `
  --preflight
```

Do not launch the grid unless `passed` is true. Preflight verifies code
identity, fixture identity, both historical seals, six source byte hashes and
semantic fingerprints, v2 balanced source quality/counts, focused parity and
infrastructure tests, available memory, and competing processes. It executes
zero study cells.

If code identity differs, stop. Do not edit the contract to bless an
unreviewed diff. If a historical seal or source identity differs, stop. If
available memory is below 4 GiB, stop. Remove competing research/Celery solver
processes before trying preflight again.

## Stage 1: infrastructure confidence

The preflight test command already runs deterministic/synthetic treatment,
parity, budget, UNKNOWN, compactness, resume, and process-cleanup tests. Do not
use any of the six target source/scope states as a smoke test.

## Stage 2: future 72-cell launch

Only after a passing Stage 0, launch exactly:

```powershell
python -m scheduling_engine.benchmark_r16_fixed_scope_search_semantics `
  --contract research/contracts/r16_fixed_scope_search_semantics_v1.json `
  --run-grid `
  --confirm-experiment-id v2_r16_fixed_scope_search_semantics_v1 `
  --confirm-cell-count 72
```

The runner creates a unique lineage matching:

```text
C:\Users\desou\research_runs\v2_r16_fixed_scope_search_semantics_<UTC>_<8hex>
```

Record that printed path. Cells run sequentially in the contract’s persisted,
balanced order, one clean child process per cell. Never launch multiple grid
runners against the same or different lineages on this host.

## Progress and validity

Inspect, without editing:

```text
<lineage>/runner_state.json
<lineage>/logs/runner_events.jsonl
<lineage>/cells/<cell_id>/execution_XX/cell_state.json
<lineage>/cells/<cell_id>/execution_XX/resource_samples.jsonl
```

`completed_valid_cells` is the durable progress count. A promoted result is
`cells/<cell_id>/valid_result.json`, with a SHA-256 sidecar. Valid cells are
skipped on resume and the skip is logged; they are never silently rerun.

An execution is invalid if the worker crashes, the hard wall fires, host sleep
is detected, resource guards fire, identities differ, the result is malformed,
or compact telemetry exceeds 1 MiB. Search `UNKNOWN`, proven scoped
`INFEASIBLE`, or no candidate is a valid scientific cell outcome when the
worker protocol completed; do not relabel or rerun it merely to seek a better
answer. Full-model validation failure remains recorded and has zero
authoritative gain.

If the host sleeps, stop the active cell, preserve its `execution_XX`
provenance, restore normal host conditions, and resume. A wall/monotonic gap
greater than 10 seconds is contaminated. Never interpret suspended wall time
as solver time.

Resource samples are streamed every five seconds. The supervisor terminates a
cell when process-tree RSS exceeds 4 GiB or available memory drops below
1.5 GiB. Preserve that execution and investigate before resuming; do not raise
the frozen thresholds. The runner cleans observed descendants after every
cell. If cleanup is not clean, stop and remove the orphan before resume.

## Resume

Use the same reviewed contract and exact lineage:

```powershell
python -m scheduling_engine.benchmark_r16_fixed_scope_search_semantics `
  --contract research/contracts/r16_fixed_scope_search_semantics_v1.json `
  --resume <LINEAGE_PATH> `
  --confirm-experiment-id v2_r16_fixed_scope_search_semantics_v1 `
  --confirm-cell-count 72
```

Resume creates a new `execution_XX` only for a missing or invalid cell. It
preserves prior failed/contaminated directories and never resumes an in-flight
solver.

## Stage 3: analysis and sealing

After `runner_state.json` reports 72 valid cells, run:

```powershell
python -m scheduling_engine.analyze_r16_fixed_scope_search_semantics `
  --contract research/contracts/r16_fixed_scope_search_semantics_v1.json `
  --lineage <LINEAGE_PATH> `
  --analyze
```

Review the generated tables and report. The analyzer applies the frozen
paired-median, reliability, component, broad-low, and direct-versus-iterative
rules. It will refuse an incomplete grid. Do not manually change a
classification.

When review is complete, seal exactly once:

```powershell
python -m scheduling_engine.analyze_r16_fixed_scope_search_semantics `
  --contract research/contracts/r16_fixed_scope_search_semantics_v1.json `
  --lineage <LINEAGE_PATH> `
  --seal-only
```

Do not write anything under the lineage after `SEALED` exists.

## Expected lineage

```text
<lineage>/
  lineage.lock
  frozen_contract.json
  cell_order.json
  preflight.json
  runner_state.json
  source/ia13.json.gz ... top27.json.gz
  logs/runner_events.jsonl
  cells/<cell_id>/
    valid_result.json
    valid_result.sha256.json
    execution_XX/
      cell_state.json
      resource_samples.jsonl
      worker.stdout.log
      worker.stderr.log
      result.json
      candidate_source_decisions.json.gz   # only when a candidate exists
      supervisor_event.json
  analysis/
    treatment_scope_repeat.csv
    treatment_scope_repeat.json
    scope_treatment_summary.csv
    scope_treatment_summary.json
    paired_medians.csv
    paired_medians.json
    component_decomposition.csv
    component_decomposition.json
    treatment_summary.json
    runtime_and_resource_summary.json
    classifications.json
    analysis.json
  report/study_report.md
  report/study_report.json
  artifact_hashes.sha256
  SEALED
```

The primary outputs contain repeat-level identity/outcome rows, scope/treatment
distributions, paired medians, component decomposition, first-versus-final
refinement gains, objective/bound gaps, runtime and gain/minute, validation
reliability, breadth, candidate diversity, resources, preregistered treatment
classifications, and the direct-versus-iterative engineering verdict.

## What must remain unchanged

- Objective Semantics v2 and its five evaluator/model components.
- all hard, completion, mandatory, primary, and backup constraints;
- R16/S4 upper bounds and exact fixed scopes;
- current authoritative-incumbent hint values;
- full-model candidate validation and strict-gain authority;
- treatment names, 3 repeats, seed 101, 8/1 workers, 300/180 seconds;
- deterministic cell order and all thresholds in the contract.

The study can inform a later research decision only. It cannot itself promote a production policy.
