"""
TrustContract — the aggregator.

Takes a DataDomain, runs all its invariants against a datum,
collects results, and produces a TrustVerdict.

Aggregation rule (from TRUST_CONTRACT.md):
  - Any FAIL → datum is UNTRUSTED and quarantined.
  - WARNs pass through but are permanently stamped into provenance.
  - The top-level answer is binary: TRUSTED or UNTRUSTED.
  - The evidence bundle is always granular regardless of top-level verdict.

A missing provenance record is itself an UNTRUSTED verdict,
before any domain invariants are run.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from src.domain.base import DataDomain
from src.invariants.core import InvariantResult, Severity
from src.provenance.record import ProvenanceRecord


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

class Verdict(str, Enum):
    TRUSTED = "TRUSTED"
    TRUSTED_WITH_WARNINGS = "TRUSTED_WITH_WARNINGS"   # §1.2
    UNTRUSTED = "UNTRUSTED"


@dataclass
class TrustVerdict:
    """
    The output of running the TrustContract against a single datum.

    Fields
    ------
    verdict         : TRUSTED or UNTRUSTED
    datum_id        : The datum this verdict covers
    domain_name     : Which domain's invariants were applied
    domain_version  : Domain spec version at time of check
    timestamp       : When this verdict was produced
    evidence        : Full list of InvariantResult for every check run
    warnings        : Subset of evidence where severity == WARN and passed == True
    failures        : Subset of evidence where passed == False
    provenance      : The ProvenanceRecord associated with this datum (if any)
    verdict_id      : Unique ID for this verdict (used by EvidenceStore)
    """

    verdict: Verdict
    datum_id: str
    domain_name: str
    domain_version: str
    timestamp: str
    evidence: list[InvariantResult]
    warnings: list[InvariantResult]
    failures: list[InvariantResult]
    provenance: Optional[ProvenanceRecord]
    verdict_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def is_trusted(self) -> bool:
        return self.verdict in (Verdict.TRUSTED, Verdict.TRUSTED_WITH_WARNINGS)

    def summary(self) -> str:
        checks = len(self.evidence)
        fails = len(self.failures)
        warns = len(self.warnings)
        return (
            f"[{self.verdict.value}] datum={self.datum_id[:12]}… "
            f"domain={self.domain_name} "
            f"checks={checks} fails={fails} warns={warns}"
        )

    def to_dict(self) -> dict:
        return {
            "verdict_id": self.verdict_id,
            "verdict": self.verdict.value,
            "datum_id": self.datum_id,
            "domain_name": self.domain_name,
            "domain_version": self.domain_version,
            "timestamp": self.timestamp,
            "evidence": [e.to_dict() for e in self.evidence],
            "warnings": [w.to_dict() for w in self.warnings],
            "failures": [f.to_dict() for f in self.failures],
            "provenance_record_id": (
                self.provenance.record_id if self.provenance else None
            ),
        }


# ---------------------------------------------------------------------------
# TrustContract
# ---------------------------------------------------------------------------

_PROVENANCE_MISSING_RESULT = InvariantResult(
    invariant_name="provenance_present",
    severity=Severity.FAIL,
    passed=False,
    field=None,
    observed_value=None,
    message=(
        "No provenance record found for this datum. "
        "A missing chain-of-custody record is itself a FAIL — "
        "the datum cannot be trusted without a verifiable origin."
    ),
)

_PROVENANCE_TAMPERED_RESULT = InvariantResult(
    invariant_name="provenance_integrity",
    severity=Severity.FAIL,
    passed=False,
    field=None,
    observed_value=None,
    message=(
        "Provenance record hash verification failed. "
        "The chain-of-custody record has been altered after creation."
    ),
)


class TrustContract:
    """
    The trust contract engine.

    Usage
    -----
        contract = TrustContract(domain)
        verdict  = contract.evaluate(datum, provenance_record=record)

    The contract is stateless between calls. It pulls domain invariants fresh
    on each evaluation (so changes to the domain config are picked up).
    """

    def __init__(self, domain: DataDomain, require_provenance: bool = True):
        """
        Parameters
        ----------
        domain              : The DataDomain whose invariants will be run.
        require_provenance  : If True (default), a missing or tampered provenance
                              record immediately produces UNTRUSTED.
        """
        self.domain = domain
        self.require_provenance = require_provenance

    def evaluate(
        self,
        datum: dict,
        datum_id: str | None = None,
        provenance_record: ProvenanceRecord | None = None,
        context: dict | None = None,
    ) -> TrustVerdict:
        """
        Run all domain invariants against datum and return a TrustVerdict.

        Parameters
        ----------
        datum               : The data dict to evaluate.
        datum_id            : Optional explicit datum ID. If omitted, looks for
                              "id" in datum, then falls back to "unknown".
        provenance_record   : The ProvenanceRecord for this datum.
        context             : Extra context passed to invariants
                              (e.g. calibration_baseline, previous_value).
        """
        resolved_datum_id = datum_id or datum.get("id") or datum.get("event_id") or "unknown"
        timestamp = datetime.now(timezone.utc).isoformat()

        evidence: list[InvariantResult] = []

        # --- Provenance check first, before any domain invariants ---
        if self.require_provenance:
            if provenance_record is None:
                evidence.append(_PROVENANCE_MISSING_RESULT)
            elif not provenance_record.verify():
                evidence.append(_PROVENANCE_TAMPERED_RESULT)
            else:
                evidence.append(InvariantResult(
                    invariant_name="provenance_present",
                    severity=Severity.PASS,
                    passed=True,
                    field=None,
                    observed_value=provenance_record.record_id,
                    message="Provenance record present and hash-verified.",
                ))

        # --- Domain invariants ---
        # §2.1: Structural invariants run first. If any structural check fails,
        # remaining invariants are not evaluated — a structurally broken datum
        # cannot be meaningfully checked by range or statistical invariants.
        ctx = context or {}
        if "calibration_baseline" not in ctx:
            ctx["calibration_baseline"] = self.domain.get_calibration_baseline()

        structural_failed = False
        for invariant in self.domain.get_invariants():
            # Structural invariants are tagged by class name or explicit category attr
            is_structural = (
                getattr(invariant, "category", None) == "structural"
                or invariant.__class__.__name__ in ("SchemaInvariant", "NullabilityInvariant",
                                                     "TypeInvariant", "SchemaVersionInvariant")
            )
            if structural_failed and not is_structural:
                # Skip non-structural invariants once a structural failure is recorded
                continue
            result = invariant.check(datum, ctx)
            evidence.append(result)
            if is_structural and not result.passed:
                structural_failed = True

        # --- Aggregate verdict ---
        failures = [r for r in evidence if not r.passed]
        warnings = [r for r in evidence if r.passed and r.severity == Severity.WARN]

        if failures:
            verdict = Verdict.UNTRUSTED
        elif warnings:
            verdict = Verdict.TRUSTED_WITH_WARNINGS
        else:
            verdict = Verdict.TRUSTED

        return TrustVerdict(
            verdict=verdict,
            datum_id=resolved_datum_id,
            domain_name=self.domain.name,
            domain_version=self.domain.version,
            timestamp=timestamp,
            evidence=evidence,
            warnings=warnings,
            failures=failures,
            provenance=provenance_record,
        )

    def evaluate_batch(
        self,
        data: list[dict],
        provenance_records: dict[str, ProvenanceRecord] | None = None,
        context: dict | None = None,
    ) -> list[TrustVerdict]:
        """
        Evaluate a list of data dicts. provenance_records maps datum_id → record.
        Returns a list of TrustVerdict in the same order as data.
        """
        records = provenance_records or {}
        results = []
        for datum in data:
            datum_id = datum.get("id") or datum.get("event_id") or "unknown"
            prov = records.get(datum_id)
            results.append(self.evaluate(datum, datum_id=datum_id, provenance_record=prov, context=context))
        return results

    def batch_summary(self, verdicts: list[TrustVerdict]) -> dict:
        trusted = sum(1 for v in verdicts if v.is_trusted)
        untrusted = len(verdicts) - trusted
        all_failures = [r for v in verdicts for r in v.failures]
        failure_counts: dict[str, int] = {}
        for r in all_failures:
            failure_counts[r.invariant_name] = failure_counts.get(r.invariant_name, 0) + 1

        return {
            "total": len(verdicts),
            "trusted": trusted,
            "untrusted": untrusted,
            "trust_rate": trusted / len(verdicts) if verdicts else 0.0,
            "failure_breakdown": failure_counts,
            "total_warnings": sum(len(v.warnings) for v in verdicts),
        }
