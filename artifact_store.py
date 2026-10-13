"""Tiny server-side artifact store for workflow-node MCP tools."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from workflow_node import WorkflowContractError, make_artifact_ref, parse_artifact_ref

LARGE_ARTIFACT_NAMES = {
    "grafana-frame",
    "grafana-query-response",
    "data-frames",
    "query-response",
    "analysis-result",
    "analysis-efficiency",
    "analysis-anomaly-detection",
    "analysis-metric-forecasting",
    "analysis-explanation",
    "method-profile",
    "method-association",
    "method-anomalies",
    "method-forecast",
    "method-explanation",
    "method-validation",
    "datasource-catalog",
    "dataset-metadata",
    "sandbox-code",
    "sandbox-execution",
    "sandbox-provenance",
    "dashboard",
}
LARGE_ARTIFACT_PREFIXES = (
    "method-engineering-",
    "analysis-engineering-",
    "method-finance-",
    "analysis-finance-",
    "render-approval-",
)


class ArtifactAuthError(PermissionError):
    """Raised when caller context does not own an artifact run."""


class ArtifactStore:
    def __init__(self, root: str | Path = ".analysis-artifacts/runs", retention_days: int | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        raw_days = retention_days if retention_days is not None else os.environ.get("ANALYSIS_ARTIFACT_RETENTION_DAYS", "7")
        try:
            days = max(0, int(raw_days))
        except (TypeError, ValueError) as exc:
            raise WorkflowContractError("retention_days must be an integer") from exc
        self.retention_seconds = days * 24 * 60 * 60

    def create_run(self, context: dict[str, Any], run_id: str | None = None) -> str:
        run_id = run_id or f"run_{uuid.uuid4().hex}"
        make_artifact_ref(run_id, "metadata")
        run_dir = self._run_dir(run_id)
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        metadata_path = self._metadata_path(run_id)
        metadata_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "org_id": str(context.get("org_id", "")),
                    "user_id": str(context.get("user_id", "")),
                    "session_id": str(context.get("session_id", "")),
                    "created_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        metadata_path.chmod(0o600)
        return run_id

    def grant_reuse(self, context: dict[str, Any], execution_ref: str, target_session: str) -> dict[str, str]:
        run_id, parts = parse_artifact_ref(execution_ref)
        if parts != ("sandbox-execution",) or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", target_session):
            raise WorkflowContractError("reuse requires an execution ref and an explicit target session identity")
        self.read_json(context, execution_ref)
        try:
            metadata = json.loads(self._metadata_path(run_id).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise WorkflowContractError("reuse source metadata is unavailable") from exc
        if not context.get("session_id") or metadata.get("session_id") != context["session_id"]:
            raise ArtifactAuthError("only the original source session may grant reuse")
        grant = {"source_session_id": context["session_id"], "target_session_id": target_session, "scope": "existing_execution_results_only"}
        name = "reuse-grant-" + hashlib.sha256(target_session.encode()).hexdigest()[:32]
        grant_ref = self.write_json(context, run_id, name, grant)
        return {"execution_ref": execution_ref, "grant_ref": grant_ref, **grant}

    @staticmethod
    def _durable_json(path: Path, value: Any) -> None:
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        with temporary.open("x", encoding="utf-8") as handle:
            temporary.chmod(0o600)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def reconcile_operation(self, context: dict[str, Any], operation_id: str) -> dict[str, Any]:
        """Recover only host-persisted completion evidence; never infer success or redispatch."""
        if not re.fullmatch(r"[a-f0-9]{64}", operation_id):
            raise WorkflowContractError("invalid operation identity")
        operation = self.root / "operations" / operation_id
        try:
            identity = json.loads((operation / "identity.json").read_text())
        except (OSError, ValueError) as exc:
            raise ArtifactAuthError("operation is unavailable for this session") from exc
        actor = {key: str(context.get(key) or "") for key in ("org_id", "user_id", "session_id")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
        if not all(actor.values()) or identity[0] != actor or digest != operation_id:
            raise ArtifactAuthError("operation identity does not match this actor/session")
        try:
            completion = json.loads((operation / "completion.json").read_text())
        except FileNotFoundError:
            return {"operation_id": operation_id, "status": "indeterminate", "redispatch_allowed": False}
        except (OSError, ValueError) as exc:
            raise WorkflowContractError("completion evidence is invalid; no redispatch allowed") from exc
        if completion.get("operation_id") != operation_id or not isinstance(completion.get("result"), dict):
            raise WorkflowContractError("completion receipt does not match the operation")
        result = completion["result"]
        if (result.get("evidence") or {}).get("effect_outcome") == "indeterminate":
            raise WorkflowContractError("unknown outcome is not completion evidence")
        status = "cancelled" if (result.get("evidence") or {}).get("effect_outcome") == "cancelled" else ("completed" if result.get("ok", True) else "failed")
        return {"operation_id": operation_id, "status": status, "result": result, "redispatch_allowed": False}

    def operation_statuses(self, context: dict[str, Any]) -> list[dict[str, str]]:
        actor = {key: str(context.get(key) or "") for key in ("org_id", "user_id", "session_id")}
        if not all(actor.values()):
            raise ArtifactAuthError("operation status requires an authenticated actor and session")
        statuses = []
        # ponytail: scan the existing journal; index only if measured history size requires it.
        paths = sorted((self.root / "operations").glob("*/identity.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                identity = json.loads(path.read_text())
            except (OSError, ValueError) as exc:
                raise WorkflowContractError("operation journal is unreadable; no new execution allowed") from exc
            if isinstance(identity, list) and identity and identity[0] == actor:
                record = self.reconcile_operation(context, path.parent.name)
                statuses.append({"operation_id": record["operation_id"], "status": record["status"]})
        return statuses

    def run_once(self, context: dict[str, Any], kind: str, inputs: dict[str, Any], execute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Reserve before effects; unresolved session work also blocks changed-code redispatch."""
        actor = {key: str(context.get(key) or "") for key in ("org_id", "user_id", "session_id")}
        if not all(actor.values()):
            raise ArtifactAuthError("effect execution requires an authenticated actor and session")
        encoded = json.dumps([actor, kind, inputs], sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        key = hashlib.sha256(encoded).hexdigest()
        directory = self.root / "operations"
        directory.mkdir(mode=0o700, exist_ok=True)
        operation = directory / key
        if operation.exists():
            recovered = self.reconcile_operation(context, key)
            if recovered["status"] in {"completed", "failed", "cancelled"}:
                return recovered["result"]
            raise WorkflowContractError(f"operation {key} is running or indeterminate; reconcile it before retrying")
        scope = hashlib.sha256(json.dumps(actor, sort_keys=True).encode()).hexdigest()
        with os.fdopen(os.open(directory / ("session-" + scope + ".lock"), os.O_CREAT | os.O_RDWR, 0o600), "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkflowContractError("a session operation is active; call reconcile_operation before another computation") from exc
            for record in self.operation_statuses(context):
                if record["status"] == "indeterminate":
                    raise WorkflowContractError(f"operation {record['operation_id']} is indeterminate; no replacement computation allowed")
            # A completed concurrent attempt can be replayed, never rerun.
            if operation.exists():
                return self.reconcile_operation(context, key)["result"]
            operation.mkdir(mode=0o700)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._durable_json(operation / "identity.json", [actor, kind, inputs])
            result = execute()
            result.setdefault("evidence", {})["operation_id"] = key
            outcome = (result.get("evidence") or {}).get("effect_outcome")
            if outcome == "indeterminate":
                return result
            if outcome == "terminated":
                # A confirmed stop before any output leaves nothing to replay;
                # forget the reservation so the same call can run again.
                shutil.rmtree(operation, ignore_errors=True)
                return result
            self._durable_json(operation / "completion.json", {"operation_id": key, "result": result})
            return result

    def write_json(self, context: dict[str, Any], run_id: str, name: str, value: Any) -> str:
        self._authorize(context, run_id, name)
        ref = make_artifact_ref(run_id, name)
        path = self._artifact_path(ref)
        payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if name in {"query-plan", "grafana-frame", "grafana-query-response", "dataframe-validation", "sandbox-code", "sandbox-execution", "sandbox-provenance"}:
            try:
                with path.open("x", encoding="utf-8") as handle:
                    path.chmod(0o600)
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError:
                if path.read_text(encoding="utf-8") != payload:
                    raise WorkflowContractError("immutable evidence cannot be replaced; create a new plan/run")
        else:
            path.write_text(payload, encoding="utf-8")
            path.chmod(0o600)
        return ref

    def read_json(self, context: dict[str, Any], ref: str) -> Any:
        run_id, parts = parse_artifact_ref(ref)
        self._authorize(context, run_id, parts[0])
        try:
            return json.loads(self._artifact_path(ref).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkflowContractError(f"cannot read artifact: {ref}") from exc

    def list_refs(self, context: dict[str, Any], name: str, limit: int = 20) -> list[str]:
        make_artifact_ref("run_probe0", name)
        refs = []
        metadata_paths = sorted(self.root.glob("run_*/metadata.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for metadata_path in metadata_paths:
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                self._authorize(context, metadata_path.parent.name, name)
            except (ArtifactAuthError, WorkflowContractError):
                continue
            ref = make_artifact_ref(metadata_path.parent.name, name)
            if self._artifact_path(ref).is_file():
                refs.append(ref)
                if len(refs) == limit:
                    break
        return refs

    def cleanup_expired(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        removed_large = 0
        removed_runs = 0
        removed_chart_files = 0
        chart_output_root = os.environ.get("ANALYSIS_CSV_OUTPUT_DIR") or "data/poc/analysis"
        if not self.root.exists():
            return {"removed_large": 0, "removed_runs": 0, "removed_chart_files": 0}
        for meta_path in self.root.glob("run_*/metadata.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            try:
                created_at = float(meta.get("created_at", now))
            except (TypeError, ValueError):
                continue
            if now - created_at < self.retention_seconds:
                continue
            run_dir = meta_path.parent
            run_id = run_dir.name
            for artifact in run_dir.glob("*.json"):
                name = artifact.stem
                if name not in LARGE_ARTIFACT_NAMES and not name.startswith(LARGE_ARTIFACT_PREFIXES):
                    continue
                try:
                    artifact.unlink()
                except OSError:
                    continue
                removed_large += 1
            if chart_output_root:
                chart_dir = Path(chart_output_root) / run_id
                if chart_dir.exists() and chart_dir.is_dir():
                    removed_chart_files += sum(1 for item in chart_dir.rglob("*") if item.is_file())
                    try:
                        shutil.rmtree(chart_dir)
                    except OSError:
                        meta["chart_cleanup_error"] = "failed to remove chart CSV directory"
            try:
                meta["large_artifacts_cleaned_at"] = now
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError:
                continue
        return {"removed_large": removed_large, "removed_runs": removed_runs, "removed_chart_files": removed_chart_files}

    def _run_dir(self, run_id: str) -> Path:
        make_artifact_ref(run_id, "metadata")
        return self.root / run_id

    def _metadata_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "metadata.json"

    def _artifact_path(self, ref: str) -> Path:
        run_id, parts = parse_artifact_ref(ref)
        path = self._run_dir(run_id) / f"{parts[0]}.json"
        root = self.root.resolve()
        resolved = path.resolve()
        if root not in resolved.parents:
            raise WorkflowContractError("artifact path escaped store root")
        return path

    def _authorize(self, context: dict[str, Any], run_id: str, name: str = "") -> None:
        try:
            meta = json.loads(self._metadata_path(run_id).read_text(encoding="utf-8"))
        except OSError as exc:
            raise WorkflowContractError(f"unknown artifact run: {run_id}") from exc
        context_org = str(context.get("org_id", ""))
        context_user = str(context.get("user_id", ""))
        if context_org != meta.get("org_id") or context_user != meta.get("user_id"):
            raise ArtifactAuthError("artifact context mismatch")
        session = str(context.get("session_id", ""))
        if session != meta.get("session_id", ""):
            if name not in {"sandbox-execution", "sandbox-provenance", "dashboard"} and not name.startswith(("report-context-", "report-inspection-")):
                raise ArtifactAuthError("session reuse does not authorize input datasets or new computations")
            grant_path = self._run_dir(run_id) / ("reuse-grant-" + hashlib.sha256(session.encode()).hexdigest()[:32] + ".json")
            try:
                grant = json.loads(grant_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ArtifactAuthError("artifact session mismatch; explicit reuse is required") from exc
            if grant.get("target_session_id") != session or grant.get("source_session_id") != meta.get("session_id") or grant.get("scope") != "existing_execution_results_only":
                raise ArtifactAuthError("invalid session reuse grant")
