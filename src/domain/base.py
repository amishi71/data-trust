"""
DataDomain — abstract interface for pluggable data domains.

Every domain must implement three things:
  1. get_invariants()          → the set of invariant checks for this domain
  2. get_calibration_baseline() → statistical reference distribution
  3. get_field_mapping()       → how domain-specific field names map to canonical names

The trust contract itself stays domain-agnostic. The domain plugin is what
supplies the specific values. A user can define a custom domain by subclassing
DataDomain, or by pointing to a YAML config (see YAMLDomain).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Calibration baseline — the statistical reference a domain ships with
# ---------------------------------------------------------------------------

@dataclass
class CalibrationBaseline:
    """
    Reference statistics for a domain's fields.
    Used by statistical drift invariants.

    field_stats maps field name → {"mean": float, "std": float, "min": float, "max": float}
    """
    domain_name: str
    version: str
    field_stats: dict[str, dict[str, float]]
    notes: str = ""

    def get_field(self, field_name: str) -> Optional[dict[str, float]]:
        return self.field_stats.get(field_name)


# ---------------------------------------------------------------------------
# Field mapping — canonical name → domain-specific name
# ---------------------------------------------------------------------------

@dataclass
class FieldMapping:
    """
    Maps canonical field names to domain-specific names.

    Canonical fields the trust contract understands:
      - "event_id"    : unique identifier per event/row
      - "timestamp"   : time of the event
      - "energy"      : primary signal field (physics) or value field (generic)
      - "status"      : optional quality flag
    """
    domain_name: str
    mappings: dict[str, str]   # canonical_name → domain_field_name

    def resolve(self, canonical_name: str) -> Optional[str]:
        """Return the domain-specific field name for a canonical name."""
        return self.mappings.get(canonical_name)

    def reverse(self, domain_field: str) -> Optional[str]:
        """Return the canonical name for a domain-specific field name."""
        return {v: k for k, v in self.mappings.items()}.get(domain_field)


# ---------------------------------------------------------------------------
# Abstract base class
# ---------------------------------------------------------------------------

class DataDomain(ABC):
    """
    Abstract base class for all data domains.

    To define a custom domain:
        class MyDomain(DataDomain):
            def get_invariants(self): ...
            def get_calibration_baseline(self): ...
            def get_field_mapping(self): ...
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable domain name. Used in reports and evidence records."""
        ...

    @property
    @abstractmethod
    def version(self) -> str:
        """Domain spec version. Bump when invariants change."""
        ...

    @abstractmethod
    def get_invariants(self) -> list:
        """
        Return the ordered list of Invariant objects for this domain.
        The invariant engine will run them in this order.
        """
        ...

    @abstractmethod
    def get_calibration_baseline(self) -> CalibrationBaseline:
        """
        Return the statistical reference distribution for this domain.
        Used by StatisticalDriftInvariant.
        """
        ...

    @abstractmethod
    def get_field_mapping(self) -> FieldMapping:
        """
        Return the field mapping for this domain.
        The trust contract uses canonical names; the mapping bridges them.
        """
        ...

    def get_absolute_bounds(self) -> dict[str, tuple[float, float]]:
        """
        §5: Physically mandated limits — not overridable by configuration.
        Returns dict of field_name → (min, max).
        Default: empty dict (domain has no absolute bounds declared).
        """
        return {}

    def get_configured_bounds(self) -> dict[str, tuple[float, float]]:
        """
        §5: Operationally declared limits — overridable in domain config.
        Returns dict of field_name → (min, max).
        Default: empty dict.
        """
        return {}

    def validate_interface(self) -> list[str]:
        """
        Verify this domain implements the interface correctly.
        Returns a list of error strings (empty = compliant).
        """
        errors = []
        try:
            invariants = self.get_invariants()
            if not isinstance(invariants, list):
                errors.append("get_invariants() must return a list")
        except Exception as e:
            errors.append(f"get_invariants() raised: {e}")

        try:
            baseline = self.get_calibration_baseline()
            if not isinstance(baseline, CalibrationBaseline):
                errors.append("get_calibration_baseline() must return CalibrationBaseline")
        except Exception as e:
            errors.append(f"get_calibration_baseline() raised: {e}")

        try:
            mapping = self.get_field_mapping()
            if not isinstance(mapping, FieldMapping):
                errors.append("get_field_mapping() must return FieldMapping")
        except Exception as e:
            errors.append(f"get_field_mapping() raised: {e}")

        return errors
