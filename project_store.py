"""Durable project state, bounded snapshots, and persistent editorial history."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path


HISTORY_VERSION = 1
DEFAULT_HISTORY_LIMIT = 100


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def json_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def payload_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def atomic_json(path: Path, payload: object, *, backup: bool = False) -> None:
    if backup and path.is_file():
        try:
            existing = path.read_bytes()
            json.loads(existing.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            existing = None
        if existing is not None:
            _atomic_bytes(path.with_suffix(path.suffix + ".bak"), existing)
    _atomic_bytes(path, json_bytes(payload))


def _merge_immutable_assets(target: dict, current: dict) -> dict:
    """Restore editorial state without deleting takes or render artifacts."""
    restored = deepcopy(target)
    current_regions = {region["id"]: region for region in current.get("regions", [])}
    restored_ids = set()
    for region in restored.get("regions", []):
        restored_ids.add(region["id"])
        current_region = current_regions.get(region["id"])
        if current_region is None:
            continue
        known = {take["id"] for take in region.get("takes", [])}
        region.setdefault("takes", []).extend(
            deepcopy(take)
            for take in current_region.get("takes", [])
            if take["id"] not in known
        )
        restored_takes = {take["id"]: take for take in region.get("takes", [])}
        for current_take in current_region.get("takes", []):
            restored_take = restored_takes.get(current_take["id"])
            if restored_take is None:
                continue
            for field in ("qa_evaluations", "approval_events"):
                existing = {item.get("id") for item in restored_take.get(field, [])}
                restored_take.setdefault(field, []).extend(
                    deepcopy(item)
                    for item in current_take.get(field, [])
                    if item.get("id") not in existing
                )

    orphaned = deepcopy(restored.get("orphaned_take_regions", []))
    orphaned_ids = {region["id"] for region in orphaned}
    for region in current.get("regions", []):
        if region["id"] in restored_ids or not region.get("takes"):
            continue
        if region["id"] not in orphaned_ids:
            orphaned.append({"id": region["id"], "takes": deepcopy(region["takes"])})
    if orphaned:
        restored["orphaned_take_regions"] = orphaned

    known_renders = {render["id"] for render in restored.get("renders", [])}
    restored.setdefault("renders", []).extend(
        deepcopy(render)
        for render in current.get("renders", [])
        if render["id"] not in known_renders
    )
    known_exports = {
        revision["id"] for revision in restored.get("export_revisions", [])
    }
    restored.setdefault("export_revisions", []).extend(
        deepcopy(revision)
        for revision in current.get("export_revisions", [])
        if revision["id"] not in known_exports
    )
    return restored


class ProjectStore:
    """Own a current project file plus recoverable, bounded editorial history."""

    def __init__(self, path: Path, *, history_limit: int = DEFAULT_HISTORY_LIMIT):
        self.path = Path(path)
        self.history_path = self.path.with_name(f"{self.path.stem}.history.json")
        self.audit_path = self.path.with_name(f"{self.path.stem}.commands.jsonl")
        self.snapshots_dir = self.path.parent / f"{self.path.stem}-history" / "snapshots"
        self.history_limit = max(1, int(history_limit))
        self.last_warning: str | None = None

    def _read_json(self, path: Path) -> dict:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"{path.name} must contain a JSON object.")
        return payload

    def _append_audit(self, event: str, **fields: object) -> None:
        record = {"timestamp": now_iso(), "event": event, **fields}
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _write_snapshot(self, project: dict) -> str:
        fingerprint = payload_sha256(project)
        path = self.snapshots_dir / f"{fingerprint}.json"
        if not path.is_file():
            atomic_json(path, project)
        return fingerprint

    def _read_snapshot(self, fingerprint: str) -> dict:
        if not fingerprint or not all(character in "0123456789abcdef" for character in fingerprint):
            raise ValueError("Invalid project snapshot fingerprint.")
        return self._read_json(self.snapshots_dir / f"{fingerprint}.json")

    def _new_index(self, project: dict) -> dict:
        snapshot = self._write_snapshot(project)
        return {
            "version": HISTORY_VERSION,
            "project_id": project.get("project_id"),
            "cursor": 0,
            "entries": [],
            "current_snapshot": snapshot,
            "pending_snapshot": None,
            "updated_at": now_iso(),
        }

    def _read_index(self) -> dict | None:
        if not self.history_path.is_file():
            return None
        try:
            index = self._read_json(self.history_path)
        except (OSError, ValueError, json.JSONDecodeError):
            backup = self.history_path.with_suffix(self.history_path.suffix + ".bak")
            if not backup.is_file():
                raise
            index = self._read_json(backup)
            self.last_warning = "History index was recovered from its last valid backup."
        if index.get("version") != HISTORY_VERSION:
            raise ValueError("Unsupported Directors Room history version.")
        return index

    def _write_index(self, index: dict) -> None:
        index["updated_at"] = now_iso()
        atomic_json(self.history_path, index, backup=True)

    def _transition(self, index: dict, project: dict, snapshot: str) -> None:
        pending = deepcopy(index)
        pending["pending_snapshot"] = snapshot
        self._write_index(pending)
        atomic_json(self.path, project, backup=True)
        completed = deepcopy(pending)
        completed["current_snapshot"] = snapshot
        completed["pending_snapshot"] = None
        self._write_index(completed)
        self._prune_snapshots(completed)

    def _prune_snapshots(self, index: dict) -> None:
        keep = {index.get("current_snapshot"), index.get("pending_snapshot")}
        for entry in index.get("entries", []):
            keep.add(entry.get("before_snapshot"))
            keep.add(entry.get("after_snapshot"))
        keep.discard(None)
        if not self.snapshots_dir.is_dir():
            return
        for path in self.snapshots_dir.glob("*.json"):
            if path.stem not in keep:
                path.unlink()

    def _stamp(self, project: dict, action: str, command_id: str | None) -> dict:
        stamped = deepcopy(project)
        metadata = stamped.setdefault("state_meta", {})
        metadata["revision"] = int(metadata.get("revision", 0)) + 1
        metadata["last_action"] = action
        metadata["last_command_id"] = command_id
        metadata["saved_at"] = now_iso()
        stamped["updated_at"] = now_iso()
        return stamped

    def initialize(self, project: dict, *, migration_source: dict | None = None) -> dict:
        project = self._stamp(project, "initialize", None)
        if migration_source is not None:
            source_version = int(migration_source.get("version", 0))
            backup = self.path.with_name(
                f"{self.path.stem}.schema-v{source_version}.backup.json"
            )
            if not backup.exists():
                atomic_json(backup, migration_source)
        # Seed the index from the existing source, then use the same pending
        # transition protocol as every later write. A crash at any point can
        # therefore finish the migration from the pending v2 snapshot.
        base = migration_source if migration_source is not None else project
        index = self._new_index(base)
        self._write_index(index)
        snapshot = self._write_snapshot(project)
        self._transition(index, project, snapshot)
        self._append_audit(
            "store_initialized",
            project_version=project.get("version"),
            state_sha256=snapshot,
            migrated_from=migration_source.get("version") if migration_source else None,
        )
        return project

    def load(self) -> dict:
        self.last_warning = None
        primary = None
        primary_error: Exception | None = None
        try:
            primary = self._read_json(self.path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            primary_error = exc

        index = self._read_index()
        if index is None:
            if primary is None:
                raise primary_error or FileNotFoundError(self.path)
            index = self._new_index(primary)
            self._write_index(index)
            return primary

        pending = index.get("pending_snapshot")
        if pending:
            recovered = self._read_snapshot(pending)
            if primary is None or payload_sha256(primary) != pending:
                atomic_json(self.path, recovered, backup=primary is not None)
                self.last_warning = "An interrupted project write was recovered from its pending snapshot."
            index["current_snapshot"] = pending
            index["pending_snapshot"] = None
            self._write_index(index)
            self._append_audit("pending_write_recovered", state_sha256=pending)
            return recovered

        if primary is not None:
            primary_hash = payload_sha256(primary)
            if primary_hash != index.get("current_snapshot"):
                index["current_snapshot"] = self._write_snapshot(primary)
                self._write_index(index)
                self._append_audit("external_state_rebased", state_sha256=primary_hash)
                self.last_warning = "Project state changed outside the command store; history was rebased safely."
            return primary

        snapshot = index.get("current_snapshot")
        try:
            recovered = self._read_snapshot(snapshot)
            recovery_source = "history snapshot"
        except (OSError, ValueError, json.JSONDecodeError):
            backup = self.path.with_suffix(self.path.suffix + ".bak")
            recovered = self._read_json(backup)
            recovery_source = "project backup"
            snapshot = self._write_snapshot(recovered)
            index["current_snapshot"] = snapshot
            self._write_index(index)
        atomic_json(self.path, recovered)
        self.last_warning = f"The project file was invalid and recovered from {recovery_source}."
        self._append_audit("project_recovered", source=recovery_source, state_sha256=snapshot)
        return recovered

    def _index_for_current(self, project: dict) -> dict:
        index = self._read_index()
        if index is None:
            index = self._new_index(project)
            self._write_index(index)
            return index
        current_hash = payload_sha256(project)
        if current_hash != index.get("current_snapshot"):
            index["current_snapshot"] = self._write_snapshot(project)
            index["pending_snapshot"] = None
            self._write_index(index)
        return index

    def write_system(self, project: dict, action: str) -> dict:
        index = self._index_for_current(self.load() if self.path.is_file() else project)
        stamped = self._stamp(project, action, None)
        snapshot = self._write_snapshot(stamped)
        self._transition(index, stamped, snapshot)
        self._append_audit("system_write", action=action, state_sha256=snapshot)
        return stamped

    def commit(self, before: dict, after: dict, action: str) -> tuple[dict, bool]:
        if payload_sha256(before) == payload_sha256(after):
            return before, False
        index = self._index_for_current(before)
        command_id = uuid.uuid4().hex
        stamped = self._stamp(after, action, command_id)
        before_snapshot = self._write_snapshot(before)
        after_snapshot = self._write_snapshot(stamped)
        cursor = int(index.get("cursor", 0))
        entries = list(index.get("entries", []))[:cursor]
        entries.append(
            {
                "id": command_id,
                "action": action,
                "created_at": now_iso(),
                "before_snapshot": before_snapshot,
                "after_snapshot": after_snapshot,
            }
        )
        if len(entries) > self.history_limit:
            entries = entries[-self.history_limit :]
        index["entries"] = entries
        index["cursor"] = len(entries)
        self._transition(index, stamped, after_snapshot)
        self._append_audit(
            "command_committed",
            command_id=command_id,
            action=action,
            before_sha256=before_snapshot,
            after_sha256=after_snapshot,
        )
        return stamped, True

    def undo(self, current: dict) -> tuple[dict, bool]:
        index = self._index_for_current(current)
        cursor = int(index.get("cursor", 0))
        if cursor <= 0:
            return current, False
        entry = index["entries"][cursor - 1]
        target = self._read_snapshot(entry["before_snapshot"])
        restored = _merge_immutable_assets(target, current)
        restored = self._stamp(restored, f"undo:{entry['action']}", entry["id"])
        snapshot = self._write_snapshot(restored)
        index["cursor"] = cursor - 1
        self._transition(index, restored, snapshot)
        self._append_audit(
            "command_undone",
            command_id=entry["id"],
            action=entry["action"],
            state_sha256=snapshot,
        )
        return restored, True

    def redo(self, current: dict) -> tuple[dict, bool]:
        index = self._index_for_current(current)
        cursor = int(index.get("cursor", 0))
        entries = index.get("entries", [])
        if cursor >= len(entries):
            return current, False
        entry = entries[cursor]
        target = self._read_snapshot(entry["after_snapshot"])
        restored = _merge_immutable_assets(target, current)
        restored = self._stamp(restored, f"redo:{entry['action']}", entry["id"])
        snapshot = self._write_snapshot(restored)
        index["cursor"] = cursor + 1
        self._transition(index, restored, snapshot)
        self._append_audit(
            "command_redone",
            command_id=entry["id"],
            action=entry["action"],
            state_sha256=snapshot,
        )
        return restored, True

    def status(self) -> dict:
        index = self._read_index()
        if index is None:
            return {
                "can_undo": False,
                "can_redo": False,
                "undo_action": None,
                "redo_action": None,
                "depth": 0,
                "cursor": 0,
            }
        entries = index.get("entries", [])
        cursor = max(0, min(int(index.get("cursor", 0)), len(entries)))
        return {
            "can_undo": cursor > 0,
            "can_redo": cursor < len(entries),
            "undo_action": entries[cursor - 1]["action"] if cursor else None,
            "redo_action": entries[cursor]["action"] if cursor < len(entries) else None,
            "depth": len(entries),
            "cursor": cursor,
        }
