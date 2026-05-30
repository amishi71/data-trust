# DECISION_LOG.md

**Project:** Scientific Data Trust System  
**Format:** One entry per significant architectural decision. Date-stamped. Each entry records the problem, options considered, the decision, and the rationale. Entries are append-only — past decisions are not edited.

---

## Entry 001 — 2026-05-27

**Problem:** How should the trust contract handle partial trust — when some invariants pass and others fail?

**Options considered:**

1. **Binary contract (pass/fail only).** A datum either satisfies all trust conditions or it doesn't. Simple, auditable, fast to implement. Risk: brittle — one miscalibrated range invariant rejects an otherwise valid event.

2. **Trust score (graduated).** Datum receives a floating-point score, e.g. 0.73. Feels more realistic. Problem: the aggregation function becomes load-bearing and is hard to defend formally. "A score of 0.73 means..." is a question with no good answer.

3. **Per-property verdict with binary top-level outcome.** Each invariant returns PASS, WARN, or FAIL independently. Contract aggregates: any FAIL → UNTRUSTED. All WARNs pass through but are stamped permanently into provenance. Top-level answer is binary; evidence bundle is granular.

**Decision:** Option 3 — per-property verdict with binary top-level outcome.

**Rationale:** Preserves formal clarity (binary verdict = unambiguous trust boundary) while retaining empirical richness (warning profile is queryable, timestamped, non-removable). Maps directly to particle physics detector logic: hard cuts vs. soft cuts. The WARN accumulation question (should N WARNs promote to UNTRUSTED?) was explicitly resolved as "no" — soft invariants never accumulate into hard failures regardless of count. This keeps the contract's claim falsifiable: a reviewer can reproduce any verdict from the evidence bundle alone.

---

## Entry 002 — 2026-05-27

**Problem:** Where should the calibration baseline come from, and how should it be stored?

**Options considered:**

1. Derive baseline from the data being validated (same dataset). Fast, automatic. Fatal flaw: creates circular validation — the system validates data against itself.

2. Derive baseline from a held-out reference dataset, hardcode into the domain class. Works for the prototype. Problem: updating the baseline requires a code change, not a config change.

3. Derive baseline from a held-out reference dataset, store in a versioned config file loaded at domain initialisation. Decouples baseline updates from code changes. Requires a clear update procedure.

**Decision:** Option 2 for v0.1 (hardcoded in domain class, clearly labeled as placeholder). Option 3 is the correct long-term path and is flagged in ASSUMPTIONS.md §2.2.

**Rationale:** For the prototype, correctness of the baseline derivation procedure is more important than its flexibility. Hardcoded baselines are transparent and auditable. The decision to use the test data to derive baselines is prohibited by contract (§2.4) and by ASSUMPTIONS.md; the hardcoded values in `DetectorEventDomain` use physically plausible placeholder statistics, not statistics derived from the synthetic test data.

---

## Entry 003 — 2026-05-27

**Problem:** Should provenance storage use JSON files, SQLite, or a document store?

**Options considered:**

1. **JSON/JSONL flat files.** Simple, human-readable, no dependencies. Problem: no query support — finding all runs with input X requires scanning all files. Provenance that can't be queried efficiently is provenance that won't be used.

2. **SQLite.** Queryable, no external dependencies, single-file, works on any machine. Well within performance envelope for millions of events. Limitations documented in ASSUMPTIONS.md §4.1.

3. **PostgreSQL or similar.** Full query power, concurrent writes, production-grade. Overkill for single-node prototype and adds an external dependency that complicates reproducibility.

**Decision:** SQLite for both provenance and evidence stores.

**Rationale:** The trust contract's central claim is that failures are auditable without reading code. That claim requires queryable storage. SQLite is the minimum viable queryable store with zero external dependencies — consistent with the project's emphasis on explicit environment specification. The interface (`ProvenanceStore`, `EvidenceStore`) is store-agnostic; swapping to PostgreSQL requires only reimplementing the store classes, not changing the contract or pipeline code.

---

## Entry 004 — 2026-05-27

**Problem:** Should the MonotonicityInvariant be checked per-datum (stateless) or across a sequence (stateful)?

**Options considered:**

1. **Per-datum, stateless.** Each datum is evaluated independently. Monotonicity cannot be checked without a previous value, so the check is skipped for isolated datums. Consistent with the pipeline's batch evaluation model.

2. **Stateful, via context threading.** The pipeline threads `context["previous_value"]` through sequential evaluation. Enables true monotonicity checking. Requires the pipeline to maintain state across datum evaluations in a batch.

**Decision:** Option 1 for v0.1 — per-datum stateless. Monotonicity check fires only when `context["previous_value"]` is explicitly provided by the caller.

**Rationale:** Option 2 is the correct long-term design but requires the pipeline runner to sort data by timestamp before evaluation and thread state across calls — a non-trivial change that adds complexity without being core to the prototype's claim. The limitation is logged in FAILURE_LOG.md as a known gap. The contract's Example H3 (non-monotonic timestamp) remains valid as a design intention; it is not yet enforced in batch mode. This must be resolved before v1.0.0.

---

## Entry 005 — 2026-05-27

**Problem:** Does the system need a Dockerfile?

**Options considered:**

1. **Docker in core scope.** Solves the PyROOT installation problem. Adds operational convenience. Risk: the Dockerfile becomes a de facto environment specification that competes with `environment.yml`, creating two sources of truth.

2. **No Docker.** The environment specification lives entirely in `environment.yml` and the `EnvironmentSnapshot` captured in every provenance record. Docker is an optional convenience layer that can be added after v1.0.0.

**Decision:** No Docker in v0.1.

**Rationale:** The contract's replayability claim (§8) is grounded in the `environment.yml` and the frozen `EnvironmentSnapshot`. Docker is a packaging mechanism, not a trust mechanism. Including it in core scope before the trust claim is proven would conflate the two. A Dockerfile added post-v1.0.0 must be documented as a convenience wrapper, not as the environment specification. The environment specification is always `environment.yml` plus the `EnvironmentSnapshot` in provenance records.

