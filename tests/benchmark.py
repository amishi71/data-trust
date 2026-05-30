"""
Full system benchmark.

Runs the complete DataTrustPipeline against synthetic datasets with known,
measurable corruption rates. Produces structured results that directly feed
the technical report's empirical section.

What this measures
------------------
- Detection rate: what fraction of injected FAIL-level corruptions were caught?
- False positive rate: what fraction of clean datums were flagged UNTRUSTED?
- Warning precision: were WARN-level events correctly isolated from FAILs?
- Replay fidelity: does a deterministic pipeline replay at 100%?
- Evidence store completeness: can a reviewer reconstruct failure history
  without touching code?

Run with: python tests/benchmark.py
Results saved to: data/benchmark_results.json
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from pathlib import Path

from data.synthetic.generator import CorruptionConfig, SyntheticDataset
from src.domain.detector_event import DetectorEventDomain
from src.domain.generic_tabular import GenericTabularDomain
from src.pipeline import DataTrustPipeline
from src.replay.engine import FidelityClass


# ---------------------------------------------------------------------------
# Shared deterministic transformations
# ---------------------------------------------------------------------------

def calibrate(datum: dict) -> dict:
    out = dict(datum)
    out["_calibrated"] = True
    out["_cal_version"] = "v1"
    return out


def normalise_energy(datum: dict) -> dict:
    out = dict(datum)
    if "energy_gev" in out and out["energy_gev"] is not None:
        try:
            out["energy_gev_normalised"] = round(float(out["energy_gev"]) / 125.0, 6)
        except (TypeError, ValueError):
            pass
    return out


TRANSFORMS = [("CALIBRATE_v1", calibrate), ("NORMALISE_ENERGY", normalise_energy)]


# ---------------------------------------------------------------------------
# Benchmark scenario definition
# ---------------------------------------------------------------------------

SCENARIOS = [
    {
        "label": "DetectorEvent — light corruption",
        "domain": "detector",
        "n": 500,
        "seed": 42,
        "corruption": CorruptionConfig(
            negative_energy_rate=0.05,
            out_of_range_channel_rate=0.03,
            null_injection_rate=0.03,
            adc_saturation_rate=0.05,
            bad_quality_flag_rate=0.04,
        ),
    },
    {
        "label": "DetectorEvent — heavy corruption",
        "domain": "detector",
        "n": 500,
        "seed": 43,
        "corruption": CorruptionConfig(
            negative_energy_rate=0.15,
            out_of_range_channel_rate=0.10,
            null_injection_rate=0.08,
            missing_field_rate=0.05,
            adc_overflow_rate=0.04,
            adc_saturation_rate=0.10,
            bad_quality_flag_rate=0.08,
            statistical_outlier_rate=0.06,
        ),
    },
    {
        "label": "DetectorEvent — clean (no corruption)",
        "domain": "detector",
        "n": 300,
        "seed": 44,
        "corruption": CorruptionConfig(),
    },
    {
        "label": "GenericTabular — moderate corruption",
        "domain": "tabular",
        "n": 400,
        "seed": 99,
        "corruption": CorruptionConfig(
            negative_energy_rate=0.08,
            null_injection_rate=0.06,
            missing_field_rate=0.04,
        ),
    },
    {
        "label": "GenericTabular — clean (no corruption)",
        "domain": "tabular",
        "n": 200,
        "seed": 100,
        "corruption": CorruptionConfig(),
    },
]


# ---------------------------------------------------------------------------
# Per-scenario benchmark runner
# ---------------------------------------------------------------------------

def run_scenario(scenario: dict) -> dict:
    label = scenario["label"]
    print(f"\n  [{label}]")

    # Generate data
    if scenario["domain"] == "detector":
        dataset = SyntheticDataset.detector_events(
            n=scenario["n"],
            corruption=scenario["corruption"],
            seed=scenario["seed"],
        )
        domain = DetectorEventDomain()
    else:
        dataset = SyntheticDataset.generic_tabular(
            n=scenario["n"],
            corruption=scenario["corruption"],
            seed=scenario["seed"],
        )
        domain = GenericTabularDomain(
            required_fields=["id", "timestamp", "value"],
            field_ranges={"value": (-1e9, 1e9)},
        )

    corrupted_ids = dataset.get_corrupted_ids()
    fail_corrupted_ids = {
        c.datum_id for c in dataset.injected_corruptions
        if c.expected_severity == "FAIL"
    }
    warn_corrupted_ids = {
        c.datum_id for c in dataset.injected_corruptions
        if c.expected_severity == "WARN"
    } - fail_corrupted_ids   # datums that have WARN only (no FAIL)

    # Run pipeline
    with DataTrustPipeline(
        domain=domain,
        db_path=":memory:",
        operator="benchmark",
        config={"calibration_version": "v1"},
        transformations=TRANSFORMS,
    ) as pipeline:

        result = pipeline.run(dataset.data)

        # --- Detection metrics ---
        untrusted_ids = {v.datum_id for v in result.verdicts if not v.is_trusted}
        trusted_ids   = {v.datum_id for v in result.verdicts if v.is_trusted}

        # True positives: FAIL-corrupted datums caught as UNTRUSTED
        true_positives  = fail_corrupted_ids & untrusted_ids
        # False negatives: FAIL-corrupted datums missed (still TRUSTED)
        false_negatives = fail_corrupted_ids - untrusted_ids
        # False positives: clean datums flagged UNTRUSTED
        clean_ids        = set(d.get("event_id") or d.get("id") for d in dataset.data) - corrupted_ids
        false_positives  = clean_ids & untrusted_ids

        detection_rate = len(true_positives) / len(fail_corrupted_ids) if fail_corrupted_ids else 1.0
        fp_rate        = len(false_positives) / len(clean_ids) if clean_ids else 0.0

        # --- Warning metrics ---
        total_warnings  = sum(len(v.warnings) for v in result.verdicts)
        warn_on_trusted = sum(len(v.warnings) for v in result.verdicts if v.is_trusted)

        # --- Replay fidelity ---
        fidelity_report = pipeline.replay(result.spec, dataset.data)

        # --- Evidence store queries ---
        evid = pipeline.evidence_store
        inv_rates = evid.invariant_failure_rates(result.run_id)

        print(f"    n={scenario['n']} | trust rate={result.summary['trust_rate']:.1%} "
              f"| detection={detection_rate:.1%} | fp={fp_rate:.1%} "
              f"| fidelity={fidelity_report.fidelity_class.value}")

        return {
            "label": label,
            "domain": scenario["domain"],
            "n": scenario["n"],
            "seed": scenario["seed"],
            "injected_fail_corruptions": len(fail_corrupted_ids),
            "injected_warn_corruptions": len(warn_corrupted_ids),
            "total_datums": result.summary["total"],
            "trusted": result.summary["trusted"],
            "untrusted": result.summary["untrusted"],
            "trust_rate": result.summary["trust_rate"],
            "true_positives": len(true_positives),
            "false_negatives": len(false_negatives),
            "false_positives": len(false_positives),
            "detection_rate": detection_rate,
            "false_positive_rate": fp_rate,
            "total_warnings": total_warnings,
            "warnings_on_trusted_datums": warn_on_trusted,
            "replay_fidelity_class": fidelity_report.fidelity_class.value,
            "replay_fidelity_rate": fidelity_report.fidelity_rate,
            "invariant_failure_rates": inv_rates,
            "failure_breakdown": result.summary.get("failure_breakdown", {}),
        }


# ---------------------------------------------------------------------------
# Benchmark report
# ---------------------------------------------------------------------------

def print_benchmark_report(results: list[dict]) -> None:
    print("\n" + "=" * 70)
    print("BENCHMARK REPORT")
    print("=" * 70)
    print(f"{'Scenario':<42} {'N':>5} {'Trust%':>7} {'Detect%':>8} {'FP%':>5} {'Replay':>12}")
    print("-" * 70)
    for r in results:
        label = r["label"][:41]
        print(
            f"{label:<42} {r['n']:>5} "
            f"{r['trust_rate']:>7.1%} "
            f"{r['detection_rate']:>8.1%} "
            f"{r['false_positive_rate']:>5.1%} "
            f"{r['replay_fidelity_class']:>12}"
        )
    print("=" * 70)

    # Aggregate across detector scenarios
    det = [r for r in results if r["domain"] == "detector" and r["injected_fail_corruptions"] > 0]
    tab = [r for r in results if r["domain"] == "tabular" and r["injected_fail_corruptions"] > 0]

    def avg(lst, key):
        vals = [x[key] for x in lst if x[key] is not None]
        return sum(vals) / len(vals) if vals else 0.0

    print(f"\nAggregated detection rates (FAIL-level corruptions only):")
    if det:
        print(f"  DetectorEvent:  {avg(det, 'detection_rate'):.1%} avg detection, "
              f"{avg(det, 'false_positive_rate'):.2%} avg false positive rate")
    if tab:
        print(f"  GenericTabular: {avg(tab, 'detection_rate'):.1%} avg detection, "
              f"{avg(tab, 'false_positive_rate'):.2%} avg false positive rate")

    # Replay summary
    all_fidelity = [r["replay_fidelity_class"] for r in results]
    identical_count = all_fidelity.count("IDENTICAL")
    print(f"\nReplay fidelity: {identical_count}/{len(results)} runs IDENTICAL "
          f"({identical_count/len(results):.0%} deterministic)")

    # Invariant effectiveness (across all runs)
    all_inv_rates: dict[str, dict] = {}
    for r in results:
        for entry in r["invariant_failure_rates"]:
            name = entry["invariant_name"]
            if name not in all_inv_rates:
                all_inv_rates[name] = {"total_checks": 0, "failures": 0}
            all_inv_rates[name]["total_checks"] += entry["total_checks"]
            all_inv_rates[name]["failures"] += entry["failures"]

    print(f"\nInvariant effectiveness (all runs combined):")
    ranked = sorted(all_inv_rates.items(), key=lambda x: -x[1]["failures"])
    for name, stats in ranked[:10]:
        rate = stats["failures"] / stats["total_checks"] if stats["total_checks"] > 0 else 0.0
        print(f"  {name:<45} {stats['failures']:>4} failures / "
              f"{stats['total_checks']:>5} checks ({rate:.1%})")

    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Data Trust System — Full Benchmark")
    print(f"Started: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 70)
    print(f"Running {len(SCENARIOS)} scenarios...")

    all_results = []
    for scenario in SCENARIOS:
        result = run_scenario(scenario)
        all_results.append(result)

    print_benchmark_report(all_results)

    # Save results for the technical report
    output_path = Path("data/benchmark_results.json")
    output_path.parent.mkdir(exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "scenarios": all_results,
            },
            f,
            indent=2,
            default=str,
        )
    print(f"\nResults saved to {output_path}")
