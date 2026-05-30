# DOMAIN_SPEC.md

**Project:** Scientific Data Trust System  
**Version:** 0.1  

This document specifies the two built-in `DataDomain` implementations shipped with v0.1. It also defines what any conforming domain must provide, so that external domains can be added without modifying core code.

---

## What a domain is

A `DataDomain` is a plugin that supplies domain-specific knowledge to the trust contract engine. The contract itself — verdict logic, provenance structure, failure evidence store — stays domain-agnostic. The domain provides the invariant set, the calibration baseline, and the field mapping.

The same trust contract operates across all domains. A reviewer observing two different domains producing verdicts is seeing the same contract applied to different knowledge sets, not two different systems.

**Detection rate is a function of domain knowledge density.** A domain with sparse invariants (few hard checks, no calibration baseline) will detect fewer corruption types than a domain with rich, physics-informed invariants. The `GenericTabularDomain`'s 67% detection rate versus `DetectorEventDomain`'s 100% is not a system failure — it reflects that generic domains cannot know what domain experts know. This is documented in FAILURE_LOG.md entry 002.

---

## Interface specification

Every conforming domain must implement:

```python
class DataDomain(ABC):

    @property
    def name(self) -> str: ...         # unique domain identifier

    @property
    def version(self) -> str: ...      # semver — bump when invariants change

    def get_invariants(self) -> list[Invariant]: ...
    # Ordered list of invariants. Structural invariants must come first.
    # The contract short-circuits after the first structural FAIL.

    def get_calibration_baseline(self) -> CalibrationBaseline: ...
    # Reference distribution for statistical drift checks.
    # Must NOT be derived from the same data being validated.

    def get_field_mapping(self) -> FieldMapping: ...
    # Maps canonical field names to domain-specific names.
```

**What a domain may NOT do (from §5 of TRUST_CONTRACT.md):**
- Remove or override hard invariants defined in the core contract (provenance, finite value, structural)
- Supply a calibration baseline derived from the data being validated
- Declare a statistical invariant as hard without written justification in this document

---

## Domain 1: DetectorEventDomain

**Class:** `src/domain/detector_event.py::DetectorEventDomain`  
**Version:** `1.0.0`  
**Use case:** Particle physics detector event data, modeled on CERN-style DQM practices.

### Field schema

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `event_id` | str | No | Globally unique event identifier |
| `run_id` | str | No | Data-taking run that produced this event |
| `timestamp` | float | No | Unix epoch timestamp; must be monotone within a run |
| `energy_gev` | float | No | Reconstructed energy in GeV; must be > 0 |
| `detector_channel` | int | No | Which detector channel fired (0 to N_CHANNELS-1) |
| `signal_adc` | float | No | Raw ADC signal value (0 to 4095 for 12-bit ADC) |
| `quality_flag` | int | No | 0=good, 1=warn, 2=bad (set by online DQM system) |
| `calibration_run` | str | Yes | Which calibration run's constants were applied |

### Hard invariants (FAIL on violation)

| Invariant name | Invariant type | Condition | Justification |
|---------------|----------------|-----------|---------------|
| `required_fields` | SchemaInvariant | All 7 required fields present | Structurally broken datum cannot be evaluated |
| `non_nullable_fields` | NullabilityInvariant | No null/NaN in required fields | NaN propagates silently (Example H4) |
| `finite_values` | FiniteValueInvariant | No NaN or Inf in numeric fields | Absolute — not overridable |
| `energy_positive` | AbsoluteRangeInvariant | `energy_gev > 0` | Physically mandated; negative energy is not a physics result |
| `energy_range` | RangeInvariant | `0 < energy_gev ≤ 14000` | 14 TeV ≈ LHC centre-of-mass; events above are noise |
| `channel_range` | RangeInvariant | `0 ≤ detector_channel < 128` | Channel outside readout range is hardware error evidence |
| `adc_range` | RangeInvariant | `0 ≤ signal_adc ≤ 4095` | 12-bit ADC physical maximum |
| `timestamp_not_null` | NullabilityInvariant | `timestamp` is not null | Event without timestamp cannot be placed in the run sequence |

### Soft invariants (WARN on violation)

| Invariant name | Invariant type | Condition | Justification |
|---------------|----------------|-----------|---------------|
| `quality_flag_bad` | ThresholdInvariant | `quality_flag < 2` | DQM flag=2 indicates online system flagged it bad; analyst may override |
| `adc_saturation` | ThresholdInvariant | `signal_adc < 0.95 × 4095` | Near-saturated ADC may indicate detector issue; not definitively bad |
| `energy_drift` | StatisticalDriftInvariant | `z-score(energy_gev) < 3.5` | High z-score is anomalous but may be rare physics; analyst decides |

### Calibration baseline

Baseline source: physically plausible placeholder statistics. In production, this would be derived from a validated reference run stored in the conditions database.

| Field | Mean | Std |
|-------|------|-----|
| `energy_gev` | 125.0 | 30.0 |
| `signal_adc` | 2048.0 | 400.0 |

**Baseline version:** `v0.1-placeholder`  
**Update trigger:** Change in beam energy, detector geometry, or calibration constants.

### Benchmark results (v0.1)

| Scenario | Detection rate | False positive rate | Replay fidelity |
|----------|---------------|---------------------|-----------------|
| Light corruption (n=500) | 100% | 0% | IDENTICAL |
| Heavy corruption (n=500) | 100% | 0% | IDENTICAL |
| Clean data (n=500) | 100% (no false alarms) | 0% | IDENTICAL |

---

## Domain 2: GenericTabularDomain

**Class:** `src/domain/generic_tabular.py::GenericTabularDomain`  
**Version:** `1.0.0`  
**Use case:** Any scientific CSV or Parquet dataset where the user provides a schema. No domain-specific physical knowledge assumed.

### Field schema

User-declared at domain instantiation. The domain requires at minimum:

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `id` | str | No | Row identifier (any unique string) |
| `value` | float | No | Primary numeric measurement |
| `timestamp` | float | Yes | Optional; if present, checked for finiteness |

Additional fields are accepted without error and passed through to the invariant engine without specific checks unless the user declares bounds via `configured_bounds`.

### Hard invariants (FAIL on violation)

| Invariant name | Invariant type | Condition |
|---------------|----------------|-----------|
| `required_fields` | SchemaInvariant | `id` and `value` present |
| `non_nullable` | NullabilityInvariant | `id` and `value` not null/NaN |
| `finite_values` | FiniteValueInvariant | No NaN or Inf in declared numeric fields |
| `value_range` | ConfiguredRangeInvariant | `value` within user-declared bounds (default: -1e9 to 1e9) |

### Soft invariants (WARN on violation)

| Invariant name | Invariant type | Condition |
|---------------|----------------|-----------|
| `value_drift` | StatisticalDriftInvariant | `z-score(value) < 3.0` against baseline |

### Detection rate limitation

The `GenericTabularDomain` detects 67% of injected moderate corruption in benchmark scenarios, compared to 100% for `DetectorEventDomain`. The gap reflects domain knowledge:

- **Detected:** NaN values, out-of-range values (within configured bounds), missing required fields, Inf values
- **Not detected:** Quality flags (domain-agnostic; no concept of "bad" flag), corruption types outside configured bounds if bounds are too wide, domain-specific relational invariants

Users who know their data should define a custom domain with domain-specific invariants rather than relying on `GenericTabularDomain` for high-stakes validation.

### Benchmark results (v0.1)

| Scenario | Detection rate | False positive rate | Replay fidelity |
|----------|---------------|---------------------|-----------------|
| Moderate corruption (n=300) | 67% | 0% | IDENTICAL |
| Clean data (n=300) | 100% (no false alarms) | 0% | IDENTICAL |

---

## Adding a custom domain

To add a new domain:

1. Subclass `DataDomain` from `src/domain/base.py`
2. Implement the three required methods
3. Document your domain in this file following the format above — field schema, hard invariants table, soft invariants table, baseline source, known limitations
4. Do not derive your calibration baseline from data you will later validate
5. Register the domain in `src/domain/__init__.py`

The core contract code does not change. The new domain is immediately usable with `DataTrustPipeline(domain=MyDomain())`.

