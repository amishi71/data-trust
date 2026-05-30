"""
ProvenanceStore — queryable, SQLite-backed store for ProvenanceRecords.

Responsibilities:
  - Persist records with tamper-detection (hash verification on read)
  - Reconstruct the full lineage graph for any datum
  - Detect provenance gaps (datum referenced with no record on file)
  - Surface chain integrity violations

The store does not mutate records. Every write is append-only.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Optional

from .record import EnvironmentSnapshot, ProvenanceRecord


# ---------------------------------------------------------------------------
# Lineage graph node — what you get back when querying a datum's history
# ---------------------------------------------------------------------------

@dataclass
class LineageNode:
    record: ProvenanceRecord
    children: list["LineageNode"]

    def to_text_dag(self, indent: int = 0) -> str:
        prefix = "  " * indent
        connector = "└─ " if indent > 0 else ""
        line = (
            f"{prefix}{connector}[{self.record.transformation_id}] "
            f"datum={self.record.datum_id[:8]}… "
            f"hash={self.record.content_hash[:12]}… "
            f"op={self.record.operator} "
            f"t={self.record.timestamp}"
        )
        child_lines = "\n".join(c.to_text_dag(indent + 1) for c in self.children)
        return line + ("\n" + child_lines if child_lines else "")


# ---------------------------------------------------------------------------
# Integrity check result
# ---------------------------------------------------------------------------

@dataclass
class IntegrityReport:
    total_records: int
    verified: int
    tampered: list[str]       # record_ids where hash verification failed
    gap_datum_ids: list[str]  # datum_ids referenced but with no record on file

    @property
    def is_clean(self) -> bool:
        return len(self.tampered) == 0 and len(self.gap_datum_ids) == 0

    def summary(self) -> str:
        status = "CLEAN" if self.is_clean else "INTEGRITY VIOLATION"
        return (
            f"[{status}] {self.verified}/{self.total_records} records verified. "
            f"Tampered: {len(self.tampered)}. Gaps: {len(self.gap_datum_ids)}."
        )


# ---------------------------------------------------------------------------
# ProvenanceStore
# ---------------------------------------------------------------------------

class ProvenanceStore:
    """
    Append-only SQLite store for ProvenanceRecords.

    Usage
    -----
        store = ProvenanceStore(":memory:")          # in-memory for tests
        store = ProvenanceStore("provenance.db")     # on-disk for production
        store.save(record)
        lineage = store.get_lineage(datum_id)
        report  = store.verify_integrity()
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS provenance_records (
        record_id          TEXT PRIMARY KEY,
        datum_id           TEXT NOT NULL,
        input_hash         TEXT NOT NULL,
        transformation_id  TEXT NOT NULL,
        timestamp          TEXT NOT NULL,
        environment_json   TEXT NOT NULL,
        operator           TEXT NOT NULL,
        parent_hash        TEXT,
        parent_ids_json    TEXT NOT NULL,
        notes              TEXT NOT NULL,
        content_hash       TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_datum_id   ON provenance_records(datum_id);
    CREATE INDEX IF NOT EXISTS idx_parent_ids ON provenance_records(parent_ids_json);
    CREATE INDEX IF NOT EXISTS idx_timestamp  ON provenance_records(timestamp);
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

    def save(self, record: ProvenanceRecord) -> None:
        """
        Persist a ProvenanceRecord. Raises ValueError if record_id already exists
        (the store is append-only; records are never updated).
        """
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM provenance_records WHERE record_id = ?",
                (record.record_id,),
            ).fetchone()
            if existing:
                raise ValueError(
                    f"Record {record.record_id!r} already exists. "
                    "ProvenanceStore is append-only."
                )

            env = record.environment
            conn.execute(
                """
                INSERT INTO provenance_records VALUES (
                    :record_id, :datum_id, :input_hash, :transformation_id,
                    :timestamp, :environment_json, :operator, :parent_hash,
                    :parent_ids_json, :notes, :content_hash
                )
                """,
                {
                    "record_id": record.record_id,
                    "datum_id": record.datum_id,
                    "input_hash": record.input_hash,
                    "transformation_id": record.transformation_id,
                    "timestamp": record.timestamp,
                    "environment_json": json.dumps({
                        "python_version": env.python_version,
                        "platform": env.platform,
                        "library_versions": env.library_versions,
                        "config_hash": env.config_hash,
                    }),
                    "operator": record.operator,
                    "parent_hash": record.parent_hash,
                    "parent_ids_json": json.dumps(list(record.parent_ids)),
                    "notes": record.notes,
                    "content_hash": record.content_hash,
                },
            )

    def save_many(self, records: list[ProvenanceRecord]) -> None:
        for record in records:
            self.save(record)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, record_id: str) -> Optional[ProvenanceRecord]:
        row = self._conn.execute(
            "SELECT * FROM provenance_records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return self._row_to_record(row) if row else None

    def get_all_for_datum(self, datum_id: str) -> list[ProvenanceRecord]:
        """All records associated with this datum_id, ordered by timestamp."""
        rows = self._conn.execute(
            "SELECT * FROM provenance_records WHERE datum_id = ? ORDER BY timestamp",
            (datum_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_roots(self, datum_id: str) -> list[ProvenanceRecord]:
        """Records with no parent — the ingestion points."""
        rows = self._conn.execute(
            """
            SELECT * FROM provenance_records
            WHERE datum_id = ? AND parent_hash IS NULL
            ORDER BY timestamp
            """,
            (datum_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    # ------------------------------------------------------------------
    # Lineage graph
    # ------------------------------------------------------------------

    def get_lineage(self, datum_id: str) -> list[LineageNode]:
        """
        Reconstruct the full lineage graph for a datum as a forest of LineageNodes.
        Returns a list of root nodes; each node has a .children list.
        """
        all_records = self.get_all_for_datum(datum_id)
        if not all_records:
            return []

        by_id: dict[str, ProvenanceRecord] = {r.record_id: r for r in all_records}
        children_map: dict[str, list[str]] = {r.record_id: [] for r in all_records}

        for record in all_records:
            for parent_id in record.parent_ids:
                if parent_id in children_map:
                    children_map[parent_id].append(record.record_id)

        def build_node(record_id: str) -> LineageNode:
            record = by_id[record_id]
            return LineageNode(
                record=record,
                children=[build_node(c) for c in children_map[record_id]],
            )

        roots = [r for r in all_records if not r.parent_ids]
        return [build_node(r.record_id) for r in roots]

    def print_lineage(self, datum_id: str) -> None:
        nodes = self.get_lineage(datum_id)
        if not nodes:
            print(f"No provenance records found for datum {datum_id!r}.")
            return
        print(f"Lineage for datum {datum_id}:")
        for node in nodes:
            print(node.to_text_dag())

    # ------------------------------------------------------------------
    # Integrity verification
    # ------------------------------------------------------------------

    def verify_integrity(self) -> IntegrityReport:
        """
        Verify every stored record by recomputing its content_hash.
        Also flags records whose parent_hash does not match the stored parent.
        """
        rows = self._conn.execute("SELECT * FROM provenance_records").fetchall()
        records = [self._row_to_record(r) for r in rows]

        tampered: list[str] = []
        gap_datum_ids: list[str] = []

        known_hashes = {r.content_hash for r in records}

        for record in records:
            if not record.verify():
                tampered.append(record.record_id)

            if record.parent_hash and record.parent_hash not in known_hashes:
                gap_datum_ids.append(record.datum_id)

        return IntegrityReport(
            total_records=len(records),
            verified=len(records) - len(tampered),
            tampered=tampered,
            gap_datum_ids=list(set(gap_datum_ids)),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> ProvenanceRecord:
        env_data = json.loads(row["environment_json"])
        env = EnvironmentSnapshot(
            python_version=env_data["python_version"],
            platform=env_data["platform"],
            library_versions=env_data["library_versions"],
            config_hash=env_data["config_hash"],
        )
        return ProvenanceRecord(
            record_id=row["record_id"],
            datum_id=row["datum_id"],
            input_hash=row["input_hash"],
            transformation_id=row["transformation_id"],
            timestamp=row["timestamp"],
            environment=env,
            operator=row["operator"],
            parent_hash=row["parent_hash"],
            parent_ids=tuple(json.loads(row["parent_ids_json"])),
            notes=row["notes"],
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
