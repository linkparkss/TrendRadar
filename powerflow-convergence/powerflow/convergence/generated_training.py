"""Controlled perturbation runner for automatically generated training samples."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .automatic import FieldAction, RecoveryCandidate
from .config import load_config
from .temp_executor import (
    PersistentSnapshot,
    apply_temp_actions,
    read_active_temp_case,
    run_legacy_wmlfadj,
)
from .wmlfadj_result import capture_wmlfadj_baseline, verify_wmlfadj_result


def load_generated_plan(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("unsupported generated-plan schema")
    samples = payload.get("samples")
    if not isinstance(samples, list) or not samples:
        raise ValueError("generated plan has no samples")
    ids = [str(item.get("sample_id", "")) for item in samples]
    if any(not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("generated plan contains empty or duplicate sample IDs")
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_plan_against_temp(
    plan: dict[str, Any],
    temp_path: str | Path,
) -> dict[str, Any]:
    root = Path(temp_path)
    active = read_active_temp_case(root)
    expected_name = str(plan["baseline"].get("temp_case_name", ""))
    if active.identity.case_name != expected_name:
        raise RuntimeError(
            f"active Temp case {active.identity.case_name!r} is not {expected_name!r}"
        )
    cards: set[Path] = set()
    samples = []
    for raw in plan["samples"]:
        actions = sample_actions(raw)
        candidate = RecoveryCandidate(
            candidate_id=f"PERTURB_{raw['sample_id']}",
            sample_id=str(raw["sample_id"]),
            actions=actions,
        )
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory)
            for action in actions:
                if action.table.startswith("ls2_"):
                    card = "LF.L2"
                elif action.table.startswith("ls3_"):
                    card = "LF.L3"
                elif action.table.startswith("ls5_"):
                    card = "LF.L5"
                elif action.table.startswith("ls6_"):
                    card = "LF.L6"
                elif action.table.startswith("ls_lcc_"):
                    card = "LF.ML4"
                else:
                    raise ValueError(f"unsupported plan table: {action.table}")
                source = root / card
                if not source.is_file():
                    raise FileNotFoundError(source)
                cards.add(source)
                target = trial / card
                if not target.exists():
                    shutil.copyfile(source, target)
            apply_temp_actions(trial, actions)
            apply_temp_actions(trial, _inverse(actions))
            for source in cards:
                target = trial / source.name
                if target.exists() and target.read_bytes() != source.read_bytes():
                    raise RuntimeError(
                        f"sample {raw['sample_id']} failed byte-exact rollback check"
                    )
        samples.append(candidate.to_dict())
    return {
        "plan_id": plan["plan_id"],
        "active_temp_case": active.identity.to_dict(),
        "sample_count": len(samples),
        "cards": {
            path.name: {"size": path.stat().st_size, "sha256": _sha256(path)}
            for path in sorted(cards)
        },
        "samples": samples,
    }


def _copy_results(temp_path: Path, run_dir: Path) -> None:
    for name in ("LF.CAL", "LF.LP1", "LFCAL.LIS", "lfreport.lis", "LFERR.LIS", "LF.adj"):
        source = temp_path / name
        if source.is_file():
            shutil.copyfile(source, run_dir / name)


def run_generated_samples(
    plan: dict[str, Any],
    config_path: str | Path,
    *,
    start_at: int = 1,
    limit: int | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    config = load_config(config_path)
    validation = validate_plan_against_temp(plan, config.project.temp_path)
    executable = config.project.psasp_path / "wmlfadj.exe"
    if not executable.is_file():
        raise FileNotFoundError(executable)
    selected = list(plan["samples"])[start_at - 1 :]
    if limit is not None:
        selected = selected[:limit]
    batch_root = (
        config.project.run_root
        / f"GENERATED_{plan['plan_id']}_{datetime.now():%Y%m%d_%H%M%S}"
    )
    batch_root.mkdir(parents=True)
    results = []
    for raw in selected:
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
        )
        payload: dict[str, Any] = {
            "sample_id": raw["sample_id"],
            "factor": raw["factor"],
            "description": raw.get("description", ""),
            "atomic_action_group": bool(raw.get("atomic_action_group", False)),
            "changes": [action.to_dict() for action in actions],
            "run_dir": str(snapshot.run_dir),
        }
        try:
            apply_temp_actions(config.project.temp_path, actions)
            snapshot.mark("perturbation_applied")
            baseline = capture_wmlfadj_baseline(config.project.temp_path)
            outcome = run_legacy_wmlfadj(
                executable,
                config.project.temp_path,
                timeout,
            )
            verification = verify_wmlfadj_result(
                config.project.temp_path,
                baseline,
                read_active_temp_case(config.project.temp_path).tolerance,
                outcome,
                config.verification.timestamp_slack_seconds,
            )
            _copy_results(config.project.temp_path, snapshot.run_dir)
            payload.update(
                outcome=outcome.__dict__,
                verification=verification.to_dict(),
                label=(
                    "converged_without_balance_limit_violation"
                    if verification.converged
                    else "nonconverged_or_limit_rejected"
                ),
            )
        except BaseException as exc:
            payload.update(label="execution_error", error=f"{type(exc).__name__}: {exc}")
        finally:
            snapshot.restore(config.project.temp_path)
            snapshot.mark("sample_archived_and_baseline_restored")
        (snapshot.run_dir / "sample_result.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        results.append(payload)
    summary = {
        "plan_id": plan["plan_id"],
        "baseline_validation": validation,
        "batch_root": str(batch_root),
        "sample_count": len(results),
        "labels": {
            label: sum(item["label"] == label for item in results)
            for label in sorted({item["label"] for item in results})
        },
        "results": results,
    }
    (batch_root / "batch_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generated PSASP training samples")
    parser.add_argument("--config", required=True)
    parser.add_argument("--generated-plan", required=True)
    parser.add_argument("--start-at", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    plan = load_generated_plan(args.generated_plan)
    config = load_config(args.config)
    if args.start_at < 1 or args.start_at > len(plan["samples"]):
        parser.error("--start-at is outside the sample plan")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.run:
        result = run_generated_samples(
            plan,
            args.config,
            start_at=args.start_at,
            limit=args.limit,
            timeout=args.timeout,
        )
    else:
        result = {
            "mode": "dry_run",
            **validate_plan_against_temp(plan, config.project.temp_path),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


# Keep the historical module path, but route every public entry through the
# fingerprinted multi-baseline implementation.
from .generated_training_v2 import (  # noqa: E402,F401
    load_generated_plan,
    main,
    resolve_active_baseline,
    run_generated_samples,
    sample_actions,
    validate_plan_against_temp,
)


__all__ = [
    "load_generated_plan",
    "main",
    "resolve_active_baseline",
    "run_generated_samples",
    "sample_actions",
    "validate_plan_against_temp",
]
