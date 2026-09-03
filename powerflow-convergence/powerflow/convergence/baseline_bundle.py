"""Capture, verify, materialize, and roll back exact PSASP Temp baselines."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .automatic import RecoveryCandidate
from .sample_factory import INPUT_CARDS, card_fingerprints
from .temp_executor import PersistentSnapshot, read_active_temp_case


_RESULT_ARTIFACTS = (
    "LF.CAL",
    "LFCAL.LIS",
    "LF.LP1",
    "lfreport.lis",
    "LFERR.LIS",
    "LF.adj",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def _require_safe_id(value: str) -> str:
    if not _SAFE_ID.fullmatch(value):
        raise ValueError("baseline_id must use 1-96 safe ASCII characters")
    return value


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fingerprint_bytes(content: bytes) -> dict[str, Any]:
    return {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}


def capture_baseline_bundle(
    temp_path: str | Path,
    bundle_root: str | Path,
    *,
    baseline_id: str,
    source_case_no: int | None = None,
    source_case_name: str | None = None,
    psasp_closed_confirmed: bool,
) -> dict[str, Any]:
    """Capture one stable, exact six-card baseline without overwriting bundles."""

    if not psasp_closed_confirmed:
        raise RuntimeError("baseline capture requires explicit PSASP-closed confirmation")
    baseline_id = _require_safe_id(baseline_id)
    temp_root = Path(temp_path).resolve()
    target_root = Path(bundle_root).resolve()
    target = target_root / baseline_id
    if target.is_relative_to(temp_root):
        raise ValueError("baseline bundles must be stored outside active Temp")
    if target.exists():
        raise FileExistsError(f"baseline bundle already exists: {target}")
    target_root.mkdir(parents=True, exist_ok=True)
    staging = target_root / f".{baseline_id}.capturing-{uuid.uuid4().hex}"
    cards = staging / "cards"
    cards.mkdir(parents=True)
    try:
        active = read_active_temp_case(temp_root)
        before = card_fingerprints(temp_root)
        for name in INPUT_CARDS:
            shutil.copy2(temp_root / name, cards / name)
        after = card_fingerprints(temp_root)
        if before != after:
            raise RuntimeError("active Temp changed while the baseline was being captured")
        copied = card_fingerprints(cards)
        if copied != before:
            raise RuntimeError("captured baseline card checksums do not match active Temp")
        manifest = {
            "schema_version": 1,
            "baseline_id": baseline_id,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "source_case_no": source_case_no,
            "source_case_name": source_case_name,
            "active_temp_case": active.identity.to_dict(),
            "tolerance": active.tolerance,
            "card_fingerprints": copied,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        staging.replace(target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {**manifest, "bundle_path": str(target)}


def verify_baseline_bundle(bundle_path: str | Path) -> dict[str, Any]:
    """Verify manifest structure and every bundled input card."""

    root = Path(bundle_path).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"baseline manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(manifest.get("schema_version", 0)) != 1:
        raise ValueError("unsupported baseline-bundle schema")
    _require_safe_id(str(manifest.get("baseline_id", "")))
    expected = manifest.get("card_fingerprints")
    if not isinstance(expected, dict) or set(expected) != set(INPUT_CARDS):
        raise ValueError("baseline manifest does not cover the exact input-card set")
    cards = root / "cards"
    actual = card_fingerprints(cards)
    if actual != expected:
        changed = [name for name in INPUT_CARDS if actual.get(name) != expected.get(name)]
        raise RuntimeError("baseline bundle checksum mismatch: " + ", ".join(changed))
    active = read_active_temp_case(cards)
    if active.identity.to_dict() != manifest.get("active_temp_case"):
        raise RuntimeError("baseline bundle Temp identity does not match its manifest")
    return {**manifest, "bundle_path": str(root)}


def materialize_baseline_bundle(
    bundle_path: str | Path,
    temp_path: str | Path,
    run_root: str | Path,
    *,
    psasp_closed_confirmed: bool,
) -> dict[str, Any]:
    """Switch active Temp to a verified bundle with recoverable byte-exact rollback."""

    if not psasp_closed_confirmed:
        raise RuntimeError("baseline materialization requires explicit PSASP-closed confirmation")
    manifest = verify_baseline_bundle(bundle_path)
    bundle = Path(str(manifest["bundle_path"]))
    temp_root = Path(temp_path).resolve()
    before = card_fingerprints(temp_root)
    candidate = RecoveryCandidate(
        candidate_id=f"MATERIALIZE_{manifest['baseline_id']}",
        sample_id=f"BASELINE_SWITCH_{manifest['baseline_id']}",
        actions=(),
    )
    snapshot = PersistentSnapshot.create(
        temp_root,
        Path(run_root),
        candidate,
        include_results=True,
        extra_input_names=INPUT_CARDS,
    )
    try:
        for name in INPUT_CARDS:
            _atomic_write_bytes(temp_root / name, (bundle / "cards" / name).read_bytes())
        for name in _RESULT_ARTIFACTS:
            (temp_root / name).unlink(missing_ok=True)
        actual = card_fingerprints(temp_root)
        if actual != manifest["card_fingerprints"]:
            raise RuntimeError("materialized Temp does not match the baseline bundle")
        active = read_active_temp_case(temp_root)
        if active.identity.to_dict() != manifest["active_temp_case"]:
            raise RuntimeError("materialized Temp identity does not match the bundle")
        snapshot.manifest["baseline_switch"] = {
            "baseline_id": manifest["baseline_id"],
            "bundle_path": str(bundle),
            "before_card_fingerprints": before,
            "after_card_fingerprints": actual,
        }
        snapshot.mark("baseline_materialized")
    except BaseException as exc:
        try:
            snapshot.restore(temp_root)
        except BaseException as rollback_exc:
            raise RuntimeError(
                f"baseline materialization failed and rollback failed: {rollback_exc}"
            ) from exc
        raise
    return {
        "status": "baseline_materialized",
        "baseline_id": manifest["baseline_id"],
        "active_temp_case": manifest["active_temp_case"],
        "card_fingerprints": manifest["card_fingerprints"],
        "switch_dir": str(snapshot.run_dir),
        "rollback_available": True,
    }


def restore_materialized_baseline(
    switch_dir: str | Path,
    temp_path: str | Path,
    *,
    psasp_closed_confirmed: bool,
) -> dict[str, Any]:
    """Restore the exact inputs and result set saved before a baseline switch."""

    if not psasp_closed_confirmed:
        raise RuntimeError("baseline restore requires explicit PSASP-closed confirmation")
    snapshot = PersistentSnapshot.open(switch_dir)
    if snapshot.manifest.get("status") != "baseline_materialized":
        raise RuntimeError("switch snapshot is not in baseline_materialized state")
    expected = snapshot.manifest.get("baseline_switch", {}).get(
        "before_card_fingerprints"
    )
    snapshot.restore(temp_path)
    actual = card_fingerprints(temp_path)
    if actual != expected:
        raise RuntimeError("restored Temp does not match its pre-switch fingerprints")
    return {
        "status": "rolled_back",
        "switch_dir": str(snapshot.run_dir),
        "card_fingerprints": actual,
        "active_temp_case": read_active_temp_case(temp_path).identity.to_dict(),
    }



def main(argv: Sequence[str] | None = None) -> int:
    """Command line entry for deliberate, operator-confirmed baseline handling."""

    import argparse

    from .config import load_config

    parser = argparse.ArgumentParser(description="Verified PSASP Temp baseline bundles")
    commands = parser.add_subparsers(dest="command", required=True)

    capture = commands.add_parser("capture")
    capture.add_argument("--config", required=True)
    capture.add_argument("--bundle-root", required=True)
    capture.add_argument("--baseline-id", required=True)
    capture.add_argument("--source-case-no", type=int)
    capture.add_argument("--source-case-name")
    capture.add_argument("--confirm-psasp-closed", action="store_true")

    verify = commands.add_parser("verify")
    verify.add_argument("--bundle", required=True)

    materialize = commands.add_parser("materialize")
    materialize.add_argument("--config", required=True)
    materialize.add_argument("--bundle", required=True)
    materialize.add_argument("--confirm-psasp-closed", action="store_true")

    restore = commands.add_parser("restore")
    restore.add_argument("--config", required=True)
    restore.add_argument("--switch-dir", required=True)
    restore.add_argument("--confirm-psasp-closed", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "verify":
        result = verify_baseline_bundle(args.bundle)
    else:
        if not args.confirm_psasp_closed:
            parser.error(f"{args.command} requires --confirm-psasp-closed")
        config = load_config(args.config)
        if args.command == "capture":
            result = capture_baseline_bundle(
                config.project.temp_path,
                args.bundle_root,
                baseline_id=args.baseline_id,
                source_case_no=args.source_case_no,
                source_case_name=args.source_case_name,
                psasp_closed_confirmed=True,
            )
        elif args.command == "materialize":
            result = materialize_baseline_bundle(
                args.bundle,
                config.project.temp_path,
                config.project.run_root,
                psasp_closed_confirmed=True,
            )
        else:
            result = restore_materialized_baseline(
                args.switch_dir,
                config.project.temp_path,
                psasp_closed_confirmed=True,
            )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
__all__ = [
    "main",
    "capture_baseline_bundle",
    "materialize_baseline_bundle",
    "restore_materialized_baseline",
    "verify_baseline_bundle",
]


if __name__ == "__main__":
    raise SystemExit(main())
