import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.services.creator import e2e


def _spec(name="input_files", shape="list[file_path]"):
    return e2e.E2ETypedInputSpec(name=name, shape=shape, target_file="scripts/analyze.py")


def _item(file_spec, *, name="input_files", shape="list[file_path]", evidence=None):
    return {"name": name, "shape": shape, "fixture": {"kind": "file_list", "files": [file_spec]},
            "evidence_requirement_ids": ["R1"] if evidence is None else evidence}


def test_grounded_csv_is_encoded_deterministically(tmp_path):
    item = _item({"format": "csv", "content_kind": "tabular",
                  "columns": [{"name": "value_a", "type": "number"}, {"name": "value_b", "type": "number", "nullable": True}],
                  "rows": [{"value_a": 1, "value_b": 10}, {"value_a": 2, "value_b": None}, {"value_a": 3, "value_b": 30}]})
    case = {"version": 1, "inputs": [item]}
    assert e2e._validate_e2e_trial_case_spec(case, input_specs={"input_files": _spec()}, requirement_ids_by_input={"input_files": {"R1"}}) == case
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
    assert e2e._validate_e2e_trial_case_spec(unknown, input_specs=specs, requirement_ids_by_input={"input_files": {"R1"}}) is None
    assert e2e._validate_e2e_trial_case_spec(invented, input_specs=specs, requirement_ids_by_input={"input_files": {"R1"}}) is None


def test_tabular_column_types_nullability_and_nonempty_rows_are_enforced():
    specs = {"input_files": _spec()}
    def accepted(columns, rows):
        case = {"version": 1, "inputs": [_item({
            "format": "csv", "content_kind": "tabular", "columns": columns, "rows": rows,
        })]}
        return e2e._validate_e2e_trial_case_spec(
            case, input_specs=specs, requirement_ids_by_input={"input_files": {"R1"}},
        )

    assert accepted([{"name": "n", "type": "number"}], [{"n": True}]) is None
    assert accepted([{"name": "i", "type": "integer"}], [{"i": 1.5}]) is None
    assert accepted([{"name": "s", "type": "string"}], [{"s": 1}]) is None
    assert accepted([{"name": "b", "type": "boolean"}], [{"b": 1}]) is None
    assert accepted([{"name": "x", "type": "decimal"}], [{"x": 1}]) is None
    assert accepted([{"name": "n", "type": "number"}], [{"n": None}]) is None
    assert accepted([{"name": "n", "type": "number", "nullable": True}], []) is None
    assert accepted([{"name": "n", "type": "number", "nullable": True}], [{"n": None}]) is not None


def test_tabular_nullable_is_canonicalized_from_primary_rows():
    proposed = {"version": 1, "inputs": [_item({
        "format": "csv", "content_kind": "tabular",
        "columns": [{"name": "salary", "type": "number", "nullable": False}],
        "rows": [{"salary": 5000}, {"salary": None}],
    })]}

    canonical = e2e._canonicalize_e2e_trial_case_spec(proposed)

    assert proposed["inputs"][0]["fixture"]["files"][0]["columns"][0]["nullable"] is False
    assert canonical["inputs"][0]["fixture"]["files"][0]["columns"][0]["nullable"] is True
    assert e2e._validate_e2e_trial_case_spec(
        canonical, input_specs={"input_files": _spec()},
        requirement_ids_by_input={"input_files": {"R1"}},
    ) == canonical
    assert e2e._stable_json_hash(canonical)


def test_tabular_canonicalization_does_not_repair_primary_cell_errors():
    proposed = {"version": 1, "inputs": [_item({
        "format": "csv", "content_kind": "tabular",
        "columns": [{"name": "value", "type": "integer", "nullable": True}],
        "rows": [{"value": "abc"}],
    })]}

    canonical = e2e._canonicalize_e2e_trial_case_spec(proposed)

    assert canonical["inputs"][0]["fixture"]["files"][0]["rows"][0]["value"] == "abc"
    assert e2e._validate_e2e_trial_case_spec(
        canonical, input_specs={"input_files": _spec()},
        requirement_ids_by_input={"input_files": {"R1"}},
    ) is None


def test_tabular_nullable_is_false_when_all_rows_have_values():
    proposed = {"version": 1, "inputs": [_item({
        "format": "csv", "content_kind": "tabular",
        "columns": [{"name": "value", "type": "integer", "nullable": True}],
        "rows": [{"value": 1}, {"value": 2}],
    })]}

    canonical = e2e._canonicalize_e2e_trial_case_spec(proposed)
    assert canonical["inputs"][0]["fixture"]["files"][0]["columns"][0]["nullable"] is False


def test_evidence_is_required_and_scoped_to_each_input_target():
    specs = {
        "input_files": _spec(),
        "config_file": _spec("config_file", "file_path"),
    }
    file_spec = {"format": "txt", "content_kind": "text", "text": "grounded"}
    missing = {"version": 1, "inputs": [_item(file_spec, evidence=[])]}
    wrong_target = {"version": 1, "inputs": [_item(file_spec, evidence=["R2"])]}
    scoped = {"input_files": {"R1"}, "config_file": {"R2"}}
    assert e2e._validate_e2e_trial_case_spec(missing, input_specs=specs, requirement_ids_by_input=scoped) is None
    assert e2e._validate_e2e_trial_case_spec(wrong_target, input_specs=specs, requirement_ids_by_input=scoped) is None


def test_shape_requires_matching_fixture_kind_and_file_cardinality():
    text_file = {"format": "txt", "content_kind": "text", "text": "grounded"}

    def validate(name, shape, fixture):
        case = {"version": 1, "inputs": [{
            "name": name,
            "shape": shape,
            "fixture": fixture,
            "evidence_requirement_ids": ["R1"],
        }]}
        return e2e._validate_e2e_trial_case_spec(
            case,
            input_specs={name: _spec(name, shape)},
            requirement_ids_by_input={name: {"R1"}},
        )

    one_file = {"kind": "file_list", "files": [text_file]}
    two_files = {"kind": "file_list", "files": [text_file, text_file]}
    scalar_string = {"kind": "scalar", "value": "sample"}
    scalar_number = {"kind": "scalar", "value": 1.5}

    assert validate("text", "string", one_file) is None
    assert validate("amount", "number", text_file) is None
    assert validate("document", "file_path", scalar_string) is None
    assert validate("document", "file_path", two_files) is None
    assert validate("documents", "list[file_path]", scalar_string) is None

    assert validate("text", "string", scalar_string) is not None
    assert validate("amount", "number", scalar_number) is not None
    assert validate("document", "file_path", text_file) is not None
    assert validate("documents", "list[file_path]", one_file) is not None


def test_structured_and_typed_list_fixtures_are_supported_without_files(tmp_path):
    cases = [
        ("options", "object", {"threshold": 0.75, "enabled": True}),
        ("labels", "list[string]", ["alpha", "beta"]),
        ("scores", "list[number]", [1, 2.5]),
        ("records", "list[object]", [{"name": "alpha"}, {"name": "beta"}]),
        ("items", "list", ["text", 2, {"enabled": True}]),
    ]
    for name, shape, value in cases:
        item = {
            "name": name,
            "shape": shape,
            "fixture": {"kind": "json_value", "value": value},
            "evidence_requirement_ids": ["R1"],
        }
        case = {"version": 1, "inputs": [item]}
        assert e2e._validate_e2e_trial_case_spec(
            case,
            input_specs={name: _spec(name, shape)},
            requirement_ids_by_input={name: {"R1"}},
        ) == case
        assert e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path) == value


def test_structured_fixture_rejects_wrong_item_types_and_empty_values():
    def validate(shape, value):
        name = "value"
        case = {"version": 1, "inputs": [{
            "name": name,
            "shape": shape,
            "fixture": {"kind": "json_value", "value": value},
            "evidence_requirement_ids": ["R1"],
        }]}
        return e2e._validate_e2e_trial_case_spec(
            case,
            input_specs={name: _spec(name, shape)},
            requirement_ids_by_input={name: {"R1"}},
        )

    assert validate("object", {}) is None
    assert validate("list[string]", [1]) is None
    assert validate("list[number]", [True]) is None
    assert validate("list[integer]", [1.5]) is None
    assert validate("list[object]", [{}]) is None


def test_prepare_trial_case_includes_non_file_structured_inputs(monkeypatch):
    captured = {}
    def build(facts, **kwargs):
        captured["facts"] = facts
        return {"status": "unsupported"}

    monkeypatch.setattr(e2e, "_build_e2e_trial_case", build)
    specs = [
        _spec("options", "object"),
        _spec("labels", "list[string]"),
        _spec("count", "integer"),
    ]
    result = e2e._prepare_e2e_trial_case(
        typed_specs=specs,
        requirements_by_file={},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={},
        requested_model=None,
        session=None,
    )
    assert result is None
    assert {
        item["platform_input"]["shape"]
        for item in captured["facts"]["external_inputs"]
    } == {"object", "list[string]", "integer"}


def test_trial_case_is_built_once_and_fixture_is_stable(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    session = e2e.CreatorE2ESession("session", "demo", skill_dir, skill_dir / ".venv", skill_dir / "outputs")
    requirement = e2e.RequirementItem(id="R1", target_file="scripts/analyze.py", purpose="Read numeric input")
    case = {"version": 1, "inputs": [_item({
        "format": "csv", "content_kind": "tabular",
        "columns": [{"name": "value", "type": "number"}], "rows": [{"value": 1}],
    })]}
    calls = []
    monkeypatch.setattr(e2e, "_build_e2e_trial_case", lambda *args, **kwargs: calls.append(1) or case)
    kwargs = dict(
        typed_specs=[_spec()], requirements_by_file={"scripts/analyze.py": [requirement]},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={}, requested_model=None, session=session,
    )
    first = e2e._prepare_e2e_trial_case(**kwargs)
    first_path = Path(e2e._materialize_e2e_trial_fixture(first["inputs"][0], skill_dir=skill_dir)[0])
    first_hash = hashlib.sha256(first_path.read_bytes()).hexdigest()
    first_digest = session.trial_case_digest
    second = e2e._prepare_e2e_trial_case(**kwargs)
    second_path = Path(e2e._materialize_e2e_trial_fixture(second["inputs"][0], skill_dir=skill_dir)[0])
    assert calls == [1]
    assert session.trial_case_digest == first_digest
    assert hashlib.sha256(second_path.read_bytes()).hexdigest() == first_hash


def test_external_and_declared_default_prevent_trial_generation(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("trial builder must not run")
    monkeypatch.setattr(e2e, "_build_e2e_trial_case", fail)
    common = dict(typed_specs=[_spec()], requirements_by_file={}, requested_model=None, session=None)
    assert e2e._prepare_e2e_trial_case(
        **common, skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={"input_files": ["/tmp/real.csv"]},
    ) is None
    assert e2e._prepare_e2e_trial_case(
        **common, skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={"input_files": ["declared.csv"]})},
        external_context={},
    ) is None


def test_declared_default_beats_trial_case_and_invalid_case_uses_generic_fallback(tmp_path):
    command = e2e.E2EWorkflowCommand(1, "SKILL.md", "scripts/analyze.py", "", "python", {"input_files": "{{input_files}}"})
    entry = SimpleNamespace(default_values={"input_files": ["declared.csv"]}, inputs=[], artifact_contract={})
    grounded = {"version": 1, "inputs": [_item({"format": "txt", "content_kind": "text", "text": "grounded"})]}
    payload = e2e._seed_initial_e2e_payload(
        [command], external_context={}, skill_dir=tmp_path, requirements_by_file={},
        skill_plan_entries={"scripts/analyze.py": entry}, trial_case=grounded,
    )
    assert payload["input_files"] == ["declared.csv"]

    invalid = {"version": 1, "inputs": [_item({"format": "unknown", "content_kind": "text", "text": "bad"})]}
    validated = e2e._validate_e2e_trial_case_spec(
        invalid, input_specs={"input_files": _spec()}, requirement_ids_by_input={"input_files": {"R1"}},
    )
    assert validated is None
    fallback_payload = e2e._seed_initial_e2e_payload(
        [command], external_context={}, skill_dir=tmp_path,
        requirements_by_file={"scripts/analyze.py": [e2e.RequirementItem(
            id="R1", target_file="scripts/analyze.py", inputs=["input_files: list[file_path]"],
        )]},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(
            default_values={}, inputs=["input_files: list[file_path]"], artifact_contract={},
        )},
        trial_case=validated,
    )
    fallback = fallback_payload["input_files"]
    assert fallback and Path(fallback[0]).is_file()
    assert "Creator E2E" in Path(fallback[0]).read_text(encoding="utf-8")


def test_trial_builder_prompt_contains_only_supplied_frozen_facts(monkeypatch):
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    def complete(**kwargs):
        captured.update(kwargs)
        return {"status": "unsupported"}
    monkeypatch.setattr(e2e, "_complete_creator_json_object_once_sync_for_e2e", complete)
    facts = {"external_inputs": [{"platform_input": {"name": "input_files", "shape": "list[file_path]"},
              "target": {"script": "scripts/analyze.py", "input": "input_files"},
              "requirements": [{"id": "R1", "text": "Read numeric CSV"}]}]}
    assert e2e._build_e2e_trial_case(facts) == {"status": "unsupported"}
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    for expected in ("input_files", "list[file_path]", "scripts/analyze.py", "R1", "Read numeric CSV"):
        assert expected in prompt
    for forbidden in ("Tool alternatives", "repair history", "previous candidate patch", "Registry search results"):
        assert forbidden not in prompt
    schema = json.dumps(captured["response_schema"], ensure_ascii=False)
    for required_contract in ("version", "inputs", "fixture", "evidence_requirement_ids", "input_files", "list[file_path]", "R1", "csv", "tabular"):
        assert required_contract in schema


def test_trial_schema_binds_file_collection_and_object_fixture_kinds():
    facts = {"external_inputs": [
        {
            "platform_input": {"name": "input_files", "shape": "list[file_path]"},
            "target": {"script": "scripts/analyze.py", "input": "input_files"},
            "requirements": [],
        },
        {
            "platform_input": {"name": "fields", "shape": "object"},
            "target": {"script": "scripts/analyze.py", "input": "fields"},
            "requirements": [],
        },
    ]}

    schema = e2e._e2e_trial_case_response_schema(facts)
    variants = schema["oneOf"][0]["properties"]["inputs"]["items"]["oneOf"]
    by_name = {
        variant["properties"]["name"]["const"]: variant
        for variant in variants
    }

    input_files = by_name["input_files"]
    assert input_files["properties"]["shape"]["const"] == "list[file_path]"
    assert input_files["properties"]["fixture"]["properties"]["kind"]["const"] == "file_list"
    fields = by_name["fields"]
    assert fields["properties"]["shape"]["const"] == "object"
    assert fields["properties"]["fixture"]["properties"]["kind"]["const"] == "json_value"


def test_csv_file_list_materializes_each_file_with_csv_suffix(tmp_path):
    csv_file = {
        "format": "csv",
        "content_kind": "tabular",
        "columns": [{"name": "value", "type": "string", "nullable": False}],
        "rows": [{"value": "one"}],
    }
    item = {
        "name": "input_files",
        "shape": "list[file_path]",
        "fixture": {"kind": "file_list", "files": [csv_file, csv_file]},
        "evidence_requirement_ids": [],
    }

    paths = [Path(value) for value in e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)]

    assert [path.name for path in paths] == ["input_files_1.csv", "input_files_2.csv"]
    assert all(path.is_file() and path.suffix == ".csv" for path in paths)


def test_indexed_file_fixture_renders_without_runtime_literal_events(tmp_path):
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("value\n1\n", encoding="utf-8")
    second.write_text("value\n2\n", encoding="utf-8")
    command = e2e.E2EWorkflowCommand(
        1, "SKILL.md", "scripts/compare.py", "", "python",
        {"input_files": ["{{input_files[0]}}", "{{input_files[1]}}"]},
    )
    payload = {"input_files": [str(first), str(second)]}
    specs = [e2e.E2ETypedInputSpec(
        name="input_files", shape="list[file_path]", source="platform_io_contract",
    )]

    rendered = e2e._render_e2e_command_payload(
        command, payload=payload, typed_input_specs=specs,
    )
    e2e._validate_e2e_input_fixtures(
        command=command, payload=payload, rendered_payload=rendered,
        typed_input_specs=specs,
    )
    materialized, events = e2e._materialize_rendered_e2e_payload_runtime_literals(
        rendered, skill_dir=tmp_path, target_file=command.script_path,
    )

    assert materialized == {"input_files": [str(first), str(second)]}
    assert events == []


@pytest.mark.parametrize("bad_value", [
    ["__RUNTIME_INPUT_FILE__"],
    [{"path": "a.csv"}],
])
def test_input_fixture_gate_rejects_unmaterialized_file_lists(tmp_path, bad_value):
    command = e2e.E2EWorkflowCommand(
        1, "SKILL.md", "scripts/compare.py", "", "python",
        {"input_files": "{{input_files}}"},
    )
    specs = [e2e.E2ETypedInputSpec(
        name="input_files", shape="list[file_path]", source="platform_io_contract",
    )]

    with pytest.raises(ValueError, match="E2E_LAYER=e2e_input_fixture"):
        e2e._validate_e2e_input_fixtures(
            command=command,
            payload={"input_files": bad_value},
            rendered_payload={"input_files": bad_value},
            typed_input_specs=specs,
        )


def test_legacy_file_kind_fallback_ignores_output_artifact_descriptions():
    kinds = e2e._infer_e2e_file_sample_kinds(
        name="csv_files",
        shape="list[file_path]",
        target_file="scripts/analyze.py",
        skill_md="Input: two CSV files. Output: report.pdf",
        script_content="output_path = 'outputs/report.pdf'",
    )

    assert kinds == ["csv"]


def test_structured_builder_contract_freezes_and_materializes_csv(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    session = e2e.CreatorE2ESession("session", "demo", skill_dir, skill_dir / ".venv", skill_dir / "outputs")
    response = {"version": 1, "inputs": [_item({
        "format": "csv", "content_kind": "tabular",
        "columns": [{"name": "value", "type": "number", "nullable": False}],
        "rows": [{"value": 1}, {"value": None}],
    }, evidence=["R1", "R7"])]}
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    async def structured_helper(**kwargs):
        captured.update(kwargs)
        return response
    monkeypatch.setattr(e2e, "complete_json_object_once", structured_helper)
    requirements = [
        e2e.RequirementItem(id="R1", target_file="scripts/analyze.py", purpose="Read CSV"),
        e2e.RequirementItem(id="R7", target_file="scripts/analyze.py", purpose="Numeric statistics"),
    ]
    accepted = e2e._prepare_e2e_trial_case(
        typed_specs=[_spec()], requirements_by_file={"scripts/analyze.py": requirements},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={}, requested_model=None, session=session,
    )
    assert accepted["inputs"][0]["fixture"]["files"][0]["columns"][0]["nullable"] is True
    assert session.trial_case_prepared is True
    assert session.trial_case_digest
    path = Path(e2e._materialize_e2e_trial_fixture(accepted["inputs"][0], skill_dir=skill_dir)[0])
    assert path.is_file()
    assert list(csv.DictReader(path.open(encoding="utf-8"))) == [{"value": "1"}, {"value": ""}]
    schema = json.dumps(captured["response_schema"])
    assert all(value in schema for value in ("input_files", "list[file_path]", "R1", "R7"))


def test_malformed_structured_builder_response_is_rejected_to_fallback(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    session = e2e.CreatorE2ESession("session", "demo", skill_dir, skill_dir / ".venv", skill_dir / "outputs")
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    monkeypatch.setattr(
        e2e, "_complete_creator_json_object_once_sync_for_e2e",
        lambda **kwargs: {"input_files": ["sample.csv"], "script": "scripts/a.py"},
    )
    accepted = e2e._prepare_e2e_trial_case(
        typed_specs=[_spec()],
        requirements_by_file={"scripts/analyze.py": [e2e.RequirementItem(
            id="R1", target_file="scripts/analyze.py", purpose="Read CSV",
        )]},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={}, requested_model=None, session=session,
    )
    assert accepted is None
    assert session.trial_case_prepared is True
    assert session.trial_case_digest == ""
    fallback = e2e._materialize_e2e_sample_value(_spec(), skill_dir=skill_dir)
    assert fallback and Path(fallback[0]).is_file()
