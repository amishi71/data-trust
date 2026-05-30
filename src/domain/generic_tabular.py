"""
GenericTabularDomain — built-in domain for generic tabular scientific data.

Covers the four invariant categories defined in TRUST_CONTRACT.md:
  - Structural  : schema, types, required fields, nullability
  - Range       : physical bounds per field
  - Relational  : cross-field constraints
  - Statistical : distribution drift from calibration baseline

This is the default domain when no specific domain is configured.
"""

from __future__ import annotations

from .base import CalibrationBaseline, DataDomain, FieldMapping


class GenericTabularDomain(DataDomain):
    """
    Domain for generic scientific tabular data.

    Config parameters (all optional, with sensible defaults):
        required_fields : list of field names that must be present and non-null
        field_ranges    : dict of field_name → (min, max) physical bounds
        relational_checks: list of (field_a, op, field_b) triples, e.g.
                           [("end_time", ">", "start_time")]
    """

    def __init__(
        self,
        required_fields: list[str] | None = None,
        field_ranges: dict[str, tuple[float, float]] | None = None,
        relational_checks: list[tuple[str, str, str]] | None = None,
        statistical_z_threshold: float = 3.0,
    ):
        self._required_fields = required_fields or ["id", "timestamp", "value"]
        self._field_ranges = field_ranges or {
            "value": (-1e9, 1e9),
        }
        self._relational_checks = relational_checks or []
        self._z_threshold = statistical_z_threshold

    @property
    def name(self) -> str:
        return "GenericTabular"

    @property
    def version(self) -> str:
        return "1.0.0"

    def get_invariants(self) -> list:
        # Import here to avoid circular dependency with invariants module
        from src.invariants.core import (
            NullabilityInvariant,
            RangeInvariant,
            RelationalInvariant,
            SchemaInvariant,
            StatisticalDriftInvariant,
        )

        invariants = []

        # --- Structural ---
        invariants.append(
            SchemaInvariant(
                name="required_fields_present",
                required_fields=self._required_fields,
                severity="FAIL",
            )
        )
        invariants.append(
            NullabilityInvariant(
                name="no_null_required_fields",
                non_nullable_fields=self._required_fields,
                severity="FAIL",
            )
        )

        # --- Range ---
        for field_name, (low, high) in self._field_ranges.items():
            invariants.append(
                RangeInvariant(
                    name=f"range_{field_name}",
                    field=field_name,
                    min_val=low,
                    max_val=high,
                    severity="FAIL",
                )
            )

        # --- Relational ---
        for field_a, op, field_b in self._relational_checks:
            invariants.append(
                RelationalInvariant(
                    name=f"relational_{field_a}_{op}_{field_b}",
                    field_a=field_a,
                    operator=op,
                    field_b=field_b,
                    severity="WARN",
                )
            )

        # --- Statistical ---
        invariants.append(
            StatisticalDriftInvariant(
                name="statistical_drift_value",
                field="value",
                z_score_threshold=self._z_threshold,
                severity="WARN",
            )
        )

        return invariants

    def get_calibration_baseline(self) -> CalibrationBaseline:
        return CalibrationBaseline(
            domain_name=self.name,
            version=self.version,
            field_stats={
                "value": {
                    "mean": 0.0,
                    "std": 1.0,
                    "min": -3.0,
                    "max": 3.0,
                }
            },
            notes="Default standard-normal baseline. Replace with real calibration data.",
        )

    def get_field_mapping(self) -> FieldMapping:
        return FieldMapping(
            domain_name=self.name,
            mappings={
                "event_id": "id",
                "timestamp": "timestamp",
                "energy": "value",
            },
        )
