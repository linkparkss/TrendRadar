"""Verification and adjustment parsing for the PSASP wmlfadj.exe backend."""

from __future__ import annotations

import csv
import importlib
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from .automatic_impl import WmlfadjOutcome
from .diagnostics import read_gbk
from .models import FileFingerprint
from .verifier import file_fingerprint


_LF_CAL_SUCCESS = re.compile(r"^\s*1\s*,\s*0\s*,?\s*$")
_LF_CAL_DATE = re.compile(r"^\s*(\d{8})\s*,\s*(\d{6})\s*,?\s*$")
_ITERATION = re.compile(
    r"^\s*(\d+)\s+(.+?)\s+([-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)"
    r"(?:\s+[-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)?\s*$"
)
_BALANCE_ROW = re.compile(
    r"^\s*(.+?)\s+([-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)(\*)?"
    r"\s+([-+]?\d+(?:\.\d*)?(?:[Ee][-+]?\d+)?)(\*)?\s*$"
)
_FILES = ("LF.CAL", "LF.LP1", "lfreport.lis", "LF.adj")


@dataclass(frozen=True)
class WmlfadjBaseline:
    captured_at: str
    files: Mapping[str, FileFingerprint]


@dataclass(frozen=True)
class WmlfadjVerification:
    converged: bool
    definitive: bool
    reasons: tuple[str, ...]
    lf_cal_first_line: str
    result_date: str
    result_time: str
    final_bus: str
    final_mismatch: float | None
    iteration_records: int
    bus_count: int
    files_fresh: bool
    balance_limit_violations: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "converged": self.converged,
            "definitive": self.definitive,
            "reasons": list(self.reasons),
            "lf_cal_first_line": self.lf_cal_first_line,
            "result_date": self.result_date,
            "result_time": self.result_time,
            "final_bus": self.final_bus,
            "final_mismatch": self.final_mismatch,
            "iteration_records": self.iteration_records,
            "bus_count": self.bus_count,
            "files_fresh": self.files_fresh,
            "balance_limit_violations": list(self.balance_limit_violations),
        }


def capture_wmlfadj_baseline(temp_path: str | Path) -> WmlfadjBaseline:
    root = Path(temp_path)
    return WmlfadjBaseline(
        captured_at=datetime.now().isoformat(timespec="microseconds"),
        files={name: file_fingerprint(root / name) for name in _FILES},
    )


def _fresh(name: str, current: FileFingerprint, baseline: WmlfadjBaseline) -> bool:
    previous = baseline.files.get(name, FileFingerprint(exists=False))
    return current.exists and current != previous


def _lf_cal(path: Path) -> tuple[str, str, str]:
    lines = read_gbk(path).splitlines()
    first = lines[0] if lines else ""
    if len(lines) > 1:
        match = _LF_CAL_DATE.match(lines[1])
        if match:
            return first, match.group(1), match.group(2)
    return first, "", ""


def _iteration_records(text: str) -> list[tuple[int, str, float]]:
    records: list[tuple[int, str, float]] = []
    for line in text.splitlines():
        # Area-control reports append a numeric interchange table after the
        # Newton iteration trace. Its rows also begin with an integer and
        # otherwise match the generic iteration regex.
        if "AREA" in line and "INTERCHANGE" in line:
            break
        tokens = line.split()
        if tokens and tokens[0].isdigit():
            pairs: list[tuple[str, float]] = []
            index = 1
            while index + 1 < len(tokens):
                try:
                    float(tokens[index])
                except ValueError:
                    try:
                        mismatch = float(tokens[index + 1])
                    except ValueError:
                        index += 1
                        continue
                    pairs.append((tokens[index], mismatch))
                    index += 2
                    continue
                index += 1
            if pairs:
                bus, mismatch = max(pairs, key=lambda item: abs(item[1]))
                records.append((int(tokens[0]), bus, mismatch))
                continue
        match = _ITERATION.match(line)
        if match:
            records.append((int(match.group(1)), match.group(2).strip(), float(match.group(3))))
    return records


def _balance_limit_violations(text: str) -> list[dict[str, Any]]:
    in_balance_section = False
    violations: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "平衡节点" in line and "注入有功" in line and "注入无功" in line:
            in_balance_section = True
            continue
        if not in_balance_section:
            continue
        if "潮流计算程序版本号" in line:
            break
        if not line.strip() or set(line.strip()) == {"-"}:
            continue
        match = _BALANCE_ROW.match(line)
        if not match:
            continue
        active_exceeded = bool(match.group(3))
        reactive_exceeded = bool(match.group(5))
        if active_exceeded or reactive_exceeded:
            violations.append(
                {
                    "bus": match.group(1).strip(),
                    "active_power": float(match.group(2)),
                    "reactive_power": float(match.group(4)),
                    "active_exceeded": active_exceeded,
                    "reactive_exceeded": reactive_exceeded,
                }
            )
    return violations


def verify_wmlfadj_result(
    temp_path: str | Path,
    baseline: WmlfadjBaseline,
    tolerance: float,
    outcome: WmlfadjOutcome,
    timestamp_slack_seconds: float = 3.0,
) -> WmlfadjVerification:
    root = Path(temp_path)
    current = {name: file_fingerprint(root / name) for name in _FILES}
    required = ("LF.CAL", "LF.LP1", "lfreport.lis")
    fresh = {name: _fresh(name, current[name], baseline) for name in _FILES}
    reasons: list[str] = []
    if outcome.timed_out:
        reasons.append("wmlfadj.exe timed out")
    if outcome.error:
        reasons.append(outcome.error)
    for name in required:
        if not fresh[name]:
            reasons.append(f"{name} is missing or stale")

    first, date, clock = _lf_cal(root / "LF.CAL")
    if not _LF_CAL_SUCCESS.match(first):
        reasons.append(f"LF.CAL does not report success: {first!r}")
    if date and clock:
        result_time = datetime.strptime(date + clock, "%Y%m%d%H%M%S")
        captured = datetime.fromisoformat(baseline.captured_at)
        if result_time + timedelta(seconds=timestamp_slack_seconds) < captured:
            reasons.append("LF.CAL timestamp predates this attempt")
    else:
        reasons.append("LF.CAL calculation timestamp is missing")

    report_text = read_gbk(root / "lfreport.lis")
    records = _iteration_records(report_text)
    final_bus = records[-1][1] if records else ""
    final_mismatch = records[-1][2] if records else None
    if final_mismatch is None:
        reasons.append("lfreport.lis has no iteration records")
    elif not math.isfinite(final_mismatch) or final_mismatch > float(tolerance):
        reasons.append(
            f"final mismatch {final_mismatch!r} exceeds tolerance {float(tolerance):.9g}"
        )
    balance_limit_violations = _balance_limit_violations(report_text)
    if balance_limit_violations:
        buses = ", ".join(
            str(item["bus"]) for item in balance_limit_violations
        )
        reasons.append(f"balance node injection limit exceeded: {buses}")

    bus_count = 0
    if fresh["LF.LP1"]:
        legacy = importlib.import_module("潮流收敛")
        try:
            bus_count = len(legacy.read_valid_bus_voltages(root / "LF.LP1"))
        except ValueError as exc:
            reasons.append(str(exc))
    files_fresh = all(fresh[name] for name in required)
    definitive = files_fresh and not any(
        reason.endswith("missing or stale")
        or reason.startswith("LF.CAL timestamp predates")
        or reason.startswith("LF.CAL calculation timestamp")
        for reason in reasons
    )
    return WmlfadjVerification(
        converged=not reasons,
        definitive=definitive,
        reasons=tuple(reasons),
        lf_cal_first_line=first,
        result_date=date,
        result_time=clock,
        final_bus=final_bus,
        final_mismatch=final_mismatch,
        iteration_records=len(records),
        bus_count=bus_count,
        files_fresh=files_fresh,
        balance_limit_violations=tuple(balance_limit_violations),
    )


def parse_lf_adj_net(path: str | Path) -> list[dict[str, Any]]:
    source = Path(path)
    if not source.is_file():
        return []
    states: dict[tuple[int, str, str], dict[str, Any]] = {}
    with source.open("r", encoding="gbk", errors="replace", newline="") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                fields = next(csv.reader([line], quotechar="'", skipinitialspace=True))
            except csv.Error:
                continue
            if len(fields) < 5:
                continue
            try:
                code = int(fields[0].strip())
            except ValueError:
                continue
            name, field = fields[1].strip(), fields[2].strip()
            before, after = fields[3].strip(), fields[4].strip()
            if not name or not field or before == "" or after == "":
                continue
            key = (code, name, field)
            entry = states.setdefault(
                key,
                {
                    "code": code,
                    "device_name": name,
                    "field": field,
                    "before": before,
                    "after": after,
                    "first_line": line_number,
                    "last_line": line_number,
                    "steps": 0,
                },
            )
            entry["after"] = after
            entry["last_line"] = line_number
            entry["steps"] += 1
    return [entry for entry in states.values() if entry["before"] != entry["after"]]


__all__ = [
    "WmlfadjBaseline",
    "WmlfadjVerification",
    "capture_wmlfadj_baseline",
    "parse_lf_adj_net",
    "verify_wmlfadj_result",
]
