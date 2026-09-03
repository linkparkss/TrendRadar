"""Generate an offline calibration profile; never runs PSASP."""

from __future__ import annotations

import argparse
import json

from powerflow.convergence.training import calibrate_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a convergence-sample calibration profile")
    parser.add_argument("--catalog", default="samples/fengcheng08_training.json")
    parser.add_argument("--output", default="samples/fengcheng08_training_profile.json")
    args = parser.parse_args()
    profile = calibrate_catalog(args.catalog, args.output)
    print(json.dumps(profile, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
