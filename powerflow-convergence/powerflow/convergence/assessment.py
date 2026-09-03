"""Add an explicit definitive/inconclusive layer around the legacy verifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import VerificationConfig
from .models import CaseIdentity, CaseStatus, ResultBaseline, VerificationResult
from .verifier import verify_ordinary_result


@dataclass(frozen=True)
class VerificationAssessment:
    result: VerificationResult
    definitive: bool


def assess_ordinary_result(
    temp_path: str | Path,
    baseline: ResultBaseline,
    expected_case: CaseIdentity,
    case_status: CaseStatus,
    config: VerificationConfig,
) -> VerificationAssessment:
    result = verify_ordinary_result(
        temp_path=temp_path,
        baseline=baseline,
        expected_case=expected_case,
        case_status=case_status,
        config=config,
    )
    reasons = set(result.reasons)
    stale_or_wrong_case = any(
        reason.startswith(prefix)
        for reason in result.reasons
        for prefix in (
            "LF.CAL is missing or stale",
            "LFCAL.LIS is missing or stale",
            "LF.LP1 is missing or stale",
            "wrong case status:",
            "LF.CAL timestamp predates",
            "LF.CAL calculation timestamp is missing",
            "lf_case calculation timestamp does not match LF.CAL",
        )
    )
    required_fresh = result.files_fresh
    if not config.require_fresh_lf_cal and "LF.CAL is missing or stale" in reasons:
        required_fresh = required_fresh or True
    definitive = required_fresh and not stale_or_wrong_case
    return VerificationAssessment(result=result, definitive=definitive)
