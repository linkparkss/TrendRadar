"""Deterministic adaptive search for a load-increase convergence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


NUMERICALLY_CONVERGED = frozenset(
    {"CONVERGED_FEASIBLE", "CONVERGED_INFEASIBLE"}
)
NUMERICALLY_FAILED = frozenset({"NUMERICAL_FAILURE"})
INDETERMINATE = frozenset({"INVALID_OR_INDETERMINATE"})
SUPPORTED_LABELS = NUMERICALLY_CONVERGED | NUMERICALLY_FAILED | INDETERMINATE


class NonMonotonicEvidenceError(ValueError):
    """Raised when a stronger load is converged after a weaker one failed."""


@dataclass(frozen=True)
class AdaptiveLoadConfig:
    enabled: bool = True
    initial_increases: tuple[float, ...] = (0.40, 0.60)
    expansion_increases: tuple[float, ...] = (0.80, 1.00, 1.20, 1.50, 2.00)
    minimum_bracket_width: float = 0.025

    def __post_init__(self) -> None:
        object.__setattr__(self, "initial_increases", tuple(self.initial_increases))
        object.__setattr__(self, "expansion_increases", tuple(self.expansion_increases))
        levels = self.initial_increases + self.expansion_increases
        if not levels or any(level <= 0.0 for level in levels):
            raise ValueError("adaptive load increases must be positive")
        if tuple(sorted(set(levels))) != levels:
            raise ValueError(
                "adaptive load increases must be unique and strictly increasing"
            )
        if self.minimum_bracket_width <= 0.0:
            raise ValueError("minimum_bracket_width must be positive")


@dataclass(frozen=True)
class LoadProbe:
    increase: float
    label: str

    def __post_init__(self) -> None:
        if self.increase <= 0.0:
            raise ValueError("probe increase must be positive")
        if self.label not in SUPPORTED_LABELS:
            raise ValueError(f"unsupported adaptive-search label: {self.label}")


@dataclass(frozen=True)
class AdaptiveLoadDecision:
    phase: str
    next_increase: float | None
    converged_increase: float | None
    failed_increase: float | None
    bracket_width: float | None
    invalid_increases: tuple[float, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "next_increase": self.next_increase,
            "converged_increase": self.converged_increase,
            "failed_increase": self.failed_increase,
            "bracket_width": self.bracket_width,
            "invalid_increases": list(self.invalid_increases),
        }


def decide_next_load_probe(
    observations: Iterable[LoadProbe],
    *,
    baseline_control_passed: bool,
    config: AdaptiveLoadConfig = AdaptiveLoadConfig(),
) -> AdaptiveLoadDecision:
    """Choose the next load increase using steps followed by bisection.

    The unmodified baseline is used as the +0% converged endpoint only when its
    control run passed. Indeterminate observations are retried and never treated
    as numerical failures.
    """

    probes = tuple(observations)
    if not config.enabled:
        return AdaptiveLoadDecision("disabled", None, None, None, None, ())
    if not baseline_control_passed:
        return AdaptiveLoadDecision("blocked_baseline", None, None, None, None, ())

    labels_by_level: dict[float, set[str]] = {}
    for probe in probes:
        level = round(float(probe.increase), 10)
        labels_by_level.setdefault(level, set()).add(probe.label)
    decisive_by_level: dict[float, str] = {}
    for level, labels in labels_by_level.items():
        decisive = labels - INDETERMINATE
        if len(decisive) > 1:
            raise ValueError(f"conflicting verified labels at load increase {level}")
        if decisive:
            decisive_by_level[level] = next(iter(decisive))

    converged = [0.0] + sorted(
        level
        for level, label in decisive_by_level.items()
        if label in NUMERICALLY_CONVERGED
    )
    failed = sorted(
        level
        for level, label in decisive_by_level.items()
        if label in NUMERICALLY_FAILED
    )
    invalid = tuple(
        sorted(
            level
            for level, labels in labels_by_level.items()
            if labels & INDETERMINATE and level not in decisive_by_level
        )
    )
    if failed and any(level > min(failed) for level in converged):
        raise NonMonotonicEvidenceError(
            "a stronger load increase converged after a weaker increase failed"
        )

    if failed:
        high = min(failed)
        low = max(level for level in converged if level < high)
        width = round(high - low, 10)
        if width <= config.minimum_bracket_width:
            return AdaptiveLoadDecision(
                "boundary_reached", None, low, high, width, invalid
            )
        midpoint = round((low + high) / 2.0, 10)
        return AdaptiveLoadDecision("bisection", midpoint, low, high, width, invalid)

    planned = config.initial_increases + config.expansion_increases
    for level in planned:
        normalized = round(level, 10)
        if normalized not in decisive_by_level:
            phase = "retry_indeterminate" if normalized in invalid else "stepping"
            return AdaptiveLoadDecision(
                phase, normalized, max(converged), None, None, invalid
            )
    return AdaptiveLoadDecision(
        "expansion_exhausted", None, max(converged), None, None, invalid
    )


__all__ = [
    "AdaptiveLoadConfig",
    "AdaptiveLoadDecision",
    "LoadProbe",
    "NonMonotonicEvidenceError",
    "decide_next_load_probe",
]
