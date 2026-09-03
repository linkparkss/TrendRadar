"""Command-line entry point; it never launches PSASP or clicks its UI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import load_config
from .journal import load_journal
from .repository import PsaspMySqlRepository
from .service import ConvergenceService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m powerflow.convergence",
        description="Manual-PSASP, journaled load-flow convergence search",
    )
    parser.add_argument("--config", required=True, help="TOML configuration path")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("start", help="diagnose and create a candidate journal")
    for name in ("next", "verify", "rollback", "status"):
        command = sub.add_parser(name)
        command.add_argument("--run-dir", required=True)
        if name in ("verify", "rollback"):
            command.add_argument(
                "--psasp-closed",
                action="store_true",
                help="confirm PSASP is closed before any rollback can occur",
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "status":
            result = load_journal(Path(args.run_dir))
        else:
            service = ConvergenceService(config, PsaspMySqlRepository(config.database))
            if args.command == "start":
                result = service.start()
            elif args.command == "next":
                result = service.apply_next(args.run_dir)
            elif args.command == "verify":
                result = service.verify(args.run_dir, psasp_closed=args.psasp_closed)
            elif args.command == "rollback":
                result = service.rollback_current(
                    args.run_dir, psasp_closed=args.psasp_closed
                )
            else:
                raise RuntimeError(f"Unknown command: {args.command}")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
