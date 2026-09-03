"""Safe multi-baseline runner for generated PSASP perturbation samples."""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from .adaptive_load import LoadProbe, decide_next_load_probe
from .automatic import FieldAction, RecoveryCandidate
from .config import load_config
from .sample_factory import (
    INPUT_CARDS,
    card_fingerprints,
    generate_plan_from_temp,
    materialize_adaptive_load_probe,
)
from .temp_executor import (
    PersistentSnapshot,
    apply_temp_actions,
    read_active_temp_case,
    run_legacy_wmlfadj,
)
from .wmlfadj_result import (
    WmlfadjVerification,
    capture_wmlfadj_baseline,
    verify_wmlfadj_result,
)


_RESULT_ARTIFACTS = (
    "LF.CAL",
    "LF.LP1",
    "LFCAL.LIS",
    "lfreport.lis",
    "LFERR.LIS",
    "LF.adj",
)
_LF_SUCCESS = re.compile(r"^\s*1\s*,\s*0\s*,?\s*$")


def load_generated_plan(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(payload.get("schema_version", 0))
    if version not in {1, 2}:
        raise ValueError("unsupported generated-plan schema")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("generated plan has no samples")
    ids = [str(item.get("sample_id", "")) for item in samples]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("generated plan contains empty or duplicate sample IDs")
    if version == 1:
        if not isinstance(payload.get("baseline"), dict):
            raise ValueError("schema v1 plan has no baseline")
        return payload
    baselines = payload.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        raise ValueError("schema v2 plan has no baselines")
    baseline_ids = [str(item.get("baseline_id", "")) for item in baselines]
    if any(not item for item in baseline_ids) or len(baseline_ids) != len(set(baseline_ids)):
        raise ValueError("generated plan contains empty or duplicate baseline IDs")
    known = set(baseline_ids)
    for sample in samples:
        if str(sample.get("baseline_id", "")) not in known:
            raise ValueError(
                f"sample {sample.get('sample_id')} refers to an unknown baseline"
            )
    return payload


def sample_actions(sample: dict[str, Any]) -> tuple[FieldAction, ...]:
    actions = tuple(
        FieldAction(
            table=str(change["table"]),
            record_id=int(change["record_id"]),
            device_name=str(change["device_name"]),
            field=str(change["field"]),
            before=change["before"],
            after=change["after"],
        )
        for change in sample.get("changes", [])
    )
    if not actions:
        raise ValueError(f"sample {sample.get('sample_id')} has no changes")
    if len({action.key for action in actions}) != len(actions):
        raise ValueError(f"sample {sample.get('sample_id')} has duplicate action keys")
    return actions


def _inverse(actions: Sequence[FieldAction]) -> tuple[FieldAction, ...]:
    return tuple(
        FieldAction(
            action.table,
            action.record_id,
            action.device_name,
            action.field,
            action.after,
            action.before,
        )
        for action in actions
    )


def _baselines(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if int(plan["schema_version"]) == 1:
        baseline = dict(plan["baseline"])
        baseline.setdefault("baseline_id", "legacy_v1")
        return [baseline]
    return list(plan["baselines"])


def _sample_baseline_id(plan: dict[str, Any], sample: dict[str, Any]) -> str:
    if int(plan["schema_version"]) == 1:
        return str(_baselines(plan)[0]["baseline_id"])
    return str(sample["baseline_id"])


def _fingerprint_mismatches(
    baseline: dict[str, Any],
    temp_path: Path,
) -> list[str]:
    expected = baseline.get("card_fingerprints")
    if not isinstance(expected, dict) or not expected:
        return []
    mismatches: list[str] = []
    for name, fingerprint in expected.items():
        path = temp_path / str(name)
        if not path.is_file():
            mismatches.append(f"{name}: missing")
            continue
        content = path.read_bytes()
        import hashlib

        actual_size = len(content)
        actual_sha = hashlib.sha256(content).hexdigest()
        if int(fingerprint.get("size", -1)) != actual_size:
            mismatches.append(f"{name}: size changed")
        elif str(fingerprint.get("sha256", "")) != actual_sha:
            mismatches.append(f"{name}: sha256 changed")
    return mismatches


def _snapshot_input_names(baseline: dict[str, Any]) -> tuple[str, ...]:
    """Include optional, fingerprinted baseline cards without breaking old plans."""

    names = list(INPUT_CARDS)
    fingerprints = baseline.get("card_fingerprints")
    if isinstance(fingerprints, dict):
        names.extend(str(name) for name in fingerprints)
    return tuple(dict.fromkeys(names))


def resolve_active_baseline(
    plan: dict[str, Any],
    temp_path: str | Path,
    baseline_id: str | None = None,
) -> dict[str, Any]:
    root = Path(temp_path)
    active = read_active_temp_case(root)
    candidates = [
        item
        for item in _baselines(plan)
        if baseline_id is None or str(item["baseline_id"]) == baseline_id
    ]
    if not candidates:
        raise ValueError(f"baseline ID is not in the plan: {baseline_id}")
    matches: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for item in candidates:
        expected_name = str(item.get("temp_case_name", ""))
        if expected_name and active.identity.case_name != expected_name:
            diagnostics.append(
                f"{item['baseline_id']}: Temp name {active.identity.case_name!r} "
                f"does not match {expected_name!r}"
            )
            continue
        mismatches = _fingerprint_mismatches(item, root)
        if mismatches:
            diagnostics.append(f"{item['baseline_id']}: " + "; ".join(mismatches))
            continue
        matches.append(item)
    if len(matches) != 1:
        detail = " | ".join(diagnostics) or "ambiguous matching baselines"
        raise RuntimeError(f"active Temp baseline is not uniquely verified: {detail}")
    return matches[0]


def _card_for_action(action: FieldAction) -> str:
    if action.table.startswith("ls2_"):
        return "LF.L2"
    if action.table.startswith("ls3_"):
        return "LF.L3"
    if action.table.startswith("ls5_"):
        return "LF.L5"
    if action.table.startswith("ls6_"):
        return "LF.L6"
    if action.table.startswith("ls_lcc_"):
        return "LF.ML4"
    raise ValueError(f"unsupported plan table: {action.table}")


def _samples_for_baseline(
    plan: dict[str, Any],
    baseline_id: str,
) -> list[dict[str, Any]]:
    return [
        item
        for item in plan["samples"]
        if _sample_baseline_id(plan, item) == baseline_id
    ]


def validate_plan_against_temp(
    plan: dict[str, Any],
    temp_path: str | Path,
    *,
    baseline_id: str | None = None,
) -> dict[str, Any]:
    root = Path(temp_path)
    baseline = resolve_active_baseline(plan, root, baseline_id)
    selected = _samples_for_baseline(plan, str(baseline["baseline_id"]))
    if not selected:
        raise ValueError(f"baseline {baseline['baseline_id']} has no samples")
    cards: set[Path] = set()
    validated: list[dict[str, Any]] = []
    for raw in selected:
        actions = sample_actions(raw)
        candidate = RecoveryCandidate(
            candidate_id=f"PERTURB_{raw['sample_id']}",
            sample_id=str(raw["sample_id"]),
            actions=actions,
        )
        sample_cards = {root / _card_for_action(action) for action in actions}
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            for source in sample_cards:
                if not source.is_file():
                    raise FileNotFoundError(source)
                shutil.copyfile(source, trial / source.name)
            apply_temp_actions(trial, actions)
            apply_temp_actions(trial, _inverse(actions))
            for source in sample_cards:
                if (trial / source.name).read_bytes() != source.read_bytes():
                    raise RuntimeError(
                        f"sample {raw['sample_id']} failed byte-exact rollback check"
                    )
        cards.update(sample_cards)
        validated.append(candidate.to_dict())
    return {
        "plan_id": plan["plan_id"],
        "baseline_id": baseline["baseline_id"],
        "active_temp_case": read_active_temp_case(root).identity.to_dict(),
        "fingerprint_verified": bool(baseline.get("card_fingerprints")),
        "sample_count": len(validated),
        "cards": card_fingerprints(root)
        if int(plan["schema_version"]) == 2
        else {path.name: {"size": path.stat().st_size} for path in sorted(cards)},
        "samples": validated,
    }


def _copy_results(temp_path: Path, run_dir: Path) -> None:
    for name in _RESULT_ARTIFACTS:
        source = temp_path / name
        if source.is_file():
            shutil.copyfile(source, run_dir / name)


def _classification(
    verification: WmlfadjVerification,
    outcome: Any,
) -> dict[str, Any]:
    success_line = bool(_LF_SUCCESS.match(verification.lf_cal_first_line))
    numerically_converged = (
        verification.definitive
        and verification.files_fresh
        and success_line
        and verification.final_mismatch is not None
        and not any(
            reason.startswith("final mismatch")
            or reason.startswith("LF.CAL does not report success")
            for reason in verification.reasons
        )
    )
    if outcome.timed_out:
        execution_status = "timeout"
    elif outcome.error or outcome.returncode not in {0, 1}:
        execution_status = "process_error"
    else:
        execution_status = "completed"
    if not verification.files_fresh:
        label = "INVALID_OR_INDETERMINATE"
        numerical_state = "indeterminate"
    elif numerically_converged and verification.balance_limit_violations:
        label = "CONVERGED_INFEASIBLE"
        numerical_state = "converged"
    elif numerically_converged:
        label = "CONVERGED_FEASIBLE"
        numerical_state = "converged"
    elif verification.definitive:
        label = "NUMERICAL_FAILURE"
        numerical_state = "nonconverged"
    else:
        label = "INVALID_OR_INDETERMINATE"
        numerical_state = "indeterminate"
    return {
        "label": label,
        "execution_status": execution_status,
        "result_freshness": "fresh" if verification.files_fresh else "missing_or_stale",
        "numerical_state": numerical_state,
        "constraint_violations": {
            "balance_node": list(verification.balance_limit_violations)
        },
        "authority": "wmlfadj_provisional",
    }


def _all_actions(samples: Iterable[dict[str, Any]]) -> tuple[FieldAction, ...]:
    result: list[FieldAction] = []
    seen: set[tuple[str, int, str, str]] = set()
    for sample in samples:
        for action in sample_actions(sample):
            if action.key not in seen:
                seen.add(action.key)
                result.append(action)
    return tuple(result)


def run_generated_samples(
    plan: dict[str, Any],
    config_path: str | Path,
    *,
    baseline_id: str | None = None,
    start_at: int = 1,
    limit: int | None = None,
    timeout: float = 120.0,
    control_only: bool = False,
) -> dict[str, Any]:
    config = load_config(config_path)
    validation = validate_plan_against_temp(
        plan,
        config.project.temp_path,
        baseline_id=baseline_id,
    )
    if not validation["fingerprint_verified"]:
        raise RuntimeError(
            "real generated-sample runs require a schema v2 plan with exact "
            "Temp card fingerprints; schema v1 is dry-run compatibility only"
        )
    resolved_id = str(validation["baseline_id"])
    resolved_baseline = next(
        item for item in _baselines(plan) if str(item["baseline_id"]) == resolved_id
    )
    snapshot_input_names = _snapshot_input_names(resolved_baseline)
    executable = config.project.psasp_path / "wmlfadj.exe"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    baseline_samples = _samples_for_baseline(plan, resolved_id)
    selected = baseline_samples[start_at - 1 :]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise ValueError("no generated samples are selected")
    backup_actions = _all_actions(baseline_samples)
    batch_root = (
        config.project.run_root
        / f"GENERATED_{plan['plan_id']}_{resolved_id}_{datetime.now():%Y%m%d_%H%M%S}"
    )
    batch_root.mkdir(parents=True)
    results: list[dict[str, Any]] = []
    batch_status = "completed"
    control_candidate = RecoveryCandidate(
        candidate_id=f"CONTROL_{resolved_id}",
        sample_id=f"{resolved_id}_BASELINE_CONTROL",
        actions=(),
    )
    control_snapshot = PersistentSnapshot.create(
        config.project.temp_path,
        batch_root,
        control_candidate,
        backup_actions=backup_actions,
        include_results=True,
        extra_input_names=snapshot_input_names,
    )
    control: dict[str, Any] = {
        "sample_id": control_candidate.sample_id,
        "baseline_id": resolved_id,
        "factor": "unmodified_baseline_control",
        "changes": [],
        "run_dir": str(control_snapshot.run_dir),
        "rollback_status": "pending",
        "dataset_eligible": False,
    }
    try:
        result_baseline = capture_wmlfadj_baseline(config.project.temp_path)
        outcome = run_legacy_wmlfadj(
            executable,
            config.project.temp_path,
            timeout,
        )
        verification = verify_wmlfadj_result(
            config.project.temp_path,
            result_baseline,
            read_active_temp_case(config.project.temp_path).tolerance,
            outcome,
            config.verification.timestamp_slack_seconds,
        )
        _copy_results(config.project.temp_path, control_snapshot.run_dir)
        control.update(
            outcome=outcome.__dict__,
            verification=verification.to_dict(),
            **_classification(verification, outcome),
        )
    except BaseException as exc:
        control.update(
            label="INVALID_OR_INDETERMINATE",
            execution_status="exception",
            result_freshness="indeterminate",
            numerical_state="indeterminate",
            constraint_violations={"balance_node": []},
            authority="wmlfadj_provisional",
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        control_snapshot.restore(config.project.temp_path)
        resolve_active_baseline(plan, config.project.temp_path, resolved_id)
        control["rollback_status"] = "byte_exact_restored"
        control_snapshot.mark("baseline_control_archived_and_restored")
    except BaseException as exc:
        control["rollback_status"] = "rollback_failed"
        control["label"] = "INVALID_OR_INDETERMINATE"
        control["rollback_error"] = f"{type(exc).__name__}: {exc}"
    control["passed"] = (
        control.get("label") == "CONVERGED_FEASIBLE"
        and control.get("rollback_status") == "byte_exact_restored"
    )
    (control_snapshot.run_dir / "sample_result.json").write_text(
        json.dumps(control, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if not control["passed"]:
        summary = {
            "schema_version": 2,
            "plan_id": plan["plan_id"],
            "baseline_id": resolved_id,
            "status": "aborted_baseline_control_failure",
            "baseline_validation": validation,
            "baseline_control": control,
            "batch_root": str(batch_root),
            "sample_count": 0,
            "labels": {},
            "results": [],
        }
        (batch_root / "batch_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary
    if control_only:
        summary = {
            "schema_version": 2,
            "plan_id": plan["plan_id"],
            "baseline_id": resolved_id,
            "status": "baseline_control_passed",
            "baseline_validation": validation,
            "baseline_control": control,
            "batch_root": str(batch_root),
            "sample_count": 0,
            "labels": {},
            "results": [],
        }
        (batch_root / "batch_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return summary
    for raw in selected:
        resolve_active_baseline(plan, config.project.temp_path, resolved_id)
        actions = sample_actions(raw)
        candidate = RecoveryCandidate(
            candidate_id=f"PERTURB_{raw['sample_id']}",
            sample_id=str(raw["sample_id"]),
            actions=actions,
        )
        snapshot = PersistentSnapshot.create(
            config.project.temp_path,
            batch_root,
            candidate,
            backup_actions=backup_actions,
            include_results=True,
            extra_input_names=snapshot_input_names,
        )
        payload: dict[str, Any] = {
            "sample_id": raw["sample_id"],
            "baseline_id": resolved_id,
            "factor": raw["factor"],
            "factor_family": raw.get("factor_family", raw["factor"]),
            "severity": raw.get("severity", "unknown"),
            "description": raw.get("description", ""),
            "atomic_action_group": bool(raw.get("atomic_action_group", False)),
            "adaptive_search": raw.get("adaptive_search"),
            "changes": [action.to_dict() for action in actions],
            "run_dir": str(snapshot.run_dir),
            "rollback_status": "pending",
            "dataset_eligible": False,
        }
        try:
            apply_temp_actions(config.project.temp_path, actions)
            snapshot.mark("perturbation_applied")
            result_baseline = capture_wmlfadj_baseline(config.project.temp_path)
            outcome = run_legacy_wmlfadj(
                executable,
                config.project.temp_path,
                timeout,
            )
            verification = verify_wmlfadj_result(
                config.project.temp_path,
                result_baseline,
                read_active_temp_case(config.project.temp_path).tolerance,
                outcome,
                config.verification.timestamp_slack_seconds,
            )
            _copy_results(config.project.temp_path, snapshot.run_dir)
            payload.update(
                outcome=outcome.__dict__,
                verification=verification.to_dict(),
                **_classification(verification, outcome),
            )
        except BaseException as exc:
            payload.update(
                label="INVALID_OR_INDETERMINATE",
                execution_status="exception",
                result_freshness="indeterminate",
                numerical_state="indeterminate",
                constraint_violations={"balance_node": []},
                authority="wmlfadj_provisional",
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            snapshot.restore(config.project.temp_path)
            resolve_active_baseline(plan, config.project.temp_path, resolved_id)
            payload["rollback_status"] = "byte_exact_restored"
            payload["dataset_eligible"] = (
                payload.get("label") != "INVALID_OR_INDETERMINATE"
                and payload.get("result_freshness") == "fresh"
            )
            snapshot.mark("sample_archived_and_baseline_restored")
        except BaseException as exc:
            payload["rollback_status"] = "rollback_failed"
            payload["dataset_eligible"] = False
            payload["label"] = "INVALID_OR_INDETERMINATE"
            payload["rollback_error"] = f"{type(exc).__name__}: {exc}"
            batch_status = "aborted_rollback_failure"
        (snapshot.run_dir / "sample_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append(payload)
        if batch_status != "completed":
            break
    summary = {
        "schema_version": 2,
        "plan_id": plan["plan_id"],
        "baseline_id": resolved_id,
        "status": batch_status,
        "baseline_validation": validation,
        "baseline_control": control,
        "batch_root": str(batch_root),
        "sample_count": len(results),
        "labels": {
            label: sum(item["label"] == label for item in results)
            for label in sorted({str(item["label"]) for item in results})
        },
        "results": results,
    }
    (batch_root / "batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def _adaptive_load_templates(
    plan: dict[str, Any],
    baseline_id: str,
) -> list[dict[str, Any]]:
    """Recover one immutable P/Q template for each adaptive search."""

    templates: dict[str, dict[str, Any]] = {}
    signatures: dict[str, tuple[tuple[object, ...], ...]] = {}
    for raw in _samples_for_baseline(plan, baseline_id):
        metadata = raw.get("adaptive_search")
        if not isinstance(metadata, dict) or not metadata.get("search_id"):
            continue
        if raw.get("factor") != "load_pq_increase":
            raise ValueError(
                f"adaptive sample {raw.get('sample_id')} is not a load P/Q increase"
            )
        search_id = str(metadata["search_id"])
        template = copy.deepcopy(raw)
        template["sample_id"] = f"{search_id}_TEMPLATE"
        template_metadata = template["adaptive_search"]
        for key in (
            "increase_fraction",
            "phase",
            "ordinary_gui_label_required",
        ):
            template_metadata.pop(key, None)
        for change in template.get("changes", []):
            change["after"] = change["before"]
        signature = tuple(
            sorted(
                (
                    change.get("table"),
                    change.get("record_id"),
                    change.get("device_name"),
                    change.get("field"),
                    change.get("before"),
                )
                for change in template.get("changes", [])
            )
        )
        if search_id in signatures and signatures[search_id] != signature:
            raise ValueError(f"adaptive search {search_id} has conflicting templates")
        signatures[search_id] = signature
        templates.setdefault(search_id, template)
    return [templates[key] for key in sorted(templates)]


def _single_probe_plan(
    plan: dict[str, Any],
    baseline_id: str,
    probe: dict[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    result["baselines"] = [
        item
        for item in result["baselines"]
        if str(item["baseline_id"]) == baseline_id
    ]
    result["samples"] = [copy.deepcopy(probe)]
    return result


def run_adaptive_load_searches(
    plan: dict[str, Any],
    config_path: str | Path,
    *,
    baseline_id: str | None = None,
    timeout: float = 120.0,
    max_indeterminate_retries: int = 2,
    max_probe_runs_per_search: int = 100,
) -> dict[str, Any]:
    """Run all adaptive load searches with a guarded control before every probe.

    The automatic labels remain provisional. Each child run uses the normal
    persistent snapshot and exact rollback path, so a failed baseline control or
    rollback stops further probing.
    """

    if int(plan.get("schema_version", 0)) != 2:
        raise ValueError("adaptive load search requires a schema v2 plan")
    if max_indeterminate_retries < 0:
        raise ValueError("max_indeterminate_retries must not be negative")
    if max_probe_runs_per_search < 1:
        raise ValueError("max_probe_runs_per_search must be positive")
    config = load_config(config_path)
    baseline = resolve_active_baseline(
        plan,
        config.project.temp_path,
        baseline_id,
    )
    resolved_id = str(baseline["baseline_id"])
    templates = _adaptive_load_templates(plan, resolved_id)
    if not templates:
        raise ValueError(f"baseline {resolved_id} has no adaptive load searches")

    stem = (
        f"ADAPTIVE_{plan['plan_id']}_{resolved_id}_"
        f"{datetime.now():%Y%m%d_%H%M%S}"
    )
    aggregate_root = config.project.run_root / stem
    suffix = 1
    while aggregate_root.exists():
        suffix += 1
        aggregate_root = config.project.run_root / f"{stem}_{suffix}"
    aggregate_root.mkdir(parents=True)
    summary_path = aggregate_root / "adaptive_summary.json"
    aggregate: dict[str, Any] = {
        "schema_version": 2,
        "plan_id": plan["plan_id"],
        "baseline_id": resolved_id,
        "status": "running",
        "authority": "wmlfadj_provisional",
        "ordinary_gui_confirmation_required": True,
        "aggregate_root": str(aggregate_root),
        "search_count": len(templates),
        "searches": [],
    }

    def persist() -> None:
        summary_path.write_text(
            json.dumps(aggregate, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    persist()
    stop_all = False
    indeterminate_search = False
    for template in templates:
        search_id = str(template["adaptive_search"]["search_id"])
        search: dict[str, Any] = {
            "search_id": search_id,
            "status": "running",
            "observations": [],
            "attempts": [],
            "decision": None,
        }
        aggregate["searches"].append(search)
        observations: list[LoadProbe] = []
        invalid_attempts: dict[float, int] = {}
        for _ in range(max_probe_runs_per_search):
            decision = decide_next_load_probe(
                observations,
                baseline_control_passed=True,
                config=config.adaptive_load,
            )
            search["decision"] = decision.to_dict()
            if decision.next_increase is None:
                search["status"] = decision.phase
                break
            increase = float(decision.next_increase)
            probe = materialize_adaptive_load_probe(
                template,
                increase,
                phase=decision.phase,
            )
            child = run_generated_samples(
                _single_probe_plan(plan, resolved_id, probe),
                config_path,
                baseline_id=resolved_id,
                timeout=timeout,
            )
            control_passed = bool(child.get("baseline_control", {}).get("passed"))
            attempt: dict[str, Any] = {
                "sample_id": probe["sample_id"],
                "increase_fraction": increase,
                "phase": decision.phase,
                "batch_status": child.get("status"),
                "batch_root": child.get("batch_root"),
                "baseline_control_passed": control_passed,
            }
            search["attempts"].append(attempt)
            if not control_passed:
                search["status"] = "blocked_baseline_control_failure"
                search["decision"] = {
                    "phase": "blocked_baseline",
                    "next_increase": None,
                }
                aggregate["status"] = "aborted_baseline_control_failure"
                stop_all = True
                persist()
                break
            results = child.get("results", [])
            if len(results) != 1:
                search["status"] = "invalid_child_result"
                aggregate["status"] = "aborted_invalid_child_result"
                stop_all = True
                persist()
                break
            result = results[0]
            label = str(result["label"])
            observation = {
                "sample_id": result["sample_id"],
                "increase_fraction": increase,
                "label": label,
                "authority": result.get("authority"),
                "dataset_eligible": bool(result.get("dataset_eligible")),
                "rollback_status": result.get("rollback_status"),
                "run_dir": result.get("run_dir"),
            }
            search["observations"].append(observation)
            attempt.update(observation)
            observations.append(LoadProbe(increase, label))
            if result.get("rollback_status") != "byte_exact_restored":
                search["status"] = "aborted_rollback_failure"
                aggregate["status"] = "aborted_rollback_failure"
                stop_all = True
                persist()
                break
            if label == "INVALID_OR_INDETERMINATE":
                level = round(increase, 10)
                invalid_attempts[level] = invalid_attempts.get(level, 0) + 1
                if invalid_attempts[level] > max_indeterminate_retries:
                    search["status"] = "indeterminate_retry_exhausted"
                    search["decision"] = {
                        "phase": "indeterminate_retry_exhausted",
                        "next_increase": None,
                        "increase_fraction": increase,
                        "attempt_count": invalid_attempts[level],
                    }
                    indeterminate_search = True
                    persist()
                    break
            persist()
        else:
            search["status"] = "probe_run_limit_reached"
            search["decision"] = {
                "phase": "probe_run_limit_reached",
                "next_increase": None,
                "limit": max_probe_runs_per_search,
            }
            aggregate["status"] = "aborted_probe_run_limit"
            stop_all = True
        persist()
        if stop_all:
            break

    if aggregate["status"] == "running":
        aggregate["status"] = (
            "completed_with_indeterminate_searches"
            if indeterminate_search
            else "completed"
        )
    aggregate["probe_run_count"] = sum(
        len(item["attempts"]) for item in aggregate["searches"]
    )
    aggregate["summary_path"] = str(summary_path)
    persist()
    return aggregate


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate or run safe multi-baseline PSASP training samples"
    )
    parser.add_argument("--config", required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--generated-plan")
    modes.add_argument("--generate-plan")
    parser.add_argument("--baseline-id")
    parser.add_argument("--plan-id")
    parser.add_argument("--source-case-no", type=int)
    parser.add_argument("--source-case-name")
    parser.add_argument("--max-per-family", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("--start-at", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--control-only", action="store_true")
    parser.add_argument("--adaptive-search", action="store_true")
    parser.add_argument("--max-indeterminate-retries", type=int, default=2)
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.generate_plan:
        output = Path(args.generate_plan)
        if args.append and args.force:
            parser.error("--append and --force cannot be used together")
        if output.exists() and not (args.force or args.append):
            parser.error(f"generated-plan output already exists: {output}")
        baseline_id = args.baseline_id or f"TEMP_{datetime.now():%Y%m%d_%H%M%S}"
        existing: dict[str, Any] | None = None
        if args.append:
            if not output.is_file():
                parser.error(f"cannot append because the plan does not exist: {output}")
            existing = load_generated_plan(output)
            if int(existing["schema_version"]) != 2:
                parser.error("--append requires a schema v2 plan")
            if any(
                str(item["baseline_id"]) == baseline_id
                for item in existing["baselines"]
            ):
                parser.error(f"baseline ID already exists: {baseline_id}")
        plan_id = (
            str(existing["plan_id"])
            if existing is not None
            else args.plan_id or f"YAHU_MULTI_{datetime.now():%Y%m%d}"
        )
        generated = generate_plan_from_temp(
            config.project.temp_path,
            plan_id=plan_id,
            baseline_id=baseline_id,
            source_case_no=args.source_case_no,
            source_case_name=args.source_case_name,
            max_per_family=args.max_per_family,
            adaptive_load=config.adaptive_load,
        )
        if existing is not None:
            generated_fingerprints = generated["baselines"][0]["card_fingerprints"]
            duplicate = next(
                (
                    item
                    for item in existing["baselines"]
                    if item.get("card_fingerprints") == generated_fingerprints
                ),
                None,
            )
            if duplicate is not None:
                parser.error(
                    "current Temp input cards are already registered as baseline "
                    f"{duplicate['baseline_id']!r}; refusing a duplicate baseline"
                )
        if existing is None:
            plan = generated
        else:
            plan = existing
            plan["baselines"].extend(generated["baselines"])
            plan["samples"].extend(generated["samples"])
            plan["updated_at"] = datetime.now().isoformat(timespec="seconds")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        result = {
            "mode": "generate_plan",
            "output": str(output),
            "plan_id": plan["plan_id"],
            "baseline_id": baseline_id,
            "added_sample_count": len(generated["samples"]),
            "total_sample_count": len(plan["samples"]),
            "baseline_count": len(plan["baselines"]),
            "factor_counts": {
                factor: sum(item["factor"] == factor for item in plan["samples"])
                for factor in sorted({item["factor"] for item in plan["samples"]})
            },
            "wmlfadj_started": False,
        }
    else:
        plan = load_generated_plan(args.generated_plan)
        baseline = resolve_active_baseline(
            plan,
            config.project.temp_path,
            args.baseline_id,
        )
        count = len(_samples_for_baseline(plan, str(baseline["baseline_id"])))
        if args.start_at < 1 or args.start_at > count:
            parser.error("--start-at is outside the selected baseline sample plan")
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be positive")
        if args.adaptive_search and not args.run:
            parser.error("--adaptive-search requires --run")
        if args.adaptive_search and args.control_only:
            parser.error("--adaptive-search cannot be combined with --control-only")
        if args.adaptive_search and (args.start_at != 1 or args.limit is not None):
            parser.error("--adaptive-search cannot be sliced with --start-at/--limit")
        if args.run and args.adaptive_search:
            result = run_adaptive_load_searches(
                plan,
                args.config,
                baseline_id=args.baseline_id,
                timeout=args.timeout,
                max_indeterminate_retries=args.max_indeterminate_retries,
            )
        elif args.run:
            result = run_generated_samples(
                plan,
                args.config,
                baseline_id=args.baseline_id,
                start_at=args.start_at,
                limit=args.limit,
                timeout=args.timeout,
                control_only=args.control_only,
            )
        else:
            result = {
                "mode": "dry_run",
                **validate_plan_against_temp(
                    plan,
                    config.project.temp_path,
                    baseline_id=args.baseline_id,
                ),
                "wmlfadj_started": False,
            }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


__all__ = [
    "load_generated_plan",
    "main",
    "resolve_active_baseline",
    "run_adaptive_load_searches",
    "run_generated_samples",
    "sample_actions",
    "validate_plan_against_temp",
]
