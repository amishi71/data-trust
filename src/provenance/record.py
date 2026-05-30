"""
ProvenanceRecord — chain-of-custody data structure.

Every datum that enters the system carries one of these. It is immutable once
created. The hash chain links records together cryptographically: each record
hashes its own contents plus its parent's hash, forming a Merkle-like chain.

A missing or unverifiable provenance record is itself a FAIL — not a warning.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """
    A fingerprint of the processing environment at the time a record was created.
    Used by the replay engine to detect environment drift between runs.

    §6 fields: python_version, platform, library_versions, contract_version,
    domain_version are zero-tolerance (divergence → hard FAIL in replay).
    config_hash is derived from the pipeline config dict.
    """
    python_version: str
    platform: str
    library_versions: dict[str, str]      # {"numpy": "1.26.0", "pandas": "2.1.0", ...}
    config_hash: str                       # SHA-256 of the pipeline config in use
    contract_version: str = "0.1-draft"   # §6: zero-tolerance field
    domain_version: str = ""              # §6: zero-tolerance field

    def to_hash(self) -> str:
        canonical = json.dumps(
            {
                "python_version": self.python_version,
                "platform": self.platform,
                "library_versions": dict(sorted(self.library_versions.items())),
                "config_hash": self.config_hash,
                "contract_version": self.contract_version,
                "domain_version": self.domain_version,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    # §6 zero-tolerance fields — divergence in these is always ENVIRONMENT_DRIFT
    ZERO_TOLERANCE_FIELDS = frozenset(["python_version", "contract_version", "domain_version"])
    # §6 warn-on-diverge fields — divergence noted but not fatal
    WARN_FIELDS = frozenset(["platform"])
    # library versions: patch divergence → warn; major.minor divergence → drift


@dataclass(frozen=True)
class ProvenanceRecord:
    """
    Immutable chain-of-custody record for a single datum.

    Fields
    ------
    record_id       : Unique identifier for this provenance record.
    datum_id        : Identifier of the datum this record describes.
    input_hash      : SHA-256 of the raw datum at ingestion.
    transformation_id : Identifier of the transformation that produced this datum.
                        "INGEST" for the initial ingestion record.
    timestamp       : UTC timestamp of when this record was created.
    environment     : Snapshot of the processing environment.
    operator        : Who or what ran the pipeline (human username or service ID).
    parent_hash     : Hash of the parent ProvenanceRecord. None for root records.
    parent_ids      : Record IDs of parent records (for derived/merged data).
    notes           : Optional free-text annotation (e.g. "reprocessed after
                        calibration update").
    """

    record_id: str
    datum_id: str
    input_hash: str
    transformation_id: str
    timestamp: str                          # ISO 8601 UTC
    environment: EnvironmentSnapshot
    operator: str
    # §4 required fields
    source_path: str = ""                  # URI/path of the input file or stream
    pipeline_version: str = "0.1.0"        # semver of the pipeline code
    contract_version: str = "0.1-draft"    # version of TRUST_CONTRACT.md in effect
    domain: str = ""                       # DataDomain name applied
    verdict: str = ""                      # TRUSTED / TRUSTED_WITH_WARNINGS / UNTRUSTED
    warning_names: tuple[str, ...] = field(default_factory=tuple)   # soft invariant names that fired
    failure_reason: str = ""               # populated if verdict == UNTRUSTED
    parent_hash: Optional[str] = None
    parent_ids: tuple[str, ...] = field(default_factory=tuple)
    notes: str = ""

    # --- computed on post_init via object.__setattr__ since frozen=True --------
    content_hash: str = field(default="", compare=False)

    def __post_init__(self):
        # Compute and store the content hash after all other fields are set.
        object.__setattr__(self, "content_hash", self._compute_content_hash())

    def _compute_content_hash(self) -> str:
        """
        SHA-256 over the canonical JSON of all fields except content_hash itself.
        Including parent_hash means the chain is tamper-evident: altering any
        ancestor invalidates every descendant's hash.
        """
        canonical = json.dumps(
            {
                "record_id": self.record_id,
                "datum_id": self.datum_id,
                "input_hash": self.input_hash,
                "transformation_id": self.transformation_id,
                "timestamp": self.timestamp,
                "environment_hash": self.environment.to_hash(),
                "operator": self.operator,
                "source_path": self.source_path,
                "pipeline_version": self.pipeline_version,
                "contract_version": self.contract_version,
                "domain": self.domain,
                "parent_hash": self.parent_hash,
                "parent_ids": list(self.parent_ids),
                "notes": self.notes,
            },
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()

    def verify(self) -> bool:
        """Return True if the stored content_hash matches a fresh recomputation."""
        return self.content_hash == self._compute_content_hash()

    def to_dict(self) -> dict:
        return {
            "record_id": self.record_id,
            "datum_id": self.datum_id,
            "input_hash": self.input_hash,
            "transformation_id": self.transformation_id,
            "timestamp": self.timestamp,
            "environment": {
                "python_version": self.environment.python_version,
                "platform": self.environment.platform,
                "library_versions": self.environment.library_versions,
                "config_hash": self.environment.config_hash,
            },
            "operator": self.operator,
            "parent_hash": self.parent_hash,
            "parent_ids": list(self.parent_ids),
            "notes": self.notes,
            "content_hash": self.content_hash,
        }


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def _hash_datum(datum: dict) -> str:
    canonical = json.dumps(datum, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def make_ingestion_record(
    datum: dict,
    operator: str,
    environment: EnvironmentSnapshot,
    datum_id: Optional[str] = None,
    notes: str = "",
) -> ProvenanceRecord:
    """
    Create the root ProvenanceRecord for a newly ingested datum.
    No parent — this is the origin point of the chain.
    """
    return ProvenanceRecord(
        record_id=str(uuid.uuid4()),
        datum_id=datum_id or str(uuid.uuid4()),
        input_hash=_hash_datum(datum),
        transformation_id="INGEST",
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=environment,
        operator=operator,
        parent_hash=None,
        parent_ids=(),
        notes=notes,
    )


def make_transformation_record(
    datum: dict,
    transformation_id: str,
    parent_record: ProvenanceRecord,
    operator: str,
    environment: EnvironmentSnapshot,
    notes: str = "",
) -> ProvenanceRecord:
    """
    Create a ProvenanceRecord for a datum that has been transformed.
    Links back to the parent record via parent_hash and parent_ids.
    """
    return ProvenanceRecord(
        record_id=str(uuid.uuid4()),
        datum_id=parent_record.datum_id,    # same logical datum, new processing step
        input_hash=_hash_datum(datum),
        transformation_id=transformation_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=environment,
        operator=operator,
        parent_hash=parent_record.content_hash,
        parent_ids=(parent_record.record_id,),
        notes=notes,
    )
