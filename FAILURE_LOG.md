# FAILURE_LOG.md

**Project:** Scientific Data Trust System  
**Format:** Real failures encountered during development. Not invented post-hoc. One entry per distinct failure. Written at the time of discovery. Entries are append-only.

A failure here is any moment where the system behaved contrary to its stated design, or where a gap was discovered between the contract and the implementation.

---

## Failure 001 — 2026-05-27

**Component:** `MonotonicityInvariant` / `TrustContract.evaluate_batch()`

**Description:** Timestamp monotonicity checking (§2.2 of TRUST_CONTRACT.md, Example H3) is not enforced in batch evaluation mode. The `MonotonicityInvariant` requires `context["previous_value"]` to be populated by the caller. The pipeline's `evaluate_batch()` evaluates each datum independently with a fresh context dict — it does not thread the previous timestamp through. As a result, every datum in a batch sees `previous_value = None` and the check short-circuits to PASS (first-event-in-sequence logic).

**Discovered:** Code audit during documentation phase. Not caught by existing tests because `test_end_to_end.py` does not inject out-of-order timestamps as a corruption type.

**Impact:** The contract's claim that non-monotonic timestamps are detected (Example H3, severity CRITICAL) is not currently true for batch-evaluated data. Single-datum evaluation with explicit context is unaffected.

**Root cause:** The batch evaluator was designed for stateless per-datum evaluation (DECISION_LOG Entry 004). Stateful checks require a different execution model that wasn't implemented.

**Resolution status:** Open. Must be resolved before v1.0.0. Two paths:
1. Add a pre-processing step in `PipelineRunner.run()` that sorts data by timestamp and threads `previous_value` through context on each call.
2. Add a batch-level monotonicity check that runs across the full sorted dataset before per-datum evaluation begins.

**Prevention:** Add an explicit test case that injects a reversed-timestamp datum into a sorted stream and asserts UNTRUSTED verdict.

---

## Failure 002 — 2026-05-27

**Component:** `GenericTabularDomain` / benchmark

**Description:** The `GenericTabularDomain` achieves 67% detection rate on moderate corruption scenarios, compared to 100% for `DetectorEventDomain`. This is not a bug — it is an expected consequence of domain-agnostic invariants — but it was not anticipated before benchmarking and is not currently documented anywhere.

**Impact:** If the benchmark results are presented without explanation, a reviewer may interpret 67% as a system failure rather than a domain-knowledge gap. The technical report must address this explicitly.

**Root cause:** `GenericTabularDomain` ships with minimal invariants (schema, nullability, finite values, one configured range) because it cannot assume domain-specific physical bounds. Corruption types that rely on domain knowledge — e.g. `quality_flag = 2` meaning "bad" in a detector context — are invisible to a generic domain.

**Resolution status:** Documented. The 67% figure is kept in benchmark results as-is; suppressing or inflating it would be dishonest. The gap between domain-specific and domain-agnostic detection rates is a finding, not a failure to fix.

**Prevention:** Add a section to DOMAIN_SPEC.md explaining that detection rate is a function of domain knowledge density. Users defining custom domains should be aware that sparse invariant sets produce lower detection rates.

---

## Failure 003 — 2026-05-27

**Component:** Project structure

**Description:** `environment.yml` was absent from the repository at the time of the initial documentation audit. The trust contract (§6) defines a mandatory environment specification. The `EnvironmentSnapshot` in provenance records captures environment metadata at runtime, but no static specification file existed for a reviewer to reconstruct the environment from scratch.

**Impact:** The replayability claim (§8) was partially unfulfillable without `environment.yml`.

**Root cause:** Implementation proceeded without creating the environment file first.

**Resolution status:** Resolved — 2026-05-30. `environment.yml` created and added to the repository. Verified: `pip install` from the file succeeds on Python 3.14 / macOS ARM64. All dependencies (numpy, pandas, scipy, pyyaml, pytest) install cleanly with pre-built wheels; no source compilation required.

**Prevention:** `environment.yml` should be the first file created in any new project, before any code is written.

---

## Failure 004 — 2026-05-30

**Component:** `tests/test_end_to_end.py`

**Description:** Two test functions — `test_detector_domain` and `test_generic_tabular_domain` — return a dict value instead of returning `None`. pytest flags this with `PytestReturnNotNoneWarning`: *"Test functions should return None, but returned `<class 'dict'>`."*

**Discovered:** First full `pytest tests/` run — 2026-05-30. Output: 18 passed, 2 warnings.

**Impact:** Tests pass correctly. The warning does not indicate a logic error — the assertions inside the functions are valid. The issue is stylistic: pytest convention is that test functions signal failure via exceptions (assert), not return values. A dict return value is silently ignored by pytest, which means if the intent was to use the return value for assertion, that assertion never runs.

**Root cause:** Test functions were written to return summary dicts for manual inspection when run as scripts (`python3 tests/test_end_to_end.py`). When collected by pytest, the return value has no effect.

**Resolution status:** Open. Fix before v1.0.0. Replace `return summary` at the end of each affected test function with explicit assertions, e.g.:

```python
assert summary["trust_rate"] > 0
assert summary["false_negatives"] == 0
```

The script-mode print output is unaffected — add a `if __name__ == "__main__":` guard if both modes need to be supported.

**Prevention:** Test functions collected by pytest should never rely on return values. Use assert throughout.