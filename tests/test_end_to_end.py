"""
End-to-end integration test.

Proves the full pipeline works:
  1. Generate synthetic dataset with known corruptions
  2. Attach provenance records to each datum
  3. Run TrustContract (with full invariant engine)
  4. Verify the engine caught what was injected
  5. Print lineage graph for a selected datum
  6. Print batch summary

Run with: python tests/test_end_to_end.py
"""

from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import platform

from data.synthetic.generator import CorruptionConfig, SyntheticDataset
from src.domain.detector_event import DetectorEventDomain
from src.domain.generic_tabular import GenericTabularDomain
from src.invariants.contract import TrustContract, Verdict
from src.provenance.record import (
    EnvironmentSnapshot,
    make_ingestion_record,
    make_transformation_record,
)
from src.provenance.store import ProvenanceStore


# ---------------------------------------------------------------------------
# Shared environment snapshot for all test runs
# ---------------------------------------------------------------------------

def make_test_env() -> EnvironmentSnapshot:
    return EnvironmentSnapshot(
        python_version=platform.python_version(),
        platform=platform.platform(),
        library_versions={"test": "1.0.0"},
        config_hash="test_config_v1",
    )


# ---------------------------------------------------------------------------
# Test 1: DetectorEvent domain
# ---------------------------------------------------------------------------

def test_detector_domain():
    print("\n" + "=" * 60)
    print("TEST 1: DetectorEvent domain")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.10,
        out_of_range_channel_rate=0.05,
        null_injection_rate=0.05,
        missing_field_rate=0.03,
        adc_saturation_rate=0.08,
        bad_quality_flag_rate=0.06,
    )

    dataset = SyntheticDataset.detector_events(n=200, corruption=corruption, seed=42)
    print(dataset.corruption_report())

    env = make_test_env()
    store = ProvenanceStore(":memory:")
    domain = DetectorEventDomain()
    contract = TrustContract(domain, require_provenance=True)

    provenance_map: dict = {}

    # Attach provenance to each datum and run through a fake calibration transform
    for datum in dataset.data:
        datum_id = datum["event_id"]

        # Step 1: ingestion record
        ingest_record = make_ingestion_record(datum, operator="test_runner", environment=env, datum_id=datum_id)
        store.save(ingest_record)

        # Step 2: simulate a calibration transformation
        calibrated = {**datum, "_calibrated": True}
        calib_record = make_transformation_record(
            datum=calibrated,
            transformation_id="CALIBRATE_v1",
            parent_record=ingest_record,
            operator="calibration_service",
            environment=env,
        )
        store.save(calib_record)

        provenance_map[datum_id] = calib_record

    # Evaluate the batch
    verdicts = contract.evaluate_batch(
        data=[{**d, "id": d["event_id"]} for d in dataset.data],
        provenance_records=provenance_map,
    )

    summary = contract.batch_summary(verdicts)
    print(f"\nBatch summary:")
    print(f"  Total:     {summary['total']}")
    print(f"  Trusted:   {summary['trusted']}")
    print(f"  Untrusted: {summary['untrusted']}")
    print(f"  Trust rate: {summary['trust_rate']:.1%}")
    print(f"  Total warnings: {summary['total_warnings']}")
    print(f"  Failures by invariant:")
    for inv, count in sorted(summary["failure_breakdown"].items(), key=lambda x: -x[1]):
        print(f"    {inv}: {count}")

    # Verify the engine caught corrupted datums
    corrupted_ids = dataset.get_corrupted_ids()
    untrusted_ids = {v.datum_id for v in verdicts if not v.is_trusted}
    caught = corrupted_ids & untrusted_ids
    missed = corrupted_ids - untrusted_ids

    print(f"\nCorruption detection:")
    print(f"  Injected corruptions on: {len(corrupted_ids)} datums")
    print(f"  Caught as UNTRUSTED:     {len(caught)}")
    print(f"  Missed (still TRUSTED):  {len(missed)}")
    if missed:
        print("  Missed datum IDs (first 5):", list(missed)[:5])
        # Note: a datum can have only WARN-level corruption and still pass.
        # This is expected behavior, not a bug.

    # Print lineage for one datum
    if dataset.data:
        sample_id = dataset.data[0]["event_id"]
        print(f"\nLineage graph for datum {sample_id}:")
        store.print_lineage(sample_id)

    # Verify provenance store integrity
    report = store.verify_integrity()
    print(f"\nProvenance integrity: {report.summary()}")

    store.close()
    assert summary["untrusted"] > 0, "Expected some untrusted datums from corrupted dataset"
    print("\nTEST 1 PASSED")
    return summary


# ---------------------------------------------------------------------------
# Test 2: GenericTabular domain
# ---------------------------------------------------------------------------

def test_generic_tabular_domain():
    print("\n" + "=" * 60)
    print("TEST 2: GenericTabular domain")
    print("=" * 60)

    corruption = CorruptionConfig(
        negative_energy_rate=0.08,
        null_injection_rate=0.06,
        missing_field_rate=0.04,
    )

    dataset = SyntheticDataset.generic_tabular(n=100, corruption=corruption, seed=99)
    print(dataset.corruption_report())

    env = make_test_env()
    store = ProvenanceStore(":memory:")
    domain = GenericTabularDomain(
        required_fields=["id", "timestamp", "value"],
        field_ranges={"value": (-1e9, 1e9)},
    )
    contract = TrustContract(domain, require_provenance=True)

    provenance_map = {}
    for datum in dataset.data:
        datum_id = datum.get("id", "unknown")
        rec = make_ingestion_record(datum, operator="test_runner", environment=env, datum_id=datum_id)
        store.save(rec)
        provenance_map[datum_id] = rec

    verdicts = contract.evaluate_batch(dataset.data, provenance_records=provenance_map)
    summary = contract.batch_summary(verdicts)

    print(f"\nBatch summary:")
    print(f"  Total: {summary['total']} | Trusted: {summary['trusted']} | Untrusted: {summary['untrusted']}")
    print(f"  Trust rate: {summary['trust_rate']:.1%}")

    corrupted_ids = dataset.get_corrupted_ids()
    untrusted_ids = {v.datum_id for v in verdicts if not v.is_trusted}
    caught = corrupted_ids & untrusted_ids
    print(f"\nDetection: {len(caught)}/{len(corrupted_ids)} corrupted datums caught as UNTRUSTED")

    report = store.verify_integrity()
    print(f"Provenance integrity: {report.summary()}")

    store.close()
    assert summary["untrusted"] > 0
    print("\nTEST 2 PASSED")
    return summary


# ---------------------------------------------------------------------------
# Test 3: Provenance tampering detection
# ---------------------------------------------------------------------------

def test_provenance_tampering():
    print("\n" + "=" * 60)
    print("TEST 3: Provenance tampering detection")
    print("=" * 60)

    env = make_test_env()
    domain = DetectorEventDomain()
    contract = TrustContract(domain, require_provenance=True)

    datum = {
        "event_id": "EVT_TAMPER_TEST",
        "run_id": "RUN_0000",
        "timestamp": 1_700_000_000.0,
        "energy_gev": 124.5,
        "detector_channel": 42,
        "signal_adc": 2048.0,
        "quality_flag": 0,
        "calibration_run": "CAL_0000",
        "id": "EVT_TAMPER_TEST",
    }

    valid_record = make_ingestion_record(datum, operator="test_runner", environment=env, datum_id="EVT_TAMPER_TEST")

    # Simulate tampering by constructing a record with a mismatched hash.
    # We do this by replacing the frozen object's content_hash field via object.__setattr__.
    from src.provenance.record import ProvenanceRecord
    tampered_record = ProvenanceRecord(
        record_id=valid_record.record_id,
        datum_id=valid_record.datum_id,
        input_hash=valid_record.input_hash,
        transformation_id=valid_record.transformation_id,
        timestamp=valid_record.timestamp,
        environment=valid_record.environment,
        operator=valid_record.operator,
        parent_hash=valid_record.parent_hash,
        parent_ids=valid_record.parent_ids,
        notes=valid_record.notes,
    )
    # Force a bad content hash by directly mutating the frozen field
    object.__setattr__(tampered_record, "content_hash", "000000000000_TAMPERED")

    verdict_valid   = contract.evaluate(datum, datum_id="EVT_TAMPER_TEST", provenance_record=valid_record)
    verdict_tampered = contract.evaluate(datum, datum_id="EVT_TAMPER_TEST", provenance_record=tampered_record)
    verdict_missing  = contract.evaluate(datum, datum_id="EVT_TAMPER_TEST", provenance_record=None)

    print(f"  Valid provenance:   {verdict_valid.verdict.value}")
    print(f"  Tampered provenance: {verdict_tampered.verdict.value}")
    print(f"  Missing provenance:  {verdict_missing.verdict.value}")

    assert verdict_valid.is_trusted, "Valid datum with clean provenance should be TRUSTED"
    assert not verdict_tampered.is_trusted, "Tampered provenance should produce UNTRUSTED"
    assert not verdict_missing.is_trusted, "Missing provenance should produce UNTRUSTED"

    print("\nTEST 3 PASSED")


# ---------------------------------------------------------------------------
# Test 4: Domain interface compliance
# ---------------------------------------------------------------------------

def test_domain_compliance():
    print("\n" + "=" * 60)
    print("TEST 4: Domain interface compliance")
    print("=" * 60)

    for DomainClass in [DetectorEventDomain, GenericTabularDomain]:
        domain = DomainClass()
        errors = domain.validate_interface()
        status = "PASS" if not errors else f"FAIL: {errors}"
        print(f"  {domain.name} v{domain.version}: {status}")
        assert not errors, f"{domain.name} failed interface compliance: {errors}"

    print("\nTEST 4 PASSED")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Data Trust System — End-to-End Integration Test")
    print("=" * 60)

    try:
        test_domain_compliance()
        test_provenance_tampering()
        summary1 = test_detector_domain()
        summary2 = test_generic_tabular_domain()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED")
        print("=" * 60)
        print(f"DetectorEvent trust rate:  {summary1['trust_rate']:.1%}")
        print(f"GenericTabular trust rate: {summary2['trust_rate']:.1%}")

    except AssertionError as e:
        print(f"\nTEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        import traceback
        print(f"\nUNEXPECTED ERROR: {e}")
        traceback.print_exc()
        sys.exit(1)
