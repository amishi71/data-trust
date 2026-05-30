# ASSUMPTIONS.md

**Project:** Scientific Data Trust System  
**Version:** 0.1  
**Status:** Active — updated as limitations are discovered during implementation  

This document records every assumption the system makes and every limitation it does not hide. It exists because a system that silently degrades is more dangerous than one with clearly documented constraints. Every entry here is a decision that was made consciously, not an oversight.

---

## 1. Replayability limitations (§8 of TRUST_CONTRACT.md)

### 1.1 Cross-platform floating-point divergence

**Status:** Known, accepted, documented.

NumPy, SciPy, and other numerical libraries may produce different low-order bits on different CPU architectures (x86 vs ARM64) and different BLAS/LAPACK backends (OpenBLAS vs MKL vs Accelerate). This is an inherent property of IEEE 754 floating-point arithmetic under different instruction ordering, not a bug in this system.

**Consequence:** Replay fidelity is guaranteed only within the same platform family declared in `E*` (the pinned environment specification). The `ReplayEngine` will classify cross-platform runs as `ENVIRONMENT_DRIFT`, not `OUTPUT_DIVERGED`. A reviewer encountering `ENVIRONMENT_DRIFT` should re-run the replay on a machine matching the original platform before concluding the pipeline is nondeterministic.

**What this system does:** The `ReplayEngine` detects environment drift, reports it prominently in the `ReplayFidelityReport`, and clearly annotates diverged datums with the note that divergence may be attributable to environmental differences rather than pipeline nondeterminism.

**What this system does not do:** Bitwise-guarantee replay results across platforms or Python versions. This is not claimed anywhere in the codebase.

---

### 1.2 Library version minor differences

**Status:** Known, accepted, documented.

Numerical library patch and minor version updates may alter low-order floating-point results even for mathematically identical operations. The tolerance policy (§6 of TRUST_CONTRACT.md) treats:

- Python `major.minor` divergence: zero-tolerance → `ENVIRONMENT_DRIFT`
- Library patch version divergence: warn-on-diverge → `ENVIRONMENT_DRIFT` with annotation
- Platform string divergence on same OS family: ignored

---

### 1.3 UUID fields in output hashes

**Status:** By design, documented.

Provenance record IDs, verdict IDs, and failure IDs are generated with `uuid.uuid4()` (random). These IDs appear in provenance records and evidence items but not in measurement data hashes. The `ReplayEngine` computes output hashes over the measurement datum dict only — not over provenance metadata. This means replay fidelity applies to data integrity, not to metadata identity.

If a pipeline injects UUIDs or timestamps into the measurement data itself (e.g. `datum["pipeline_run_id"] = str(uuid.uuid4())`), output hashes will differ across runs and the pipeline will be classified as nondeterministic. This is correct behaviour — a pipeline that injects run-specific metadata into measurement data is nondeterministic with respect to measurement content.

---

## 2. Semantic trust limitations (§9.1 of TRUST_CONTRACT.md)

### 2.1 The system does not validate physics

A datum can pass every invariant, carry an intact provenance record, and replay with perfect fidelity — and still represent a physically wrong measurement. If the detector calibration constants used were from the wrong run period, or if the simulation cross-sections were incorrect, or if the experimenter applied the wrong beam energy, this system will not detect it.

The system makes trust *explicit and auditable*. It does not substitute for physics review.

**Implication for users:** A `TRUSTED` verdict means: the data is structurally sound, values are within declared bounds, provenance is intact, and the processing environment matched the specification. It does not mean the measurement is scientifically correct.

---

### 2.2 Calibration baseline quality

Statistical invariants (§2.4 of TRUST_CONTRACT.md) compare data against a calibration baseline. The system cannot validate the quality of the baseline itself. A baseline derived from corrupted data will produce meaningless statistical check results — the invariants will pass when they should flag and flag when they should pass.

**Requirement:** Baselines must be derived from independently validated reference data, version-controlled, and regenerated whenever domain operational conditions change. This is a human process requirement, not a software requirement. The system enforces that a baseline exists; it cannot enforce that the baseline is correct.

---

## 3. Scope limitations (§9 of TRUST_CONTRACT.md)

### 3.1 Single-node pipeline only

This version of the system assumes all transformations run on a single node under a single provenance store. Distributed pipeline provenance — where different machines apply different transformations and provenance records must be reconciled across nodes — is out of scope for v0.1.

### 3.2 Upstream data quality is not covered

The system's trust boundary begins at ingestion. What happened before the data entered the pipeline — detector failures, simulation errors, operator mistakes — is not covered. The `input_hash` in the provenance record verifies that the data was not altered after ingestion; it does not verify the quality of the data as received.

### 3.3 Analysis correctness is not covered

The system delivers trusted data to downstream analysis. Whether the analysis consumes that data correctly is outside scope.

---

## 4. Implementation decisions

### 4.1 SQLite as backing store

The provenance store and evidence store use SQLite. This is appropriate for single-node, moderate-volume scientific computing (millions of events). It is not appropriate for:
- Concurrent writes from multiple processes without coordination
- Datasets exceeding ~100M rows where SQLite's write performance degrades
- Production deployments requiring ACID guarantees across distributed nodes

For those use cases, the store interface (`ProvenanceStore`, `EvidenceStore`) should be re-implemented against PostgreSQL or a document store. The query API is unchanged.

### 4.2 WARN accumulation does not promote to FAIL

Per §11.4 of TRUST_CONTRACT.md (open question), this implementation takes the position that soft invariants do not accumulate into a hard failure regardless of count. A datum with 20 WARN violations is still `TRUSTED_WITH_WARNINGS`, not `UNTRUSTED`. This decision is recorded here, not just in the contract, so it is visible without reading the contract.

---

## 5. Open questions tracked here

The following questions from §11 of TRUST_CONTRACT.md remain unresolved. This file is updated when they are resolved.

| Question | Status | Decision |
|----------|--------|----------|
| Duplicate event IDs: hard or soft for generic_tabular? | Unresolved | Current implementation: hard (SchemaInvariant checks for field presence; uniqueness requires a cross-datum check not yet implemented) |
| Calibration baseline staleness limit | Unresolved | No age limit currently enforced |
| Partial provenance: UNTRUSTED vs TRUSTED_WITH_WARNINGS? | Resolved | UNTRUSTED — any provenance gap is a hard FAIL |
| WARN accumulation promotion | Resolved | No promotion — binary verdict based on hard invariants only |
