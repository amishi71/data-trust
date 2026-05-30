# Scientific Data Trust System

A detector-inspired trust contract for scientific data. Makes data integrity **explicit, verifiable, and auditable** before compute cycles are invested in analysis.

---

## What this is

Scientific analyses assume input data is trustworthy. This assumption is almost never verified programmatically. Corruption is found late, provenance is lost, failures are silent, results cannot be reproduced.

This system applies the discipline of particle physics detector operation — trigger logic, calibration baselines, dead-channel detection, event building — to arbitrary scientific data pipelines. Every datum that passes through the system either carries a verifiable trust verdict or is quarantined with full evidence.

The central claim: **if someone hands you this system and asks "is this data trustworthy?", you can answer with evidence, not just with code.**

---

## What it does

Four things, unified under one contract:

**Invariant checking** — each datum is evaluated against a configurable set of hard (FAIL) and soft (WARN) rules. Structural checks run first; a structurally broken datum is not evaluated further.

**Provenance capture** — every datum carries an immutable chain-of-custody record: where it came from, when, what transformations were applied, under what environment. Records are SHA-256-chained. Tampering is detectable.

**Replayability** — given a provenance record, the processing can be independently reconstructed. Environment snapshots are sealed at run time. Hash mismatches between original and replay are first-class failures.

**Failure evidence** — failures are never silent. Every violation produces a timestamped, classified evidence record queryable without reading any code.

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verified on Python 3.14.4 / macOS ARM64. See `environment.yml` for the full pinned dependency specification. PyROOT is not required for v0.1.

---

## Quickstart

```python
from src.pipeline import DataTrustPipeline
from src.domain.detector_event import DetectorEventDomain

pipeline = DataTrustPipeline(
    domain=DetectorEventDomain(),
    db_path="runs/my_run",
    operator="my_username",
)

data = [
    {"event_id": "evt_001", "run_id": "run_42", "timestamp": 1700000000.0,
     "energy_gev": 125.3, "detector_channel": 14, "signal_adc": 2100.0,
     "quality_flag": 0, "calibration_run": "cal_v3"},
    # ... more events
]

result = pipeline.run(data)
result.print_summary()
result.failure_report.print()

In this example, `data` is a Python `list` of `dict` objects, where each `dict` is one record.

For a generic dataset with a user-declared schema:

```python
from src.domain.generic_tabular import GenericTabularDomain

domain = GenericTabularDomain(
    configured_bounds={"value": (0.0, 1000.0)},
)
pipeline = DataTrustPipeline(domain=domain, operator="analyst")
```

---

## Running the tests

```bash
python3 tests/test_end_to_end.py   # full integration test with printed output
python3 tests/benchmark.py         # detection rates and replay fidelity benchmarks
pytest tests/                      # unit tests (18 passed)
```

Benchmark results are written to `data/benchmark_results.json`.

---

## Benchmark results (v0.1)

| Scenario                             | N   | Detection | FP rate | Replay    |
| ------------------------------------ | --- | --------- | ------- | --------- |
| DetectorEvent — light corruption     | 500 | 100%      | 0.0%    | IDENTICAL |
| DetectorEvent — heavy corruption     | 500 | 100%      | 0.0%    | IDENTICAL |
| DetectorEvent — clean                | 300 | 100%      | 0.0%    | IDENTICAL |
| GenericTabular — moderate corruption | 400 | 66.7%     | 0.3%    | IDENTICAL |
| GenericTabular — clean               | 200 | 100%      | 0.0%    | IDENTICAL |

The 66.7% detection rate on generic tabular data is a domain-knowledge gap, not a system failure — see `DOMAIN_SPEC.md` and `TECHNICAL_REPORT.md` §4.3.

---

## Repository structure

```
TRUST_CONTRACT.md      ← formal definition of what "trusted" means
ASSUMPTIONS.md         ← documented limitations and known gaps
DECISION_LOG.md        ← architectural decisions with rationale
FAILURE_LOG.md         ← real failures found during development
DOMAIN_SPEC.md         ← specification of built-in data domains
TECHNICAL_REPORT.md    ← full evaluation and findings
CHANGELOG.md           ← verified runs and open items
environment.yml        ← pinned dependency specification
requirements.txt       ← pip install list

src/
  pipeline.py          ← unified entry point (DataTrustPipeline)
  invariants/
    contract.py        ← TrustContract, TrustVerdict
    core.py            ← invariant types (Schema, Range, Statistical, ...)
  domain/
    base.py            ← DataDomain abstract interface
    detector_event.py  ← DetectorEventDomain
    generic_tabular.py ← GenericTabularDomain
  provenance/
    record.py          ← ProvenanceRecord, EnvironmentSnapshot
    store.py           ← ProvenanceStore (SQLite, append-only)
  evidence/
    store.py           ← EvidenceStore, FailureReport
  replay/
    engine.py          ← ReplayEngine, fidelity classification
    runner.py          ← PipelineRunner, environment capture
    spec.py            ← ReplaySpec

data/
  synthetic/
    generator.py       ← synthetic data generator with injected corruptions
  benchmark_results.json
```

---

## Key design decisions

All significant decisions are documented with rationale in `DECISION_LOG.md`. Short version:

- **Per-property verdict, binary top-level outcome.** Each invariant returns PASS/WARN/FAIL. Any FAIL → UNTRUSTED. WARNs stamp provenance and pass through.
- **SQLite for both stores.** Queryable, zero external dependencies, consistent with the auditability claim.
- **No Docker in core scope.** The environment specification is `environment.yml` plus the `EnvironmentSnapshot` in every provenance record. Docker is packaging, not trust.
- **Monotonicity checking is stateless in v0.1.** A known gap. See FAILURE_LOG.md entry 001.

---

## Known limitations

See `ASSUMPTIONS.md` for the full list. Critical ones:

- **Semantic correctness is out of scope.** A `TRUSTED` verdict means structurally sound, bounds-clean, provenance-intact. It does not mean the physics is correct.
- **Replay fidelity is within-platform only.** Cross-platform floating-point divergence is documented, not hidden.
- **Monotonicity is not enforced in batch mode.** See FAILURE_LOG.md entry 001. Open before v1.0.0.
- **The `GenericTabularDomain` detects 66.7% of injected corruption.** This is a domain-knowledge gap, not a system failure. See DOMAIN_SPEC.md.

---

## Documentation map

| Document              | What it answers                                       |
| --------------------- | ----------------------------------------------------- |
| `TRUST_CONTRACT.md`   | What does "trusted" mean, formally?                   |
| `TECHNICAL_REPORT.md` | Does the system work, and what did building it teach? |
| `ASSUMPTIONS.md`      | What does the system not promise?                     |
| `DECISION_LOG.md`     | Why was X built this way?                             |
| `FAILURE_LOG.md`      | What broke, and what was done about it?               |
| `DOMAIN_SPEC.md`      | What does each domain check, and how well?            |