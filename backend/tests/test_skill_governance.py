from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def _write_skill(root: Path, name: str, description: str, *, version: str = "0.1.0") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _patch_governance_dirs(tmp_path: Path):
    from backend.config import settings

    managed = tmp_path / "managed"
    workspace = tmp_path / "workspace"
    shared = tmp_path / "shared"
    bundled = tmp_path / "bundled"
    governance = tmp_path / "governance"
    for path in (managed, workspace, shared, bundled, governance):
        path.mkdir(parents=True, exist_ok=True)

    stack = ExitStack()
    stack.enter_context(patch.object(settings, "skills_path", managed))
    stack.enter_context(patch.object(settings, "managed_skills_path", managed))
    stack.enter_context(patch.object(settings, "workspace_skills_path", workspace))
    stack.enter_context(patch.object(settings, "shared_skills_path", shared))
    stack.enter_context(patch.object(settings, "bundled_skills_path", bundled))
    stack.enter_context(patch.object(settings, "governance_path", governance))
    return stack, managed, workspace, shared, bundled, governance


def test_list_skills_resolves_scope_priority(tmp_path):
    from backend.services.skill_manager import list_skills

    stack, managed, workspace, _shared, _bundled, _gov = _patch_governance_dirs(tmp_path)
    with stack:
        _write_skill(managed, "demo", "managed")
        _write_skill(workspace, "demo", "workspace")

        result = list_skills(mode="manage", include_hidden=True)

    assert len(result) == 1
    assert result[0]["scope"] == "workspace"
    assert "managed" in result[0]["shadowed_scopes"]


def test_parse_skill_frontmatter_invalid_yaml_returns_empty():
    from backend.services.skill_metadata import parse_skill_frontmatter

    result = parse_skill_frontmatter("---\nname: ok\nbad: :\n---\n")

    assert result == {}


def test_save_skill_creates_governance_metadata(tmp_path):
    from backend.services.skill_manager import save_skill

    stack, _managed, _workspace, _shared, _bundled, _gov = _patch_governance_dirs(tmp_path)
    with stack:
        result = save_skill("draft-skill", "---\nname: draft-skill\ndescription: hello\n---\n# body\n")

    assert result["status"] == "draft"
    assert result["scope"] == "managed"
    assert result["install_history"][-1]["event"] == "create"


def test_allowlist_blocks_sandbox_visibility(tmp_path):
    from backend.services.skill_governance import update_allowlist
    from backend.services.skill_manager import list_skills

    stack, managed, _workspace, _shared, _bundled, _gov = _patch_governance_dirs(tmp_path)
    with stack:
        _write_skill(managed, "demo", "managed")
        update_allowlist({
            "modes": {
                "manage": {"visible_names": ["*"], "execute_names": ["*"], "visible_scopes": ["managed"], "execute_scopes": ["managed"]},
                "sandbox": {"visible_names": [], "execute_names": [], "visible_scopes": ["managed"], "execute_scopes": ["managed"]},
                "creator": {"visible_names": ["*"], "execute_names": ["*"], "visible_scopes": ["managed"], "execute_scopes": ["managed"]},
            }
        })

        result = list_skills(mode="sandbox")

    assert result == []


def test_transition_and_rollback(tmp_path):
    from backend.services.skill_governance import transition_skill_status
    from backend.services.skill_manager import get_skill, rollback_skill, save_skill

    stack, _managed, _workspace, _shared, _bundled, _gov = _patch_governance_dirs(tmp_path)
    with stack:
        save_skill("demo", "---\nname: demo\ndescription: v1\nversion: 1.0.0\n---\n# one\n")
        save_skill("demo", "---\nname: demo\ndescription: v2\nversion: 2.0.0\n---\n# two\n")
        transition_skill_status("demo", "approve")
        rollback_skill("demo", "1.0.0")
        result = get_skill("demo", mode="manage")

    assert result["status"] == "pending_review"
    assert result["version"] == "1.0.0"
    assert "one" in result["content"]
