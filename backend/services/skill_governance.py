import json
import shutil
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from ..config import settings
from .skill_metadata import parse_skill_frontmatter

# Scope resolution follows OpenClaw-style precedence: workspace overrides shared,
# shared overrides managed, and bundled provides the lowest-priority fallback.
SCOPE_PRIORITY = ["workspace", "shared", "managed", "bundled"]
# Only approved skills are executable in governed runtime paths.
EXECUTABLE_STATUSES = {"approved"}
# Managed skills are editable in this app; all other scopes are treated as
# imported or externally managed sources and are therefore read-only.
NON_EDITABLE_SCOPES = {"workspace", "shared", "bundled"}
DEFAULT_VERSION = "0.1.0"


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _state_path() -> Path:
    return settings.governance_path / "state.json"


def _snapshots_root() -> Path:
    root = settings.governance_path / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _skill_scope_roots() -> list[tuple[str, Path]]:
    managed_root = Path(getattr(settings, "skills_path", settings.managed_skills_path))
    return [
        ("workspace", settings.workspace_skills_path),
        ("shared", settings.shared_skills_path),
        ("managed", managed_root),
        ("bundled", settings.bundled_skills_path),
    ]


def allowed_skill_roots() -> list[Path]:
    return [root for _scope, root in _skill_scope_roots()]


def _scope_rank(scope: str) -> int:
    try:
        return SCOPE_PRIORITY.index(scope)
    except ValueError:
        return len(SCOPE_PRIORITY)


def _key(scope: str, name: str) -> str:
    return f"{scope}:{name}"


def _load_state() -> dict:
    path = _state_path()
    if not path.exists():
        state = {
            "schema_version": 1,
            "skills": {},
            "events": [],
            "allowlist": {
                "modes": {
                    "manage": {
                        "visible_names": ["*"],
                        "execute_names": ["*"],
                        "visible_scopes": SCOPE_PRIORITY,
                        "execute_scopes": SCOPE_PRIORITY,
                    },
                    "sandbox": {
                        "visible_names": ["*"],
                        "execute_names": ["*"],
                        "visible_scopes": SCOPE_PRIORITY,
                        "execute_scopes": SCOPE_PRIORITY,
                    },
                    "creator": {
                        "visible_names": ["*"],
                        "execute_names": ["*"],
                        "visible_scopes": SCOPE_PRIORITY,
                        "execute_scopes": SCOPE_PRIORITY,
                    },
                }
            },
        }
        _save_state(state)
        return state
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent) as tmp:
        json.dump(state, tmp, ensure_ascii=False, indent=2)
        tmp.flush()
        Path(tmp.name).replace(path)


def _append_event(state: dict, *, skill_name: str, scope: str, event_type: str, details: dict | None = None) -> None:
    state.setdefault("events", []).append({
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "scope": scope,
        "type": event_type,
        "timestamp": _now(),
        "details": details or {},
    })
    if len(state["events"]) > 1000:
        state["events"] = state["events"][-1000:]


def _normalize_record(record: dict, *, root_path: Path, scope: str, meta: dict) -> dict:
    now = _now()
    record.setdefault("skill_id", str(uuid.uuid4()))
    record.setdefault("name", root_path.name)
    record["display_name"] = meta.get("name", record["name"])
    record["description"] = meta.get("description", "")
    record["version"] = str(meta.get("version") or record.get("version") or DEFAULT_VERSION)
    record["source"] = record.get("source") or {
        "type": "directory",
        "scope": scope,
        "origin": str(root_path),
    }
    record["install_type"] = record.get("install_type") or (
        "bundle" if scope == "bundled" else "workspace" if scope == "workspace" else "shared" if scope == "shared" else "local"
    )
    record["scope"] = scope
    record["root_path"] = str(root_path)
    record["status"] = record.get("status") or "approved"
    record["created_at"] = record.get("created_at") or now
    record["updated_at"] = now
    record["approval_requested_at"] = record.get("approval_requested_at")
    record["install_history"] = record.get("install_history") or []
    record["version_history"] = record.get("version_history") or []
    return record


def refresh_registry() -> dict:
    state = _load_state()
    changed = False
    discovered_keys: set[str] = set()

    for scope, root in _skill_scope_roots():
        root.mkdir(parents=True, exist_ok=True)
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            meta = parse_skill_frontmatter(content)
            key = _key(scope, skill_dir.name)
            old = deepcopy(state["skills"].get(key, {}))
            record = _normalize_record(state["skills"].get(key, {}), root_path=skill_dir, scope=scope, meta=meta)
            record["present"] = True
            if not record["version_history"]:
                record["version_history"] = [{
                    "version": record["version"],
                    "timestamp": record["created_at"],
                    "source_type": record["source"].get("type", "directory"),
                    "snapshot": None,
                }]
            if not record["install_history"]:
                record["install_history"] = [{
                    "id": str(uuid.uuid4()),
                    "event": "discovered",
                    "timestamp": record["created_at"],
                    "version": record["version"],
                    "scope": scope,
                    "status_after": record["status"],
                    "source": record["source"],
                }]
            state["skills"][key] = record
            discovered_keys.add(key)
            if old != record:
                changed = True

    for key, record in state["skills"].items():
        present = key in discovered_keys
        if record.get("present", True) != present:
            record["present"] = present
            changed = True

    if changed:
        _save_state(state)
    return state


def _record_visible(mode: str, entry: dict, *, action: str) -> bool:
    policy = _load_state()["allowlist"]["modes"].get(mode) or _load_state()["allowlist"]["modes"]["manage"]
    names = policy.get(f"{action}_names", ["*"])
    scopes = policy.get(f"{action}_scopes", SCOPE_PRIORITY)
    if entry["scope"] not in scopes:
        return False
    return "*" in names or entry["name"] in names


def _decorate_entry(entry: dict, shadowed: list[dict], *, mode: str) -> dict:
    item = deepcopy(entry)
    item["shadowed_scopes"] = [candidate["scope"] for candidate in shadowed]
    item["available_scopes"] = [entry["scope"], *item["shadowed_scopes"]]
    item["resolved_scope"] = entry["scope"]
    item["can_view"] = _record_visible(mode, entry, action="visible")
    item["allowlisted_for_execute"] = _record_visible(mode, entry, action="execute")
    item["can_execute"] = item["allowlisted_for_execute"] and item["status"] in EXECUTABLE_STATUSES
    item["editable"] = entry["scope"] not in NON_EDITABLE_SCOPES
    item["executable_status"] = entry["status"] in EXECUTABLE_STATUSES
    item["governance"] = {
        "status": item["status"],
        "scope": item["scope"],
        "source": item["source"],
        "install_type": item["install_type"],
        "version": item["version"],
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
        "approval_requested_at": item.get("approval_requested_at"),
    }
    return item


def list_skills_for_mode(mode: str = "manage", *, include_hidden: bool = False) -> list[dict]:
    state = refresh_registry()
    grouped: dict[str, list[dict]] = {}
    for record in state["skills"].values():
        if not record.get("present"):
            continue
        grouped.setdefault(record["name"], []).append(record)

    items: list[dict] = []
    for candidates in grouped.values():
        ordered = sorted(candidates, key=lambda candidate: _scope_rank(candidate["scope"]))
        resolved = _decorate_entry(ordered[0], ordered[1:], mode=mode)
        if include_hidden or resolved["can_view"]:
            items.append(resolved)

    return sorted(items, key=lambda item: (item["resolved_scope"], item["name"]))


def resolve_skill_record(
    skill_name: str,
    *,
    mode: str = "manage",
    require_visible: bool = True,
    require_executable: bool = False,
) -> dict:
    for item in list_skills_for_mode(mode, include_hidden=True):
        if item["name"] != skill_name:
            continue
        if require_visible and not item["can_view"]:
            raise PermissionError(f"Skill '{skill_name}' is not visible in mode '{mode}'")
        if require_executable and not item["can_execute"]:
            raise PermissionError(f"Skill '{skill_name}' is not executable in mode '{mode}'")
        return item
    raise FileNotFoundError(f"Skill '{skill_name}' not found")


def get_scope_skill_record(skill_name: str, scope: str) -> dict:
    state = refresh_registry()
    key = _key(scope, skill_name)
    record = state["skills"].get(key)
    if not record or not record.get("present"):
        raise FileNotFoundError(f"Skill '{skill_name}' not found in scope '{scope}'")
    item = _decorate_entry(record, [], mode="manage")
    item["root_path"] = record["root_path"]
    return item


def managed_skill_root(skill_name: str) -> Path:
    return Path(getattr(settings, "skills_path", settings.managed_skills_path)) / skill_name


def _snapshot_skill(skill_name: str, version: str, root: Path) -> str | None:
    if not root.exists():
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = _snapshots_root() / skill_name / f"{version}-{stamp}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(root, dest)
    return str(dest.relative_to(settings.governance_path))


def record_installation(
    *,
    skill_name: str,
    scope: str,
    root_path: Path,
    source: dict,
    install_type: str,
    status: str,
    version: str,
    event: str,
    approval_requested: bool = False,
    extra: dict | None = None,
) -> dict:
    state = refresh_registry()
    key = _key(scope, skill_name)
    record = state["skills"].get(key, {
        "name": skill_name,
        "scope": scope,
        "created_at": _now(),
        "skill_id": str(uuid.uuid4()),
    })
    skill_md = root_path / "SKILL.md"
    content = skill_md.read_text(encoding="utf-8", errors="replace") if skill_md.exists() else ""
    meta = parse_skill_frontmatter(content)
    record = _normalize_record(record, root_path=root_path, scope=scope, meta=meta)
    record["source"] = source
    record["install_type"] = install_type
    record["status"] = status
    record["version"] = version or record.get("version") or DEFAULT_VERSION
    record["approval_requested_at"] = _now() if approval_requested else record.get("approval_requested_at")
    snapshot = _snapshot_skill(skill_name, record["version"], root_path)
    record["version_history"].append({
        "version": record["version"],
        "timestamp": _now(),
        "source_type": source.get("type", install_type),
        "snapshot": snapshot,
    })
    install_record = {
        "id": str(uuid.uuid4()),
        "event": event,
        "timestamp": _now(),
        "version": record["version"],
        "scope": scope,
        "status_after": status,
        "source": source,
        "install_type": install_type,
        "snapshot": snapshot,
    }
    if extra:
        install_record["details"] = extra
    record["install_history"].append(install_record)
    state["skills"][key] = record
    _append_event(state, skill_name=skill_name, scope=scope, event_type=event, details={
        "version": record["version"],
        "status": status,
        "install_type": install_type,
        **(extra or {}),
    })
    _save_state(state)
    return _decorate_entry(record, [], mode="manage")


def transition_skill_status(skill_name: str, action: str, *, reason: str = "") -> dict:
    state = refresh_registry()
    try:
        record = state["skills"][_key("managed", skill_name)]
    except KeyError as exc:
        raise FileNotFoundError(f"Managed skill '{skill_name}' not found") from exc

    status_map = {
        "request_review": "pending_review",
        "approve": "approved",
        "reject": "rejected",
        "quarantine": "quarantined",
        "disable": "disabled",
        "enable": "approved",
    }
    if action not in status_map:
        raise ValueError(f"Unknown action: {action}")

    record["status"] = status_map[action]
    record["updated_at"] = _now()
    if action == "request_review":
        record["approval_requested_at"] = _now()
    state["skills"][_key("managed", skill_name)] = record
    _append_event(state, skill_name=skill_name, scope="managed", event_type=f"status:{action}", details={
        "status": record["status"],
        "reason": reason,
    })
    _save_state(state)
    return _decorate_entry(record, [], mode="manage")


def _find_version_entry(history: list[dict], version: str) -> dict | None:
    for item in reversed(history):
        if item["version"] == version and item.get("snapshot"):
            return item
    return None


def rollback_skill(skill_name: str, version: str) -> dict:
    state = refresh_registry()
    try:
        record = state["skills"][_key("managed", skill_name)]
    except KeyError as exc:
        raise FileNotFoundError(f"Managed skill '{skill_name}' not found") from exc

    target = _find_version_entry(record.get("version_history", []), version)
    if not target:
        raise FileNotFoundError(f"Version '{version}' not found for skill '{skill_name}'")

    snapshot_root = settings.governance_path / target["snapshot"]
    if not snapshot_root.exists():
        raise FileNotFoundError(f"Snapshot for version '{version}' is missing")

    root = Path(record["root_path"])
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(snapshot_root, root)
    result = record_installation(
        skill_name=skill_name,
        scope="managed",
        root_path=root,
        source={"type": "rollback", "from_version": record.get("version"), "to_version": version},
        install_type="rollback",
        status="pending_review",
        version=version,
        event="rollback",
        approval_requested=True,
        extra={"rolled_back_from": record.get("version"), "rolled_back_to": version},
    )
    return result


def skill_versions(skill_name: str) -> dict:
    record = get_scope_skill_record(skill_name, "managed")
    history = list(record.get("version_history", []))
    return {
        "current_version": record["version"],
        "versions": history,
        "latest_known_version": history[-1]["version"] if history else record["version"],
        "has_update": any(item["version"] != record["version"] for item in history),
    }


def get_events(skill_name: str | None = None) -> list[dict]:
    events = refresh_registry().get("events", [])
    if skill_name is None:
        return list(reversed(events[-100:]))
    return [event for event in reversed(events) if event["skill_name"] == skill_name][:100]


def get_allowlist() -> dict:
    return deepcopy(refresh_registry()["allowlist"])


def update_allowlist(payload: dict) -> dict:
    state = refresh_registry()
    allowlist = deepcopy(payload)
    for mode in ("manage", "sandbox", "creator"):
        allowlist.setdefault("modes", {}).setdefault(mode, {})
        allowlist["modes"][mode].setdefault("visible_names", ["*"])
        allowlist["modes"][mode].setdefault("execute_names", ["*"])
        allowlist["modes"][mode].setdefault("visible_scopes", SCOPE_PRIORITY)
        allowlist["modes"][mode].setdefault("execute_scopes", SCOPE_PRIORITY)
    state["allowlist"] = allowlist
    _append_event(state, skill_name="*", scope="managed", event_type="allowlist:update")
    _save_state(state)
    return deepcopy(allowlist)


def log_access_decision(skill_name: str, scope: str, *, mode: str, action: str, allowed: bool, reason: str = "") -> None:
    state = refresh_registry()
    _append_event(state, skill_name=skill_name, scope=scope, event_type="access", details={
        "mode": mode,
        "action": action,
        "allowed": allowed,
        "reason": reason,
    })
    _save_state(state)
