"""
Synthetic data generator for both built-in domains.

Generates datasets with configurable, measurable corruption injection.
This is the benchmark ground truth: you know exactly what was injected,
so you can measure exactly what the invariant engine caught.

Usage
-----
    from data.synthetic.generator import SyntheticDataset, CorruptionConfig

    # Generate 500 detector events with 10% energy corruption, 5% null injection
    config = CorruptionConfig(
        negative_energy_rate=0.10,
        null_injection_rate=0.05,
        out_of_range_channel_rate=0.03,
    )
    dataset = SyntheticDataset.detector_events(n=500, corruption=config, seed=42)
    dataset.save("data/synthetic/detector_500.json")
    print(dataset.corruption_report())
"""

from __future__ import annotations

import json
import math
import random
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Corruption configuration
# ---------------------------------------------------------------------------

@dataclass
class CorruptionConfig:
    """
    Controls how much and what kind of corruption is injected into a dataset.
    All rates are fractions in [0, 1]. Default is no corruption.

    Corruption types map directly to invariant types so results are measurable.
    """

    # --- FAIL-level corruptions ---
    negative_energy_rate: float = 0.0        # RangeInvariant: energy_gev <= 0
    out_of_range_channel_rate: float = 0.0   # RangeInvariant: detector_channel OOB
    null_injection_rate: float = 0.0         # NullabilityInvariant: null critical field
    missing_field_rate: float = 0.0          # SchemaInvariant: drop a required field
    adc_overflow_rate: float = 0.0           # RangeInvariant: signal_adc > 4095

    # --- WARN-level corruptions ---
    adc_saturation_rate: float = 0.0         # ThresholdInvariant: signal_adc > 95% max
    bad_quality_flag_rate: float = 0.0       # ThresholdInvariant: quality_flag == 2
    statistical_outlier_rate: float = 0.0    # StatisticalDriftInvariant: z > 4.0
    non_monotone_timestamp_rate: float = 0.0 # MonotonicityInvariant

    def total_corruption_rate(self) -> float:
        return (
            self.negative_energy_rate
            + self.out_of_range_channel_rate
            + self.null_injection_rate
            + self.missing_field_rate
            + self.adc_overflow_rate
        )

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Datum corruption tracker — lets you verify the engine caught everything
# ---------------------------------------------------------------------------

@dataclass
class InjectedCorruption:
    datum_id: str
    corruption_type: str     # e.g. "negative_energy", "null_injection"
    field_affected: str
    original_value: object
    corrupted_value: object
    expected_severity: str   # "FAIL" or "WARN"


# ---------------------------------------------------------------------------
# SyntheticDataset
# ---------------------------------------------------------------------------

@dataclass
class SyntheticDataset:
    domain: str
    data: list[dict]
    injected_corruptions: list[InjectedCorruption]
    corruption_config: CorruptionConfig
    seed: int
    n: int

    @classmethod
    def detector_events(
        cls,
        n: int = 100,
        corruption: CorruptionConfig | None = None,
        seed: int = 42,
    ) -> "SyntheticDataset":
        """Generate n synthetic detector events with optional corruption."""
        rng = random.Random(seed)
        corruption = corruption or CorruptionConfig()
        data: list[dict] = []
        injected: list[InjectedCorruption] = []

        base_timestamp = 1_700_000_000.0
        run_id = f"RUN_{seed:04d}"
        calibration_run = f"CAL_{seed:04d}"

        for i in range(n):
            event_id = f"EVT_{i:06d}_{uuid.uuid4().hex[:8]}"
            timestamp = base_timestamp + i * 0.025   # 25ms inter-event gap
            energy_gev = abs(rng.gauss(125.0, 2.5))
            channel = rng.randint(0, 127)
            signal_adc = rng.gauss(2048.0, 300.0)
            signal_adc = max(0.0, min(4095.0, signal_adc))
            quality_flag = 0

            datum = {
                "event_id": event_id,
                "run_id": run_id,
                "timestamp": timestamp,
                "energy_gev": energy_gev,
                "detector_channel": channel,
                "signal_adc": signal_adc,
                "quality_flag": quality_flag,
                "calibration_run": calibration_run,
                "_id": event_id,   # convenience alias
            }

            # --- Inject corruptions ---

            if rng.random() < corruption.negative_energy_rate:
                original = datum["energy_gev"]
                datum["energy_gev"] = -abs(rng.gauss(10.0, 5.0))
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="negative_energy",
                    field_affected="energy_gev",
                    original_value=original,
                    corrupted_value=datum["energy_gev"],
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.out_of_range_channel_rate:
                original = datum["detector_channel"]
                datum["detector_channel"] = rng.choice([-1, 128, 999])
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="out_of_range_channel",
                    field_affected="detector_channel",
                    original_value=original,
                    corrupted_value=datum["detector_channel"],
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.null_injection_rate:
                target_field = rng.choice(["energy_gev", "timestamp", "detector_channel"])
                original = datum[target_field]
                datum[target_field] = None
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="null_injection",
                    field_affected=target_field,
                    original_value=original,
                    corrupted_value=None,
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.missing_field_rate:
                target_field = rng.choice(["calibration_run", "run_id", "quality_flag"])
                original = datum.pop(target_field, None)
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="missing_field",
                    field_affected=target_field,
                    original_value=original,
                    corrupted_value="<MISSING>",
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.adc_overflow_rate:
                original = datum["signal_adc"]
                datum["signal_adc"] = rng.uniform(4096.0, 5000.0)
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="adc_overflow",
                    field_affected="signal_adc",
                    original_value=original,
                    corrupted_value=datum["signal_adc"],
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.adc_saturation_rate:
                original = datum["signal_adc"]
                datum["signal_adc"] = rng.uniform(3896.0, 4095.0)  # >95% of max
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="adc_saturation",
                    field_affected="signal_adc",
                    original_value=original,
                    corrupted_value=datum["signal_adc"],
                    expected_severity="WARN",
                ))

            if rng.random() < corruption.bad_quality_flag_rate:
                datum["quality_flag"] = 2
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="bad_quality_flag",
                    field_affected="quality_flag",
                    original_value=0,
                    corrupted_value=2,
                    expected_severity="WARN",
                ))

            if rng.random() < corruption.statistical_outlier_rate:
                original = datum["energy_gev"]
                datum["energy_gev"] = rng.choice([
                    125.0 + 4.5 * 2.5,    # +4.5σ
                    125.0 - 4.5 * 2.5,    # -4.5σ
                ])
                injected.append(InjectedCorruption(
                    datum_id=event_id,
                    corruption_type="statistical_outlier",
                    field_affected="energy_gev",
                    original_value=original,
                    corrupted_value=datum["energy_gev"],
                    expected_severity="WARN",
                ))

            data.append(datum)

        return cls(
            domain="DetectorEvent",
            data=data,
            injected_corruptions=injected,
            corruption_config=corruption,
            seed=seed,
            n=n,
        )

    @classmethod
    def generic_tabular(
        cls,
        n: int = 100,
        corruption: CorruptionConfig | None = None,
        seed: int = 42,
    ) -> "SyntheticDataset":
        """Generate n synthetic generic tabular records with optional corruption."""
        rng = random.Random(seed)
        corruption = corruption or CorruptionConfig()
        data: list[dict] = []
        injected: list[InjectedCorruption] = []

        base_ts = 1_700_000_000.0

        for i in range(n):
            record_id = f"REC_{i:06d}_{uuid.uuid4().hex[:8]}"
            datum = {
                "id": record_id,
                "timestamp": base_ts + i * 1.0,
                "value": rng.gauss(0.0, 1.0),
                "source": f"sensor_{rng.randint(1, 10):02d}",
            }

            if rng.random() < corruption.negative_energy_rate:
                original = datum["value"]
                datum["value"] = rng.uniform(-1e10, -1.0)
                injected.append(InjectedCorruption(
                    datum_id=record_id,
                    corruption_type="out_of_range_value",
                    field_affected="value",
                    original_value=original,
                    corrupted_value=datum["value"],
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.null_injection_rate:
                target = rng.choice(["id", "timestamp", "value"])
                original = datum[target]
                datum[target] = None
                injected.append(InjectedCorruption(
                    datum_id=record_id,
                    corruption_type="null_injection",
                    field_affected=target,
                    original_value=original,
                    corrupted_value=None,
                    expected_severity="FAIL",
                ))

            if rng.random() < corruption.missing_field_rate:
                target = "source"
                datum.pop(target, None)
                injected.append(InjectedCorruption(
                    datum_id=record_id,
                    corruption_type="missing_field",
                    field_affected=target,
                    original_value="present",
                    corrupted_value="<MISSING>",
                    expected_severity="FAIL",
                ))

            data.append(datum)

        return cls(
            domain="GenericTabular",
            data=data,
            injected_corruptions=injected,
            corruption_config=corruption,
            seed=seed,
            n=n,
        )

    # ------------------------------------------------------------------
    # Analysis
    # ------------------------------------------------------------------

    def corruption_report(self) -> str:
        if not self.injected_corruptions:
            return f"Dataset: {self.n} records, domain={self.domain}, no corruptions injected."

        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for c in self.injected_corruptions:
            by_type[c.corruption_type] = by_type.get(c.corruption_type, 0) + 1
            by_severity[c.expected_severity] = by_severity.get(c.expected_severity, 0) + 1

        lines = [
            f"Corruption report — domain={self.domain}, n={self.n}, seed={self.seed}",
            f"  Total corruptions injected: {len(self.injected_corruptions)}",
            f"  Unique data points affected: {len({c.datum_id for c in self.injected_corruptions})}",
            "  By type:",
        ]
        for k, v in sorted(by_type.items(), key=lambda x: -x[1]):
            lines.append(f"    {k}: {v}")
        lines.append("  By expected severity:")
        for k, v in sorted(by_severity.items()):
            lines.append(f"    {k}: {v}")
        return "\n".join(lines)

    def get_corrupted_ids(self) -> set[str]:
        return {c.datum_id for c in self.injected_corruptions}

    def get_corruptions_for(self, datum_id: str) -> list[InjectedCorruption]:
        return [c for c in self.injected_corruptions if c.datum_id == datum_id]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "domain": self.domain,
            "n": self.n,
            "seed": self.seed,
            "corruption_config": self.corruption_config.to_dict(),
            "corruption_report": self.corruption_report(),
            "injected_corruptions": [
                {
                    "datum_id": c.datum_id,
                    "corruption_type": c.corruption_type,
                    "field_affected": c.field_affected,
                    "original_value": str(c.original_value),
                    "corrupted_value": str(c.corrupted_value),
                    "expected_severity": c.expected_severity,
                }
                for c in self.injected_corruptions
            ],
            "data": self.data,
        }
        with open(path, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print(f"Saved {self.n} records to {path}")
