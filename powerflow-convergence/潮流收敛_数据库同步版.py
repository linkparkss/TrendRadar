"""Synchronize a learned recovery to the PSASP job database and run wmlfadj."""

from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path
from typing import Sequence

from powerflow.convergence.automatic import (
    AutomaticFieldRepository,
    RecoveryCandidate,
    candidate_from_catalog,
)
from powerflow.convergence.config import load_config
from powerflow.convergence.temp_executor import (
    PersistentSnapshot,
    read_active_temp_case,
    run_legacy_wmlfadj,
    synchronize_temp_candidate,
)
from powerflow.convergence.training import TrainingCatalog
from powerflow.convergence.wmlfadj_result import (
    capture_wmlfadj_baseline,
    parse_lf_adj_net,
    verify_wmlfadj_result,
)


def recovery_candidate_plan(
    candidate: RecoveryCandidate,
    search_subsets: bool,
    preferred_action_keys: Sequence[str] = (),
    rejected_action_key_sets: Sequence[Sequence[str]] = (),
) -> tuple[RecoveryCandidate, ...]:
    by_key = {action.key: action for action in candidate.actions}
    preferred_keys = tuple(preferred_action_keys)
    unknown_preferred = sorted(set(preferred_keys) - set(by_key))
    if unknown_preferred:
        raise ValueError(f"preferred recovery contains unknown actions: {unknown_preferred}")
    preferred = None
    if preferred_keys:
        preferred_set = set(preferred_keys)
        preferred = RecoveryCandidate(
            candidate_id=f"{candidate.candidate_id}_ORDINARY_CONFIRMED",
            sample_id=candidate.sample_id,
            actions=tuple(
                action for action in candidate.actions if action.key in preferred_set
            ),
        )
    if not search_subsets or len(candidate.actions) <= 1:
        return (preferred or candidate,)
    rejected_sets = {
        frozenset(keys) for keys in rejected_action_key_sets
    }
    plan = []
    for size in range(1, len(candidate.actions) + 1):
        for actions in itertools.combinations(candidate.actions, size):
            suffix = "_".join(
                f"{action.record_id}_{action.field}" for action in actions
            )
            if frozenset(action.key for action in actions) not in rejected_sets:
                plan.append(
                    RecoveryCandidate(
                        candidate_id=f"{candidate.candidate_id}_SUBSET_{suffix}",
                        sample_id=candidate.sample_id,
                        actions=tuple(actions),
                    )
                )
    if preferred is not None:
        preferred_set = frozenset(action.key for action in preferred.actions)
        plan = [
            item
            for item in plan
            if frozenset(action.key for action in item.actions) != preferred_set
        ]
        plan.insert(0, preferred)
    if not plan:
        raise ValueError("candidate plan is empty after applying ordinary-flow evidence")
    return tuple(plan)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Database-synchronized PSASP recovery")
    parser.add_argument("--config", required=True)
    parser.add_argument("--catalog", default="samples/fengcheng08_training.json")
    parser.add_argument("--sample", required=True)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--search-subsets", action="store_true")
    parser.add_argument("--start-at", type=int, default=1)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    catalog = TrainingCatalog.from_json(args.catalog)
    sample = next(
        sample for sample in catalog.samples if sample.sample_id == args.sample
    )
    base_candidate = candidate_from_catalog(catalog, args.sample)
    full_candidate_plan = recovery_candidate_plan(
        base_candidate,
        args.search_subsets,
        preferred_action_keys=sample.confirmed_recovery.get("action_keys", []),
        rejected_action_key_sets=[
            item.get("action_keys", [])
            for item in sample.rejected_recoveries
        ],
    )
    if args.start_at < 1 or args.start_at > len(full_candidate_plan):
        parser.error(
            f"--start-at must be between 1 and {len(full_candidate_plan)}"
        )
    candidates = full_candidate_plan[args.start_at - 1 :]
    active = read_active_temp_case(config.project.temp_path)
    executable = config.project.psasp_path / "wmlfadj.exe"
    preview = {
        "mode": "database_sync_wmlfadj" if args.run else "dry_run",
        "sample_id": args.sample,
        "active_temp_case": {
            "case_no": active.identity.case_no,
            "case_name": active.identity.case_name,
            "tolerance": active.tolerance,
        },
        "database_actions": [action.to_dict() for action in base_candidate.actions],
        "search_subsets": args.search_subsets,
        "start_at": args.start_at,
        "candidate_plan": [
            candidate.to_dict() for candidate in full_candidate_plan
        ],
        "active_candidate_plan": [
            candidate.to_dict() for candidate in candidates
        ],
        "ordinary_confirmed_recovery": sample.confirmed_recovery,
        "ordinary_rejected_recoveries": list(sample.rejected_recoveries),
        "executable": str(executable),
    }
    if not args.run:
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0
    if active.identity.case_name != "AI_Adjust_Case_Temp":
        raise RuntimeError("database synchronization requires AI_Adjust_Case_Temp")
    if not executable.is_file():
        raise FileNotFoundError(f"wmlfadj.exe does not exist: {executable}")

    repository = AutomaticFieldRepository(config.database)
    attempts = []
    for attempt_number, candidate in enumerate(candidates, start=1):
        snapshot = PersistentSnapshot.create(
            config.project.temp_path,
            config.project.run_root,
            candidate,
            backup_actions=base_candidate.actions,
        )
        database_applied = False
        temp_applied = False
        payload = dict(preview)
        payload.update(
            attempt_number=attempt_number,
            candidate=candidate.to_dict(),
            run_dir=str(snapshot.run_dir),
        )
        try:
            synchronize_temp_candidate(
                config.project.temp_path,
                base_candidate.actions,
                candidate.actions,
            )
            temp_applied = True
            snapshot.mark("temp_candidate_applied")
            repository.apply_field_actions(candidate.actions)
            database_applied = True
            snapshot.mark("database_recovery_applied")
            baseline = capture_wmlfadj_baseline(config.project.temp_path)
            outcome = run_legacy_wmlfadj(
                executable,
                config.project.temp_path,
                args.timeout,
            )
            verification = verify_wmlfadj_result(
                config.project.temp_path,
                baseline,
                active.tolerance,
                outcome,
                config.verification.timestamp_slack_seconds,
            )
            adjustments = parse_lf_adj_net(config.project.temp_path / "LF.adj")
            (snapshot.run_dir / "wmlfadj_adjustments.json").write_text(
                json.dumps(adjustments, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            payload.update(
                outcome=outcome.__dict__,
                verification=verification.to_dict(),
                learned_recovery=[action.to_dict() for action in candidate.actions],
                wmlfadj_net_adjustments=adjustments,
            )
            if not verification.definitive or not verification.converged:
                repository.restore_field_actions(candidate.actions)
                database_applied = False
                snapshot.restore(config.project.temp_path)
                temp_applied = False
                snapshot.mark(
                    "database_and_temp_rolled_back",
                    "wmlfadj result was not definitively converged",
                )
                payload.update(status="not_converged", database_rolled_back=True)
                attempts.append(payload)
                continue
            confirmed_keys = frozenset(
                sample.confirmed_recovery.get("action_keys", [])
            )
            candidate_keys = frozenset(
                action.key for action in candidate.actions
            )
            ordinary_confirmed = bool(confirmed_keys) and (
                candidate_keys == confirmed_keys
            )
            status = (
                "converged"
                if ordinary_confirmed
                else "provisional_converged"
            )
            snapshot.mark(status)
            payload.update(
                status=status,
                database_rolled_back=False,
                previous_attempts=attempts,
                ordinary_verification_required=not ordinary_confirmed,
                ordinary_verification_evidence=(
                    sample.confirmed_recovery
                    if ordinary_confirmed
                    else None
                ),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 0
        except BaseException as exc:
            rollback_errors = []
            if database_applied:
                try:
                    repository.restore_field_actions(candidate.actions)
                except BaseException as rollback_error:
                    rollback_errors.append(f"database: {rollback_error}")
            if temp_applied:
                try:
                    snapshot.restore(config.project.temp_path)
                except BaseException as rollback_error:
                    rollback_errors.append(f"Temp: {rollback_error}")
            if rollback_errors:
                snapshot.mark("rollback_failed", "; ".join(rollback_errors))
                raise RuntimeError(
                    "automatic recovery failed and rollback failed; "
                    f"run_dir={snapshot.run_dir}; "
                    + "; ".join(rollback_errors)
                ) from exc
            snapshot.mark("database_and_temp_rolled_back_error", str(exc))
            raise
    result = dict(preview)
    result.update(
        status="not_converged",
        database_rolled_back=True,
        attempts=attempts,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
