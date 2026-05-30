# TRUST_CONTRACT.md

**Version:** 0.1-draft  
**Status:** Pending stress-test  
**Project:** Scientific Data Trust System  

---

## 0. Purpose of this document

This document is the intellectual core of the system. It defines, formally and operationally, what it means for a datum to be *trusted*. All code, all checks, all reports exist to enforce or verify what is written here.

A reviewer who reads only this document should be able to understand:
- What the system promises
- What it explicitly does not promise
- How to falsify its claims

If any part of the implementation cannot be traced back to a clause in this document, that part of the implementation is out of scope.

---

## 1. Formal definition of trust

**Definition 1.1 — Trusted datum**

A datum `D` is *trusted* with respect to domain `Ω` and environment `E` if and only if all three of the following conditions hold:

1. **Invariant condition:** Every hard invariant defined in `Ω`'s contract returns `PASS` when evaluated against `D`.
2. **Provenance condition:** The provenance record `P(D)` is present, complete, and cryptographically intact — meaning all required fields are populated and the input hash matches the hash of the data as received.
3. **Environment condition:** The processing environment `E` matches the pinned specification `E*` stored at pipeline initialisation, within declared tolerance.

**Definition 1.2 — Trusted with warnings**

A datum `D` is *trusted with warnings* if condition (1) holds for all hard invariants and conditions (2) and (3) hold, but one or more soft invariants return `WARN`. The datum proceeds to analysis. Every warning is permanently stamped into `P(D)` and is non-removable.

**Definition 1.3 — Untrusted datum**

A datum `D` is *untrusted* if any of the following holds:
- Any hard invariant returns `FAIL`
- The provenance record `P(D)` is absent, incomplete, or hash-mismatched
- The environment diverges from `E*` beyond declared tolerance

An untrusted datum is quarantined. It does not proceed to analysis. A `FailureRecord` is written to the failure evidence store with full context.

**What trust is not:**

Trust, as defined here, is not a statement that the datum is *scientifically correct*. A measurement can be within all declared ranges, carry a valid provenance record, and still be physically wrong — because the detector was miscalibrated, because the simulation assumptions were faulty, because the experimenter made an error upstream. This system makes trust *explicit and verifiable*; it does not replace scientific judgment.

---

## 2. Invariant taxonomy

Invariants are organised into four categories. Each category maps to a detector-physics analogue for conceptual grounding. Every invariant in the system must belong to exactly one category.

### 2.1 Structural invariants (schema)
*Detector analogue: channel existence check — is this branch present in the readout?*

Structural invariants check that the datum has the expected shape before any values are examined. They are always hard invariants. A datum that fails a structural check cannot be evaluated by any other invariant — evaluation stops and the datum is immediately untrusted.

| Invariant | Type | Description |
|-----------|------|-------------|
| `RequiredFieldInvariant` | HARD | All required fields declared in `Ω` are present |
| `TypeInvariant` | HARD | Each field holds the declared data type |
| `NullabilityInvariant` | HARD | Non-nullable fields contain no null values |
| `SchemaVersionInvariant` | HARD | Data schema version matches the contract version |

Structural invariants are evaluated first, in the order listed. If any fails, remaining invariants are not evaluated.

### 2.2 Range invariants (physical bounds)
*Detector analogue: trigger threshold — reject events outside the physically meaningful window.*

Range invariants check that values fall within bounds that are either physically mandated or domain-declared. The distinction matters:

- **Absolute bounds** are physically mandated. They cannot be overridden by configuration. Example: energy cannot be negative. A negative energy value is not an anomaly — it is evidence of corruption or a code error.
- **Configured bounds** are domain-declared. They are specified in `Ω`'s invariant config and may be updated when the domain's operational parameters change. Example: expected momentum range for a given run condition.

| Invariant | Type | Overridable |
|-----------|------|-------------|
| `AbsoluteRangeInvariant` | HARD | No |
| `ConfiguredRangeInvariant` | HARD or SOFT | Yes, in `Ω` config |
| `MonotonicityInvariant` | HARD | No — timestamps must increase |
| `FiniteValueInvariant` | HARD | No — NaN and Inf are never valid measurements |

### 2.3 Relational invariants (internal consistency)
*Detector analogue: energy-momentum conservation check — does the event make physical sense as a whole?*

Relational invariants check that relationships between fields hold. These are always evaluated after structural and range invariants pass.

| Invariant | Type | Description |
|-----------|------|-------------|
| `SumInvariant` | HARD | Column A + Column B = Column C, within tolerance ε |
| `OrderingInvariant` | HARD | Field A ≤ Field B when declared (e.g. t_start ≤ t_end) |
| `ReferentialInvariant` | HARD | Foreign key or ID reference exists in declared registry |
| `CorrelationInvariant` | SOFT | Correlation between fields within expected bounds |

The tolerance `ε` for `SumInvariant` must be declared explicitly in the contract. It is not inferred. A zero-tolerance `SumInvariant` requires exact equality — use only for integer fields.

### 2.4 Statistical invariants (distribution health)
*Detector analogue: noise floor monitoring — has the background changed in a way that suggests a detector problem?*

Statistical invariants compare the datum (or a rolling batch of data) against a calibration baseline. They are always soft by default. They may be promoted to hard if a domain explicitly declares that a distribution shift invalidates analysis.

| Invariant | Type | Description |
|-----------|------|-------------|
| `ZScoreInvariant` | SOFT | Value is within N standard deviations of calibration mean |
| `IQRInvariant` | SOFT | Value is within IQR bounds of calibration distribution |
| `KSTestInvariant` | SOFT | Batch distribution is not significantly shifted (KS test, p > threshold) |
| `DistributionShiftInvariant` | SOFT | KL divergence from baseline below declared threshold |

Statistical invariants require a calibration baseline `B(Ω)`. The baseline must be: independently derived from a held-out reference dataset, version-controlled alongside the contract, and regenerated whenever the domain's operational conditions change. Using the test data to derive the baseline is a protocol violation and will produce meaningless detection rates.

---

## 3. The WARN / FAIL line

This is the most consequential design decision in the contract. The criterion:

**A hard invariant (FAIL on violation) is one where: if the invariant is violated, the datum is scientifically meaningless *regardless of what the downstream analysis intends to do with it*.**

**A soft invariant (WARN on violation) is one where: the violation flags an anomaly that a downstream analyst might legitimately choose to handle, filter, or investigate — but the datum retains potential scientific value.**

### Worked examples — hard (FAIL)

**Example H1: Negative energy**  
An event with `energy = -4.3 GeV`. Energy is a magnitude. A negative value is not a physics result — it is evidence of a sign error, a bit flip, or a calibration failure. No downstream analysis can make use of it. Hard invariant. FAIL.

**Example H2: Missing provenance record**  
A datum arrives with no provenance record attached. The system cannot verify where this datum came from, what transformations were applied, or whether the environment was correct. The central claim of the system — that trust is auditable — collapses if provenance gaps are treated as warnings. Hard invariant. FAIL.

**Example H3: Non-monotonic timestamp in an ordered stream**  
Event 10042 has timestamp earlier than event 10041 in a stream where ordering is guaranteed by the detector's readout. This is not noise — it is evidence of a merge error, a replay error, or buffer corruption. The temporal integrity of the dataset is broken. Hard invariant. FAIL.

**Example H4: NaN in a required measurement field**  
`momentum_x = NaN`. NaN propagates silently through NumPy operations. An analysis that consumes this value will produce a NaN result with no error, no warning, and no indication that anything went wrong. This is precisely the class of silent failure the system exists to prevent. Hard invariant. FAIL.

### Worked examples — soft (WARN)

**Example S1: Z-score outlier**  
An energy value is 4.7 standard deviations above the calibration mean. This is anomalous. It may be a cosmic ray, a rare physics process, a detector glitch, or a legitimate extreme measurement. A physicist studying rare events may specifically want these. The datum is flagged, the warning is stamped into provenance, and the analyst decides. Soft invariant. WARN.

**Example S2: Correlation drift**  
The correlation between `theta` and `phi` in a batch has shifted from 0.12 to 0.31 relative to the calibration baseline. This may indicate a detector alignment change, a new run condition, or emerging systematic bias. It does not make individual data points meaningless — it means the analyst should be aware of the shift. Soft invariant. WARN.

**Example S3: Configured range boundary**  
A momentum value of 98.4 GeV/c exceeds the configured upper bound of 95 GeV/c for the current run condition. The absolute physical maximum is much higher. This may be a legitimate high-momentum event, or it may indicate that the run condition config is stale. The datum is not obviously wrong. Soft invariant. WARN — unless the domain explicitly promotes this to HARD.

### The boundary cases

These are the invariants that require explicit domain judgment. They cannot be pre-assigned:

- **Duplicate event IDs**: Hard if the domain guarantees unique IDs; soft if deduplication is an expected downstream step.
- **Missing optional fields**: Structural soft by default; promote to hard if the downstream analysis requires them.
- **Frequency gaps in time series**: Hard if gap > N seconds indicates a dead channel; soft if gaps are operationally expected during run transitions.

Every boundary case must be resolved in `Ω`'s invariant config file, not left to runtime defaults.

---

## 4. Provenance minimum specification

Every datum that enters the system must carry a provenance record `P(D)`. The record is a first-class data structure — not a log entry, not a comment, not metadata appended to a file. It is a structured object stored in the provenance store alongside the datum.

**Required fields — absence of any field is itself a hard FAIL:**

| Field | Type | Description |
|-------|------|-------------|
| `record_id` | UUID4 | Unique identifier for this provenance record |
| `datum_id` | string | Identifier of the datum this record describes |
| `input_hash` | SHA-256 hex | Hash of the datum as received, before any transformation |
| `source_path` | string | Absolute path or URI of the input file/stream |
| `ingestion_timestamp` | ISO 8601 UTC | When the datum entered the system |
| `pipeline_version` | semver string | Version of the pipeline code that processed this datum |
| `environment_hash` | SHA-256 hex | Hash of the frozen environment specification `E*` |
| `operator` | string | Identifier of the process or user that ran the pipeline |
| `domain` | string | Name of the `DataDomain` `Ω` applied |
| `contract_version` | semver string | Version of this trust contract document |
| `transformations` | list of records | Ordered list of transformations applied (see below) |
| `verdict` | enum | `TRUSTED`, `TRUSTED_WITH_WARNINGS`, or `UNTRUSTED` |
| `warnings` | list of strings | One entry per soft invariant violation; empty list if none |
| `failure_reason` | string or null | Populated only if verdict is `UNTRUSTED` |

**Transformation record — each entry in `transformations`:**

| Field | Type | Description |
|-------|------|-------------|
| `transform_id` | string | Name and version of the transformation applied |
| `input_hash` | SHA-256 hex | Hash of datum before this transformation |
| `output_hash` | SHA-256 hex | Hash of datum after this transformation |
| `timestamp` | ISO 8601 UTC | When this transformation was applied |
| `parameters` | dict | All parameters passed to the transformation |

The chain of `output_hash` → next `input_hash` must be unbroken. A gap in this chain is a provenance integrity failure — hard FAIL.

**What provenance is not:**

Provenance is not logging. A log records that something happened. Provenance records *what the datum was, where it came from, and what was done to it* — in enough detail that the processing can be independently reconstructed. A log entry saying "transform applied at 14:32" is not provenance. A transformation record with input hash, output hash, parameters, and timestamp is.

---

## 5. DataDomain plugin interface

The trust contract is domain-agnostic. A `DataDomain` `Ω` is a plugin that supplies domain-specific knowledge to the contract engine. The same trust contract — the same verdict logic, the same provenance structure, the same failure evidence store — operates across all domains.

**A conforming `DataDomain` must provide:**

```
DataDomain interface:
  name: str                          # Unique domain identifier
  version: str                       # Semver, version-controlled alongside contract
  field_schema: dict                 # Required fields, types, nullability
  hard_invariants: list[Invariant]   # Violations produce FAIL
  soft_invariants: list[Invariant]   # Violations produce WARN
  calibration_baseline: Baseline     # Reference distribution for statistical invariants
  absolute_bounds: dict              # Physically mandated limits; not overridable
  configured_bounds: dict            # Operationally declared limits; overridable
```

**What a domain may NOT do:**

- Remove or override hard invariants defined in the core contract (structural, finite value, provenance)
- Supply a calibration baseline derived from the same data being validated
- Declare a statistical invariant as hard without explicit written justification in the domain's own documentation

**Shipped domains (v0.1):**

| Domain name | Description |
|-------------|-------------|
| `detector_events` | Simulated particle detector events: energy, momentum, theta, phi, trigger flags, quality flags |
| `generic_tabular` | Generic scientific CSV/Parquet data with user-declared schema |

Additional domains are added by implementing the interface and registering the domain. No core contract code changes.

---

## 6. Environment specification

The environment `E` at pipeline execution is captured as a frozen specification `E*` at initialisation. The environment hash is the SHA-256 of the serialised `E*`.

**`E*` must include:**

| Component | Captured as |
|-----------|-------------|
| Python version | `sys.version` string |
| All installed packages | `pip freeze` or `conda list --export` |
| ROOT version | `ROOT.gROOT.GetVersion()` |
| Platform | `platform.platform()` |
| Contract version | This document's semver |
| Domain version | `Ω.version` |

**Tolerance policy:**

The environment condition in Definition 1.1 allows declared tolerance because strict bitwise environment matching is not always achievable across platforms. The tolerance rules are:

- **Zero tolerance (hard FAIL on divergence):** Python major.minor version, contract version, domain version, ROOT major version
- **Warn on divergence:** Package patch versions, platform string differences on the same OS family
- **Ignore:** Process ID, hostname, wall-clock time

Tolerance rules are frozen at pipeline initialisation and stored in `E*`. They cannot be changed at runtime.

---

## 7. Failure evidence specification

A `FailureRecord` is written to the failure evidence store every time a datum is untrusted. It is distinct from the provenance record — provenance describes the datum's history; failure evidence describes what the system observed and what it did.

**Required fields:**

| Field | Type | Description |
|-------|------|-------------|
| `failure_id` | UUID4 | Unique identifier for this failure record |
| `datum_id` | string | The datum that failed |
| `record_id` | UUID4 | The provenance record ID for cross-reference |
| `timestamp` | ISO 8601 UTC | When the failure was recorded |
| `category` | enum | `INVARIANT_VIOLATION`, `PROVENANCE_GAP`, `ENVIRONMENT_DIVERGENCE`, `REPLAY_MISMATCH` |
| `severity` | enum | `CRITICAL`, `HIGH`, `MEDIUM` |
| `violated_invariant` | string or null | Name of the invariant that failed, if applicable |
| `observed_value` | any or null | The value that triggered the failure |
| `expected` | string | Human-readable description of what was expected |
| `context` | dict | Full datum state at time of failure — enough to reproduce the failure without re-running the pipeline |
| `action_taken` | enum | `QUARANTINED`, `FLAGGED` |

**Severity assignment:**

| Category | Default severity |
|----------|-----------------|
| Structural invariant violation | CRITICAL |
| Provenance gap | CRITICAL |
| Absolute range violation | CRITICAL |
| Environment divergence (zero-tolerance field) | HIGH |
| Configured range violation | HIGH |
| Relational invariant violation | HIGH |
| Replay hash mismatch | HIGH |
| Environment divergence (warn-on-diverge field) | MEDIUM |

Severity can be overridden in the domain config, but only downward (HIGH → MEDIUM, not MEDIUM → CRITICAL). A domain may not declare any failure as lower than MEDIUM.

---

## 8. Replayability specification

**Definition 8.1 — Replayable pipeline**

A pipeline run `R` is *replayable* if, given the provenance record `P(D)` for any datum `D` processed in `R`, a second pipeline run `R'` on the same input under environment `E*` produces output `D'` such that `hash(D') == hash(D)`.

This is a strong definition. Known limitations that prevent full replayability must be declared here and nowhere else:

**Declared non-replayability:**

1. **Cross-platform floating-point divergence.** NumPy and SciPy operations may produce different low-order bits on different CPU architectures (x86 vs ARM). Replayability is guaranteed only within the same platform family declared in `E*`.

2. **ROOT version minor differences.** ROOT fitting routines may produce slightly different parameter estimates across minor versions. Replayability is guaranteed only within the same ROOT major.minor version.

3. **OS-level entropy sources.** If any pipeline stage consumes system entropy (e.g. UUID generation for record IDs), the output hashes for those fields will differ across runs. This is expected and does not constitute a replay failure — only measurement field hashes are checked.

Any divergence not covered by the above declarations is an unexplained replay mismatch — a hard FAIL, category `REPLAY_MISMATCH`.

---

## 9. What this contract does not cover

Explicit scope exclusions. These are not oversights — they are deliberate decisions that must be defended if challenged:

1. **Semantic correctness.** This contract does not verify that the physics modelled in the data is correct. A perfectly valid provenance record attached to a dataset generated with the wrong cross-section is still trusted by this system.

2. **Upstream data quality.** This contract governs data from the moment it enters this pipeline. What happened before ingestion — detector failures, simulation bugs, operator errors — is outside scope. The provenance record captures the input hash; it does not validate what produced that input.

3. **Analysis correctness.** The system delivers trusted data to the analysis stage. Whether the analysis consumes that data correctly is not covered.

4. **Real-time performance.** The system is designed for correctness, auditability, and completeness. It is not optimised for throughput. Performance benchmarks are reported as overhead measurements, not design constraints.

5. **Distributed pipeline provenance.** This version of the contract assumes a single-node pipeline. Multi-node provenance (where different machines apply different transformations) requires a distributed provenance protocol not covered here.

---

## 10. Version history

| Version | Date | Changes |
|---------|------|---------|
| 0.1-draft | TBD | Initial draft for stress-test |

---

## 11. Open questions (to be resolved before v1.0)

These questions are unresolved and must be answered before the contract is considered final. Leaving them open is intentional — closing them prematurely with bad answers is worse than acknowledging uncertainty.

1. **Duplicate event IDs**: Hard or soft by default for `generic_tabular`? Current position: hard, because silent deduplication downstream is a form of silent failure. Needs domain owner sign-off.

2. **Calibration baseline staleness**: How old can a baseline be before it is considered invalid? No current answer. Candidates: wall-clock age limit, or a drift-detection trigger. Decision needed before `KSTestInvariant` is implemented.

3. **Partial provenance**: If 3 of 4 required transformation records are present but one is missing, is the verdict UNTRUSTED (current position) or TRUSTED_WITH_WARNINGS? Current position stands unless a compelling use case requires softening.

4. **WARN accumulation threshold**: If a datum accumulates 12 WARN violations across soft invariants, should there be a mechanism to promote the overall verdict to UNTRUSTED? Current position: no — the binary verdict is based on hard invariants only. Soft invariants do not accumulate into a hard failure. Open for challenge.

