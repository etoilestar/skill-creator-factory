"""Tests for backend/services/kernel_loader.py — frontmatter and Skill loading."""

import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_skill_md(path: Path, frontmatter: str, body: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_simple_frontmatter (yaml-based)
# ---------------------------------------------------------------------------

def test_parse_frontmatter_basic():
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = "---\nname: my-skill\ndescription: hello world\n---\n# Body"
    meta, body = _parse_simple_frontmatter(text)

    assert meta["name"] == "my-skill"
    assert meta["description"] == "hello world"
    assert "Body" in body


def test_parse_frontmatter_multiline_value():
    """Multiline YAML values should be parsed correctly by PyYAML."""
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = "---\ndescription: |\n  line one\n  line two\n---\n# Body"
    meta, body = _parse_simple_frontmatter(text)

    assert "line one" in meta["description"]
    assert "line two" in meta["description"]


def test_parse_frontmatter_no_frontmatter():
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = "# Just a markdown file\nNo frontmatter."
    meta, body = _parse_simple_frontmatter(text)

    assert meta == {}
    assert "Just a markdown file" in body


def test_parse_frontmatter_invalid_yaml_returns_empty():
    from backend.services.kernel_loader import _parse_simple_frontmatter

    # Invalid YAML (e.g. tab indentation in wrong place)
    text = "---\nname: ok\nbad: :\n---\n# Body"
    meta, body = _parse_simple_frontmatter(text)

    # Should not raise — returns empty or partial dict
    assert isinstance(meta, dict)


def test_parse_frontmatter_empty_frontmatter():
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = "---\n---\n# Body"
    meta, body = _parse_simple_frontmatter(text)

    assert meta == {}
    assert "Body" in body


def test_parse_frontmatter_special_chars():
    """Quoted strings with special characters should be handled by PyYAML."""
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = '---\nname: "skill: the-one"\n---\n'
    meta, body = _parse_simple_frontmatter(text)

    assert "skill: the-one" in meta["name"]


# ---------------------------------------------------------------------------
# _parse_simple_frontmatter — list value (new capability via yaml.safe_load)
# ---------------------------------------------------------------------------

def test_parse_frontmatter_list_value():
    from backend.services.kernel_loader import _parse_simple_frontmatter

    text = "---\ntags:\n  - python\n  - ai\n---\n# Body"
    meta, body = _parse_simple_frontmatter(text)

    assert isinstance(meta.get("tags"), list)
    assert "python" in meta["tags"]


# ---------------------------------------------------------------------------
# _load_skill_from_root — metadata only
# ---------------------------------------------------------------------------

def test_load_skill_metadata_only(tmp_path):
    from backend.services.kernel_loader import _load_skill_from_root

    skill_dir = tmp_path / "my-skill"
    _write_skill_md(
        skill_dir / "SKILL.md",
        "name: my-skill\ndescription: a test skill",
        "# Body Content\n",
    )

    pkg = _load_skill_from_root(skill_dir, include_body=False)

    assert pkg.name == "my-skill"
    assert pkg.description == "a test skill"
    assert pkg.skill_md_text == ""  # body not loaded


def test_load_skill_with_body(tmp_path):
    from backend.services.kernel_loader import _load_skill_from_root

    skill_dir = tmp_path / "body-skill"
    _write_skill_md(
        skill_dir / "SKILL.md",
        "name: body-skill\ndescription: desc",
        "## Step 1\nDo something.\n",
    )

    pkg = _load_skill_from_root(skill_dir, include_body=True)

    assert pkg.name == "body-skill"
    assert "Step 1" in pkg.skill_md_text


def test_load_skill_not_found_raises(tmp_path):
    from backend.services.kernel_loader import _load_skill_from_root

    with pytest.raises(FileNotFoundError):
        _load_skill_from_root(tmp_path / "ghost")


# ---------------------------------------------------------------------------
# _scan_resource_dir
# ---------------------------------------------------------------------------

def test_scan_resource_dir_empty(tmp_path):
    from backend.services.kernel_loader import _scan_resource_dir

    result = _scan_resource_dir(tmp_path, "scripts", "script")
    assert result == []


def test_scan_resource_dir_finds_files(tmp_path):
    from backend.services.kernel_loader import _scan_resource_dir

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "run.py").write_text("print('ok')")
    (scripts / "helper.sh").write_text("#!/bin/bash\necho ok")

    result = _scan_resource_dir(tmp_path, "scripts", "script")

    paths = [r.path for r in result]
    assert any("run.py" in p for p in paths)
    assert any("helper.sh" in p for p in paths)


# ---------------------------------------------------------------------------
# compose_metadata_prompt / compose_body_prompt — smoke tests
# ---------------------------------------------------------------------------

def test_compose_metadata_prompt_contains_name(tmp_path):
    from backend.services.kernel_loader import _load_skill_from_root, compose_metadata_prompt

    skill_dir = tmp_path / "smoke-skill"
    _write_skill_md(
        skill_dir / "SKILL.md",
        "name: smoke-skill\ndescription: smoke test",
    )

    pkg = _load_skill_from_root(skill_dir, include_body=False)
    prompt = compose_metadata_prompt(pkg)

    assert "smoke-skill" in prompt
    assert "smoke test" in prompt


def test_compose_body_prompt_contains_body_text(tmp_path):
    from backend.services.kernel_loader import _load_skill_from_root, compose_body_prompt

    skill_dir = tmp_path / "body-skill"
    _write_skill_md(
        skill_dir / "SKILL.md",
        "name: body-skill\ndescription: body test",
        "## Instructions\nDo the thing.\n",
    )

    pkg = _load_skill_from_root(skill_dir, include_body=True)
    prompt = compose_body_prompt(pkg)

    assert "Instructions" in prompt
    assert "Do the thing" in prompt


# ---------------------------------------------------------------------------
# load_user_skill_package
# ---------------------------------------------------------------------------

def test_load_user_skill_package_not_found(tmp_path):
    from backend.services import kernel_loader

    with patch.object(kernel_loader.settings, "skills_path", tmp_path / "skills"):
        with pytest.raises(FileNotFoundError):
            kernel_loader.load_user_skill_package("nosuchskill")


def test_load_user_skill_package_found(tmp_path):
    from backend.services import kernel_loader

    skills_path = tmp_path / "skills"
    skill_dir = skills_path / "my-skill"
    _write_skill_md(
        skill_dir / "SKILL.md",
        "name: my-skill\ndescription: loaded",
    )

    with patch.object(kernel_loader.settings, "skills_path", skills_path):
        pkg = kernel_loader.load_user_skill_package("my-skill")

    assert pkg.name == "my-skill"
