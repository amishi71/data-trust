"""
PipelineRunner — the execution wrapper.

Every pipeline run is tagged with a run_id. Inputs, transformations, and
outputs are all recorded into a ReplaySpec that is sealed at run completion.
Provenance records are stored in the ProvenanceStore alongside each datum.

The runner does not know what transformations do — it only sees their inputs
and outputs. Transformations are plain callables: (datum: dict) -> dict.
This keeps the runner decoupled from domain logic.

Usage
-----
    runner = PipelineRunner(
        domain=DetectorEventDomain(),
        store=ProvenanceStore("provenance.db"),
        environment=env_snapshot,
        operator="pipeline_service",
        config={"calibration_version": "v3"},
    )

    results = runner.run(data)          # list of (datum, verdict, prov_record)
    spec    = runner.get_spec()         # sealed ReplaySpec
    spec.save("runs/RUN_001.json")
"""

from __future__ import annotations

import hashlib
import json
import platform as _platform
import sys
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from src.domain.base import DataDomain
from src.invariants.contract import TrustContract, TrustVerdict
from src.provenance.record import (
    EnvironmentSnapshot,
    ProvenanceRecord,
    make_ingestion_record,
    make_transformation_record,
    _hash_datum,
)
from src.provenance.store import ProvenanceStore
from src.replay.spec import ReplaySpec, TransformStep


# ---------------------------------------------------------------------------
# PipelineRunner
# ---------------------------------------------------------------------------

class PipelineRunner:
    """
    Executes a pipeline over a list of data dicts, recording a full ReplaySpec.

    Parameters
    ----------
    domain          : DataDomain whose invariants will be used for trust checks.
    store           : ProvenanceStore where all provenance records are saved.
    environment     : EnvironmentSnapshot for this run.
    operator        : Username or service ID initiating the run.
    config          : Serialisable dict of pipeline configuration.
    transformations : Ordered list of (name, callable) pairs. Each callable
                      takes a datum dict and returns a transformed datum dict.
                      Transformations must be pure and deterministic.
    require_provenance : Whether to fail data with missing provenance.
    """

    def __init__(
        self,
        domain: DataDomain,
        store: ProvenanceStore,
        environment: EnvironmentSnapshot,
        operator: str,
        config: dict | None = None,
        transformations: list[tuple[str, Callable[[dict], dict]]] | None = None,
        require_provenance: bool = True,
    ):
        self.domain = domain
        self.store = store
        self.environment = environment
        self.operator = operator
        self.config = config or {}
        self.transformations = transformations or []
        self.require_provenance = require_provenance

        self._run_id = str(uuid.uuid4())
        self._spec = ReplaySpec(
            run_id=self._run_id,
            domain_name=domain.name,
            domain_version=domain.version,
            operator=operator,
            started_at=datetime.now(timezone.utc).isoformat(),
            environment=environment,
            config_snapshot=self.config,
        )
        self._contract = TrustContract(domain, require_provenance=require_provenance)
        self._sealed = False

    @property
    def run_id(self) -> str:
        return self._run_id

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        data: list[dict],
        context: dict | None = None,
    ) -> list[tuple[dict, TrustVerdict, ProvenanceRecord]]:
        """
        Process a list of data dicts through the full pipeline.

        For each datum:
          1. Record an ingestion provenance record.
          2. Apply each transformation in order, recording a step in the spec
             and a transformation provenance record for each.
          3. Run the TrustContract against the final (post-transform) datum.
          4. Record the result.

        Returns
        -------
        List of (final_datum, TrustVerdict, final_ProvenanceRecord) triples,
        in the same order as the input data.
        """
        if self._sealed:
            raise RuntimeError("This PipelineRunner has already been sealed. Create a new one.")

        results = []

        for datum in data:
            datum_id = datum.get("event_id") or datum.get("id") or str(uuid.uuid4())

            # --- Record the raw input hash in the spec ---
            self._spec.input_hashes.append(_hash_datum(datum))

            # --- Ingestion provenance ---
            current_record = make_ingestion_record(
                datum=datum,
                operator=self.operator,
                environment=self.environment,
                datum_id=datum_id,
            )
            self.store.save(current_record)
            self._spec.provenance_record_ids.append(current_record.record_id)

            # --- Apply transformations ---
            current_datum = datum
            for step_index, (transform_name, transform_fn) in enumerate(self.transformations):
                step_input_hash = _hash_datum(current_datum)

                try:
                    output_datum = transform_fn(current_datum)
                except Exception as e:
                    # Transformation failure: record the failure in the spec as a
                    # degenerate step with an error note, and stop processing this datum.
                    step = TransformStep(
                        step_index=step_index,
                        transformation_id=transform_name,
                        input_hash=step_input_hash,
                        output_hash="ERROR",
                        operator=self.operator,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        notes=f"TRANSFORMATION ERROR: {e}",
                    )
                    self._spec.steps.append(step)
                    # Use the pre-transform datum and provenance for the verdict
                    output_datum = current_datum
                    break

                step_output_hash = _hash_datum(output_datum)

                step = TransformStep(
                    step_index=step_index,
                    transformation_id=transform_name,
                    input_hash=step_input_hash,
                    output_hash=step_output_hash,
                    operator=self.operator,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    config_snapshot=self.config,
                )
                self._spec.steps.append(step)

                # Record transformation provenance
                transform_record = make_transformation_record(
                    datum=output_datum,
                    transformation_id=transform_name,
                    parent_record=current_record,
                    operator=self.operator,
                    environment=self.environment,
                )
                self.store.save(transform_record)
                self._spec.provenance_record_ids.append(transform_record.record_id)

                current_datum = output_datum
                current_record = transform_record

            # --- Trust evaluation ---
            verdict = self._contract.evaluate(
                datum=current_datum,
                datum_id=datum_id,
                provenance_record=current_record,
                context=context,
            )

            # --- Record the output hash in the spec ---
            self._spec.output_hashes.append(_hash_datum(current_datum))

            results.append((current_datum, verdict, current_record))

        # --- Seal the spec ---
        self._spec.seal()
        self._sealed = True

        return results

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_spec(self) -> ReplaySpec:
        if not self._sealed:
            raise RuntimeError("Run has not completed yet. Call run() first.")
        return self._spec

    def verdict_summary(
        self, results: list[tuple[dict, TrustVerdict, ProvenanceRecord]]
    ) -> dict:
        verdicts = [r[1] for r in results]
        return self._contract.batch_summary(verdicts)


# ---------------------------------------------------------------------------
# Convenience: build an EnvironmentSnapshot from the current runtime
# ---------------------------------------------------------------------------

def current_environment(config: dict | None = None) -> EnvironmentSnapshot:
    """Capture the current Python runtime as an EnvironmentSnapshot."""
    import importlib.metadata

    lib_versions: dict[str, str] = {}
    for lib in ["numpy", "pandas", "scipy"]:
        try:
            lib_versions[lib] = importlib.metadata.version(lib)
        except Exception:
            lib_versions[lib] = "not_installed"

    config_snapshot = config or {}
    config_hash = hashlib.sha256(
        json.dumps(config_snapshot, sort_keys=True).encode()
    ).hexdigest()

    return EnvironmentSnapshot(
        python_version=_platform.python_version(),
        platform=_platform.platform(),
        library_versions=lib_versions,
        config_hash=config_hash,
    )
