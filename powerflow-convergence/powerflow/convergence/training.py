"""Offline calibration catalog for observed PSASP convergence cases.

This module intentionally does not run a solver. It turns labeled case
differences into an auditable profile that can later guide, or be evaluated
against, the search policy.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SUPPORTED_DIAGNOSTIC_FACTORS = {
    "generator_pg_derating",
    "lcc_dc_power_derating",
    "lcc_dc_power_uprating",
    "generator_outage",
    "ac_line_outage",
    "transformer_outage",
    "load_active_power_increase",
    "load_reactive_power_increase",
    "load_pq_increase",
    "generator_reactive_limit_tightening",
    "shunt_reactor_outage",
    "combined_line_outage_load_increase",
}


@dataclass(frozen=True)
class SampleChange:
    table: str
    record_id: int
    device_name: str
    field: str
    before: Any
    after: Any
    ratio: float | None = None


@dataclass(frozen=True)
class TrainingSample:
    sample_id: str
    case_no: int
    case_name: str
    factor: str
    expected_converged: bool
    expected_limit_warning: bool
    result_timestamp: str
    changes: tuple[SampleChange, ...]
    observed_result: dict[str, Any]
    artifacts: dict[str, Any]
    confirmed_recovery: dict[str, Any]
    rejected_recoveries: tuple[dict[str, Any], ...]
    reference_case: dict[str, Any]


@dataclass(frozen=True)
class TrainingCatalog:
    catalog_id: str
    description: str
    baseline: dict[str, Any]
    samples: tuple[TrainingSample, ...]
    excluded_samples: tuple[dict[str, Any], ...]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TrainingCatalog":
        baseline = dict(payload["baseline"])
        raw_samples = payload.get("samples", [])
        if not isinstance(raw_samples, list):
            raise ValueError("samples must be an array")
        samples = []
        for raw in raw_samples:
            changes = tuple(
                SampleChange(
                    table=str(item["table"]),
                    record_id=int(item["record_id"]),
                    device_name=str(item["device_name"]),
                    field=str(item["field"]),
                    before=item.get("before"),
                    after=item.get("after"),
                    ratio=(float(item["ratio"]) if item.get("ratio") is not None else None),
                )
                for item in raw.get("changes", [])
            )
            samples.append(
                TrainingSample(
                    sample_id=str(raw["sample_id"]),
                    case_no=int(raw["case_no"]),
                    case_name=str(raw["case_name"]),
                    factor=str(raw["factor"]),
                    expected_converged=bool(raw["expected_converged"]),
                    expected_limit_warning=bool(raw.get("expected_limit_warning", False)),
                    result_timestamp=str(raw.get("result_timestamp", "")),
                    changes=changes,
                    observed_result=dict(raw.get("observed_result", {})),
                    artifacts=dict(raw.get("artifacts", {})),
                    confirmed_recovery=dict(raw.get("confirmed_recovery", {})),
                    rejected_recoveries=tuple(
                        dict(item)
                        for item in raw.get("rejected_recoveries", [])
                    ),
                    reference_case=dict(
                        raw.get("reference_case", baseline)
                    ),
                )
            )
        catalog = cls(
            catalog_id=str(payload["catalog_id"]),
            description=str(payload.get("description", "")),
            baseline=baseline,
            samples=tuple(samples),
            excluded_samples=tuple(payload.get("excluded_samples", [])),
        )
        catalog.validate()
        return catalog

    @classmethod
    def from_json(cls, path: str | Path) -> "TrainingCatalog":
        with Path(path).open("r", encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def validate(self) -> None:
        if not self.catalog_id:
            raise ValueError("catalog_id must not be empty")
        if int(self.baseline.get("case_no", -1)) < 0:
            raise ValueError("baseline.case_no is required")
        if not self.baseline.get("case_name"):
            raise ValueError("baseline.case_name is required")
        if not self.baseline.get("expected_converged", False):
            raise ValueError("the calibration baseline must be converged")
        ids = [sample.sample_id for sample in self.samples]
        if len(ids) != len(set(ids)):
            raise ValueError("sample_id values must be unique")
        case_numbers = {int(self.baseline["case_no"])}
        for sample in self.samples:
            if sample.factor not in SUPPORTED_DIAGNOSTIC_FACTORS:
                raise ValueError(f"unsupported diagnostic factor: {sample.factor}")
            if int(sample.reference_case.get("case_no", -1)) < 0:
                raise ValueError(
                    f"sample {sample.sample_id} reference_case.case_no is required"
                )
            if not sample.reference_case.get("case_name"):
                raise ValueError(
                    f"sample {sample.sample_id} reference_case.case_name is required"
                )
            if not sample.reference_case.get("expected_converged", False):
                raise ValueError(
                    f"sample {sample.sample_id} reference case must be converged"
                )
            if sample.case_no in case_numbers:
                raise ValueError(
                    f"sample case_no {sample.case_no} collides with baseline or another sample"
                )
            case_numbers.add(sample.case_no)
            if sample.expected_converged:
                raise ValueError(
                    f"this catalog is for failed perturbations; {sample.sample_id} is marked converged"
                )
            if not sample.changes:
                raise ValueError(f"sample {sample.sample_id} has no recorded changes")
            change_keys = {
                f"{change.table}:{change.record_id}:{change.field}"
                for change in sample.changes
            }
            for change in sample.changes:
                if change.field not in {
                    "Pg",
                    "Qmax",
                    "Qmin",
                    "Pl",
                    "Ql",
                    "Valid",
                    "GivenDCPower_High",
                    "GivenDCPower_Low",
                }:
                    raise ValueError(
                        f"sample {sample.sample_id} contains unsupported field {change.field}"
                    )
            confirmed_keys = list(
                sample.confirmed_recovery.get("action_keys", [])
            )
            if sample.confirmed_recovery:
                if not confirmed_keys or len(confirmed_keys) != len(set(confirmed_keys)):
                    raise ValueError(
                        f"sample {sample.sample_id} confirmed recovery keys are empty or duplicated"
                    )
                unknown = sorted(set(confirmed_keys) - change_keys)
                if unknown:
                    raise ValueError(
                        f"sample {sample.sample_id} confirmed recovery has unknown keys: {unknown}"
                    )
                if sample.confirmed_recovery.get("verification_source") != (
                    "psasp_gui_ordinary_load_flow"
                ):
                    raise ValueError(
                        f"sample {sample.sample_id} confirmed recovery lacks ordinary-flow evidence"
                    )
            for rejection in sample.rejected_recoveries:
                rejected_keys = list(rejection.get("action_keys", []))
                if not rejected_keys or len(rejected_keys) != len(set(rejected_keys)):
                    raise ValueError(
                        f"sample {sample.sample_id} rejected recovery keys are empty or duplicated"
                    )
                unknown = sorted(set(rejected_keys) - change_keys)
                if unknown:
                    raise ValueError(
                        f"sample {sample.sample_id} rejected recovery has unknown keys: {unknown}"
                    )
                if rejection.get("verification_source") != "psasp_gui_ordinary_load_flow":
                    raise ValueError(
                        f"sample {sample.sample_id} rejected recovery lacks ordinary-flow evidence"
                    )

    def factor_counts(self) -> dict[str, int]:
        return dict(Counter(sample.factor for sample in self.samples))

    def action_space_gaps(self) -> list[dict[str, Any]]:
        fields = sorted({change.field for sample in self.samples for change in sample.changes})
        gaps = []
        if "Pg" in fields:
            gaps.append(
                {
                    "field": "Pg",
                    "status": "diagnostic_only",
                    "reason": "current Valid-only searcher does not write generator active power",
                }
            )
        if any(field.startswith("GivenDCPower_") for field in fields):
            gaps.append(
                {
                    "field": "GivenDCPower_High/Low",
                    "status": "diagnostic_only",
                    "reason": "LCC setpoint action needs explicit bounds, step and rollback policy",
                }
            )
        return gaps

    def profile(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "baseline": self.baseline,
            "included_sample_ids": [sample.sample_id for sample in self.samples],
            "excluded_samples": list(self.excluded_samples),
            "sample_count": len(self.samples),
            "failure_factor_counts": self.factor_counts(),
            "all_included_samples_observed_nonconverged": all(
                not sample.expected_converged for sample in self.samples
            ),
            "ordinary_confirmed_recovery_count": sum(
                bool(sample.confirmed_recovery) for sample in self.samples
            ),
            "ordinary_confirmed_sample_ids": [
                sample.sample_id
                for sample in self.samples
                if sample.confirmed_recovery
            ],
            "sample_reference_cases": {
                sample.sample_id: sample.reference_case
                for sample in self.samples
            },
            "changed_fields": sorted(
                {change.field for sample in self.samples for change in sample.changes}
            ),
            "action_space_gaps": self.action_space_gaps(),
            "recommendation": (
                "Use this catalog to calibrate diagnosis and candidate-family priors. "
                "Do not enable Pg or LCC setpoint writes until successful recovery labels "
                "and bounds are recorded."
            ),
        }

    def write_profile(self, path: str | Path) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(self.profile(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return output


def calibrate_catalog(catalog_path: str | Path, output_path: str | Path) -> dict[str, Any]:
    catalog = TrainingCatalog.from_json(catalog_path)
    output = catalog.write_profile(output_path)
    profile = catalog.profile()
    profile["output_path"] = str(output)
    return profile
