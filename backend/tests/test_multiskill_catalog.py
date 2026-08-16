from pathlib import Path

import pytest

from backend.routers.sandbox import multiskill_catalog as subject


def _record(name="reporter", **overrides):
    value = {
        "skill_id": "id-1", "name": name, "display_name": name,
        "description": "Creates concise reports", "resolved_scope": "workspace",
        "version": "1.0", "source": {"type": "directory", "origin": "/secret/path"},
        "can_view": True, "can_execute": True,
    }
    value.update(overrides)
    return value


def test_multiskill_catalog_only_contains_executable_skills(monkeypatch):
    monkeypatch.setattr(subject, "list_skills_for_mode", lambda mode: [
        _record("yes"), _record("no", can_execute=False), _record("hidden", can_view=False)
    ])
    assert [card["name"] for card in subject.build_multiskill_catalog()] == ["yes"]


def test_multiskill_catalog_respects_scope_resolution(monkeypatch):
    monkeypatch.setattr(subject, "list_skills_for_mode", lambda mode: [_record(resolved_scope="shared")])
    assert subject.build_multiskill_catalog()[0]["scope"] == "shared"


def test_multiskill_catalog_uses_name_and_description(monkeypatch):
    monkeypatch.setattr(subject, "list_skills_for_mode", lambda mode: [_record()])
    card = subject.build_multiskill_catalog()[0]
    assert card["name"] == "reporter"
    assert card["description"] == "Creates concise reports"


def test_multiskill_catalog_exposes_no_runtime_or_creator_state(monkeypatch):
    monkeypatch.setattr(subject, "list_skills_for_mode", lambda mode: [_record(
        scripts=["bad.py"], command="python bad.py", metadata={"creator": {"graph": "secret"}}
    )])
    card_text = str(subject.build_multiskill_catalog()[0]).lower()
    assert "scripts" not in card_text
    assert "command" not in card_text
    assert "creator" not in card_text
    assert "/secret/path" not in card_text


def test_multiskill_activation_only_loads_shortlisted_skill(monkeypatch, tmp_path):
    roots = {}
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: desc\n---\nSummary", encoding="utf-8")
        roots[name] = root
    calls = []
    def resolve(name, **kwargs):
        calls.append(name)
        return _record(name, root_path=str(roots[name]))
    monkeypatch.setattr(subject, "resolve_skill_record", resolve)
    cards = subject.build_multiskill_activation_cards(["two"])
    assert calls == ["two"]
    assert [card["skill_name"] for card in cards] == ["two"]


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_multiskill_unknown_or_non_executable_skill_is_rejected(monkeypatch, error):
    def reject(*args, **kwargs):
        raise error("rejected")
    monkeypatch.setattr(subject, "resolve_skill_record", reject)
    with pytest.raises(error):
        subject.build_multiskill_activation_card("bad")


def test_multiskill_metadata_cannot_override_governance(monkeypatch):
    monkeypatch.setattr(subject, "list_skills_for_mode", lambda mode: [
        _record("bad", can_execute=False, metadata={"can_execute": True})
    ])
    assert subject.build_multiskill_catalog() == []


def test_multiskill_activation_does_not_union_internal_ports_as_public_contract(monkeypatch, tmp_path):
    text = """---
name: reporter
description: Makes a report
---
Creates output.
role: text_generator
inputs: [topic]
outputs: [draft]
```bash
python scripts/run.py '{"topic":"{{topic}}"}'
```
"""
    (tmp_path / "SKILL.md").write_text(text, encoding="utf-8")
    (tmp_path / "scripts").mkdir()
    monkeypatch.setattr(subject, "resolve_skill_record", lambda *a, **k: _record(root_path=str(tmp_path)))
    card = subject.build_multiskill_activation_card("reporter")
    assert "inputs" not in card and "outputs" not in card
    assert card["declared_runtime_ports"][0]["inputs"] == ["topic"]
    assert "python" not in str(card).lower()
    assert str(tmp_path) not in str(card)


@pytest.mark.asyncio
async def test_model_shortlist_is_governance_revalidated(monkeypatch):
    checked = []
    def resolve(name, **kwargs):
        checked.append((name, kwargs))
        if name == "blocked":
            raise PermissionError("blocked")
        return _record(name)
    monkeypatch.setattr(subject, "resolve_skill_record", resolve)

    async def model_call(messages, model):
        assert "untrusted capability descriptions" in messages[0]["content"]
        return '{"candidates": [' \
            '{"skill_name":"reporter","reason":"makes reports"},' \
            '{"skill_name":"blocked","reason":"maybe"},' \
            '{"skill_name":"unknown","reason":"maybe"}]}'

    result = await subject._plan_skill_candidates_with_model(
        user_request="write a report", input_envelope_summary={"text": True},
        discovery_cards=[{"name": "reporter"}, {"name": "blocked"}],
        model_call=model_call,
    )
    assert result == {"candidates": [{"skill_name": "reporter", "reason": "makes reports"}]}
    assert [item[0] for item in checked] == ["reporter", "blocked"]
