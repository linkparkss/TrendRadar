"""Strict verification of a fresh ordinary PSASP load-flow result."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import VerificationConfig
from .diagnostics import parse_iteration_records, read_gbk
from .models import (
    CaseIdentity,
    CaseStatus,
    FileFingerprint,
    ResultBaseline,
    VerificationResult,
)


LF_CAL_SUCCESS_RE = re.compile(r"^\s*1\s*,\s*0\s*,?\s*$")
LF_CAL_DATE_RE = re.compile(r"^\s*(\d{8})\s*,\s*(\d{6})\s*,?\s*$")


def file_fingerprint(path: Path) -> FileFingerprint:
    if not path.is_file():
        return FileFingerprint(exists=False)
    content = path.read_bytes()
    stat = path.stat()
    return FileFingerprint(
        exists=True,
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def capture_result_baseline(temp_path: str | Path) -> ResultBaseline:
    root = Path(temp_path)
    names = ("LF.CAL", "LFCAL.LIS", "LF.LP1", "lfreport.lis")
    return ResultBaseline(
        captured_at=datetime.now().isoformat(timespec="microseconds"),
        files={name: file_fingerprint(root / name) for name in names},
    )


def _parse_lf_cal(path: Path) -> tuple[str, str, str]:
    text = read_gbk(path)
    lines = text.splitlines()
    first_line = lines[0] if lines else ""
    result_date = ""
    result_time = ""
    if len(lines) > 1:
        match = LF_CAL_DATE_RE.match(lines[1])
        if match:
            result_date, result_time = match.groups()
    return first_line, result_date, result_time


def _fresh(
    name: str,
    current: FileFingerprint,
    baseline: ResultBaseline,
) -> bool:
    previous = baseline.files.get(name, FileFingerprint(exists=False))
    return current.exists and current != previous


def verify_ordinary_result(
    temp_path: str | Path,
    baseline: ResultBaseline,
    expected_case: CaseIdentity,
    case_status: CaseStatus,
    config: VerificationConfig,
) -> VerificationResult:
    root = Path(temp_path)
    current = {
        name: file_fingerprint(root / name)
        for name in ("LF.CAL", "LFCAL.LIS", "LF.LP1")
    }
    reasons: list[str] = []
    lf_cal_fresh = _fresh("LF.CAL", current["LF.CAL"], baseline)
    lfcal_fresh = _fresh("LFCAL.LIS", current["LFCAL.LIS"], baseline)
    lp1_fresh = _fresh("LF.LP1", current["LF.LP1"], baseline)

    if config.require_fresh_lf_cal and not lf_cal_fresh:
        reasons.append("LF.CAL is missing or stale")
    if config.require_fresh_lfcal and not lfcal_fresh:
        reasons.append("LFCAL.LIS is missing or stale")
    if config.require_fresh_lp1 and not lp1_fresh:
        reasons.append("LF.LP1 is missing or stale")

    first_line, result_date, result_time = _parse_lf_cal(root / "LF.CAL")
    if not LF_CAL_SUCCESS_RE.match(first_line):
        reasons.append(f"LF.CAL does not report success: {first_line!r}")

    lfcal_text = read_gbk(root / "LFCAL.LIS")
    if "\u6f6e\u6d41\u8ba1\u7b97\u4e0d\u6536\u655b" in lfcal_text:
        reasons.append("LFCAL.LIS reports non-converged load flow")
    records = parse_iteration_records(lfcal_text)
    final_bus = records[-1][1] if records else ""
    final_mismatch = records[-1][2] if records else None
    if final_mismatch is None:
        reasons.append("LFCAL.LIS has no iteration records")
    elif final_mismatch > case_status.tolerance:
        reasons.append(
            f"final mismatch {final_mismatch:.9g} exceeds tolerance "
            f"{case_status.tolerance:.9g}"
        )

    if case_status.identity != expected_case:
        reasons.append(
            f"wrong case status: {case_status.identity.case_no}/"
            f"{case_status.identity.case_name}"
        )
    if config.require_case_status and case_status.calculate != 1:
        reasons.append(f"lf_case.CALCULATE is {case_status.calculate}, expected 1")

    if result_date and result_time:
        result_dt = datetime.strptime(result_date + result_time, "%Y%m%d%H%M%S")
        captured_at = datetime.fromisoformat(baseline.captured_at)
        if result_dt + timedelta(seconds=config.timestamp_slack_seconds) < captured_at:
            reasons.append("LF.CAL timestamp predates this attempt")
        normalized_date = f"{result_date[:4]}/{result_date[4:6]}/{result_date[6:]}"
        normalized_time = f"{result_time[:2]}:{result_time[2:4]}:{result_time[4:]}"
        if config.require_case_status and (
            case_status.calculation_date != normalized_date
            or case_status.calculation_time != normalized_time
        ):
            reasons.append("lf_case calculation timestamp does not match LF.CAL")
    else:
        reasons.append("LF.CAL calculation timestamp is missing")

    files_fresh = lf_cal_fresh and lfcal_fresh and (
        lp1_fresh or not config.require_fresh_lp1
    )
    return VerificationResult(
        converged=not reasons,
        reasons=tuple(reasons),
        lf_cal_first_line=first_line,
        result_date=result_date,
        result_time=result_time,
        final_bus=final_bus,
        final_mismatch=final_mismatch,
        iteration_records=len(records),
        files_fresh=files_fresh,
    )

