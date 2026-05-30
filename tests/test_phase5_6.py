"""
Phase 5 + 6 test suite — EvidenceStore and DataTrustPipeline.

Tests
-----
1. Evidence store: records verdicts, queries failures, warnings, run summary.
2. Analyst disposition update: reviewer can annotate a quarantined datum.
3. Invariant failure rates: compound query returns correctly ranked results.
4. Pipeline unified run: single call, all components wired, results complete.
5. Pipeline replay via unified entry point.
6. Evidence report: readable without touching code.
7. Cross-run datum history: same datum across two runs tracked correctly.

Run with: python tests/test_phase5_6.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.synthetic.generator import CorruptionConfig, SyntheticDataset
from src.domain.detector_event import DetectorEventDomain
from src.domain.generic_tabular import GenericTabularDomain
from src.evidence.store import Disposition, EvidenceStore
from src.invariants.contract import TrustContract
from src.pipeline import DataTrustPipeline
from src.provenance.record import EnvironmentSnapshot, make_ingestion_record
from src.provenance.store import ProvenanceStore
from src.replay.engine import FidelityClass
import platform


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_env() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        python_version=platform.python_version(),
        platform=platform.platform(),
        library_versions={},
        config_hash="test",
    )


def calibrate(datum: dict) -> dict:
    out = dict(datum)
    out["_calibrated"] = True
    return out


def normalise(datum: dict) -> dict:
    out = dict(datum)
    if "energy_gev" in out and out["energy_gev"] is not None:
        try:
            out["energy_gev_normalised"] = round(float(out["energy_gev"]) / 125.0, 6)
        except (TypeError, ValueError):
            pass
    return out


TRANSFORMS = [("CALIBRATE_v1", calibrate), ("NORMALISE", normalise)]


# ---------------------------------------------------------------------------
# Test 1: Evidence store — record and query
# ---------------------------------------------------------------------------

def test_evidence_store_basics():
    print("\n" + "=" * 60)
    print("TEST 1: EvidenceStore — record and query")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.15,
        null_injection_rate=0.08,
        adc_saturation_rate=0.10,
        bad_quality_flag_rate=0.08,
    )
    dataset = SyntheticDataset.detector_events(n=100, corruption=corruption, seed=10)
    domain = DetectorEventDomain()

    env = make_env()
    prov_store = ProvenanceStore(":memory:")
    evid_store = EvidenceStore(":memory:")
    contract = TrustContract(domain, require_provenance=True)

    prov_map = {}
    for datum in dataset.data:
        did = datum["event_id"]
        rec = make_ingestion_record(datum, operator="test", environment=env, datum_id=did)
        prov_store.save(rec)
        prov_map[did] = rec

    verdicts = contract.evaluate_batch(
        [{**d, "id": d["event_id"]} for d in dataset.data],
        provenance_records=prov_map,
    )
    run_id = "TEST_RUN_001"
    evid_store.record_many_verdicts(verdicts, run_id=run_id)
    summary = contract.batch_summary(verdicts)
    evid_store.record_run_summary(
        run_id=run_id,
        domain_name=domain.name,
        domain_version=domain.version,
        operator="test",
        started_at="2026-01-01T00:00:00+00:00",
        summary=summary,
    )

    # Query failures
    failures = evid_store.get_failures_for_run(run_id)
    assert len(failures) == summary["untrusted"], \
        f"Expected {summary['untrusted']} failure verdicts, got {len(failures)}"

    # Query warnings
    warnings = evid_store.get_warnings_for_run(run_id)
    print(f"  Failures: {len(failures)} | Warnings (evidence items): {len(warnings)}")

    # Query by invariant
    energy_fails = evid_store.get_failures_for_invariant("energy_gev_positive", run_id)
    print(f"  energy_gev_positive failures: {len(energy_fails)}")

    # Run summary
    run_row = evid_store.get_run_summary(run_id)
    assert run_row is not None
    assert run_row.total_datums == 100
    assert abs(run_row.trust_rate - summary["trust_rate"]) < 0.001
    run_row.print_summary()

    # Invariant failure rates
    rates = evid_store.invariant_failure_rates(run_id)
    assert len(rates) > 0
    print(f"  Invariant failure rates ({len(rates)} invariants tracked):")
    for r in rates[:4]:
        print(f"    {r['invariant_name']}: {r['failures']}/{r['total_checks']} ({r['failure_rate']:.1%})")

    evid_store.close()
    prov_store.close()
    print("TEST 1 PASSED")


# ---------------------------------------------------------------------------
# Test 2: Analyst disposition update
# ---------------------------------------------------------------------------

def test_disposition_update():
    print("\n" + "=" * 60)
    print("TEST 2: Disposition update (analyst review)")
    print("=" * 60)

    dataset = SyntheticDataset.detector_events(
        n=30,
        corruption=CorruptionConfig(negative_energy_rate=0.30),
        seed=11,
    )
    domain = DetectorEventDomain()
    env = make_env()
    prov_store = ProvenanceStore(":memory:")
    evid_store = EvidenceStore(":memory:")
    contract = TrustContract(domain, require_provenance=True)

    prov_map = {}
    for datum in dataset.data:
        did = datum["event_id"]
        rec = make_ingestion_record(datum, operator="test", environment=env, datum_id=did)
        prov_store.save(rec)
        prov_map[did] = rec

    verdicts = contract.evaluate_batch(
        [{**d, "id": d["event_id"]} for d in dataset.data],
        provenance_records=prov_map,
    )
    run_id = "TEST_RUN_002"
    evid_store.record_many_verdicts(verdicts, run_id=run_id)

    # Find a quarantined datum
    failures = evid_store.get_failures_for_run(run_id)
    assert failures, "Expected at least one quarantined datum."
    target_verdict_id = failures[0].verdict_id

    # Analyst reviews it
    updated = evid_store.update_disposition(
        verdict_id=target_verdict_id,
        disposition=Disposition.ANALYST_REVIEWED,
        analyst_notes="Confirmed negative energy — hardware glitch in channel 42. Reject.",
    )
    assert updated, "Disposition update should return True."

    # Verify it persisted
    row = evid_store.get_verdict(target_verdict_id)
    assert row.disposition == Disposition.ANALYST_REVIEWED
    assert "hardware glitch" in row.analyst_notes
    print(f"  Updated disposition for {target_verdict_id[:16]}… → {row.disposition}")
    print(f"  Notes: {row.analyst_notes}")

    evid_store.close()
    prov_store.close()
    print("TEST 2 PASSED")


# ---------------------------------------------------------------------------
# Test 3: Cross-run datum history
# ---------------------------------------------------------------------------

def test_datum_history():
    print("\n" + "=" * 60)
    print("TEST 3: Cross-run datum history")
    print("=" * 60)

    # Run a datum through two different runs (simulated by two verdict records)
    dataset = SyntheticDataset.detector_events(n=5, seed=12)
    domain = DetectorEventDomain()
    env = make_env()
    prov_store = ProvenanceStore(":memory:")
    evid_store = EvidenceStore(":memory:")
    contract = TrustContract(domain, require_provenance=True)

    # Run 1 — clean
    prov_map = {}
    for datum in dataset.data:
        did = datum["event_id"]
        rec = make_ingestion_record(datum, operator="test", environment=env, datum_id=did)
        prov_store.save(rec)
        prov_map[did] = rec

    verdicts_r1 = contract.evaluate_batch(
        [{**d, "id": d["event_id"]} for d in dataset.data],
        provenance_records=prov_map,
    )
    evid_store.record_many_verdicts(verdicts_r1, run_id="RUN_A")

    # Run 2 — same datum but now corrupt
    target_datum = dict(dataset.data[0])
    target_id = target_datum["event_id"]
    target_datum["energy_gev"] = -50.0    # inject failure

    rec2 = make_ingestion_record(target_datum, operator="test", environment=env, datum_id=target_id)
    prov_store.save(rec2)
    verdict_r2 = contract.evaluate(
        {**target_datum, "id": target_id},
        datum_id=target_id,
        provenance_record=rec2,
    )
    evid_store.record_verdict(verdict_r2, run_id="RUN_B")

    history = evid_store.get_datum_history(target_id)
    assert len(history) == 2, f"Expected 2 history entries, got {len(history)}"
    verdicts_for_datum = {row.run_id: row.verdict for row in history}
    assert verdicts_for_datum["RUN_A"] == "TRUSTED"
    assert verdicts_for_datum["RUN_B"] == "UNTRUSTED"

    print(f"  Datum {target_id[:16]}… history:")
    for row in history:
        print(f"    {row.run_id}: {row.verdict} ({row.disposition})")

    evid_store.close()
    prov_store.close()
    print("TEST 3 PASSED")


# ---------------------------------------------------------------------------
# Test 4: Unified pipeline — single call, complete results
# ---------------------------------------------------------------------------

def test_unified_pipeline():
    print("\n" + "=" * 60)
    print("TEST 4: DataTrustPipeline — unified single call")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.10,
        out_of_range_channel_rate=0.05,
        null_injection_rate=0.05,
        adc_saturation_rate=0.08,
        bad_quality_flag_rate=0.06,
    )
    dataset = SyntheticDataset.detector_events(n=200, corruption=corruption, seed=20)

    with DataTrustPipeline(
        domain=DetectorEventDomain(),
        db_path=":memory:",
        operator="test_pipeline",
        config={"calibration_version": "v1"},
        transformations=TRANSFORMS,
    ) as pipeline:

        result = pipeline.run(dataset.data, notes="Phase 6 integration test")
        result.print_summary()

        # Verify all components populated
        assert result.spec.sealed_at, "Spec should be sealed."
        assert len(result.verdicts) == 200
        assert result.summary["total"] == 200
        assert result.summary["untrusted"] > 0

        # Evidence store populated
        run_row = pipeline.evidence_store.get_run_summary(result.run_id)
        assert run_row is not None
        assert run_row.total_datums == 200

        # Provenance store populated and clean
        integrity = pipeline.provenance_store.verify_integrity()
        assert integrity.is_clean, integrity.summary()
        print(f"  Provenance integrity: {integrity.summary()}")

    print("TEST 4 PASSED")


# ---------------------------------------------------------------------------
# Test 5: Replay via unified pipeline
# ---------------------------------------------------------------------------

def test_pipeline_replay():
    print("\n" + "=" * 60)
    print("TEST 5: DataTrustPipeline — replay")
    print("=" * 60)

    dataset = SyntheticDataset.detector_events(n=100, seed=21)

    with DataTrustPipeline(
        domain=DetectorEventDomain(),
        db_path=":memory:",
        operator="test_pipeline",
        config={"calibration_version": "v1"},
        transformations=TRANSFORMS,
    ) as pipeline:

        result = pipeline.run(dataset.data)
        fidelity = pipeline.replay(result.spec, dataset.data)
        fidelity.print_summary()

        assert fidelity.fidelity_class == FidelityClass.IDENTICAL, \
            f"Expected IDENTICAL, got {fidelity.fidelity_class.value}"
        assert fidelity.fidelity_rate == 1.0

    print("TEST 5 PASSED")


# ---------------------------------------------------------------------------
# Test 6: Failure report — readable without code
# ---------------------------------------------------------------------------

def test_failure_report():
    print("\n" + "=" * 60)
    print("TEST 6: FailureReport — readable without code")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.12,
        out_of_range_channel_rate=0.08,
        null_injection_rate=0.05,
        missing_field_rate=0.04,
        adc_saturation_rate=0.10,
        bad_quality_flag_rate=0.07,
    )
    dataset = SyntheticDataset.detector_events(n=150, corruption=corruption, seed=30)

    with DataTrustPipeline(
        domain=DetectorEventDomain(),
        db_path=":memory:",
        operator="test_pipeline",
        transformations=TRANSFORMS,
    ) as pipeline:

        result = pipeline.run(dataset.data)
        report = result.failure_report
        report.print()

        # A reviewer should be able to answer these questions without touching code:
        assert report.total_datums == 150
        assert len(report.quarantined_datum_ids) == result.summary["untrusted"]
        assert report.domain_name == "DetectorEvent"
        assert len(report.failure_rows) > 0

        # Most problematic datums query
        worst = pipeline.evidence_store.most_problematic_datums(result.run_id, limit=5)
        print(f"\n  Most problematic datums (top 5):")
        for entry in worst:
            print(f"    {entry['datum_id'][:20]}… — {entry['failure_count']} failure(s)")

    print("TEST 6 PASSED")


# ---------------------------------------------------------------------------
# Test 7: GenericTabular domain through unified pipeline
# ---------------------------------------------------------------------------

def test_generic_tabular_pipeline():
    print("\n" + "=" * 60)
    print("TEST 7: GenericTabular — unified pipeline")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.08,
        null_injection_rate=0.06,
        missing_field_rate=0.04,
        statistical_outlier_rate=0.05,
    )
    dataset = SyntheticDataset.generic_tabular(n=200, corruption=corruption, seed=77)

    with DataTrustPipeline(
        domain=GenericTabularDomain(
            required_fields=["id", "timestamp", "value"],
            field_ranges={"value": (-1e9, 1e9)},
        ),
        db_path=":memory:",
        operator="test_pipeline",
        transformations=[("CALIBRATE_v1", calibrate)],
    ) as pipeline:

        result = pipeline.run(dataset.data)
        result.print_summary()

        fidelity = pipeline.replay(result.spec, dataset.data)
        assert fidelity.fidelity_class == FidelityClass.IDENTICAL

        print(f"  Replay fidelity: {fidelity.fidelity_class.value} ({fidelity.fidelity_rate:.0%})")

    print("TEST 7 PASSED")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Phase 5 + 6 Test Suite — EvidenceStore + DataTrustPipeline")
    print("=" * 60)

    try:
        test_evidence_store_basics()
        test_disposition_update()
        test_datum_history()
        test_unified_pipeline()
        test_pipeline_replay()
        test_failure_report()
        test_generic_tabular_pipeline()

        print("\n" + "=" * 60)
        print("ALL PHASE 5 + 6 TESTS PASSED")
        print("=" * 60)

    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR: {e}")
        import traceback; traceback.print_exc()
        sys.exit(1)
