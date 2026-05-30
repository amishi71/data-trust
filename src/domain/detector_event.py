"""
DetectorEventDomain — built-in domain for particle physics detector events.

Modeled on CERN-style detector data quality management (DQM) practices.
Hard invariants correspond to trigger logic: violations make an event
scientifically meaningless regardless of context.

Field schema for a detector event:
    event_id        : str   — globally unique event identifier
    run_id          : str   — which data-taking run produced this event
    timestamp       : float — Unix epoch timestamp, must be monotone within a run
    energy_gev      : float — reconstructed energy in GeV, must be > 0
    detector_channel: int   — which detector channel fired (0–N_CHANNELS)
    signal_adc      : float — raw ADC signal value
    quality_flag    : int   — 0=good, 1=warn, 2=bad (set by online DQM)
    calibration_run : str   — which calibration run's constants were applied
"""

from __future__ import annotations

from .base import CalibrationBaseline, DataDomain, FieldMapping


# Real experiments would pull these from a conditions database.
# For the prototype, they're hardcoded and clearly labeled as placeholder.
N_CHANNELS = 128
ENERGY_MIN_GEV = 0.0      # energy is strictly positive; 0 itself is an edge case
ENERGY_MAX_GEV = 14000.0  # ~LHC center-of-mass energy; events above this are noise
ADC_MIN = 0.0
ADC_MAX = 4095.0           # 12-bit ADC


class DetectorEventDomain(DataDomain):
    """
    Domain for particle physics detector events.

    Hard FAILs (event is scientifically meaningless):
      - energy_gev <= 0 (physically impossible)
      - detector_channel out of range
      - event_id is null or non-unique (tracked externally)
      - timestamp is null

    Soft WARNs (anomalous but potentially legitimate):
      - quality_flag == 2 (online DQM flagged bad, but analyst may override)
      - signal_adc near saturation (>95% of max)
      - energy_gev above 99th percentile of calibration baseline
    """

    def __init__(
        self,
        n_channels: int = N_CHANNELS,
        energy_min: float = ENERGY_MIN_GEV,
        energy_max: float = ENERGY_MAX_GEV,
        adc_min: float = ADC_MIN,
        adc_max: float = ADC_MAX,
        adc_saturation_threshold: float = 0.95,
    ):
        self._n_channels = n_channels
        self._energy_min = energy_min
        self._energy_max = energy_max
        self._adc_min = adc_min
        self._adc_max = adc_max
        self._adc_saturation = adc_saturation_threshold * adc_max

    @property
    def name(self) -> str:
        return "DetectorEvent"

    @property
    def version(self) -> str:
        return "1.0.0"

    def get_invariants(self) -> list:
        from src.invariants.core import (
            AbsoluteRangeInvariant,
            ConfiguredRangeInvariant,
            FiniteValueInvariant,
            NullabilityInvariant,
            RangeInvariant,
            SchemaInvariant,
            StatisticalDriftInvariant,
            ThresholdInvariant,
        )

        required = [
            "event_id", "run_id", "timestamp",
            "energy_gev", "detector_channel", "signal_adc",
            "quality_flag", "calibration_run",
        ]

        return [
            # --- Structural (FAIL) ---
            SchemaInvariant(
                name="detector_required_fields",
                required_fields=required,
                severity="FAIL",
            ),
            NullabilityInvariant(
                name="detector_no_null_critical_fields",
                non_nullable_fields=["event_id", "timestamp", "energy_gev", "detector_channel"],
                severity="FAIL",
            ),

            # --- Absolute range (FAIL, §2.2): physically impossible values, not overridable ---
            AbsoluteRangeInvariant(
                name="energy_gev_positive",
                field="energy_gev",
                min_val=self._energy_min,
                max_val=self._energy_max,
                notes="Energy must be > 0 GeV. Negative energy is physically impossible.",
            ),
            AbsoluteRangeInvariant(
                name="detector_channel_valid",
                field="detector_channel",
                min_val=0,
                max_val=self._n_channels - 1,
                notes=f"Channel must be in [0, {self._n_channels - 1}].",
            ),
            AbsoluteRangeInvariant(
                name="adc_in_range",
                field="signal_adc",
                min_val=self._adc_min,
                max_val=self._adc_max,
                notes="ADC value out of hardware range — sensor malfunction.",
            ),
            # §H4: NaN/Inf are never valid measurements
            FiniteValueInvariant(
                name="no_nonfinite_values",
                fields=["energy_gev", "signal_adc", "timestamp"],
            ),

            # --- Threshold checks (WARN): anomalous but not impossible ---
            ThresholdInvariant(
                name="adc_saturation_warning",
                field="signal_adc",
                threshold=self._adc_saturation,
                direction="above",
                severity="WARN",
                notes="ADC near saturation (>95% of max). Signal may be clipped.",
            ),
            ThresholdInvariant(
                name="quality_flag_bad",
                field="quality_flag",
                threshold=1.5,       # quality_flag == 2 triggers this
                direction="above",
                severity="WARN",
                notes="Online DQM flagged this event as bad. Analyst review required.",
            ),

            # --- Statistical (WARN) ---
            StatisticalDriftInvariant(
                name="energy_distribution_drift",
                field="energy_gev",
                z_score_threshold=4.0,  # tighter than generic; physics has narrower priors
                severity="WARN",
                notes="Energy value deviates >4σ from calibration baseline.",
            ),
        ]

    def get_absolute_bounds(self) -> dict:
        """§5: These bounds are physically mandated — not overridable."""
        return {
            "energy_gev": (self._energy_min, self._energy_max),
            "detector_channel": (0, self._n_channels - 1),
            "signal_adc": (self._adc_min, self._adc_max),
        }

    def get_configured_bounds(self) -> dict:
        """§5: Operationally declared bounds — overridable via domain config."""
        return {
            "signal_adc_saturation": (self._adc_min, self._adc_saturation),
        }

    def get_calibration_baseline(self) -> CalibrationBaseline:
        # Placeholder values. In production these come from a conditions DB
        # keyed by calibration_run.
        return CalibrationBaseline(
            domain_name=self.name,
            version=self.version,
            field_stats={
                "energy_gev": {
                    "mean": 125.0,      # Higgs-like signal peak, illustrative
                    "std": 2.5,
                    "min": 0.1,
                    "max": 200.0,
                },
                "signal_adc": {
                    "mean": 2048.0,     # mid-range for a 12-bit ADC
                    "std": 300.0,
                    "min": 100.0,
                    "max": 3900.0,
                },
            },
            notes=(
                "Placeholder calibration baseline. "
                "Replace with values from the conditions database for run period."
            ),
        )

    def get_field_mapping(self) -> FieldMapping:
        return FieldMapping(
            domain_name=self.name,
            mappings={
                "event_id": "event_id",
                "timestamp": "timestamp",
                "energy": "energy_gev",
                "status": "quality_flag",
            },
        )
