# What Does Data Trust Mean Operationally?

**Project:** Scientific Data Trust System  
**Version:** 0.1  
**Date:** 2026-05-30  
**Status:** Post-evaluation draft — all benchmark data collected  

---

## Abstract

Scientific data pipelines assume their inputs are trustworthy. This assumption is almost never verified programmatically. Corruption is detected late or not at all, provenance is lost between pipeline stages, failures are silent, and results cannot be independently reproduced.

This report describes a system built to make that assumption explicit. Drawing on the operational discipline of particle physics detector systems — trigger logic, calibration baselines, event building, dead-channel monitoring — the system defines a formal trust contract for data entering a pipeline and enforces it before any analysis runs. A datum that passes the contract carries a verifiable verdict, an immutable chain-of-custody record, and a sealed environment specification sufficient to reproduce the processing independently. A datum that fails is quarantined with timestamped, classified evidence.

The system was evaluated on synthetic detector-like data with known, measurable corruption. The `DetectorEventDomain` achieved 100% detection of injected hard-fail corruptions across all scenarios with zero false positives and bitwise-identical replay across five independent benchmark runs. The `GenericTabularDomain` achieved 66.7% detection — a result that is not a system failure but a finding: detection rate is a direct function of how much domain knowledge is encoded in the invariant set.

The central question this report answers: *can data trust be made explicit, verifiable, and auditable before compute cycles are invested in analysis?* The answer the system gives is yes — conditionally. The conditions, and what they cost, are the substance of the discussion below.

---

## 1. Problem statement

Every scientific analysis rests on a data quality assumption: that the numbers being analyzed are what they claim to be. In practice, this assumption fails silently. A bit flip in a ROOT file produces a negative energy value that propagates through a fitting routine and into a result. A missing calibration constant shifts a distribution. A pipeline re-run on a modified dataset produces different numbers than the original, with no record of what changed or why.

These are not exotic failure modes. They are the routine operational reality of data-intensive science. At HL-LHC scale — projected at one petabyte per second of detector output — the cost of analyzing untrustworthy data is not recoverable.

Existing tools address adjacent problems. Schema validators check structure, not content. Unit tests check code, not data. ML anomaly detectors lack interpretable audit trails. What is missing is a systematic layer that sits between data ingestion and analysis and answers a single question with evidence: *is this datum trustworthy?*

This system is an attempt to build that layer.

---

## 2. What "trusted" means in this system

The trust contract specifies the following formal definition:

> A datum D is *trusted* with respect to domain Ω and environment E if and only if: (1) every hard invariant defined in Ω returns PASS when evaluated against D; (2) the provenance record P(D) is present, complete, and hash-verified; and (3) the processing environment E matches the pinned specification E* within declared tolerance.

Three things are immediately notable about this definition.

First, it is an if-and-only-if. A datum that satisfies all three conditions is trusted. A datum that fails any one is untrusted and quarantined. There is no partial trust, no score, no probability. The binary verdict is a deliberate design choice: a system that issues fractional trust scores requires an aggregation function, and that function becomes load-bearing for the entire claim. Defending "0.73 trusted" to a skeptical reviewer is harder than defending the invariant set that produced a binary FAIL.

Second, trust is explicitly not a statement about scientific correctness. A datum can pass every invariant, carry an intact provenance record, and still be physically wrong — because the simulation cross-sections were incorrect, because the calibration constants were from the wrong run period, because the experimenter made an error upstream of the pipeline. The system makes trust *explicit and auditable*. It does not replace scientific judgment.

Third, the definition is falsifiable. Given the invariant set, the provenance record, and the environment specification, any reviewer can reconstruct the verdict independently. This is what distinguishes the system from a logging framework.

The contract also defines a graduated soft path: *trusted with warnings*. Soft invariants produce WARN rather than FAIL. WARNs do not block analysis, but they are stamped permanently into the provenance record and are non-removable. The top-level verdict remains binary; the evidence bundle is granular.

---

## 3. Architecture

The system has five components unified under the trust contract.

**Invariant engine.** Invariants are typed checks organized into four categories: structural (schema, type, nullability — evaluated first, short-circuit on failure), range (absolute physical bounds and configured operational bounds), relational (cross-field constraints), and statistical (distribution drift against a calibration baseline). Each invariant is independently classified as hard (FAIL on violation) or soft (WARN on violation). The classification criterion: a hard invariant is one where violation makes the datum scientifically meaningless regardless of downstream intent. A soft invariant flags an anomaly that an analyst might legitimately choose to handle. The worked examples in the trust contract — negative energy as hard, z-score outlier as soft — are not illustrative; they are the design boundary.

**Provenance layer.** Every datum that enters the system is assigned a `ProvenanceRecord`: a frozen dataclass containing the input hash, transformation history, environment snapshot, operator, and a SHA-256 content hash that chains to its parent. Records are stored in an append-only SQLite database. The chain is Merkle-like: altering any ancestor invalidates every descendant's hash. Tampering is detectable. A missing provenance record is itself a hard FAIL — not a warning, not a logged event, but a trust-contract violation at the same severity level as a negative energy value.

**Replay mechanism.** Every pipeline run produces a sealed `ReplaySpec`: a JSON document containing the input hashes, the ordered transformation sequence with per-step input/output hashes, the full environment snapshot, and a spec-level SHA-256. Given the spec and the original data, the `ReplayEngine` can reconstruct the exact sequence of operations and compare output hashes. The fidelity claim is explicitly scoped: *replay-within-platform*. Cross-platform floating-point divergence is documented in `ASSUMPTIONS.md` and is not suppressed.

**Evidence store.** Every verdict — trusted, trusted with warnings, or untrusted — is written to an append-only SQLite evidence store with full context: the invariant that fired, the observed value, the provenance record ID, the failure category, and the severity. The `FailureReport` is designed to be readable by a reviewer without access to any code.

**DataDomain plugin interface.** The trust contract is domain-agnostic. A `DataDomain` implementation supplies the invariant set, the calibration baseline, and the field mapping. The system ships with two built-in domains. Additional domains are added by subclassing `DataDomain` without modifying any core contract code. This is the architectural decision that makes the 67% versus 100% detection result interpretable rather than ambiguous.

---

## 4. Evaluation

### 4.1 Method

Evaluation was conducted on synthetic data generated by a purpose-built generator that injects known, measurable corruption. The generator records exactly which datum IDs were corrupted, what type of corruption was injected, and what the expected severity is. This allows exact measurement of detection rate (corrupted datums caught as UNTRUSTED) and false positive rate (clean datums incorrectly flagged as UNTRUSTED).

Five benchmark scenarios were run, covering both domains and a range of corruption densities:

| Scenario            | Domain         | N   | Corruption              |
| ------------------- | -------------- | --- | ----------------------- |
| Light corruption    | DetectorEvent  | 500 | ~8% of datums affected  |
| Heavy corruption    | DetectorEvent  | 500 | ~48% of datums affected |
| Clean               | DetectorEvent  | 300 | None injected           |
| Moderate corruption | GenericTabular | 400 | ~12% of datums affected |
| Clean               | GenericTabular | 200 | None injected           |

Replay fidelity was measured by re-running each scenario from its sealed `ReplaySpec` and comparing output hashes datum-by-datum.

### 4.2 Results

**DetectorEvent domain:**

| Scenario         | Trust rate | Detection | FP rate | Replay    |
| ---------------- | ---------- | --------- | ------- | --------- |
| Light corruption | 83.8%      | 100%      | 0.0%    | IDENTICAL |
| Heavy corruption | 51.6%      | 100%      | 0.0%    | IDENTICAL |
| Clean            | 100.0%     | 100%      | 0.0%    | IDENTICAL |

100% detection across all three scenarios. Zero false positives. Replay bitwise-identical. The clean scenario confirms that the invariant set does not over-fire: 300 clean datums produced 300 TRUSTED verdicts with no false alarms.

**GenericTabular domain:**

| Scenario            | Trust rate | Detection | FP rate | Replay    |
| ------------------- | ---------- | --------- | ------- | --------- |
| Moderate corruption | 87.8%      | 66.7%     | 0.3%    | IDENTICAL |
| Clean               | 100.0%     | 100%      | 0.0%    | IDENTICAL |

**Invariant effectiveness across all runs:**

| Invariant                          | Failures | Checks | Fire rate |
| ---------------------------------- | -------- | ------ | --------- |
| `energy_distribution_drift`        | 98       | 1,238  | 7.9%      |
| `adc_saturation_warning`           | 88       | 1,238  | 7.1%      |
| `energy_gev_positive`              | 78       | 1,238  | 6.3%      |
| `detector_channel_valid`           | 65       | 1,238  | 5.3%      |
| `quality_flag_bad`                 | 52       | 1,238  | 4.2%      |
| `detector_no_null_critical_fields` | 41       | 1,300  | 3.2%      |
| `statistical_drift_value`          | 27       | 578    | 4.7%      |
| `range_value`                      | 22       | 578    | 3.8%      |
| `no_null_required_fields`          | 22       | 600    | 3.7%      |
| `detector_required_fields`         | 21       | 1,300  | 1.6%      |

Provenance integrity: 400/400 records verified clean in the end-to-end test. The tamper-detection test confirmed that a single-field modification to a provenance record produces UNTRUSTED correctly.

### 4.3 The 66.7% result

The `GenericTabularDomain`'s 66.7% detection rate requires explicit discussion, because presenting it without explanation invites misreading.

The missed corruptions were not corruption types that slipped past a working invariant. They were corruption types for which the generic domain has no invariant — specifically, quality-flag-based corruption (`quality_flag = 2` indicating a bad event) and statistical outlier detection calibrated to detector physics. A domain-agnostic invariant set cannot know that a flag value of 2 means "bad" in a given context, or what the expected energy distribution of a given detector looks like.

This is not a failure of the trust contract. It is a finding about what trust contracts can and cannot do without domain knowledge. The contract's claim — *trust is explicit and auditable* — is satisfied for the invariants that exist. The 66.7% figure is the measurable cost of not having that domain knowledge encoded. A user who defines a `CustomDomain` with physics-appropriate invariants will recover the missing detection.

The one genuine anomaly in the generic tabular results is the 0.3% false positive rate (one datum in 400 flagged UNTRUSTED on clean data). This is under investigation. The most likely cause is a calibration baseline boundary condition — a clean datum whose value happens to fall exactly at the edge of the configured range. It is documented in `FAILURE_LOG.md` and does not affect the DetectorEvent results.

---

## 5. What building this system taught about data trust

Three things became clear during implementation that were not obvious from the design.

**The provenance gap is the hardest failure to accept.** When a provenance record is missing, the instinct is to treat it as a warning — the data might still be fine, after all. Making a missing provenance record a hard FAIL felt aggressive during implementation. In retrospect it is the correct decision, and it is the decision that makes the system's claim coherent rather than aspirational. A trust system that accepts data of unknown origin is not a trust system. It is a logging system with extra steps.

**The domain plugin architecture does more work than expected.** The original design motivation for `DataDomain` was extensibility — make it easy to add new data types. The unanticipated benefit was that it made the 66.7% result *interpretable*. Without the explicit domain boundary, a 66.7% detection rate on generic tabular data would look like a system failure. With it, the result is a measurement of domain knowledge density. The architecture made the limitation visible rather than hiding it in aggregate numbers.

**Replayability is a stronger claim than it first appears.** "The same inputs produce the same outputs" sounds trivial. In practice it requires: deterministic transformation functions, seed management, environment pinning, exclusion of UUIDs and timestamps from output hashes, and explicit documentation of the floating-point cases where bitwise identity is not achievable. The `ReplaySpec` is not just a convenience; it is the mechanism by which the system's reproducibility claim can be independently tested. Without it, "reproducible" is a statement of intent. With it, it is a verifiable property.

---

## 6. Limitations

The following limitations are not edge cases. They are inherent to the design and must be stated clearly.

**Semantic correctness is out of scope.** The system validates that data is structurally sound, within declared bounds, and provenance-intact. It cannot validate that the physics encoded in the data is correct. A perfectly trusted datum from a miscalibrated detector is still a trusted datum.

**Detection rate is bounded by domain knowledge.** The invariant set defines the ceiling of what can be detected. Corruption types not covered by any invariant are invisible to the system. This is expected and measurable — which is why the benchmark uses injected corruption with known expected severities rather than field data.

**Monotonicity checking is not enforced in batch mode.** The `MonotonicityInvariant` requires `context["previous_value"]` to be threaded through sequential evaluation. The current batch evaluator is stateless — each datum is evaluated independently. Non-monotonic timestamps are therefore not detected in the current implementation. This gap is documented in `FAILURE_LOG.md` entry 001 and must be resolved before v1.0.0.

**Replay fidelity is within-platform only.** Floating-point operations on different CPU architectures or BLAS backends may produce different low-order bits for identical inputs. The replay fidelity claim — 5/5 IDENTICAL across all benchmark scenarios — was obtained on a single machine (macOS ARM64). Cross-platform fidelity is not claimed and has not been tested.

**Single-node pipeline only.** The provenance model assumes all transformations run under a single provenance store. Distributed pipelines where different nodes apply different transformations require a reconciliation protocol not implemented in v0.1.

---

## 7. Conclusion

The system's central claim is that data trust can be made explicit, verifiable, and auditable before analysis. The evaluation supports this claim for the `DetectorEventDomain` with 100% detection of injected hard-fail corruptions, zero false positives, and bitwise-identical replay. It partially supports it for the `GenericTabularDomain`, where the 66.7% detection rate reflects an honest measurement of what domain-agnostic invariants can and cannot see.

What the system does not do is make data trustworthy. It makes the trust assumption visible, so that when it is violated, the violation is recorded with evidence rather than propagated silently into a result.

That is a narrower claim than it might appear. It is also, in the context of scientific computing at scale, a genuinely useful one.

---

## Appendix A — Running the system

```bash
cd data-trust-output
source .venv/bin/activate
python3 tests/test_end_to_end.py   # integration test
python3 tests/benchmark.py         # full benchmark
pytest tests/                      # unit tests (18 passed, 2 warnings)
```

Benchmark results are written to `data/benchmark_results.json`.

## Appendix B — Open items before v1.0.0

| Item                                          | Location          | Status                 |
| --------------------------------------------- | ----------------- | ---------------------- |
| Monotonicity not enforced in batch mode       | FAILURE_LOG 001   | Open                   |
| pytest return value warnings                  | FAILURE_LOG 004   | Open                   |
| Calibration baseline staleness limit          | ASSUMPTIONS.md §5 | Unresolved             |
| Duplicate event ID policy for generic_tabular | ASSUMPTIONS.md §5 | Unresolved             |
| environment.yml verified on macOS ARM64 only  | CHANGELOG         | Linux/Windows untested |

## Appendix C — Document map

| Document            | Purpose                                             |
| ------------------- | --------------------------------------------------- |
| `TRUST_CONTRACT.md` | Formal definition of trust — the load-bearing claim |
| `ASSUMPTIONS.md`    | What the system does not promise                    |
| `DECISION_LOG.md`   | Why the system was built this way                   |
| `FAILURE_LOG.md`    | What broke and what was done about it               |
| `DOMAIN_SPEC.md`    | What each domain checks and how well                |
| `CHANGELOG.md`      | Verified runs and open items                        |