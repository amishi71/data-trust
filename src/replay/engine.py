"""
ReplayEngine — given a run_id and its ReplaySpec, reconstruct and re-execute
the exact sequence of operations, then compare outputs to the original.

Fidelity model
--------------
The honest fidelity claim for this system is "replay-within-platform": given
the same Python version, same library versions, and same config, outputs should
be bitwise-identical. Cross-platform or cross-version replay may diverge due
to floating-point nondeterminism. This is documented, not hidden.

The ReplayFidelityReport classifies the result as one of:
  IDENTICAL       — every output hash matches the original. Full fidelity.
  ENVIRONMENT_DRIFT — environment differs from the original run. Results may
                    differ for reasons outside the pipeline's control.
  OUTPUT_DIVERGED — same environment, but output hashes differ. This is the
                    serious case: it implies nondeterminism in the pipeline itself.
  PARTIAL         — some outputs matched, some didn't.
  ERROR           — replay failed to complete (e.g. missing provenance records).

ASSUMPTIONS.md note: floating-point operations on different hardware or with
different BLAS/LAPACK libraries can produce different low-order bits even for
identical inputs. The engine logs but does not suppress these divergences.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from src.provenance.record import EnvironmentSnapshot, _hash_datum
from src.provenance.store import ProvenanceStore
from src.replay.spec import ReplaySpec, TransformStep
from src.replay.runner import PipelineRunner, current_environment
from src.domain.base import DataDomain


# ---------------------------------------------------------------------------
# Fidelity classification
# ---------------------------------------------------------------------------

class FidelityClass(str, Enum):
    IDENTICAL         = "IDENTICAL"
    ENVIRONMENT_DRIFT = "ENVIRONMENT_DRIFT"
    OUTPUT_DIVERGED   = "OUTPUT_DIVERGED"
    PARTIAL           = "PARTIAL"
    ERROR             = "ERROR"


# ---------------------------------------------------------------------------
# Per-datum comparison result
# ---------------------------------------------------------------------------

@dataclass
class DatumFidelity:
    datum_index: int
    original_output_hash: str
    replay_output_hash: str
    matched: bool
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "datum_index": self.datum_index,
            "original_output_hash": self.original_output_hash,
            "replay_output_hash": self.replay_output_hash,
            "matched": self.matched,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Environment drift detail
# ---------------------------------------------------------------------------

@dataclass
class EnvironmentDiff:
    field: str
    original: str
    replay: str

    def to_dict(self) -> dict:
        return {"field": self.field, "original": self.original, "replay": self.replay}


# ---------------------------------------------------------------------------
# ReplayFidelityReport
# ---------------------------------------------------------------------------

@dataclass
class ReplayFidelityReport:
    """
    The output of a replay run.

    Fields
    ------
    run_id              : The run_id being replayed.
    fidelity_class      : Overall fidelity classification.
    original_spec_hash  : Hash of the original ReplaySpec.
    replay_timestamp    : When this replay was performed.
    replay_environment  : Environment of the replay run.
    original_environment: Environment of the original run.
    environment_diffs   : List of environment fields that differ.
    datum_results       : Per-datum comparison.
    total_datums        : Total number of datums replayed.
    matched             : Number of datums with identical output hashes.
    diverged            : Number of datums with different output hashes.
    error_message       : Set if fidelity_class == ERROR.
    notes               : Human-readable explanation of the result.
    """
    run_id: str
    fidelity_class: FidelityClass
    original_spec_hash: str
    replay_timestamp: str
    replay_environment: EnvironmentSnapshot
    original_environment: EnvironmentSnapshot
    environment_diffs: list[EnvironmentDiff]
    datum_results: list[DatumFidelity]
    total_datums: int
    matched: int
    diverged: int
    error_message: str = ""
    notes: str = ""

    @property
    def fidelity_rate(self) -> float:
        if self.total_datums == 0:
            return 0.0
        return self.matched / self.total_datums

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "fidelity_class": self.fidelity_class.value,
            "original_spec_hash": self.original_spec_hash,
            "replay_timestamp": self.replay_timestamp,
            "replay_environment": {
                "python_version": self.replay_environment.python_version,
                "platform": self.replay_environment.platform,
                "library_versions": self.replay_environment.library_versions,
            },
            "original_environment": {
                "python_version": self.original_environment.python_version,
                "platform": self.original_environment.platform,
                "library_versions": self.original_environment.library_versions,
            },
            "environment_diffs": [d.to_dict() for d in self.environment_diffs],
            "datum_results": [r.to_dict() for r in self.datum_results],
            "total_datums": self.total_datums,
            "matched": self.matched,
            "diverged": self.diverged,
            "fidelity_rate": self.fidelity_rate,
            "error_message": self.error_message,
            "notes": self.notes,
        }

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_dict(), f, indent=2)

    def print_summary(self) -> None:
        lines = [
            "",
            "=" * 60,
            f"REPLAY FIDELITY REPORT",
            "=" * 60,
            f"  run_id         : {self.run_id}",
            f"  fidelity class : {self.fidelity_class.value}",
            f"  fidelity rate  : {self.fidelity_rate:.1%}  ({self.matched}/{self.total_datums} matched)",
            f"  diverged       : {self.diverged}",
            f"  replayed at    : {self.replay_timestamp}",
        ]

        if self.environment_diffs:
            lines.append(f"\n  Environment drift ({len(self.environment_diffs)} fields):")
            for d in self.environment_diffs:
                lines.append(f"    {d.field}: '{d.original}' → '{d.replay}'")
        else:
            lines.append("  Environment: identical to original run.")

        if self.error_message:
            lines.append(f"\n  ERROR: {self.error_message}")

        if self.notes:
            lines.append(f"\n  Notes: {self.notes}")

        if self.diverged > 0:
            lines.append(f"\n  First 3 diverged datums:")
            shown = 0
            for r in self.datum_results:
                if not r.matched:
                    lines.append(
                        f"    datum[{r.datum_index}] "
                        f"original={r.original_output_hash[:16]}… "
                        f"replay={r.replay_output_hash[:16]}…"
                    )
                    shown += 1
                    if shown >= 3:
                        break

        lines.append("=" * 60)
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# ReplayEngine
# ---------------------------------------------------------------------------

class ReplayEngine:
    """
    Replays a past pipeline run from its ReplaySpec.

    The engine re-executes the same transformation sequence against the same
    input hashes, then compares output hashes to the original spec.

    It does NOT re-ingest data from disk — it re-runs transformations against
    the data provided to replay(). This means the caller is responsible for
    providing the original input data in the original order. In production,
    raw inputs would be retrieved from a content-addressed store keyed by
    input_hash. Here, we verify the caller's data matches the original
    input_hashes before proceeding.

    Parameters
    ----------
    store           : The ProvenanceStore holding records from the original run.
    domain          : The DataDomain to use for trust checks during replay.
    """

    def __init__(self, store: ProvenanceStore, domain: DataDomain):
        self.store = store
        self.domain = domain

    def replay(
        self,
        spec: ReplaySpec,
        original_data: list[dict],
        transformations: list[tuple[str, object]],  # same list as original run
        replay_operator: str = "replay_engine",
        context: dict | None = None,
    ) -> ReplayFidelityReport:
        """
        Re-execute the run described by spec against original_data.

        Parameters
        ----------
        spec            : The sealed ReplaySpec from the original run.
        original_data   : The raw input data in original order.
        transformations : The same (name, callable) pairs used in the original run.
        replay_operator : Who is performing this replay.
        context         : Optional context dict passed to invariants.

        Returns
        -------
        ReplayFidelityReport — complete fidelity analysis.
        """
        replay_env = current_environment(spec.config_snapshot)
        replay_ts = datetime.now(timezone.utc).isoformat()

        # --- Spec integrity check ---
        if not spec.verify():
            return self._error_report(
                spec, replay_env, replay_ts,
                "ReplaySpec hash verification failed — spec has been tampered with."
            )

        # --- Input count check ---
        if len(original_data) != len(spec.input_hashes):
            return self._error_report(
                spec, replay_env, replay_ts,
                f"Input count mismatch: spec expects {len(spec.input_hashes)}, "
                f"got {len(original_data)}."
            )

        # --- Input hash verification ---
        for i, (datum, expected_hash) in enumerate(zip(original_data, spec.input_hashes)):
            actual_hash = _hash_datum(datum)
            if actual_hash != expected_hash:
                return self._error_report(
                    spec, replay_env, replay_ts,
                    f"Input hash mismatch at datum[{i}]: "
                    f"expected {expected_hash[:16]}…, got {actual_hash[:16]}…. "
                    "The data provided does not match the original run inputs."
                )

        # --- Environment diff ---
        env_diffs = self._compare_environments(spec.environment, replay_env)

        # --- Re-run transformations ---
        replay_store = ProvenanceStore(":memory:")
        runner = PipelineRunner(
            domain=self.domain,
            store=replay_store,
            environment=replay_env,
            operator=replay_operator,
            config=spec.config_snapshot,
            transformations=transformations,
            require_provenance=True,
        )

        try:
            replay_results = runner.run(original_data, context=context)
            replay_spec = runner.get_spec()
        except Exception as e:
            return self._error_report(spec, replay_env, replay_ts, f"Replay execution failed: {e}")

        # --- Compare output hashes ---
        datum_results: list[DatumFidelity] = []
        for i, (original_hash, (_, _, replay_prov)) in enumerate(
            zip(spec.output_hashes, replay_results)
        ):
            if i >= len(replay_spec.output_hashes):
                datum_results.append(DatumFidelity(
                    datum_index=i,
                    original_output_hash=original_hash,
                    replay_output_hash="MISSING",
                    matched=False,
                    notes="Replay produced fewer outputs than original.",
                ))
                continue

            replay_hash = replay_spec.output_hashes[i]
            matched = (original_hash == replay_hash)
            notes = ""
            if not matched and env_diffs:
                notes = (
                    "Output differs. Environment drift detected — "
                    "divergence may be due to floating-point nondeterminism "
                    "across environments rather than pipeline nondeterminism."
                )
            elif not matched:
                notes = (
                    "Output differs in identical environment. "
                    "Pipeline may contain nondeterminism (e.g. un-seeded RNG, "
                    "timestamp injection, or OS-level randomness)."
                )

            datum_results.append(DatumFidelity(
                datum_index=i,
                original_output_hash=original_hash,
                replay_output_hash=replay_hash,
                matched=matched,
                notes=notes,
            ))

        matched_count = sum(1 for r in datum_results if r.matched)
        diverged_count = len(datum_results) - matched_count

        # --- Classify fidelity ---
        if diverged_count == 0:
            fidelity_class = FidelityClass.IDENTICAL
            notes = "All output hashes match. Full within-platform fidelity confirmed."
        elif env_diffs and diverged_count > 0:
            fidelity_class = FidelityClass.ENVIRONMENT_DRIFT
            notes = (
                "Environment differs from original run. Output divergences are expected "
                "and may be due to floating-point nondeterminism across environments. "
                "Re-run in the original environment to isolate pipeline nondeterminism."
            )
        elif diverged_count == len(datum_results):
            fidelity_class = FidelityClass.OUTPUT_DIVERGED
            notes = (
                "All outputs diverged in an identical environment. "
                "The pipeline contains nondeterminism. "
                "Inspect transformation steps for un-seeded RNG or timestamp injection."
            )
        else:
            fidelity_class = FidelityClass.PARTIAL
            notes = (
                f"{matched_count}/{len(datum_results)} outputs matched. "
                "Partial divergence — inspect diverged datums for patterns."
            )

        return ReplayFidelityReport(
            run_id=spec.run_id,
            fidelity_class=fidelity_class,
            original_spec_hash=spec.spec_hash,
            replay_timestamp=replay_ts,
            replay_environment=replay_env,
            original_environment=spec.environment,
            environment_diffs=env_diffs,
            datum_results=datum_results,
            total_datums=len(datum_results),
            matched=matched_count,
            diverged=diverged_count,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compare_environments(
        self,
        original: EnvironmentSnapshot,
        replay: EnvironmentSnapshot,
    ) -> list[EnvironmentDiff]:
        """
        §6 tolerance policy:
          zero-tolerance  : python major.minor, contract_version, domain_version
                            → any divergence creates an EnvironmentDiff
          warn-on-diverge : platform string, library patch versions
                            → divergence noted with [WARN] prefix, not fatal
          ignored         : process ID, hostname (not captured in EnvironmentSnapshot)
        """
        diffs: list[EnvironmentDiff] = []

        # --- Zero-tolerance fields ---
        def _major_minor(ver: str) -> str:
            parts = ver.split(".")
            return ".".join(parts[:2]) if len(parts) >= 2 else ver

        if _major_minor(original.python_version) != _major_minor(replay.python_version):
            diffs.append(EnvironmentDiff(
                "python_version [ZERO_TOLERANCE]",
                original.python_version, replay.python_version
            ))
        elif original.python_version != replay.python_version:
            # Same major.minor, different patch — warn only
            diffs.append(EnvironmentDiff(
                "python_version [WARN]",
                original.python_version, replay.python_version
            ))

        orig_cv = getattr(original, "contract_version", "")
        rep_cv  = getattr(replay,   "contract_version", "")
        if orig_cv != rep_cv:
            diffs.append(EnvironmentDiff("contract_version [ZERO_TOLERANCE]", orig_cv, rep_cv))

        orig_dv = getattr(original, "domain_version", "")
        rep_dv  = getattr(replay,   "domain_version", "")
        if orig_dv != rep_dv:
            diffs.append(EnvironmentDiff("domain_version [ZERO_TOLERANCE]", orig_dv, rep_dv))

        # --- Warn-on-diverge: platform ---
        if original.platform != replay.platform:
            diffs.append(EnvironmentDiff("platform [WARN]", original.platform, replay.platform))

        # --- Library versions: major.minor divergence → zero-tolerance; patch → warn ---
        all_libs = set(original.library_versions) | set(replay.library_versions)
        for lib in sorted(all_libs):
            orig_v = original.library_versions.get(lib, "not_installed")
            rep_v  = replay.library_versions.get(lib, "not_installed")
            if orig_v == rep_v:
                continue
            if _major_minor(orig_v) != _major_minor(rep_v):
                diffs.append(EnvironmentDiff(f"lib:{lib} [ZERO_TOLERANCE]", orig_v, rep_v))
            else:
                diffs.append(EnvironmentDiff(f"lib:{lib} [WARN]", orig_v, rep_v))

        return diffs

    def _error_report(
        self,
        spec: ReplaySpec,
        replay_env: EnvironmentSnapshot,
        replay_ts: str,
        error_message: str,
    ) -> ReplayFidelityReport:
        return ReplayFidelityReport(
            run_id=spec.run_id,
            fidelity_class=FidelityClass.ERROR,
            original_spec_hash=spec.spec_hash,
            replay_timestamp=replay_ts,
            replay_environment=replay_env,
            original_environment=spec.environment,
            environment_diffs=[],
            datum_results=[],
            total_datums=0,
            matched=0,
            diverged=0,
            error_message=error_message,
        )
