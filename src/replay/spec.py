"""
ReplaySpec — a complete, serialisable snapshot of a pipeline run.

Every run produces one ReplaySpec and stores it alongside the provenance
records it generated. Given a run_id, the ReplayEngine uses the spec to
reconstruct the exact sequence of operations and re-execute them.

Design notes
------------
- The spec is immutable once sealed at run completion.
- It records inputs, the ordered transformation sequence, config, and the
  full EnvironmentSnapshot so the ReplayEngine can detect environment drift.
- "Replay-within-platform" is the explicit fidelity claim. Cross-platform
  bitwise reproducibility is not claimed. Floating-point results may differ
  across Python versions or library versions — this is documented, not hidden.
  See ASSUMPTIONS.md.
- Each transformation step records its input_hash and output_hash, forming
  an auditable chain independent of the provenance records.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from src.provenance.record import EnvironmentSnapshot


# ---------------------------------------------------------------------------
# TransformStep — one unit of work in the pipeline
# ---------------------------------------------------------------------------

@dataclass
class TransformStep:
    """
    Records a single transformation applied to a datum during a run.

    Fields
    ------
    step_index      : Position in the ordered transformation sequence (0-based).
    transformation_id : Human-readable name, e.g. "CALIBRATE_v1".
    input_hash      : SHA-256 of the datum entering this step.
    output_hash     : SHA-256 of the datum exiting this step.
    operator        : Service or user that ran this step.
    timestamp       : When this step executed.
    config_snapshot : Serialisable dict of any config values consumed by this step.
    notes           : Optional free-text.
    """
    step_index: int
    transformation_id: str
    input_hash: str
    output_hash: str
    operator: str
    timestamp: str
    config_snapshot: dict = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "step_index": self.step_index,
            "transformation_id": self.transformation_id,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "operator": self.operator,
            "timestamp": self.timestamp,
            "config_snapshot": self.config_snapshot,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TransformStep":
        return cls(**d)


# ---------------------------------------------------------------------------
# ReplaySpec
# ---------------------------------------------------------------------------

@dataclass
class ReplaySpec:
    """
    Complete record of a pipeline run.

    Produced by PipelineRunner.seal() at run completion.
    Consumed by ReplayEngine to reconstruct and re-execute the run.

    Fields
    ------
    run_id          : Unique identifier for this run.
    domain_name     : Which DataDomain was active.
    domain_version  : Domain spec version used.
    operator        : Who initiated this run.
    started_at      : ISO 8601 UTC timestamp of run start.
    sealed_at       : ISO 8601 UTC timestamp of run completion (set by seal()).
    environment     : Full EnvironmentSnapshot at run time.
    config_hash     : SHA-256 of the full pipeline config dict.
    config_snapshot : The pipeline config dict itself (for replay).
    input_hashes    : Ordered list of SHA-256 hashes of the raw input data.
    steps           : Ordered list of TransformStep records.
    output_hashes   : Ordered list of SHA-256 hashes of the final output data.
    provenance_record_ids : IDs of ProvenanceRecords generated during this run.
    notes           : Free-text annotations.
    spec_hash       : SHA-256 of the sealed spec (set by seal(), used to detect
                      post-seal tampering of the spec file itself).
    """
    run_id: str
    domain_name: str
    domain_version: str
    operator: str
    started_at: str
    environment: EnvironmentSnapshot
    config_snapshot: dict
    sealed_at: str = ""
    input_hashes: list[str] = field(default_factory=list)
    steps: list[TransformStep] = field(default_factory=list)
    output_hashes: list[str] = field(default_factory=list)
    provenance_record_ids: list[str] = field(default_factory=list)
    notes: str = ""
    spec_hash: str = ""

    @property
    def config_hash(self) -> str:
        canonical = json.dumps(self.config_snapshot, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def seal(self) -> None:
        """
        Finalise the spec. Sets sealed_at and computes spec_hash.
        Called by PipelineRunner once all data has been processed.
        """
        if self.sealed_at:
            raise RuntimeError(f"ReplaySpec for run {self.run_id!r} is already sealed.")
        self.sealed_at = datetime.now(timezone.utc).isoformat()
        self.spec_hash = self._compute_spec_hash()

    def verify(self) -> bool:
        """Return True if spec_hash matches a fresh recomputation."""
        if not self.spec_hash:
            return False
        return self.spec_hash == self._compute_spec_hash()

    def _compute_spec_hash(self) -> str:
        payload = json.dumps(
            {
                "run_id": self.run_id,
                "domain_name": self.domain_name,
                "domain_version": self.domain_version,
                "operator": self.operator,
                "started_at": self.started_at,
                "sealed_at": self.sealed_at,
                "environment_hash": self.environment.to_hash(),
                "config_hash": self.config_hash,
                "input_hashes": self.input_hashes,
                "steps": [s.to_dict() for s in self.steps],
                "output_hashes": self.output_hashes,
                "provenance_record_ids": self.provenance_record_ids,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "domain_name": self.domain_name,
            "domain_version": self.domain_version,
            "operator": self.operator,
            "started_at": self.started_at,
            "sealed_at": self.sealed_at,
            "environment": {
                "python_version": self.environment.python_version,
                "platform": self.environment.platform,
                "library_versions": self.environment.library_versions,
                "config_hash": self.environment.config_hash,
            },
            "config_hash": self.config_hash,
            "config_snapshot": self.config_snapshot,
            "input_hashes": self.input_hashes,
            "steps": [s.to_dict() for s in self.steps],
            "output_hashes": self.output_hashes,
            "provenance_record_ids": self.provenance_record_ids,
            "notes": self.notes,
            "spec_hash": self.spec_hash,
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, path: str | Path) -> "ReplaySpec":
        with open(path) as f:
            d = json.load(f)
        env_d = d["environment"]
        env = EnvironmentSnapshot(
            python_version=env_d["python_version"],
            platform=env_d["platform"],
            library_versions=env_d["library_versions"],
            config_hash=env_d.get("config_hash", ""),
        )
        return cls(
            run_id=d["run_id"],
            domain_name=d["domain_name"],
            domain_version=d["domain_version"],
            operator=d["operator"],
            started_at=d["started_at"],
            environment=env,
            config_snapshot=d.get("config_snapshot", {}),
            sealed_at=d.get("sealed_at", ""),
            input_hashes=d.get("input_hashes", []),
            steps=[TransformStep.from_dict(s) for s in d.get("steps", [])],
            output_hashes=d.get("output_hashes", []),
            provenance_record_ids=d.get("provenance_record_ids", []),
            notes=d.get("notes", ""),
            spec_hash=d.get("spec_hash", ""),
        )

    def summary(self) -> str:
        status = "SEALED" if self.sealed_at else "OPEN"
        return (
            f"[{status}] run_id={self.run_id[:16]}… "
            f"domain={self.domain_name}@{self.domain_version} "
            f"steps={len(self.steps)} inputs={len(self.input_hashes)} "
            f"outputs={len(self.output_hashes)}"
        )
