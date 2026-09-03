"""Replay a labeled failure in Temp, then apply its learned recovery action."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from powerflow.convergence.assessment import assess_ordinary_result
from powerflow.convergence.automatic import FieldAction, candidate_from_catalog
from powerflow.convergence.config import load_config
from powerflow.convergence.models import CaseStatus
from powerflow.convergence.temp_executor import (
    PersistentSnapshot,
    apply_temp_actions,
    read_active_temp_case,
    run_legacy_wmlfadj,
)
from powerflow.convergence.training import TrainingCatalog
from powerflow.convergence.verifier import capture_result_baseline


def _lf_cal_timestamp(temp_path: Path) -> tuple[str, str]:
    path = temp_path / "LF.CAL"
    if not path.is_file():
        return "", ""
    lines = path.read_text(encoding="gbk", errors="replace").splitlines()
    if len(lines) < 2:
        return "", ""
    values = lines[1].split(",")
    if len(values) < 2:
        return "", ""
    date, clock = values[0].strip(), values[1].strip()
    if not re.fullmatch(r"\d{8}", date) or not re.fullmatch(r"\d{6}", clock):
        return "", ""
    return f"{date[:4]}/{date[4:6]}/{date[6:]}", f"{clock[:2]}:{clock[2:4]}:{clock[4:]}"


def _assessment(temp_path, baseline, active, verification):
    date, clock = _lf_cal_timestamp(temp_path)
    status = CaseStatus(
        identity=active.identity,
        calculate=1,
        tolerance=active.tolerance,
        iteration_limit=0,
        calculation_date=date,
        calculation_time=clock,
    )
    return assess_ordinary_result(
        temp_path,
        baseline,
        active.identity,
        status,
        verification,
    )


def _assessment_dict(value):
    return {
        "definitive": value.definitive,
        "result": value.result.to_dict(),
    }


def _outcome_dict(value):
    return value.__dict__


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay and recover a labeled PSASP sample")
    parser.add_argument("--config", required=True)
    parser.add_argument("--catalog", default="samples/fengcheng08_training.json")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()

    config = load_config(args.config)
    catalog = TrainingCatalog.from_json(args.catalog)
    candidate = candidate_from_catalog(catalog, args.sample)
    active = read_active_temp_case(config.project.temp_path)
    executable = config.project.psasp_path / "wmlfadj.exe"
    fault_actions = tuple(
        FieldAction(
            table=action.table,
            record_id=action.record_id,
            device_name=action.device_name,
            field=action.field,
            before=action.after,
            after=action.before,
        )
        for action in candidate.actions
    )
    preview = {
        "mode": "sample_replay" if args.run else "dry_run",
        "sample_id": args.sample,
        "active_temp_case": {
            "case_no": active.identity.case_no,
            "case_name": active.identity.case_name,
        },
        "fault_actions": [action.to_dict() for action in fault_actions],
        "recovery_actions": [action.to_dict() for action in candidate.actions],
        "executable": str(executable),
    }
    if not args.run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if active.identity.case_name != "AI_Adjust_Case_Temp":
        raise RuntimeError(
            "sample replay is allowed only for the explicit AI_Adjust_Case_Temp alias"
        )
    if not executable.is_file():
        raise FileNotFoundError(f"wmlfadj.exe does not exist: {executable}")

    snapshot = PersistentSnapshot.create(
        config.project.temp_path,
        config.project.run_root,
        candidate,
    )
    payload = dict(preview)
    payload["run_dir"] = str(snapshot.run_dir)
    try:
        fault_baseline = capture_result_baseline(config.project.temp_path)
        apply_temp_actions(config.project.temp_path, fault_actions)
        snapshot.mark("fault_applied")
        fault_outcome = run_legacy_wmlfadj(
            executable,
            config.project.temp_path,
            args.timeout,
        )
        fault_assessment = _assessment(
            config.project.temp_path,
            fault_baseline,
            active,
            config.verification,
        )
        payload["fault_stage"] = {
            "outcome": _outcome_dict(fault_outcome),
            "assessment": _assessment_dict(fault_assessment),
        }
        failure_confirmed = (
            fault_outcome.error is None
            and not fault_outcome.timed_out
            and fault_assessment.definitive
            and not fault_assessment.result.converged
        )
        if not failure_confirmed:
            snapshot.restore(config.project.temp_path)
            snapshot.mark("rolled_back", "failure sample was not definitively reproduced")
            payload.update(status="fault_not_reproduced", rolled_back=True)
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 2

        snapshot.mark("failure_confirmed")
        recovery_baseline = capture_result_baseline(config.project.temp_path)
        apply_temp_actions(config.project.temp_path, candidate.actions)
        snapshot.mark("recovery_applied")
        recovery_outcome = run_legacy_wmlfadj(
            executable,
            config.project.temp_path,
            args.timeout,
        )
        recovery_assessment = _assessment(
            config.project.temp_path,
            recovery_baseline,
            active,
            config.verification,
        )
        payload["recovery_stage"] = {
            "outcome": _outcome_dict(recovery_outcome),
            "assessment": _assessment_dict(recovery_assessment),
        }
        recovered = (
            recovery_outcome.error is None
            and not recovery_outcome.timed_out
            and recovery_assessment.definitive
            and recovery_assessment.result.converged
        )
        if not recovered:
            snapshot.restore(config.project.temp_path)
            snapshot.mark("rolled_back", "recovery was not definitively converged")
            payload.update(status="recovery_failed", rolled_back=True)
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 2

        snapshot.mark("converged")
        payload.update(
            status="converged",
            rolled_back=False,
            adjusted_devices=[action.to_dict() for action in candidate.actions],
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0
    except BaseException as exc:
        try:
            snapshot.restore(config.project.temp_path)
            snapshot.mark("rolled_back_error", str(exc))
        except BaseException as rollback_error:
            snapshot.mark("rollback_failed", str(rollback_error))
            raise RuntimeError(
                f"sample replay and rollback both failed; run_dir={snapshot.run_dir}"
            ) from exc
        raise


if __name__ == "__main__":
    raise SystemExit(main())
