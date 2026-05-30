"""
EvidenceStore — queryable, SQLite-backed store for trust verdicts and failure evidence.

This is not a log. It is a structured record of what failed, when, why, and what
the system did about it. A reviewer should be able to query this store and
reconstruct the complete failure history of any dataset or run without reading
any code.

Schema
------
  verdicts          : One row per TrustVerdict (one per datum per run).
  evidence_items    : One row per InvariantResult within a verdict.
  run_summaries     : One row per pipeline run (aggregated stats).

Disposition values
------------------
  QUARANTINED       : UNTRUSTED datum, held for review.
  PASSED            : TRUSTED datum, no warnings.
  PASSED_WITH_WARNS : TRUSTED datum, one or more WARNs recorded.
  ANALYST_REVIEWED  : Disposition manually updated by a reviewer.

Query API (all return structured objects, no raw SQL exposed externally)
--------
  store.record_verdict(verdict, run_id)
  store.record_run_summary(run_id, domain, summary_dict)
  store.get_verdict(verdict_id)
  store.get_failures_for_run(run_id)
  store.get_warnings_for_run(run_id)
  store.get_failures_for_invariant(invariant_name, run_id=None)
  store.get_datum_history(datum_id)
  store.get_run_summary(run_id)
  store.list_runs()
  store.failure_report(run_id)     ← human-readable, no code required
  store.update_disposition(verdict_id, disposition, analyst_notes)
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from src.invariants.contract import TrustVerdict, Verdict
from src.invariants.core import InvariantResult, Severity


# ---------------------------------------------------------------------------
# Disposition
# ---------------------------------------------------------------------------

class Disposition:
    QUARANTINED        = "QUARANTINED"
    PASSED             = "PASSED"
    PASSED_WITH_WARNS  = "PASSED_WITH_WARNS"
    ANALYST_REVIEWED   = "ANALYST_REVIEWED"

    @staticmethod
    def from_verdict(verdict: TrustVerdict) -> str:
        if not verdict.is_trusted:
            return Disposition.QUARANTINED
        if verdict.warnings:
            return Disposition.PASSED_WITH_WARNS
        return Disposition.PASSED


# ---------------------------------------------------------------------------
# §7: Failure category and severity enums
# ---------------------------------------------------------------------------

class FailureCategory:
    """§7 category enum — what class of failure this evidence records."""
    INVARIANT_VIOLATION    = "INVARIANT_VIOLATION"
    PROVENANCE_GAP         = "PROVENANCE_GAP"
    ENVIRONMENT_DIVERGENCE = "ENVIRONMENT_DIVERGENCE"
    REPLAY_MISMATCH        = "REPLAY_MISMATCH"


class FailureSeverity:
    """
    §7 severity assignment table.
    Domains may override downward (HIGH → MEDIUM) but not upward.
    No failure may be below MEDIUM.
    """
    CRITICAL = "CRITICAL"
    HIGH     = "HIGH"
    MEDIUM   = "MEDIUM"

    # §7 default severity table
    DEFAULTS: dict = {
        "structural_invariant": "CRITICAL",
        "provenance_gap":        "CRITICAL",
        "absolute_range":        "CRITICAL",
        "environment_zero_tol":  "HIGH",
        "configured_range":      "HIGH",
        "relational_invariant":  "HIGH",
        "replay_mismatch":       "HIGH",
        "environment_warn":      "MEDIUM",
    }

    @staticmethod
    def for_invariant(invariant_name: str, invariant_class_name: str = "") -> str:
        """Assign default severity from §7 table based on invariant type."""
        name_lower = invariant_name.lower()
        cls_lower  = invariant_class_name.lower()
        if "provenance" in name_lower:
            return FailureSeverity.CRITICAL
        if "absoluterange" in cls_lower or "absoluterange" in name_lower:
            return FailureSeverity.CRITICAL
        if cls_lower in ("schemainvariant", "nullabilityinvariant",
                         "finitevalueinvariant", "typeinvariant"):
            return FailureSeverity.CRITICAL
        if cls_lower in ("relationalinvariant", "orderinginvariant",
                         "suminvariant", "referentialinvariant"):
            return FailureSeverity.HIGH
        if "range" in cls_lower or "range" in name_lower:
            return FailureSeverity.HIGH
        return FailureSeverity.MEDIUM


# ---------------------------------------------------------------------------
# Query result types
# ---------------------------------------------------------------------------

@dataclass
class VerdictRow:
    verdict_id: str
    run_id: str
    datum_id: str
    verdict: str
    disposition: str
    domain_name: str
    domain_version: str
    timestamp: str
    failure_count: int
    warning_count: int
    provenance_record_id: Optional[str]
    analyst_notes: str

    def is_trusted(self) -> bool:
        return self.verdict == Verdict.TRUSTED.value


@dataclass
class EvidenceRow:
    evidence_id: str
    verdict_id: str
    run_id: str
    datum_id: str
    invariant_name: str
    severity: str
    passed: bool
    field: Optional[str]
    observed_value: str
    message: str
    notes: str
    timestamp: str
    # §7 fields
    failure_category: str = "INVARIANT_VIOLATION"
    failure_severity: str = "HIGH"
    action_taken: str = "QUARANTINED"
    context_json: str = "{}"

    def context(self) -> dict:
        import json as _json
        return _json.loads(self.context_json)


@dataclass
class RunSummaryRow:
    run_id: str
    domain_name: str
    domain_version: str
    operator: str
    started_at: str
    total_datums: int
    trusted: int
    untrusted: int
    trust_rate: float
    total_warnings: int
    failure_breakdown_json: str
    notes: str

    def failure_breakdown(self) -> dict:
        return json.loads(self.failure_breakdown_json)

    def print_summary(self) -> None:
        print(f"\n  Run: {self.run_id}")
        print(f"  Domain: {self.domain_name} v{self.domain_version}")
        print(f"  Operator: {self.operator}")
        print(f"  Started: {self.started_at}")
        print(f"  Total: {self.total_datums} | Trusted: {self.trusted} | "
              f"Untrusted: {self.untrusted} | Trust rate: {self.trust_rate:.1%}")
        print(f"  Warnings: {self.total_warnings}")
        bd = self.failure_breakdown()
        if bd:
            print("  Failure breakdown:")
            for inv, count in sorted(bd.items(), key=lambda x: -x[1]):
                print(f"    {inv}: {count}")


# ---------------------------------------------------------------------------
# FailureReport — human-readable, no code required
# ---------------------------------------------------------------------------

@dataclass
class FailureReport:
    run_id: str
    domain_name: str
    generated_at: str
    total_datums: int
    trusted: int
    untrusted: int
    trust_rate: float
    failure_rows: list[EvidenceRow]
    warning_rows: list[EvidenceRow]
    quarantined_datum_ids: list[str]

    def print(self) -> None:
        lines = [
            "",
            "=" * 70,
            f"FAILURE EVIDENCE REPORT",
            f"  run_id    : {self.run_id}",
            f"  domain    : {self.domain_name}",
            f"  generated : {self.generated_at}",
            f"  datums    : {self.total_datums} total | "
            f"{self.trusted} trusted | {self.untrusted} untrusted | "
            f"{self.trust_rate:.1%} trust rate",
            "=" * 70,
        ]

        if not self.failure_rows:
            lines.append("  No failures recorded.")
        else:
            # Group failures by invariant
            by_invariant: dict[str, list[EvidenceRow]] = {}
            for row in self.failure_rows:
                by_invariant.setdefault(row.invariant_name, []).append(row)

            lines.append(f"\n  FAILURES ({len(self.failure_rows)} total across "
                         f"{len(self.quarantined_datum_ids)} datums)\n")
            for inv_name, rows in sorted(by_invariant.items(), key=lambda x: -len(x[1])):
                lines.append(f"  [{inv_name}]  — {len(rows)} occurrence(s)")
                for row in rows[:3]:   # show first 3 per invariant
                    lines.append(
                        f"    datum={row.datum_id[:16]}…  "
                        f"field={row.field or 'n/a'}  "
                        f"value={row.observed_value[:40] if row.observed_value else 'n/a'}"
                    )
                    lines.append(f"    → {row.message}")
                if len(rows) > 3:
                    lines.append(f"    … and {len(rows) - 3} more.")
                lines.append("")

        if self.warning_rows:
            by_invariant_w: dict[str, list[EvidenceRow]] = {}
            for row in self.warning_rows:
                by_invariant_w.setdefault(row.invariant_name, []).append(row)

            lines.append(f"  WARNINGS ({len(self.warning_rows)} total — datums passed but anomalies recorded)\n")
            for inv_name, rows in sorted(by_invariant_w.items(), key=lambda x: -len(x[1])):
                lines.append(f"  [{inv_name}]  — {len(rows)} occurrence(s)")
                for row in rows[:2]:
                    lines.append(
                        f"    datum={row.datum_id[:16]}…  "
                        f"field={row.field or 'n/a'}  "
                        f"value={row.observed_value[:40] if row.observed_value else 'n/a'}"
                    )
                if len(rows) > 2:
                    lines.append(f"    … and {len(rows) - 2} more.")
                lines.append("")

        lines.append("=" * 70)
        print("\n".join(lines))

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "domain_name": self.domain_name,
            "generated_at": self.generated_at,
            "total_datums": self.total_datums,
            "trusted": self.trusted,
            "untrusted": self.untrusted,
            "trust_rate": self.trust_rate,
            "failure_count": len(self.failure_rows),
            "warning_count": len(self.warning_rows),
            "quarantined_datum_ids": self.quarantined_datum_ids,
            "failures_by_invariant": {
                inv: len([r for r in self.failure_rows if r.invariant_name == inv])
                for inv in {r.invariant_name for r in self.failure_rows}
            },
            "warnings_by_invariant": {
                inv: len([r for r in self.warning_rows if r.invariant_name == inv])
                for inv in {r.invariant_name for r in self.warning_rows}
            },
        }


# ---------------------------------------------------------------------------
# EvidenceStore
# ---------------------------------------------------------------------------

class EvidenceStore:
    """
    Queryable SQLite store for trust verdicts and failure evidence.

    Usage
    -----
        store = EvidenceStore(":memory:")
        store = EvidenceStore("evidence.db")

        store.record_verdict(verdict, run_id="RUN_001")
        store.record_run_summary("RUN_001", "DetectorEvent", "1.0.0", "operator", summary)

        report = store.failure_report("RUN_001")
        report.print()
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS verdicts (
        verdict_id              TEXT PRIMARY KEY,
        run_id                  TEXT NOT NULL,
        datum_id                TEXT NOT NULL,
        verdict                 TEXT NOT NULL,
        disposition             TEXT NOT NULL,
        domain_name             TEXT NOT NULL,
        domain_version          TEXT NOT NULL,
        timestamp               TEXT NOT NULL,
        failure_count           INTEGER NOT NULL,
        warning_count           INTEGER NOT NULL,
        provenance_record_id    TEXT,
        analyst_notes           TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS evidence_items (
        evidence_id       TEXT PRIMARY KEY,
        verdict_id        TEXT NOT NULL,
        run_id            TEXT NOT NULL,
        datum_id          TEXT NOT NULL,
        invariant_name    TEXT NOT NULL,
        severity          TEXT NOT NULL,
        passed            INTEGER NOT NULL,
        field             TEXT,
        observed_value    TEXT,
        message           TEXT NOT NULL,
        notes             TEXT NOT NULL DEFAULT '',
        timestamp         TEXT NOT NULL,
        failure_category  TEXT NOT NULL DEFAULT 'INVARIANT_VIOLATION',
        failure_severity  TEXT NOT NULL DEFAULT 'HIGH',
        action_taken      TEXT NOT NULL DEFAULT 'QUARANTINED',
        context_json      TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (verdict_id) REFERENCES verdicts(verdict_id)
    );

    CREATE TABLE IF NOT EXISTS run_summaries (
        run_id                  TEXT PRIMARY KEY,
        domain_name             TEXT NOT NULL,
        domain_version          TEXT NOT NULL,
        operator                TEXT NOT NULL,
        started_at              TEXT NOT NULL,
        total_datums            INTEGER NOT NULL,
        trusted                 INTEGER NOT NULL,
        untrusted               INTEGER NOT NULL,
        trust_rate              REAL NOT NULL,
        total_warnings          INTEGER NOT NULL,
        failure_breakdown_json  TEXT NOT NULL,
        notes                   TEXT NOT NULL DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_verdicts_run_id   ON verdicts(run_id);
    CREATE INDEX IF NOT EXISTS idx_verdicts_datum_id ON verdicts(datum_id);
    CREATE INDEX IF NOT EXISTS idx_verdicts_verdict  ON verdicts(verdict);
    CREATE INDEX IF NOT EXISTS idx_evidence_run_id   ON evidence_items(run_id);
    CREATE INDEX IF NOT EXISTS idx_evidence_invariant ON evidence_items(invariant_name);
    CREATE INDEX IF NOT EXISTS idx_evidence_passed   ON evidence_items(passed);
    """

    def __init__(self, db_path: str | Path = ":memory:"):
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def record_verdict(self, verdict: TrustVerdict, run_id: str) -> None:
        """Persist a TrustVerdict and all its InvariantResults."""
        import uuid as _uuid
        disposition = Disposition.from_verdict(verdict)
        prov_id = verdict.provenance.record_id if verdict.provenance else None

        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO verdicts VALUES (
                    :verdict_id, :run_id, :datum_id, :verdict, :disposition,
                    :domain_name, :domain_version, :timestamp,
                    :failure_count, :warning_count, :provenance_record_id, ''
                )
                """,
                {
                    "verdict_id": verdict.verdict_id,
                    "run_id": run_id,
                    "datum_id": verdict.datum_id,
                    "verdict": verdict.verdict.value,
                    "disposition": disposition,
                    "domain_name": verdict.domain_name,
                    "domain_version": verdict.domain_version,
                    "timestamp": verdict.timestamp,
                    "failure_count": len(verdict.failures),
                    "warning_count": len(verdict.warnings),
                    "provenance_record_id": prov_id,
                },
            )

            for item in verdict.evidence:
                # §7: derive failure category from invariant name
                inv_lower = item.invariant_name.lower()
                if "provenance" in inv_lower:
                    category = FailureCategory.PROVENANCE_GAP
                elif "replay" in inv_lower or "environment" in inv_lower:
                    category = FailureCategory.ENVIRONMENT_DIVERGENCE
                else:
                    category = FailureCategory.INVARIANT_VIOLATION

                # §7: assign failure severity from table
                fseverity = FailureSeverity.for_invariant(item.invariant_name)

                action = "QUARANTINED" if not verdict.is_trusted else "FLAGGED"

                # §7: context bundle — full datum state at time of failure
                context_bundle = {
                    "datum_id": verdict.datum_id,
                    "domain": verdict.domain_name,
                    "invariant_name": item.invariant_name,
                    "field": item.field,
                    "observed_value": str(item.observed_value),
                    "provenance_record_id": prov_id,
                }

                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence_items VALUES (
                        :evidence_id, :verdict_id, :run_id, :datum_id,
                        :invariant_name, :severity, :passed, :field,
                        :observed_value, :message, :notes, :timestamp,
                        :failure_category, :failure_severity, :action_taken, :context_json
                    )
                    """,
                    {
                        "evidence_id": str(_uuid.uuid4()),
                        "verdict_id": verdict.verdict_id,
                        "run_id": run_id,
                        "datum_id": verdict.datum_id,
                        "invariant_name": item.invariant_name,
                        "severity": item.severity.value,
                        "passed": 1 if item.passed else 0,
                        "field": item.field,
                        "observed_value": str(item.observed_value)[:500],
                        "message": item.message[:1000],
                        "notes": item.notes[:500],
                        "timestamp": verdict.timestamp,
                        "failure_category": category,
                        "failure_severity": fseverity,
                        "action_taken": action,
                        "context_json": json.dumps(context_bundle),
                    },
                )

    def record_many_verdicts(self, verdicts: list[TrustVerdict], run_id: str) -> None:
        for v in verdicts:
            self.record_verdict(v, run_id)

    def record_run_summary(
        self,
        run_id: str,
        domain_name: str,
        domain_version: str,
        operator: str,
        started_at: str,
        summary: dict,
        notes: str = "",
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_summaries VALUES (
                    :run_id, :domain_name, :domain_version, :operator, :started_at,
                    :total_datums, :trusted, :untrusted, :trust_rate,
                    :total_warnings, :failure_breakdown_json, :notes
                )
                """,
                {
                    "run_id": run_id,
                    "domain_name": domain_name,
                    "domain_version": domain_version,
                    "operator": operator,
                    "started_at": started_at,
                    "total_datums": summary.get("total", 0),
                    "trusted": summary.get("trusted", 0),
                    "untrusted": summary.get("untrusted", 0),
                    "trust_rate": summary.get("trust_rate", 0.0),
                    "total_warnings": summary.get("total_warnings", 0),
                    "failure_breakdown_json": json.dumps(
                        summary.get("failure_breakdown", {})
                    ),
                    "notes": notes,
                },
            )

    def update_disposition(
        self,
        verdict_id: str,
        disposition: str,
        analyst_notes: str = "",
    ) -> bool:
        """
        Update the disposition of a verdict (e.g. after analyst review).
        Returns True if a row was updated.
        """
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE verdicts SET disposition = ?, analyst_notes = ? WHERE verdict_id = ?",
                (disposition, analyst_notes, verdict_id),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Read — verdicts
    # ------------------------------------------------------------------

    def get_verdict(self, verdict_id: str) -> Optional[VerdictRow]:
        row = self._conn.execute(
            "SELECT * FROM verdicts WHERE verdict_id = ?", (verdict_id,)
        ).fetchone()
        return self._row_to_verdict(row) if row else None

    def get_verdicts_for_run(self, run_id: str) -> list[VerdictRow]:
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE run_id = ? ORDER BY timestamp",
            (run_id,),
        ).fetchall()
        return [self._row_to_verdict(r) for r in rows]

    def get_failures_for_run(self, run_id: str) -> list[VerdictRow]:
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE run_id = ? AND verdict = 'UNTRUSTED' ORDER BY timestamp",
            (run_id,),
        ).fetchall()
        return [self._row_to_verdict(r) for r in rows]

    def get_datum_history(self, datum_id: str) -> list[VerdictRow]:
        """All verdicts ever issued for a datum, across all runs."""
        rows = self._conn.execute(
            "SELECT * FROM verdicts WHERE datum_id = ? ORDER BY timestamp",
            (datum_id,),
        ).fetchall()
        return [self._row_to_verdict(r) for r in rows]

    # ------------------------------------------------------------------
    # Read — evidence items
    # ------------------------------------------------------------------

    def get_evidence_for_verdict(self, verdict_id: str) -> list[EvidenceRow]:
        rows = self._conn.execute(
            "SELECT * FROM evidence_items WHERE verdict_id = ? ORDER BY severity DESC",
            (verdict_id,),
        ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def get_failures_for_invariant(
        self, invariant_name: str, run_id: str | None = None
    ) -> list[EvidenceRow]:
        """All FAIL-level evidence for a specific invariant, optionally filtered by run."""
        if run_id:
            rows = self._conn.execute(
                """SELECT * FROM evidence_items
                   WHERE invariant_name = ? AND run_id = ? AND passed = 0
                   ORDER BY timestamp""",
                (invariant_name, run_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM evidence_items
                   WHERE invariant_name = ? AND passed = 0
                   ORDER BY timestamp""",
                (invariant_name,),
            ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    def get_warnings_for_run(self, run_id: str) -> list[EvidenceRow]:
        rows = self._conn.execute(
            """SELECT * FROM evidence_items
               WHERE run_id = ? AND severity = 'WARN' AND passed = 1
               ORDER BY invariant_name, timestamp""",
            (run_id,),
        ).fetchall()
        return [self._row_to_evidence(r) for r in rows]

    # ------------------------------------------------------------------
    # Read — run summaries
    # ------------------------------------------------------------------

    def get_run_summary(self, run_id: str) -> Optional[RunSummaryRow]:
        row = self._conn.execute(
            "SELECT * FROM run_summaries WHERE run_id = ?", (run_id,)
        ).fetchone()
        return self._row_to_summary(row) if row else None

    def list_runs(self) -> list[RunSummaryRow]:
        rows = self._conn.execute(
            "SELECT * FROM run_summaries ORDER BY started_at DESC"
        ).fetchall()
        return [self._row_to_summary(r) for r in rows]

    # ------------------------------------------------------------------
    # Compound queries
    # ------------------------------------------------------------------

    def invariant_failure_rates(self, run_id: str | None = None) -> list[dict]:
        """
        For each invariant, return failure count, total checks, and failure rate.
        Useful for identifying which invariants fire most often.
        """
        if run_id:
            rows = self._conn.execute(
                """
                SELECT invariant_name,
                       COUNT(*) as total,
                       SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) as failures
                FROM evidence_items
                WHERE run_id = ?
                GROUP BY invariant_name
                ORDER BY failures DESC
                """,
                (run_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT invariant_name,
                       COUNT(*) as total,
                       SUM(CASE WHEN passed = 0 THEN 1 ELSE 0 END) as failures
                FROM evidence_items
                GROUP BY invariant_name
                ORDER BY failures DESC
                """
            ).fetchall()

        return [
            {
                "invariant_name": r["invariant_name"],
                "total_checks": r["total"],
                "failures": r["failures"],
                "failure_rate": r["failures"] / r["total"] if r["total"] > 0 else 0.0,
            }
            for r in rows
        ]

    def most_problematic_datums(
        self, run_id: str, limit: int = 10
    ) -> list[dict]:
        """Datums with the most failure evidence items, for a given run."""
        rows = self._conn.execute(
            """
            SELECT datum_id, COUNT(*) as failure_count
            FROM evidence_items
            WHERE run_id = ? AND passed = 0
            GROUP BY datum_id
            ORDER BY failure_count DESC
            LIMIT ?
            """,
            (run_id, limit),
        ).fetchall()
        return [{"datum_id": r["datum_id"], "failure_count": r["failure_count"]} for r in rows]

    # ------------------------------------------------------------------
    # Human-readable failure report
    # ------------------------------------------------------------------

    def failure_report(self, run_id: str) -> FailureReport:
        """
        Generate a complete FailureReport for a run.
        A reviewer can read this without touching any code.
        """
        summary_row = self.get_run_summary(run_id)
        failures = self._conn.execute(
            "SELECT * FROM evidence_items WHERE run_id = ? AND passed = 0 ORDER BY invariant_name",
            (run_id,),
        ).fetchall()
        warnings = self._conn.execute(
            "SELECT * FROM evidence_items WHERE run_id = ? AND severity = 'WARN' AND passed = 1 ORDER BY invariant_name",
            (run_id,),
        ).fetchall()
        quarantined = self._conn.execute(
            "SELECT datum_id FROM verdicts WHERE run_id = ? AND verdict = 'UNTRUSTED'",
            (run_id,),
        ).fetchall()

        return FailureReport(
            run_id=run_id,
            domain_name=summary_row.domain_name if summary_row else "unknown",
            generated_at=datetime.now(timezone.utc).isoformat(),
            total_datums=summary_row.total_datums if summary_row else 0,
            trusted=summary_row.trusted if summary_row else 0,
            untrusted=summary_row.untrusted if summary_row else 0,
            trust_rate=summary_row.trust_rate if summary_row else 0.0,
            failure_rows=[self._row_to_evidence(r) for r in failures],
            warning_rows=[self._row_to_evidence(r) for r in warnings],
            quarantined_datum_ids=[r["datum_id"] for r in quarantined],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_verdict(row: sqlite3.Row) -> VerdictRow:
        return VerdictRow(
            verdict_id=row["verdict_id"],
            run_id=row["run_id"],
            datum_id=row["datum_id"],
            verdict=row["verdict"],
            disposition=row["disposition"],
            domain_name=row["domain_name"],
            domain_version=row["domain_version"],
            timestamp=row["timestamp"],
            failure_count=row["failure_count"],
            warning_count=row["warning_count"],
            provenance_record_id=row["provenance_record_id"],
            analyst_notes=row["analyst_notes"],
        )

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> EvidenceRow:
        keys = row.keys()
        return EvidenceRow(
            evidence_id=row["evidence_id"],
            verdict_id=row["verdict_id"],
            run_id=row["run_id"],
            datum_id=row["datum_id"],
            invariant_name=row["invariant_name"],
            severity=row["severity"],
            passed=bool(row["passed"]),
            field=row["field"],
            observed_value=row["observed_value"],
            message=row["message"],
            notes=row["notes"],
            timestamp=row["timestamp"],
            failure_category=row["failure_category"] if "failure_category" in keys else "INVARIANT_VIOLATION",
            failure_severity=row["failure_severity"] if "failure_severity" in keys else "HIGH",
            action_taken=row["action_taken"] if "action_taken" in keys else "QUARANTINED",
            context_json=row["context_json"] if "context_json" in keys else "{}",
        )

    @staticmethod
    def _row_to_summary(row: sqlite3.Row) -> RunSummaryRow:
        return RunSummaryRow(
            run_id=row["run_id"],
            domain_name=row["domain_name"],
            domain_version=row["domain_version"],
            operator=row["operator"],
            started_at=row["started_at"],
            total_datums=row["total_datums"],
            trusted=row["trusted"],
            untrusted=row["untrusted"],
            trust_rate=row["trust_rate"],
            total_warnings=row["total_warnings"],
            failure_breakdown_json=row["failure_breakdown_json"],
            notes=row["notes"],
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
