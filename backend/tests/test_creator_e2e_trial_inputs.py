import csv
import json
from pathlib import Path

from backend.services.creator import e2e


def _spec(name="input_files", shape="list[file_path]"):
    return e2e.E2ETypedInputSpec(name=name, shape=shape, target_file="scripts/analyze.py")


def _item(file_spec, *, name="input_files", shape="list[file_path]", evidence=None):
    return {"name": name, "shape": shape, "fixture": {"kind": "file_list", "files": [file_spec]},
            "evidence_requirement_ids": evidence or ["R1"]}


def test_grounded_csv_is_encoded_deterministically(tmp_path):
    item = _item({"format": "csv", "content_kind": "tabular",
                  "columns": [{"name": "value_a", "type": "number"}, {"name": "value_b", "type": "number", "nullable": True}],
                  "rows": [{"value_a": 1, "value_b": 10}, {"value_a": 2, "value_b": None}, {"value_a": 3, "value_b": 30}]})
    case = {"version": 1, "inputs": [item]}
    assert e2e._validate_e2e_trial_case_spec(case, input_specs={"input_files": _spec()}, requirement_ids={"R1"}) == case
    path = Path(e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)[0])
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == ["value_a", "value_b"]
    assert [row["value_a"] for row in rows] == ["1", "2", "3"]
    assert rows[1]["value_b"] == ""


def test_json_and_document_fixtures_use_existing_writers(tmp_path):
    value = {"title": "sample", "items": [{"name": "item_1"}]}
    json_item = _item({"format": "json", "content_kind": "json", "value": value}, name="config_file", shape="file_path")
    json_path = Path(e2e._materialize_e2e_trial_fixture(json_item, skill_dir=tmp_path))
    assert json.loads(json_path.read_text(encoding="utf-8")) == value
    for fmt in ("pdf", "docx"):
        item = _item({"format": fmt, "content_kind": "text", "text": "Grounded document body."})
        path = Path(e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)[0])
        assert path.is_file() and path.stat().st_size > 0


def test_invalid_input_format_and_evidence_are_rejected():
    specs = {"input_files": _spec()}
    unknown = {"version": 1, "inputs": [_item({"format": "unknown_binary", "content_kind": "text", "text": "x"})]}
    invented = {"version": 1, "inputs": [_item({"format": "txt", "content_kind": "text", "text": "x"}, evidence=["R999"])]}
    assert e2e._validate_e2e_trial_case_spec(unknown, input_specs=specs, requirement_ids={"R1"}) is None
    assert e2e._validate_e2e_trial_case_spec(invented, input_specs=specs, requirement_ids={"R1"}) is None


def test_trial_builder_prompt_contains_only_supplied_frozen_facts(monkeypatch):
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    def complete(messages, model):
        captured["messages"] = messages
        return '{"status":"unsupported"}'
    monkeypatch.setattr(e2e, "_complete_chat_once_sync_for_e2e", complete)
    facts = {"external_inputs": [{"platform_input": {"name": "input_files", "shape": "list[file_path]"},
              "target": {"script": "scripts/analyze.py", "input": "input_files"},
              "requirements": [{"id": "R1", "text": "Read numeric CSV"}]}]}
    assert e2e._build_e2e_trial_case(facts) == {"status": "unsupported"}
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    for expected in ("input_files", "list[file_path]", "scripts/analyze.py", "R1", "Read numeric CSV"):
        assert expected in prompt
    for forbidden in ("Tool alternatives", "repair history", "previous candidate patch", "Registry search results"):
        assert forbidden not in prompt
