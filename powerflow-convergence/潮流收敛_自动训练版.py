"""Run one of the three labeled recovery rules through wmlfadj.exe."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from powerflow.convergence.automatic import candidate_from_catalog
from powerflow.convergence.config import load_config
from powerflow.convergence.temp_executor import (
    TempRecoveryService,
    read_active_temp_case,
)
from powerflow.convergence.training import TrainingCatalog


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sample-seeded PSASP Temp recovery with automatic wmlfadj.exe execution"
    )
    parser.add_argument("--config", required=True, help="TOML configuration")
    parser.add_argument("--catalog", default="samples/fengcheng08_training.json")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--sample", help="included training sample_id")
    target.add_argument("--restore-run", help="restore LF input files from a previous run_dir")
    parser.add_argument("--exe", help="wmlfadj.exe path; defaults to project.psasp_path")
    parser.add_argument("--temp", help="Temp path; defaults to project.temp_path")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--run",
        action="store_true",
        help="modify Temp input files and launch wmlfadj.exe; omit for preview",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    catalog = TrainingCatalog.from_json(args.catalog)
    temp_path = Path(args.temp) if args.temp else config.project.temp_path
    executable = Path(args.exe) if args.exe else config.project.psasp_path / "wmlfadj.exe"
    service = TempRecoveryService(
        catalog=catalog,
        temp_path=temp_path,
        executable=executable,
        run_root=config.project.run_root,
        timeout_seconds=args.timeout,
        verification=config.verification,
    )

    if args.restore_run:
        if not args.run:
            _print(
                {
                    "mode": "restore_preview",
                    "run_dir": args.restore_run,
                    "temp_path": str(temp_path),
                    "warning": "add --run to restore the byte-exact LF input backup",
                }
            )
            return 0
        restored = service.restore_run(args.restore_run)
        _print({"status": "restored", "run_dir": str(restored), "temp_path": str(temp_path)})
        return 0

    assert args.sample is not None
    if not args.run:
        candidate = candidate_from_catalog(catalog, args.sample)
        active = read_active_temp_case(temp_path)
        sample = next(item for item in catalog.samples if item.sample_id == args.sample)
        expected = {"case_no": sample.case_no, "case_name": sample.case_name}
        actual = {
            "case_no": active.identity.case_no,
            "case_name": active.identity.case_name,
            "tolerance": active.tolerance,
        }
        _print(
            {
                "mode": "dry_run",
                "sample_id": args.sample,
                "candidate": candidate.to_dict(),
                "expected_temp_case": expected,
                "active_temp_case": actual,
                "case_matches": (
                    active.identity.case_no == sample.case_no
                    and active.identity.case_name == sample.case_name
                ),
                "temp_path": str(temp_path),
                "executable": str(executable),
                "warning": "--run edits LF.L5/LF.ML4, launches wmlfadj.exe, and rolls back on failure",
            }
        )
        return 0

    result = service.run_sample(args.sample, execute=True)
    _print(result.to_dict())
    return 0 if result.assessment is not None and result.assessment.result.converged else 2


if __name__ == "__main__":
    raise SystemExit(main())
