"""Basic smoke tests for all backend modules."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import pytest
import tempfile
import shutil
from pathlib import Path


# ---- config ----
def test_config_paths():
    from backend.modules.config import KERNEL_PATH, SKILL_DATA_PATH
    assert KERNEL_PATH
    assert SKILL_DATA_PATH


# ---- skill_kernel_loader ----
def test_load_kernel_template():
    from backend.modules.skill_kernel_loader import load_kernel_template
    text = load_kernel_template()
    assert len(text) > 100


def test_get_skill_steps():
    from backend.modules.skill_kernel_loader import get_skill_steps
    steps = get_skill_steps()
    assert len(steps) == 5
    for phase in steps:
        assert "phase" in phase
        assert "steps" in phase


def test_get_required_fields():
    from backend.modules.skill_kernel_loader import get_required_fields
    fields = get_required_fields()
    assert "skill_name" in fields
    assert "skill_description" in fields


# ---- state_machine ----
def test_state_machine_flow():
    from backend.modules import state_machine
    sid = "test-session-001"
    state_machine.reset_session(sid)
    s = state_machine.get_current_step(sid)
    assert s["phase"] == 1
    assert s["step_index"] == 0
    assert not s["completed"]

    state_machine.save_field_data(sid, "name", "my-skill")
    data = state_machine.get_collected_data(sid)
    assert data["name"] == "my-skill"

    state_machine.next_step(sid)
    s2 = state_machine.get_current_step(sid)
    assert s2["step_index"] == 1 or s2["phase"] == 2

    state_machine.reset_session(sid)


# ---- user_input_handler ----
def test_validate_skill_name_valid():
    from backend.modules.user_input_handler import validate_input
    ok, err = validate_input("name", "my-skill-123")
    assert ok, err


def test_validate_skill_name_invalid():
    from backend.modules.user_input_handler import validate_input
    ok, _ = validate_input("name", "MY SKILL!")
    assert not ok


def test_validate_empty_required():
    from backend.modules.user_input_handler import validate_input
    ok, _ = validate_input("name", "")
    assert not ok


def test_validate_description_too_long():
    from backend.modules.user_input_handler import validate_input
    ok, _ = validate_input("description", "x" * 1025)
    assert not ok


# ---- data_store ----
def test_data_store_roundtrip(tmp_path, monkeypatch):
    import backend.modules.data_store as ds
    import backend.modules.config as cfg
    monkeypatch.setattr(cfg, "SKILL_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(ds, "_sessions_dir", lambda: tmp_path / ".sessions")
    (tmp_path / ".sessions").mkdir()

    ds.save_session("s1", {"name": "test-skill"})
    loaded = ds.load_session("s1")
    assert loaded == {"name": "test-skill"}

    none_val = ds.load_session("nonexistent")
    assert none_val is None


# ---- prompt_generator ----
def test_prompt_generator():
    from backend.modules import state_machine, prompt_generator
    sid = "test-prompt-001"
    state_machine.reset_session(sid)
    prompt = prompt_generator.generate_prompt(sid)
    assert "question" in prompt
    assert "options" in prompt
    assert "step" in prompt
    assert len(prompt["question"]) > 0
    state_machine.reset_session(sid)


# ---- skill_file_generator ----
def test_skill_file_generator(tmp_path, monkeypatch):
    import backend.modules.config as cfg
    import backend.modules.skill_file_generator as sfg
    monkeypatch.setattr(cfg, "SKILL_DATA_PATH", str(tmp_path))
    # Point to actual kernel
    root = Path(__file__).parents[2]
    monkeypatch.setattr(cfg, "KERNEL_PATH", str(root / "kernel"))

    skill_data = {
        "name": "test-skill",
        "description": "A test skill for unit testing",
        "trigger_words": "test trigger",
        "input_format": "text",
        "output_format": "text",
    }
    # reload module to pick up monkeypatched values
    import importlib
    importlib.reload(sfg)
    monkeypatch.setattr(sfg, "SKILL_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(sfg, "KERNEL_PATH", str(root / "kernel"))

    path = sfg.generate_skill_folder(skill_data)
    skill_md = Path(path) / "SKILL.md"
    assert skill_md.exists()
    content = skill_md.read_text()
    assert "test-skill" in content
    assert "---" in content


# ---- packager ----
def test_packager(tmp_path, monkeypatch):
    import backend.modules.config as cfg
    import backend.modules.packager as pkgr
    monkeypatch.setattr(cfg, "SKILL_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(pkgr, "SKILL_DATA_PATH", str(tmp_path))

    # Create a fake skill
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# my-skill")

    zip_path = pkgr.package_skill("my-skill")
    assert Path(zip_path).exists()

    import zipfile
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert any("SKILL.md" in n for n in names)
