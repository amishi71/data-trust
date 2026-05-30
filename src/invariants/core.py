"""
Invariant enforcement engine.

Defines the base Invariant class and all concrete invariant types.
The TrustContract (in contract.py) runs these against each datum.

Invariant severity:
  FAIL — violation makes the datum untrusted; it is quarantined.
  WARN — violation is recorded in provenance permanently, but datum passes.

Adding a new invariant type: subclass Invariant, implement check().
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# InvariantResult — what a single check returns
# ---------------------------------------------------------------------------

@dataclass
class InvariantResult:
    invariant_name: str
    severity: Severity
    passed: bool
    field: Optional[str]
    observed_value: Any
    message: str
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "invariant_name": self.invariant_name,
            "severity": self.severity.value,
            "passed": self.passed,
            "field": self.field,
            "observed_value": str(self.observed_value),
            "message": self.message,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Invariant(ABC):
    """
    Base class for all invariants.

    Subclasses implement check(), which receives the datum dict and
    an optional context dict (calibration baseline, run metadata, etc.)
    and returns an InvariantResult.
    """

    def __init__(self, name: str, severity: str, notes: str = ""):
        self.name = name
        self.severity = Severity(severity.upper())
        self.notes = notes

    @abstractmethod
    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        ...

    def _pass(self, field: str | None = None, observed=None, message: str = "OK") -> InvariantResult:
        return InvariantResult(
            invariant_name=self.name,
            severity=Severity.PASS,
            passed=True,
            field=field,
            observed_value=observed,
            message=message,
            notes=self.notes,
        )

    def _fail(self, field: str | None, observed, message: str) -> InvariantResult:
        return InvariantResult(
            invariant_name=self.name,
            severity=self.severity,
            passed=False,
            field=field,
            observed_value=observed,
            message=message,
            notes=self.notes,
        )


# ---------------------------------------------------------------------------
# Concrete invariant types
# ---------------------------------------------------------------------------

class SchemaInvariant(Invariant):
    """All required fields must be present in the datum."""

    def __init__(self, name: str, required_fields: list[str], severity: str, notes: str = ""):
        super().__init__(name, severity, notes)
        self.required_fields = required_fields

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        missing = [f for f in self.required_fields if f not in datum]
        if missing:
            return self._fail(
                field=None,
                observed=missing,
                message=f"Missing required fields: {missing}",
            )
        return self._pass(message=f"All {len(self.required_fields)} required fields present.")


class NullabilityInvariant(Invariant):
    """Specified fields must not be None or NaN."""

    def __init__(self, name: str, non_nullable_fields: list[str], severity: str, notes: str = ""):
        super().__init__(name, severity, notes)
        self.non_nullable_fields = non_nullable_fields

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        null_fields = []
        for f in self.non_nullable_fields:
            val = datum.get(f)
            if val is None:
                null_fields.append(f)
            elif isinstance(val, float) and math.isnan(val):
                null_fields.append(f)

        if null_fields:
            return self._fail(
                field=null_fields[0] if len(null_fields) == 1 else None,
                observed=null_fields,
                message=f"Null/NaN values in non-nullable fields: {null_fields}",
            )
        return self._pass(message="No null values in required fields.")


class RangeInvariant(Invariant):
    """A numeric field must fall within [min_val, max_val]."""

    def __init__(
        self,
        name: str,
        field: str,
        min_val: float,
        max_val: float,
        severity: str,
        notes: str = "",
    ):
        super().__init__(name, severity, notes)
        self.field = field
        self.min_val = min_val
        self.max_val = max_val

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val = datum.get(self.field)
        if val is None:
            return self._fail(self.field, val, f"Field '{self.field}' is absent or None.")

        try:
            val = float(val)
        except (TypeError, ValueError):
            return self._fail(self.field, val, f"Field '{self.field}' is not numeric: {val!r}")

        if math.isnan(val):
            return self._fail(self.field, val, f"Field '{self.field}' is NaN.")

        if not (self.min_val <= val <= self.max_val):
            return self._fail(
                self.field,
                val,
                f"Field '{self.field}' = {val} is outside [{self.min_val}, {self.max_val}].",
            )

        return self._pass(self.field, val, f"Field '{self.field}' = {val} in range.")


class RelationalInvariant(Invariant):
    """
    Cross-field constraint: field_a <operator> field_b must be True.
    Operators: '>', '>=', '<', '<=', '=='
    """

    OPERATORS = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        "==": lambda a, b: a == b,
    }

    def __init__(
        self,
        name: str,
        field_a: str,
        operator: str,
        field_b: str,
        severity: str,
        notes: str = "",
    ):
        super().__init__(name, severity, notes)
        if operator not in self.OPERATORS:
            raise ValueError(f"Unsupported operator '{operator}'. Use one of {list(self.OPERATORS)}")
        self.field_a = field_a
        self.operator = operator
        self.field_b = field_b

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val_a = datum.get(self.field_a)
        val_b = datum.get(self.field_b)

        if val_a is None or val_b is None:
            return self._fail(
                None, (val_a, val_b),
                f"Relational check requires both '{self.field_a}' and '{self.field_b}'.",
            )

        op_fn = self.OPERATORS[self.operator]
        try:
            result = op_fn(val_a, val_b)
        except TypeError as e:
            return self._fail(None, (val_a, val_b), f"Type error in relational check: {e}")

        if not result:
            return self._fail(
                None,
                f"{val_a} {self.operator} {val_b}",
                f"Constraint violated: '{self.field_a}' ({val_a}) {self.operator} '{self.field_b}' ({val_b})",
            )
        return self._pass(
            message=f"Constraint satisfied: '{self.field_a}' ({val_a}) {self.operator} '{self.field_b}' ({val_b})"
        )


class ThresholdInvariant(Invariant):
    """
    A field must be above or below a single threshold.
    direction: 'above' means value > threshold triggers the result.
               'below' means value < threshold triggers the result.
    """

    def __init__(
        self,
        name: str,
        field: str,
        threshold: float,
        direction: str,   # "above" or "below"
        severity: str,
        notes: str = "",
    ):
        super().__init__(name, severity, notes)
        if direction not in ("above", "below"):
            raise ValueError("direction must be 'above' or 'below'")
        self.field = field
        self.threshold = threshold
        self.direction = direction

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val = datum.get(self.field)
        if val is None:
            return self._pass(self.field, val, f"Field '{self.field}' absent — threshold not applicable.")

        try:
            val = float(val)
        except (TypeError, ValueError):
            return self._fail(self.field, val, f"Field '{self.field}' is not numeric.")

        triggered = (val > self.threshold) if self.direction == "above" else (val < self.threshold)

        if triggered:
            return self._fail(
                self.field,
                val,
                f"Field '{self.field}' = {val} is {'above' if self.direction == 'above' else 'below'} threshold {self.threshold}.",
            )
        return self._pass(self.field, val)


class StatisticalDriftInvariant(Invariant):
    """
    A field's value must not deviate more than z_score_threshold standard
    deviations from the calibration baseline mean.

    Requires context["calibration_baseline"] to be a CalibrationBaseline object.
    If no baseline is available, the check emits a WARN and passes.
    """

    def __init__(
        self,
        name: str,
        field: str,
        z_score_threshold: float,
        severity: str,
        notes: str = "",
    ):
        super().__init__(name, severity, notes)
        self.field = field
        self.z_score_threshold = z_score_threshold

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val = datum.get(self.field)
        if val is None:
            return self._pass(self.field, val, f"Field '{self.field}' absent — drift check skipped.")

        try:
            val = float(val)
        except (TypeError, ValueError):
            return self._fail(self.field, val, f"Field '{self.field}' is not numeric.")

        baseline = (context or {}).get("calibration_baseline")
        if baseline is None:
            # No baseline = can't check. Record a warning but don't fail.
            return InvariantResult(
                invariant_name=self.name,
                severity=Severity.WARN,
                passed=True,    # passes, but warning is recorded
                field=self.field,
                observed_value=val,
                message="No calibration baseline available — statistical drift check skipped.",
                notes=self.notes,
            )

        field_stats = baseline.get_field(self.field)
        if field_stats is None:
            return InvariantResult(
                invariant_name=self.name,
                severity=Severity.WARN,
                passed=True,
                field=self.field,
                observed_value=val,
                message=f"Field '{self.field}' has no baseline stats — drift check skipped.",
                notes=self.notes,
            )

        mean = field_stats.get("mean", 0.0)
        std = field_stats.get("std", 1.0)

        if std == 0:
            return self._fail(self.field, val, "Calibration baseline std = 0 — baseline is degenerate.")

        z_score = abs(val - mean) / std

        if z_score > self.z_score_threshold:
            return self._fail(
                self.field,
                val,
                f"Field '{self.field}' z-score = {z_score:.2f} exceeds threshold {self.z_score_threshold}. "
                f"Value = {val}, baseline mean = {mean}, std = {std}.",
            )

        return self._pass(
            self.field, val,
            f"z-score = {z_score:.2f} within threshold {self.z_score_threshold}."
        )


class MonotonicityInvariant(Invariant):
    """
    Checks that a field's value is monotonically increasing across a sequence.
    Requires context["previous_value"] to be set.
    Used for timestamp ordering checks within a run.
    """

    def __init__(self, name: str, field: str, severity: str, strict: bool = True, notes: str = ""):
        super().__init__(name, severity, notes)
        self.field = field
        self.strict = strict   # strict=True means a == b is a violation

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val = datum.get(self.field)
        if val is None:
            return self._fail(self.field, val, f"Field '{self.field}' is None — cannot check monotonicity.")

        prev = (context or {}).get("previous_value")
        if prev is None:
            # First event in a sequence — no comparison possible.
            return self._pass(self.field, val, "First event in sequence — monotonicity baseline set.")

        try:
            val = float(val)
            prev = float(prev)
        except (TypeError, ValueError):
            return self._fail(self.field, val, f"Field '{self.field}' is not numeric.")

        if self.strict:
            ok = val > prev
        else:
            ok = val >= prev

        if not ok:
            return self._fail(
                self.field, val,
                f"Monotonicity violated: current {val} {'<=' if self.strict else '<'} previous {prev}.",
            )
        return self._pass(self.field, val)


class FiniteValueInvariant(Invariant):
    """
    §2.2 / Example H4: NaN and Inf are never valid measurements.
    This is an absolute hard invariant — not overridable by domain config.
    Checks all numeric fields in the datum, or a specific declared field.
    """

    def __init__(self, name: str, fields: list[str], severity: str = "FAIL", notes: str = ""):
        super().__init__(name, severity, notes)
        self.fields = fields

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        bad = []
        for f in self.fields:
            val = datum.get(f)
            if val is None:
                continue
            try:
                fval = float(val)
                if math.isnan(fval) or math.isinf(fval):
                    bad.append((f, val))
            except (TypeError, ValueError):
                pass  # non-numeric fields are not checked here

        if bad:
            field_names = [b[0] for b in bad]
            return self._fail(
                field=field_names[0] if len(bad) == 1 else None,
                observed=bad,
                message=(
                    f"Non-finite values (NaN or Inf) in fields: "
                    f"{[(f, str(v)) for f, v in bad]}. "
                    "NaN propagates silently — this is the class of silent failure "
                    "the system exists to prevent."
                ),
            )
        return self._pass(message=f"All {len(self.fields)} fields are finite.")


class AbsoluteRangeInvariant(Invariant):
    """
    §2.2: Physically mandated bounds — cannot be overridden by domain configuration.
    Use for constraints like energy > 0 that are not operational parameters
    but fundamental physical requirements.

    Distinct from ConfiguredRangeInvariant (which uses domain-declared, overridable bounds).
    The is_absolute=True flag marks this for the domain interface compliance check.
    """
    is_absolute = True   # sentinel: domain plugin interface checks this

    def __init__(
        self,
        name: str,
        field: str,
        min_val: float,
        max_val: float,
        notes: str = "",
    ):
        # Always FAIL — absolute bounds are never warnings
        super().__init__(name, "FAIL", notes)
        self.field = field
        self.min_val = min_val
        self.max_val = max_val

    def check(self, datum: dict, context: dict | None = None) -> InvariantResult:
        val = datum.get(self.field)
        if val is None:
            return self._fail(self.field, val, f"Field '{self.field}' is absent.")
        try:
            val = float(val)
        except (TypeError, ValueError):
            return self._fail(self.field, val, f"Field '{self.field}' is not numeric.")
        if math.isnan(val) or math.isinf(val):
            return self._fail(self.field, val, f"Field '{self.field}' is non-finite.")
        if not (self.min_val <= val <= self.max_val):
            return self._fail(
                self.field, val,
                f"ABSOLUTE BOUND VIOLATED: '{self.field}' = {val} outside "
                f"[{self.min_val}, {self.max_val}]. This bound is physically "
                f"mandated and cannot be overridden by domain configuration.",
            )
        return self._pass(self.field, val)


class ConfiguredRangeInvariant(RangeInvariant):
    """
    §2.2: Operationally declared bounds — overridable in domain config.
    Subclasses RangeInvariant; adds is_absolute=False sentinel and
    accepts severity as a parameter (may be FAIL or WARN depending on domain).
    """
    is_absolute = False
