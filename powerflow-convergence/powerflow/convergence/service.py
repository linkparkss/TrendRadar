"""Recoverable manual-PSASP convergence search state machine."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .assessment import VerificationAssessment, assess_ordinary_result
from .candidates import generate_candidates
from .config import AppConfig
from .diagnostics import diagnose_load_flow
from .journal import (
    baseline_from_journal,
    candidate_from_journal,
    create_journal,
    load_journal,
    save_journal,
)
from .models import Candidate, CaseIdentity, RunStatus
from .repository import StateRepository
from .verifier import capture_result_baseline


ROLLBACK_REQUIRED = "rollback_required"


def _run_id() -> str:
    return "PFCONV_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


class ConvergenceService:
    def __init__(self, config: AppConfig, repository: StateRepository) -> None:
        config.validate()
        self.config = config
        self.repository = repository

    @property
    def temp_path(self) -> Path:
        return self.config.project.temp_path

    def start(self) -> dict[str, Any]:
        case_status = self.repository.resolve_case(self.config.case)
        diagnosis = diagnose_load_flow(
            self.temp_path / "LFCAL.LIS",
            self.temp_path / "lfreport.lis",
        )
        if not diagnosis.nonconverged and case_status.calculate == 1:
            raise RuntimeError(
                "Precheck did not find a non-convergent target case; "
                "refusing to search against an apparently successful result"
            )
        devices = self.repository.discover_devices(case_status.identity, self.config.actions)
        candidates = generate_candidates(
            devices,
            diagnosis,
            self.config.actions,
            self.config.search,
        )
        if not candidates:
            raise RuntimeError(
                "No permitted Valid-only candidates were discovered. "
                "Check device tables, name encoding, and action policy."
            )
        root = Path(self.config.project.run_root)
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / _run_id()
        run_dir.mkdir()
        payload = create_journal(
            run_dir,
            run_id=run_dir.name,
            config=self.config.safe_dict(),
            case_status=case_status,
            diagnosis=diagnosis,
            devices=devices,
            candidates=candidates,
        )
        payload["run_dir"] = str(run_dir)
        save_journal(run_dir, payload)
        return {
            "run_dir": str(run_dir),
            "run_id": run_dir.name,
            "status": payload["status"],
            "case": case_status.to_dict(),
            "diagnosis": diagnosis.to_dict(),
            "device_count": len(devices),
            "candidate_count": len(candidates),
            "first_candidate": candidates[0].to_dict(),
        }

    @staticmethod
    def _identity(payload: dict[str, Any]) -> CaseIdentity:
        identity = payload["case_status"]["identity"]
        return CaseIdentity(case_no=int(identity["case_no"]), case_name=str(identity["case_name"]))

    @staticmethod
    def _candidate_payload(candidate: Candidate) -> dict[str, Any]:
        return {
            "candidate_id": candidate.candidate_id,
            "actions": [action.to_dict() for action in candidate.actions],
        }

    def _recover_applying(self, run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
        current = payload.get("current_attempt") or {}
        candidate = candidate_from_journal(payload, int(current.get("candidate_index", 0)))
        state = self.repository.candidate_state(candidate)
        if state == "before":
            self.repository.apply_candidate(candidate)
            state = "after"
        elif state == "mixed":
            self.repository.restore_candidate(candidate)
            payload["status"] = RunStatus.ERROR.value
            payload["error"] = "Crash recovery found a partially applied candidate; it was restored"
            save_journal(run_dir, payload)
            raise RuntimeError(payload["error"])
        payload["status"] = RunStatus.WAITING_FOR_MANUAL_RUN.value
        current["recovered"] = True
        current["applied_state"] = state
        payload["current_attempt"] = current
        save_journal(run_dir, payload)
        return {
            "status": payload["status"],
            "recovered": True,
            "candidate": self._candidate_payload(candidate),
            "instruction": self.config.executor.reload_instruction,
        }

    def apply_next(self, run_dir: str | Path) -> dict[str, Any]:
        root = Path(run_dir)
        payload = load_journal(root)
        status = payload.get("status")
        if status == RunStatus.APPLYING.value:
            return self._recover_applying(root, payload)
        if status == RunStatus.WAITING_FOR_MANUAL_RUN.value:
            raise RuntimeError(
                "The current candidate is already applied; run ordinary PSASP load flow "
                "and use verify"
            )
        if status == ROLLBACK_REQUIRED:
            raise RuntimeError("Rollback the rejected candidate before applying the next one")
        if status in (RunStatus.COMPLETED.value, RunStatus.EXHAUSTED.value):
            raise RuntimeError(f"Run is already terminal: {status}")
        if status == RunStatus.ERROR.value:
            raise RuntimeError("Run is in error state; inspect run.json before continuing")

        cursor = int(payload.get("candidate_cursor", 0))
        candidates = payload.get("candidates", [])
        if cursor >= len(candidates):
            payload["status"] = RunStatus.EXHAUSTED.value
            save_journal(root, payload)
            return {"status": payload["status"], "candidate_count": len(candidates)}
        candidate = candidate_from_journal(payload, cursor)
        state = self.repository.candidate_state(candidate)
        if state != "before":
            raise RuntimeError(
                f"Candidate {candidate.candidate_id} is {state}, expected before-state; "
                "inspect the database and journal"
            )

        baseline = capture_result_baseline(self.temp_path)
        attempt_number = len(payload.get("attempts", [])) + 1
        payload["status"] = RunStatus.APPLYING.value
        payload["current_attempt"] = {
            "attempt_number": attempt_number,
            "candidate_index": cursor,
            "candidate_id": candidate.candidate_id,
            "candidate": candidate.to_dict(),
            "baseline": baseline.to_dict(),
            "prepared_at": datetime.now().isoformat(timespec="microseconds"),
        }
        save_journal(root, payload)
        try:
            self.repository.apply_candidate(candidate)
        except BaseException as exc:
            payload["status"] = RunStatus.ERROR.value
            payload["error"] = f"Candidate application failed: {exc}"
            save_journal(root, payload)
            raise
        payload["status"] = RunStatus.WAITING_FOR_MANUAL_RUN.value
        payload["current_attempt"]["applied_at"] = datetime.now().isoformat(
            timespec="microseconds"
        )
        payload["current_attempt"]["applied_state"] = "after"
        save_journal(root, payload)
        return {
            "status": payload["status"],
            "attempt_number": attempt_number,
            "candidate": self._candidate_payload(candidate),
            "instruction": self.config.executor.reload_instruction,
        }

    def verify(self, run_dir: str | Path, psasp_closed: bool = False) -> dict[str, Any]:
        root = Path(run_dir)
        payload = load_journal(root)
        if payload.get("status") != RunStatus.WAITING_FOR_MANUAL_RUN.value:
            raise RuntimeError(
                "Verification requires status waiting_for_manual_run; "
                f"current status is {payload.get('status')!r}"
            )
        current = payload.get("current_attempt") or {}
        candidate = candidate_from_journal(payload, int(current.get("candidate_index", 0)))
        case_status = self.repository.get_case_status(self._identity(payload))
        assessment = assess_ordinary_result(
            self.temp_path,
            baseline_from_journal(payload),
            case_status.identity,
            case_status,
            self.config.verification,
        )
        current["last_verification"] = assessment.result.to_dict()
        current["definitive"] = assessment.definitive
        payload["current_attempt"] = current
        if not assessment.definitive:
            payload["status"] = RunStatus.WAITING_FOR_MANUAL_RUN.value
            save_journal(root, payload)
            return {
                "status": payload["status"],
                "definitive": False,
                "converged": False,
                "reasons": list(assessment.result.reasons),
                "instruction": "Result is stale, incomplete, or for another case; rerun the selected case and verify again.",
            }
        if assessment.result.converged:
            payload["status"] = RunStatus.COMPLETED.value
            payload["result"] = assessment.result.to_dict()
            payload["selected_candidate"] = candidate.to_dict()
            payload["current_attempt"] = current
            save_journal(root, payload)
            return {
                "status": payload["status"],
                "definitive": True,
                "converged": True,
                "candidate": self._candidate_payload(candidate),
                "result": assessment.result.to_dict(),
            }

        payload["status"] = ROLLBACK_REQUIRED
        payload["result"] = assessment.result.to_dict()
        payload["current_attempt"] = current
        save_journal(root, payload)
        response = {
            "status": payload["status"],
            "definitive": True,
            "converged": False,
            "candidate": self._candidate_payload(candidate),
            "reasons": list(assessment.result.reasons),
            "instruction": "Close PSASP after preserving the ordinary result, then run rollback before the next candidate.",
        }
        if psasp_closed:
            response.update(self.rollback_current(root, psasp_closed=True))
        return response

    def rollback_current(
        self, run_dir: str | Path, psasp_closed: bool = False
    ) -> dict[str, Any]:
        if not psasp_closed:
            raise RuntimeError(
                "Refusing database rollback until you confirm PSASP is closed"
            )
        root = Path(run_dir)
        payload = load_journal(root)
        if payload.get("status") != ROLLBACK_REQUIRED:
            raise RuntimeError(
                "Rollback is only valid after a definitive non-converged verification"
            )
        current = payload.get("current_attempt") or {}
        candidate = candidate_from_journal(payload, int(current.get("candidate_index", 0)))
        self.repository.restore_candidate(candidate)
        payload.setdefault("attempts", []).append(
            {
                "attempt_number": current.get("attempt_number"),
                "candidate_index": current.get("candidate_index"),
                "candidate_id": candidate.candidate_id,
                "candidate": candidate.to_dict(),
                "verification": current.get("last_verification"),
                "rolled_back_at": datetime.now().isoformat(timespec="microseconds"),
            }
        )
        payload["candidate_cursor"] = int(current.get("candidate_index", 0)) + 1
        payload["current_attempt"] = None
        payload["result"] = None
        if payload["candidate_cursor"] >= len(payload.get("candidates", [])):
            payload["status"] = RunStatus.EXHAUSTED.value
        else:
            payload["status"] = RunStatus.READY.value
        save_journal(root, payload)
        return {
            "status": payload["status"],
            "next_candidate_index": payload["candidate_cursor"],
            "rolled_back_candidate": candidate.candidate_id,
        }
