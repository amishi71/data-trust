# CHANGELOG.md

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).  
This file is updated at each tagged release.

---

## [Unreleased]

### Verified working — 2026-05-30
Full run on Python 3.14.4 / macOS ARM64 (Apple Silicon):
- `python3 tests/test_end_to_end.py` — all 4 tests passed
- `python3 tests/benchmark.py` — 5/5 scenarios complete, results saved to `data/benchmark_results.json`
- `pytest tests/` — 18 passed, 2 warnings (see FAILURE_LOG entry 004)
- `pip install -r requirements.txt` — clean install verified, all wheels pre-built, no source compilation

### Benchmark results (current)
| Scenario                             | N   | Detection | FP rate | Replay    |
| ------------------------------------ | --- | --------- | ------- | --------- |
| DetectorEvent — light corruption     | 500 | 100%      | 0.0%    | IDENTICAL |
| DetectorEvent — heavy corruption     | 500 | 100%      | 0.0%    | IDENTICAL |
| DetectorEvent — clean                | 300 | 100%      | 0.0%    | IDENTICAL |
| GenericTabular — moderate corruption | 400 | 66.7%     | 0.3%    | IDENTICAL |
| GenericTabular — clean               | 200 | 100%      | 0.0%    | IDENTICAL |

Replay fidelity: 5/5 runs IDENTICAL across all scenarios.

### Resolved since last entry
- **Failure 003** — `environment.yml` now present and verified. `requirements.txt` added as pip-native install path. Original conda-based spec superseded (was Python 3.11 / conda, actual environment is Python 3.14 / pip).

### Open items before v1.0.0
- **Failure 001** — Monotonicity not enforced in batch mode. Must fix + add test before tagging.
- **Failure 004** — pytest return value warnings in `test_end_to_end.py`. Replace `return` with `assert`.
- **ASSUMPTIONS.md §5** — Calibration baseline staleness limit still unresolved.
- **ASSUMPTIONS.md §5** — Duplicate event ID handling for `generic_tabular` still unresolved.
- `environment.yml` — verified on macOS ARM64 only. Linux/Windows not yet tested.
- `TECHNICAL_REPORT.md` — to be written. All benchmark data available.