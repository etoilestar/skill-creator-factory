"""Tests for backend/services/skill_manager.py."""

import io
import zipfile
import pytest
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(tmp_path: Path):
    """Return a mock settings object pointing at tmp_path."""
    from types import SimpleNamespace
    return SimpleNamespace(skills_path=tmp_path / "skills", kernel_path=tmp_path / "kernel")


def _create_skill_dir(skills_path: Path, name: str, content: str | None = None) -> Path:
    skill_dir = skills_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    md = skill_dir / "SKILL.md"
    md.write_text(content or f"---\nname: {name}\ndescription: test skill\n---\n# {name}\n")
    return skill_dir


def _build_zip(files: dict[str, bytes], top_dir: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            arc_name = f"{top_dir}/{name}" if top_dir else name
            zf.writestr(arc_name, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# list_skills
# ---------------------------------------------------------------------------

def test_list_skills_empty(tmp_path):
    from backend.services import skill_manager

    with patch.object(skill_manager.settings, "skills_path", tmp_path / "skills"):
        (tmp_path / "skills").mkdir()
        result = skill_manager.list_skills()
    assert result == []


def test_list_skills_returns_metadata(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "my-skill")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.list_skills()

    assert len(result) == 1
    assert result[0]["name"] == "my-skill"
    assert result[0]["description"] == "test skill"


def test_list_skills_skips_dirs_without_skill_md(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    (skills_path / "not-a-skill").mkdir()

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.list_skills()

    assert result == []


# ---------------------------------------------------------------------------
# get_skill
# ---------------------------------------------------------------------------

def test_get_skill_not_found(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(FileNotFoundError):
            skill_manager.get_skill("nonexistent")


def test_get_skill_returns_content(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "test-skill")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.get_skill("test-skill")

    assert result["name"] == "test-skill"
    assert "SKILL.md" in result["content"] or result["content"]


# ---------------------------------------------------------------------------
# save_skill / delete_skill
# ---------------------------------------------------------------------------

def test_save_and_delete_skill(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    content = "---\nname: new-skill\ndescription: hello\n---\n# New\n"

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        skill_manager.save_skill("new-skill", content)
        assert (skills_path / "new-skill" / "SKILL.md").exists()
        skill_manager.delete_skill("new-skill")
        assert not (skills_path / "new-skill").exists()


def test_delete_nonexistent_skill_raises(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(FileNotFoundError):
            skill_manager.delete_skill("ghost")


# ---------------------------------------------------------------------------
# save_asset / list_skill_assets / get_asset / update_asset / delete_asset
# ---------------------------------------------------------------------------

def test_save_and_get_asset(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.save_asset("s1", "scripts", "run.py", b"print('hello')")
        assert result["filename"] == "run.py"
        assert result["size"] == len(b"print('hello')")

        text = skill_manager.get_asset("s1", "scripts", "run.py")
        assert "print" in text


def test_save_asset_invalid_folder(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(ValueError, match="folder must be one of"):
            skill_manager.save_asset("s1", "evil", "x.py", b"data")


def test_save_asset_path_traversal_stripped_safely(tmp_path):
    """Path traversal via '../' is neutralised by Path.name — the file is saved
    with just the base name 'evil.py', which is valid."""
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.save_asset("s1", "scripts", "../evil.py", b"data")

    # Should succeed but save as 'evil.py' inside scripts/
    assert result["filename"] == "evil.py"
    assert (skills_path / "s1" / "scripts" / "evil.py").exists()
    # Must NOT have escaped the skill directory
    assert not (skills_path.parent / "evil.py").exists()


def test_save_asset_hidden_file_rejected(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(ValueError, match="Invalid filename"):
            skill_manager.save_asset("s1", "scripts", ".hidden", b"data")


def test_save_asset_size_limit(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")
    big = b"x" * (11 * 1024 * 1024)

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(ValueError, match="10 MB"):
            skill_manager.save_asset("s1", "scripts", "big.py", big)


def test_list_skill_assets(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        skill_manager.save_asset("s1", "scripts", "main.py", b"code")
        result = skill_manager.list_skill_assets("s1")

    assert "main.py" in result["scripts"]
    assert result["references"] == []
    assert result["assets"] == []


def test_update_asset(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        skill_manager.save_asset("s1", "scripts", "main.py", b"old")
        skill_manager.update_asset("s1", "scripts", "main.py", "new content")
        text = skill_manager.get_asset("s1", "scripts", "main.py")

    assert text == "new content"


def test_delete_asset(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "s1")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        skill_manager.save_asset("s1", "scripts", "todel.py", b"bye")
        skill_manager.delete_asset("s1", "scripts", "todel.py")
        with pytest.raises(FileNotFoundError):
            skill_manager.get_asset("s1", "scripts", "todel.py")


# ---------------------------------------------------------------------------
# import_skill_zip
# ---------------------------------------------------------------------------

def test_import_zip_flat(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    content = b"---\nname: zip-flat\ndescription: from zip\n---\n# Zip Flat\n"
    data = _build_zip({"SKILL.md": content})

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.import_skill_zip(data)

    assert result["name"] == "zip-flat"
    assert (skills_path / "zip-flat" / "SKILL.md").exists()


def test_import_zip_rooted(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    content = b"---\nname: zip-rooted\ndescription: from zip rooted\n---\n# Zip Rooted\n"
    data = _build_zip({"SKILL.md": content}, top_dir="zip-rooted")

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.import_skill_zip(data)

    assert result["name"] == "zip-rooted"


def test_import_zip_missing_skill_md(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    data = _build_zip({"README.md": b"hello"})

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(ValueError, match="SKILL.md"):
            skill_manager.import_skill_zip(data)


def test_import_zip_path_traversal_rejected(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil/SKILL.md", b"---\nname: evil\ndescription: bad\n---\n# Evil\n")
    data = buf.getvalue()

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(ValueError, match="非法路径"):
            skill_manager.import_skill_zip(data)


def test_import_zip_already_exists_no_overwrite(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "existing")
    content = b"---\nname: existing\ndescription: exists\n---\n# Existing\n"
    data = _build_zip({"SKILL.md": content})

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        with pytest.raises(FileExistsError):
            skill_manager.import_skill_zip(data, overwrite=False)


def test_import_zip_overwrite(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    _create_skill_dir(skills_path, "existing", content="---\nname: existing\ndescription: old\n---\n")
    content = b"---\nname: existing\ndescription: new\n---\n# Existing Updated\n"
    data = _build_zip({"SKILL.md": content})

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        result = skill_manager.import_skill_zip(data, overwrite=True)

    assert result["description"] == "new"


def test_import_zip_only_allowed_subdirs_extracted(tmp_path):
    from backend.services import skill_manager

    skills_path = tmp_path / "skills"
    skills_path.mkdir()
    content = b"---\nname: clean\ndescription: safe\n---\n# Clean\n"
    data = _build_zip({
        "SKILL.md": content,
        "scripts/run.py": b"print('ok')",
        "secrets/token.txt": b"secret123",
    })

    with patch.object(skill_manager.settings, "skills_path", skills_path):
        skill_manager.import_skill_zip(data)

    skill_dir = skills_path / "clean"
    assert (skill_dir / "scripts" / "run.py").exists()
    assert not (skill_dir / "secrets").exists()
