"""Deterministic cross-case perturbation-plan generation from PSASP Temp cards."""

from __future__ import annotations

import hashlib
import copy
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence
from .adaptive_load import AdaptiveLoadConfig


from .temp_executor import read_active_temp_case


INPUT_CARDS = ("LF.L0", "LF.L2", "LF.L3", "LF.L5", "LF.L6", "LF.ML4")
_SHUNT_HINTS = (
    "\u7535\u6297\u5668",
    "\u7535\u5bb9\u5668",
    "\u5e76\u8054\u7535\u6297",
    "\u5e76\u8054\u7535\u5bb9",
    "\u4e32\u8865",
)
_SLACK_HINTS = (
    "\u5e73\u8861",
    "\u6eaa\u6d1b\u6e21\u5de6",
    "\u4e09\u5ce1\u5de6",
    "\u6362\u6d41",
)
_POLE_SUFFIXES = (
    "_\u6b63\u6781",
    "_\u8d1f\u6781",
    "\u6b63\u6781",
    "\u8d1f\u6781",
    "_\u9ad8\u7aef",
    "_\u4f4e\u7aef",
)


def _read_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("gbk", errors="surrogateescape").splitlines()


def _name(value: str) -> str:
    return value.strip().strip("'").strip('"').strip()


def _integer(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _number(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None


def card_fingerprints(temp_path: str | Path) -> dict[str, dict[str, Any]]:
    root = Path(temp_path)
    result: dict[str, dict[str, Any]] = {}
    for name in INPUT_CARDS:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f"PSASP input card is missing: {path}")
        content = path.read_bytes()
        result[name] = {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    return result


def _simple_records(
    path: Path,
    *,
    name_index: int,
    id_index: int,
    value_indices: dict[str, int],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for ordinal, line in enumerate(_read_lines(path), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(",")
        if len(fields) <= max(name_index, id_index, *value_indices.values()):
            continue
        name = _name(fields[name_index])
        valid = _integer(fields[0])
        if not name or valid is None:
            continue
        values: dict[str, Any] = {"Valid": valid}
        malformed = False
        for label, index in value_indices.items():
            value = _number(fields[index])
            if value is None:
                malformed = True
                break
            values[label] = value
        if malformed:
            continue
        record_id = _integer(fields[id_index])
        records.append(
            {
                "record_id": ordinal if record_id is None else record_id,
                "name": name,
                **values,
            }
        )
    return records


def _lcc_starts(lines: Sequence[str]) -> list[int]:
    starts: list[int] = []
    in_section = False
    index = 0
    while index < len(lines):
        if lines[index].startswith("#4,"):
            in_section = True
            index += 1
            continue
        if in_section and lines[index].startswith("#"):
            break
        if in_section and lines[index].strip() and index + 2 < len(lines):
            if all(not lines[position].startswith("#") for position in range(index, index + 3)):
                starts.append(index)
                index += 3
                continue
        index += 1
    return starts


def _lcc_records(path: Path) -> list[dict[str, Any]]:
    lines = _read_lines(path)
    records: list[dict[str, Any]] = []
    for ordinal, start in enumerate(_lcc_starts(lines), start=1):
        first = lines[start].split(",")
        control = lines[start + 2].split(",")
        if len(first) <= 8 or len(control) <= 4:
            continue
        name = _name(first[8])
        power = _number(control[4])
        if name and power is not None and power != 0:
            records.append({"record_id": ordinal, "name": name, "power": power})
    return records


def _spread(records: Sequence[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    ordered = sorted(records, key=lambda item: str(item["name"]))
    if count <= 0 or not ordered:
        return []
    if len(ordered) <= count:
        return ordered
    if count == 1:
        return [ordered[len(ordered) // 2]]
    indexes = [round(index * (len(ordered) - 1) / (count - 1)) for index in range(count)]
    return [ordered[index] for index in indexes]


def _change(
    table: str,
    record: dict[str, Any],
    field: str,
    before: Any,
    after: Any,
) -> dict[str, Any]:
    return {
        "table": table,
        "record_id": int(record["record_id"]),
        "device_name": str(record["name"]),
        "field": field,
        "before": before,
        "after": after,
    }


def _sample(
    sample_id: str,
    baseline_id: str,
    factor: str,
    severity: str,
    description: str,
    changes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    materialized = list(changes)
    if not materialized:
        raise ValueError(f"sample {sample_id} has no changes")
    return {
        "sample_id": sample_id,
        "baseline_id": baseline_id,
        "factor": factor,
        "factor_family": factor,
        "severity": severity,
        "description": description,
        "atomic_action_group": len(materialized) > 1,
        "changes": materialized,
    }


def materialize_adaptive_load_probe(
    template: dict[str, Any],
    increase: float,
    *,
    phase: str,
) -> dict[str, Any]:
    """Create one P/Q probe from a load-search template."""

    if increase <= 0.0:
        raise ValueError("adaptive load increase must be positive")
    metadata = template.get("adaptive_search")
    if not isinstance(metadata, dict) or not metadata.get("search_id"):
        raise ValueError("adaptive load template has no search_id")
    changes = template.get("changes")
    fields = {item.get("field") for item in changes} if isinstance(changes, list) else set()
    if len(changes or ()) != 2 or fields != {"Pl", "Ql"}:
        raise ValueError("adaptive load template must contain one Pl and one Ql change")
    if len({item.get("record_id") for item in changes}) != 1:
        raise ValueError("adaptive load template must target one load record")
    basis_points = int(round(increase * 10000))
    if abs(increase - basis_points / 10000.0) > 1e-10:
        raise ValueError("adaptive load increase needs at most four decimal places")
    result = copy.deepcopy(template)
    result["sample_id"] = f"{metadata['search_id']}_INC{basis_points:05d}BP"
    result["severity"] = "medium" if increase <= 0.40 else "high"
    device_name = str(changes[0]["device_name"])
    result["description"] = (
        f"Adaptive probe: increase load P and Q by {increase * 100:g}% "
        f"for {device_name}"
    )
    for change in result["changes"]:
        change["after"] = round(float(change["before"]) * (1.0 + increase), 6)
    result["adaptive_search"].update(
        increase_fraction=increase,
        phase=phase,
        ordinary_gui_label_required=True,
    )
    return result

def _prefix(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", value.upper()).strip("_")
    return normalized[:28] or "YAHU"


def _pole_group(name: str) -> str:
    result = re.sub(
        r"(?:_?(?:\u6b63\u6781|\u8d1f\u6781|\u9ad8\u7aef|\u4f4e\u7aef))(?:@\d+)?$",
        "",
        name,
    )
    if result != name:
        return result
    for suffix in _POLE_SUFFIXES:
        if result.endswith(suffix):
            result = result[: -len(suffix)]
            break
    return result


def generate_plan_from_temp(
    temp_path: str | Path,
    *,
    plan_id: str,
    baseline_id: str,
    source_case_no: int | None = None,
    source_case_name: str | None = None,
    max_per_family: int = 2,
    adaptive_load: AdaptiveLoadConfig = AdaptiveLoadConfig(),
) -> dict[str, Any]:
    """Build bounded N-1, setpoint, load, and compound samples for one baseline."""

    if max_per_family < 1:
        raise ValueError("max_per_family must be positive")
    root = Path(temp_path)
    active = read_active_temp_case(root)
    lines = _simple_records(
        root / "LF.L2",
        name_index=17,
        id_index=3,
        value_indices={},
    )
    transformers = _simple_records(
        root / "LF.L3",
        name_index=24,
        id_index=3,
        value_indices={},
    )
    generators = _simple_records(
        root / "LF.L5",
        name_index=19,
        id_index=1,
        value_indices={"Pg": 4, "Qg": 5, "Qmax": 8, "Qmin": 9},
    )
    loads = _simple_records(
        root / "LF.L6",
        name_index=18,
        id_index=1,
        value_indices={"Pl": 4, "Ql": 5},
    )
    lcc = _lcc_records(root / "LF.ML4")

    online_lines = [
        item
        for item in lines
        if item["Valid"] == 1 and not any(hint in item["name"] for hint in _SHUNT_HINTS)
    ]
    online_shunts = [
        item
        for item in lines
        if item["Valid"] == 1 and any(hint in item["name"] for hint in _SHUNT_HINTS)
    ]
    online_transformers = [item for item in transformers if item["Valid"] == 1]
    online_generators = [
        item
        for item in generators
        if item["Valid"] == 1
        and abs(item["Pg"]) > 1e-6
        and not any(hint in item["name"] for hint in _SLACK_HINTS)
    ]
    online_loads = [
        item for item in loads if item["Valid"] == 1 and abs(item["Pl"]) > 1e-6
    ]

    prefix = _prefix(baseline_id)
    samples: list[dict[str, Any]] = []

    for index, record in enumerate(_spread(online_lines, max_per_family), start=1):
        samples.append(
            _sample(
                f"{prefix}_LINE_N1_{index:02d}",
                baseline_id,
                "ac_line_outage",
                "medium",
                f"N-1 outage of AC line {record['name']}",
                [_change("ls2_0", record, "Valid", 1, 0)],
            )
        )

    for index, record in enumerate(_spread(online_transformers, max_per_family), start=1):
        samples.append(
            _sample(
                f"{prefix}_TRANSFORMER_N1_{index:02d}",
                baseline_id,
                "transformer_outage",
                "medium",
                f"N-1 outage of transformer {record['name']}",
                [_change("ls3_0", record, "Valid", 1, 0)],
            )
        )

    ranked_generators = sorted(
        online_generators,
        key=lambda item: (-abs(float(item["Pg"])), str(item["name"])),
    )
    for index, record in enumerate(ranked_generators[:max_per_family], start=1):
        samples.append(
            _sample(
                f"{prefix}_GEN_OUTAGE_{index:02d}",
                baseline_id,
                "generator_outage",
                "high",
                f"Outage of online generator {record['name']}",
                [_change("ls5_0", record, "Valid", 1, 0)],
            )
        )
        after = round(float(record["Pg"]) * 0.8, 6)
        samples.append(
            _sample(
                f"{prefix}_GEN_PG080_{index:02d}",
                baseline_id,
                "generator_pg_derating",
                "medium",
                f"Reduce generator active power to 80% for {record['name']}",
                [_change("ls5_0", record, "Pg", record["Pg"], after)],
            )
        )

    ranked_loads = sorted(
        online_loads,
        key=lambda item: (-abs(float(item["Pl"])), str(item["name"])),
    )
    for index, record in enumerate(ranked_loads[:max_per_family], start=1):
        search_id = f"{prefix}_LOAD_PQ_{index:02d}"
        template = _sample(
            f"{search_id}_TEMPLATE",
            baseline_id,
            "load_pq_increase",
            "medium",
            f"Adaptive load probe for {record['name']}",
            [
                _change("ls6_0", record, "Pl", record["Pl"], record["Pl"]),
                _change("ls6_0", record, "Ql", record["Ql"], record["Ql"]),
            ],
        )
        template["adaptive_search"] = {
            "search_id": search_id,
            "parameter": "load_pq_increase_fraction",
            "invalid_result_is_not_failure": True,
        }
        increases = (
            adaptive_load.initial_increases if adaptive_load.enabled else (0.20,)
        )
        for increase in increases:
            samples.append(
                materialize_adaptive_load_probe(
                    template,
                    increase,
                    phase="initial" if adaptive_load.enabled else "fixed",
                )
            )

    q_candidates = [
        item
        for item in ranked_generators
        if item["Qmin"] < item["Qg"] < item["Qmax"]
    ]
    for index, record in enumerate(q_candidates[:max_per_family], start=1):
        qmax = round(record["Qg"] + 0.25 * (record["Qmax"] - record["Qg"]), 6)
        qmin = round(record["Qg"] - 0.25 * (record["Qg"] - record["Qmin"]), 6)
        samples.append(
            _sample(
                f"{prefix}_GEN_QRESERVE25_{index:02d}",
                baseline_id,
                "generator_q_limit_tightening",
                "high",
                f"Retain 25% reactive reserve for {record['name']}",
                [
                    _change("ls5_0", record, "Qmax", record["Qmax"], qmax),
                    _change("ls5_0", record, "Qmin", record["Qmin"], qmin),
                ],
            )
        )

    for index, record in enumerate(_spread(online_shunts, max_per_family), start=1):
        samples.append(
            _sample(
                f"{prefix}_SHUNT_OUTAGE_{index:02d}",
                baseline_id,
                "shunt_outage",
                "medium",
                f"Outage of shunt/reactive device {record['name']}",
                [_change("ls2_0", record, "Valid", 1, 0)],
            )
        )

    grouped_lcc: dict[str, list[dict[str, Any]]] = {}
    for record in lcc:
        grouped_lcc.setdefault(_pole_group(record["name"]), []).append(record)
    eligible_lcc = sorted(grouped_lcc.items(), key=lambda item: item[0])
    for index, (_, records) in enumerate(eligible_lcc[:max_per_family], start=1):
        changes = [
            _change(
                "ls_lcc_0",
                record,
                "GivenDCPower_High",
                record["power"],
                round(record["power"] * 0.8, 6),
            )
            for record in records
        ]
        samples.append(
            _sample(
                f"{prefix}_LCC_POWER080_{index:02d}",
                baseline_id,
                "lcc_power_derating",
                "medium",
                f"Reduce LCC power to 80% for pole group {records[0]['name']}",
                changes,
            )
        )

    if online_lines and ranked_loads:
        line = _spread(online_lines, 1)[0]
        load = ranked_loads[0]
        samples.append(
            _sample(
                f"{prefix}_LINE_LOAD_COMBINED_01",
                baseline_id,
                "combined_line_outage_load_increase",
                "high",
                "N-1 line outage combined with a 10% P/Q load increase",
                [
                    _change("ls2_0", line, "Valid", 1, 0),
                    _change("ls6_0", load, "Pl", load["Pl"], round(load["Pl"] * 1.1, 6)),
                    _change("ls6_0", load, "Ql", load["Ql"], round(load["Ql"] * 1.1, 6)),
                ],
            )
        )

    if not samples:
        raise RuntimeError("no supported adjustable devices were found in Temp")
    ids = [item["sample_id"] for item in samples]
    if len(ids) != len(set(ids)):
        raise RuntimeError("generated duplicate sample IDs")
    return {
        "schema_version": 2,
        "plan_id": plan_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "policy": {
            "voltage_adjustment": False,
            "max_per_family": max_per_family,
            "ordinary_gui_confirmation_required": True,
            "adaptive_load_search": {
                "enabled": adaptive_load.enabled,
                "initial_increases": list(adaptive_load.initial_increases),
                "expansion_increases": list(adaptive_load.expansion_increases),
                "minimum_bracket_width": adaptive_load.minimum_bracket_width,
                "boundary": "numerically_converged_to_numerical_failure",
                "requires_baseline_control": True,
                "requires_ordinary_gui_labels": True,
            },
            "factor_is_perturbation_not_proven_root_cause": True,
        },
        "baselines": [
            {
                "baseline_id": baseline_id,
                "source_case_no": source_case_no,
                "source_case_name": source_case_name,
                "temp_case_name": active.identity.case_name,
                "temp_case_no": active.identity.case_no,
                "card_fingerprints": card_fingerprints(root),
            }
        ],
        "samples": samples,
    }


__all__ = [
    "INPUT_CARDS",
    "card_fingerprints",
    "generate_plan_from_temp",
    "materialize_adaptive_load_probe",
]
