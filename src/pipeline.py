"""
DataTrustPipeline — unified entry point for the data trust system.

Wires together all five components:
  1. Provenance layer   (ProvenanceStore, ProvenanceRecord)
  2. Domain abstraction (DataDomain)
  3. Invariant engine   (TrustContract, invariant types)
  4. Replay mechanism   (PipelineRunner, ReplaySpec, ReplayEngine)
  5. Evidence store     (EvidenceStore, FailureReport)

One call to pipeline.run() is all that's needed. Everything else —
provenance recording, trust evaluation, evidence storage, spec sealing —
happens automatically.

Usage
-----
    pipeline = DataTrustPipeline(
        domain=DetectorEventDomain(),
        db_path="runs/my_run",          # creates provenance.db + evidence.db
        operator="my_service",
        config={"calibration_version": "v3"},
        transformations=[
            ("CALIBRATE_v1", my_calibrate_fn),
            ("FILTER_NOISE", my_filter_fn),
        ],
    )

    result = pipeline.run(data)

    result.print_summary()
    result.failure_report.print()
    result.spec.save("runs/RUN_001.json")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from src.domain.base import DataDomain
from src.evidence.store import EvidenceStore, FailureReport
from src.invariants.contract import TrustVerdict
from src.provenance.record import EnvironmentSnapshot, ProvenanceRecord
from src.provenance.store import ProvenanceStore
from src.replay.engine import FidelityClass, ReplayEngine, ReplayFidelityReport
from src.replay.runner import PipelineRunner, current_environment
from src.replay.spec import ReplaySpec


# ---------------------------------------------------------------------------
# PipelineResult — everything a caller needs from a single run
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    run_id: str
    domain_name: str
    spec: ReplaySpec
    verdicts: list[TrustVerdict]
    summary: dict
    failure_report: FailureReport
    provenance_db_path: str
    evidence_db_path: str

    def print_summary(self) -> None:
        s = self.summary
        print(f"\n{'=' * 60}")
        print(f"PIPELINE RUN COMPLETE")
        print(f"  run_id      : {self.run_id}")
        print(f"  domain      : {self.domain_name}")
        print(f"  total       : {s['total']}")
        print(f"  trusted     : {s['trusted']}")
        print(f"  untrusted   : {s['untrusted']}")
        print(f"  trust rate  : {s['trust_rate']:.1%}")
        print(f"  warnings    : {s['total_warnings']}")
        if s.get("failure_breakdown"):
            print("  failures by invariant:")
            for inv, count in sorted(s["failure_breakdown"].items(), key=lambda x: -x[1]):
                print(f"    {inv}: {count}")
        print(f"  provenance  : {self.provenance_db_path}")
        print(f"  evidence    : {self.evidence_db_path}")
        print(f"{'=' * 60}")


# ---------------------------------------------------------------------------
# DataTrustPipeline
# ---------------------------------------------------------------------------

class DataTrustPipeline:
    """
    Unified pipeline that wires all five system components.

    Parameters
    ----------
    domain          : DataDomain to use for trust evaluation.
    db_path         : Directory or prefix for the database files.
                      Creates <db_path>/provenance.db and <db_path>/evidence.db.
                      Use ":memory:" for both (tests only).
    operator        : Username or service ID running the pipeline.
    config          : Serialisable config dict (stored in ReplaySpec).
    transformations : Ordered list of (name, callable) transformation pairs.
    require_provenance : If True, missing provenance → UNTRUSTED (default True).
    environment     : Override the environment snapshot (default: auto-detected).
    """

    def __init__(
        self,
        domain: DataDomain,
        db_path: str | Path = ":memory:",
        operator: str = "pipeline",
        config: dict | None = None,
        transformations: list[tuple[str, Callable]] | None = None,
        require_provenance: bool = True,
        environment: EnvironmentSnapshot | None = None,
    ):
        self.domain = domain
        self.operator = operator
        self.config = config or {}
        self.transformations = transformations or []
        self.require_provenance = require_provenance

        # Set up database paths
        if str(db_path) == ":memory:":
            self._prov_path = ":memory:"
            self._evid_path = ":memory:"
        else:
            db_dir = Path(db_path)
            db_dir.mkdir(parents=True, exist_ok=True)
            self._prov_path = str(db_dir / "provenance.db")
            self._evid_path = str(db_dir / "evidence.db")

        self._prov_store = ProvenanceStore(self._prov_path)
        self._evid_store = EvidenceStore(self._evid_path)
        self._env = environment or current_environment(self.config)

    def run(
        self,
        data: list[dict],
        context: dict | None = None,
        notes: str = "",
    ) -> PipelineResult:
        """
        Process data through the full pipeline.

        Returns a PipelineResult containing verdicts, the sealed ReplaySpec,
        and a FailureReport ready to print without reading any code.
        """
        runner = PipelineRunner(
            domain=self.domain,
            store=self._prov_store,
            environment=self._env,
            operator=self.operator,
            config=self.config,
            transformations=self.transformations,
            require_provenance=self.require_provenance,
        )

        raw_results = runner.run(data, context=context)
        spec = runner.get_spec()
        if notes:
            spec.notes = notes

        verdicts = [r[1] for r in raw_results]
        summary = runner.verdict_summary(raw_results)

        # Record everything in the evidence store
        self._evid_store.record_many_verdicts(verdicts, run_id=spec.run_id)
        self._evid_store.record_run_summary(
            run_id=spec.run_id,
            domain_name=self.domain.name,
            domain_version=self.domain.version,
            operator=self.operator,
            started_at=spec.started_at,
            summary=summary,
            notes=notes,
        )

        failure_report = self._evid_store.failure_report(spec.run_id)

        return PipelineResult(
            run_id=spec.run_id,
            domain_name=self.domain.name,
            spec=spec,
            verdicts=verdicts,
            summary=summary,
            failure_report=failure_report,
            provenance_db_path=self._prov_path,
            evidence_db_path=self._evid_path,
        )

    def replay(
        self,
        spec: ReplaySpec,
        original_data: list[dict],
        context: dict | None = None,
    ) -> ReplayFidelityReport:
        """
        Replay a past run from its ReplaySpec and return a fidelity report.
        Uses the same transformations as the pipeline was configured with.
        """
        engine = ReplayEngine(
            store=ProvenanceStore(":memory:"),
            domain=self.domain,
        )
        return engine.replay(
            spec=spec,
            original_data=original_data,
            transformations=self.transformations,
            replay_operator=f"{self.operator}:replay",
            context=context,
        )

    @property
    def provenance_store(self) -> ProvenanceStore:
        return self._prov_store

    @property
    def evidence_store(self) -> EvidenceStore:
        return self._evid_store

    def close(self) -> None:
        self._prov_store.close()
        self._evid_store.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
