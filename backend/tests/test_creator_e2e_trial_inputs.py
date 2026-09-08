import csv
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from PIL import Image

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


def test_structured_fixture_rejects_wrong_item_types_but_allows_empty_values():
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

    assert validate("object", {}) is not None
    assert validate("list[string]", []) is not None
    assert validate("list[string]", [1]) is None
    assert validate("list[number]", [True]) is None
    assert validate("list[integer]", [1.5]) is None
    assert validate("list[object]", [{}]) is not None


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
    assert {item["name"] for item in result["inputs"]} == {"options", "labels", "count"}
    assert {
        item["platform_input"]["shape"]
        for item in captured["facts"]["external_inputs"]
    } == {"object", "list[string]", "integer"}


def test_trial_case_is_built_once_and_fixture_is_stable(tmp_path, monkeypatch):
    skill_dir = tmp_path / "demo"
    skill_dir.mkdir()
    session = e2e.CreatorE2ESession("session", "demo", skill_dir, skill_dir / ".venv", skill_dir / "outputs")
    requirement = e2e.RequirementItem(
        id="R1", target_file="scripts/analyze.py", purpose="Read numeric CSV input",
        constraints=[{"name": "input_file_contract", "kind": "input", "value": {
            "allowed_formats": ["csv"],
        }}],
    )
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


@pytest.mark.parametrize(("external_context", "default_values"), [
    ({"input_files": ["/tmp/real.csv"]}, {}),
    ({}, {"input_files": ["declared.csv"]}),
])
def test_no_trial_candidates_is_a_normal_frozen_session_state(
    tmp_path, monkeypatch, external_context, default_values,
):
    monkeypatch.setattr(
        e2e, "_build_e2e_trial_case",
        lambda *args, **kwargs: pytest.fail("trial builder must not run"),
    )
    session = e2e.CreatorE2ESession(
        "session", "demo", tmp_path, tmp_path / ".venv", tmp_path / "outputs",
    )

    assert e2e._prepare_e2e_trial_case(
        typed_specs=[_spec()], requirements_by_file={},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values=default_values)},
        external_context=external_context, requested_model=None, session=session,
    ) is None
    assert session.trial_case is None
    assert session.trial_case_digest == ""
    assert session.trial_case_prepared is True
    assert session.input_case_plan_failure == ""
    assert session.input_case_plan_failure_details == {}


def test_declared_default_beats_trial_case_and_unknown_format_has_no_generic_fallback(tmp_path):
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
    with pytest.raises(ValueError, match="format authority is unknown"):
        e2e._seed_initial_e2e_payload(
        [command], external_context={}, skill_dir=tmp_path,
        requirements_by_file={"scripts/analyze.py": [e2e.RequirementItem(
            id="R1", target_file="scripts/analyze.py", inputs=["input_files: list[file_path]"],
        )]},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(
            default_values={}, inputs=["input_files: list[file_path]"], artifact_contract={},
        )},
        trial_case=validated,
        )


def test_trial_builder_prompt_contains_only_supplied_frozen_facts(monkeypatch):
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    def complete(**kwargs):
        captured.update(kwargs)
        return {"status": "unsupported"}
    monkeypatch.setattr(e2e, "_complete_creator_json_object_once_sync_for_e2e", complete)
    facts = {"external_inputs": [{"platform_input": {"name": "input_files", "shape": "list[file_path]", "file_contract": {"allowed_formats": ["csv"], "min_items": 1, "max_items": 2}},
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


def test_trial_builder_rebuild_feedback_explicitly_tells_model_to_rebuild(monkeypatch):
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    monkeypatch.setattr(
        e2e, "_complete_creator_json_object_once_sync_for_e2e",
        lambda **kwargs: captured.update(kwargs) or {"version": 1, "inputs": []},
    )
    facts = {"version": 1, "external_inputs": []}
    e2e._build_e2e_trial_case(facts, rebuild_feedback={
        "previous_trial_case": {"status": "unsupported"},
        "validation_issues": [{"code": "unsupported_or_invalid_case", "message": "invalid"}],
    })

    feedback = json.loads(captured["messages"][-1]["content"])
    assert feedback["instruction"].startswith("Rebuild the Trial Case from scratch")
    assert feedback["previous_trial_case"] == {"status": "unsupported"}
    assert feedback["validation_issues"][0]["code"] == "unsupported_or_invalid_case"


def test_prepare_rebuilds_invalid_first_wave_before_review(monkeypatch):
    calls = []
    valid = {"version": 1, "inputs": [{
        "name": "title", "shape": "string",
        "fixture": {"kind": "scalar", "value": "sample"},
        "evidence_requirement_ids": [],
    }]}

    def build(_facts, **kwargs):
        calls.append(kwargs.get("rebuild_feedback"))
        return {"status": "unsupported"} if len(calls) == 1 else valid

    monkeypatch.setattr(e2e, "_build_e2e_trial_case", build)
    monkeypatch.setattr(
        e2e, "_review_e2e_trial_case",
        lambda **kwargs: {"passed": True, "issues": [], "repair_instructions": ""},
    )
    accepted = e2e._prepare_e2e_trial_case(
        typed_specs=[_spec("title", "string")], requirements_by_file={},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={}, requested_model=None, session=None,
    )

    assert accepted == valid
    assert calls[0] is None
    assert calls[1]["previous_trial_case"] == {"status": "unsupported"}
    assert calls[1]["validation_issues"][0]["code"] == "unsupported_or_invalid_case"


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


def test_case_plan_merges_nested_roots_and_derives_index_cardinality():
    plan = e2e._build_e2e_input_case_plan([
        e2e.E2ETypedInputSpec(name="fields", shape="object", required=False),
        e2e.E2ETypedInputSpec(name="fields.key", shape="string"),
        e2e.E2ETypedInputSpec(name="records", shape="list[object]", required_paths=("1.id",), min_items=2),
    ])

    assert set(plan.inputs) == {"fields", "records"}
    assert plan.inputs["fields"].required_paths == ("key",)
    assert plan.inputs["records"].min_items == 2
    fixture = e2e._synthesize_e2e_input_fixture(plan.inputs["records"])
    assert fixture["value"][1]["id"] == "sample"


def test_strict_argv_requiredness_has_independent_highest_authority():
    plan = e2e._build_e2e_input_case_plan([
        e2e.E2ETypedInputSpec(name="options", shape="object", required=True, source="requirement_graph"),
        e2e.E2ETypedInputSpec(name="options", shape="object", required=False, source="argv_schema"),
        e2e.E2ETypedInputSpec(name="options", shape="object", required=True, source="placeholder"),
    ])

    assert plan.inputs["options"].required is False
    assert plan.inputs["options"].required_source == "argv_schema"


def test_per_input_validation_preserves_valid_siblings_and_falls_back_locally():
    plan = e2e._build_e2e_input_case_plan([
        _spec("mode", "string"),
        e2e.E2ETypedInputSpec(name="fields", shape="object", required_paths=("key",)),
    ])
    trial = {"version": 1, "inputs": [
        {"name": "mode", "shape": "string", "fixture": {"kind": "scalar", "value": "fast"}, "evidence_requirement_ids": []},
        {"name": "fields", "shape": "object", "fixture": {"kind": "json_value", "value": {}}, "evidence_requirement_ids": []},
    ]}

    states = e2e._validate_e2e_trial_inputs(trial, plan=plan, requirement_ids_by_input={})
    assert states["mode"].status == "accepted"
    assert states["fields"].status == "invalid"
    assert e2e._synthesize_e2e_input_fixture(plan.inputs["fields"])["value"] == {"key": "sample"}


def test_optional_empty_object_and_list_are_valid_case_values():
    object_spec = e2e.E2EInputCaseSpec("fields", "argv_schema", "object", required=False)
    list_spec = e2e.E2EInputCaseSpec("tags", "argv_schema", "list[string]", item_shape="string", required=False)
    assert e2e._e2e_value_matches_case_spec({}, object_spec)
    assert e2e._e2e_value_matches_case_spec([], list_spec)


def test_candidate_invariant_veto_rejects_new_runtime_sentinel():
    assert e2e._e2e_candidate_invariant_veto(
        '"foo": "{{foo}}"', '"foo": "__RUNTIME_INPUT_FILES__"',
    ) == ["introduced_runtime_sentinel"]


def test_candidate_invariant_veto_rejects_model_command_changes():
    before = "# Skill\n```bash\npython scripts/run.py '{\"text\":\"{{text}}\"}'\n```\n"
    after = "# Updated prose\n```bash\npython scripts/run.py '{\"text\":\"changed\"}'\n```\n"

    assert "canonical_command_changed_by_model" in e2e._e2e_candidate_invariant_veto(
        before, after,
    )


def test_candidate_invariant_veto_rejects_frozen_boundary_changes():
    before = e2e._e2e_error(target="scripts/x.py", layer="script_exit", message="before", details={
        "frozen_provenance": {"foo": "external_context"},
        "frozen_argv_interface": {"foo": "list[string]"},
    })
    after = e2e._e2e_error(target="scripts/x.py", layer="script_exit", message="after", details={
        "frozen_provenance": {"foo": "literal"},
        "frozen_argv_interface": {"foo": "string"},
    })

    assert e2e._e2e_candidate_invariant_veto(
        "unchanged", "unchanged", original_errors=[before], new_errors=[after],
    ) == ["frozen_provenance_changed", "frozen_argv_interface_changed"]


def test_unknown_file_authority_is_creator_owned_case_plan_failure(tmp_path):
    session = e2e.CreatorE2ESession(
        "session", "demo", tmp_path, tmp_path / ".venv", tmp_path / "outputs",
    )
    with pytest.raises(e2e.E2ECaseInfrastructureError) as raised:
        e2e._prepare_e2e_trial_case(
            typed_specs=[_spec("uploads", "list[file_path]")],
            requirements_by_file={},
            skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
            external_context={}, requested_model=None, session=session,
        )

    failure = e2e._structured_failure_from_errors([
        e2e._e2e_case_infrastructure_failure(raised.value),
    ])
    assert failure["target_file"] == "creator_e2e"
    assert failure["layer"] == "e2e_case_plan"
    assert failure["details"]["repair_owner"] == "creator_e2e"
    assert failure["details"]["skill_repair_allowed"] is False
    assert session.input_case_plan_failure == "file_format_unknown"
    with pytest.raises(e2e.E2ECaseInfrastructureError, match="file_format_unknown"):
        e2e._prepare_e2e_trial_case(
            typed_specs=[_spec("uploads", "list[file_path]")],
            requirements_by_file={},
            skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
            external_context={}, requested_model=None, session=session,
        )


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


def test_input_fixture_gate_validates_rendered_argv_not_only_source(tmp_path):
    real = tmp_path / "real.csv"
    real.write_text("value\n1\n", encoding="utf-8")
    command = e2e.E2EWorkflowCommand(
        1, "SKILL.md", "scripts/compare.py", "", "python",
        {"documents": "{{documents}}"},
    )
    specs = [e2e.E2ETypedInputSpec(name="documents", shape="list[file_path]")]
    with pytest.raises(ValueError, match="rendered_argv.documents"):
        e2e._validate_e2e_input_fixtures(
            command=command, payload={"documents": [str(real)]},
            rendered_payload={"documents": [{"path": str(real)}]},
            typed_input_specs=specs,
        )


def test_legacy_file_kind_fallback_never_guesses_from_names_or_output_descriptions():
    kinds = e2e._infer_e2e_file_sample_kinds(
        name="csv_files",
        shape="list[file_path]",
        target_file="scripts/analyze.py",
        skill_md="Input: two CSV files. Output: report.pdf",
        script_content="output_path = 'outputs/report.pdf'",
    )

    assert kinds == []


@pytest.mark.parametrize(("fmt", "content_kind", "extension"), [
    ("txt", "text", ".txt"), ("md", "text", ".md"),
    ("html", "text", ".html"), ("pdf", "document", ".pdf"),
    ("docx", "document", ".docx"),
])
def test_registry_materializes_valid_text_and_document_formats(tmp_path, fmt, content_kind, extension):
    item = _item({"format": fmt, "content_kind": content_kind, "text": "Creator fixture"})
    path = Path(e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)[0])
    assert path.suffix == extension and path.stat().st_size > 0
    if fmt == "html":
        assert "<html" in path.read_text(encoding="utf-8").lower()
    elif fmt == "pdf":
        assert path.read_bytes().startswith(b"%PDF-")
    elif fmt == "docx":
        with zipfile.ZipFile(path) as archive:
            assert "word/document.xml" in archive.namelist()


@pytest.mark.parametrize(("fmt", "expected_format"), [
    ("png", "PNG"), ("jpg", "JPEG"), ("jpeg", "JPEG"),
    ("tif", "TIFF"), ("tiff", "TIFF"), ("webp", "WEBP"), ("bmp", "BMP"),
])
def test_registry_materializes_real_image_aliases(tmp_path, fmt, expected_format):
    item = _item({"format": fmt, "content_kind": "image", "width": 64, "height": 64, "mode": "RGB"})
    path = Path(e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)[0])
    with Image.open(path) as image:
        assert image.format == expected_format
        image.verify()


def test_registry_materializes_json_and_restricts_schema_to_frozen_formats(tmp_path):
    item = _item({"format": "json", "content_kind": "json", "value": {"ok": True}})
    path = Path(e2e._materialize_e2e_trial_fixture(item, skill_dir=tmp_path)[0])
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    facts = {"external_inputs": [{
        "platform_input": {"name": "images", "shape": "list[file_path]", "file_contract": {
            "allowed_formats": ["tif", "png"], "min_items": 2, "max_items": 2,
        }}, "target": {}, "requirements": [],
    }]}
    schema_text = json.dumps(e2e._e2e_trial_case_response_schema(facts))
    assert '"const": "tiff"' in schema_text and '"const": "png"' in schema_text
    assert '"const": "csv"' not in schema_text


@pytest.mark.parametrize("purpose", [
    "Read PNG image and output report.pdf",
    "Read TIFF image and output preview.png",
    "Read HTML page and output screenshot.png",
    "读取 Markdown 文件并输出 index.json",
])
def test_file_format_resolver_does_not_keyword_map_requirement_prose(purpose):
    spec = e2e._resolve_e2e_file_input_spec(
        _spec("source", "list[file_path]"),
        [e2e.RequirementItem(target_file="scripts/a.py", purpose=purpose)],
    )
    assert spec.allowed_formats == ()
    assert spec.format_source == "unknown"


def test_structured_file_contract_remains_authoritative():
    typed = _spec("uploads", "list[file_path]")
    requirement = e2e.RequirementItem(
        id="R1", target_file=typed.target_file, requirement="process the uploads",
        constraints=[{"name": "input_file_contract", "kind": "input", "value": {
            "allowed_formats": ["csv", "png"],
        }}],
    )
    resolved = e2e._resolve_e2e_file_input_spec(typed, [requirement])
    assert resolved.allowed_formats == ("csv", "png")
    assert resolved.format_source == "requirement_constraint"
    assert resolved.homogeneous is False


def test_model_plans_unknown_file_semantics_with_frozen_contract(monkeypatch):
    typed = _spec("uploads", "list[file_path]")
    requirement = e2e.RequirementItem(
        id="R1", target_file=typed.target_file,
        requirement="读取用户上传的 Markdown 文档并提取标题。",
    )
    plan = e2e._build_e2e_input_case_plan(
        [typed], requirements_by_file={typed.target_file: [requirement]},
    )
    captured = {}
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    def complete(**kwargs):
        captured.update(kwargs)
        return {"inputs": [{
            "name": "uploads", "decision": "resolved", "format": "md",
            "evidence": [{"requirement_id": "R1", "quote": "Markdown 文档"}],
            "reason": "The requirement explicitly names Markdown.",
        }]}
    monkeypatch.setattr(e2e, "_complete_creator_json_object_once_sync_for_e2e", complete)

    resolved = e2e._plan_unknown_e2e_file_formats(
        plan, requirements_by_file={typed.target_file: [requirement]}, requested_model=None,
    )

    assert resolved.inputs["uploads"].allowed_formats == ("md",)
    assert resolved.inputs["uploads"].file_format_source == "model_semantic_planner_verified_evidence"
    assert resolved.inputs["uploads"].runtime_shape == "list[file_path]"
    prompt = json.dumps(captured["messages"], ensure_ascii=False)
    assert "Markdown" in prompt and "materialization_capabilities" in prompt
    schema = json.dumps(captured["response_schema"])
    assert '"format"' in schema
    assert '"formats"' not in schema
    assert '"enum": ["resolved", "ambiguous"]' in schema
    user_payload = json.loads(captured["messages"][1]["content"])
    mentions = user_payload["unresolved_inputs"][0]["explicit_format_mentions"]
    assert list(mentions) == ["md"]
    assert mentions["md"][0]["requirement_id"] == "R1"


def test_rejected_file_semantic_plan_is_rebuilt_with_validation_feedback(monkeypatch):
    typed = _spec("uploads", "list[file_path]")
    requirement = e2e.RequirementItem(
        id="R1", target_file=typed.target_file,
        requirement="读取用户上传的 Markdown 文档并提取标题。",
    )
    plan = e2e._build_e2e_input_case_plan(
        [typed], requirements_by_file={typed.target_file: [requirement]},
    )
    calls = []
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())

    def complete(**kwargs):
        calls.append(kwargs["messages"])
        quote = "并不存在的引用" if len(calls) == 1 else "Markdown 文档"
        return {"inputs": [{
            "name": "uploads", "decision": "resolved", "format": "md",
            "evidence": [{"requirement_id": "R1", "quote": quote}],
            "reason": "The requirement explicitly names Markdown.",
        }]}

    monkeypatch.setattr(e2e, "_complete_creator_json_object_once_sync_for_e2e", complete)
    resolved = e2e._plan_unknown_e2e_file_formats(
        plan, requirements_by_file={typed.target_file: [requirement]}, requested_model=None,
    )

    assert resolved.inputs["uploads"].allowed_formats == ("md",)
    assert len(calls) == 2
    feedback = json.loads(calls[1][-1]["content"])
    assert feedback["instruction"].startswith("Rebuild")
    assert feedback["validation_issues"][0]["code"] == "format_evidence_rejected"


@pytest.mark.parametrize(("prose", "expected"), [
    ("上传 Markdown 文档", "md"),
    ("read a text/csv upload", "csv"),
    ("accept application/json", "json"),
    ("process a JPG image", "jpeg"),
    ("读取 .docx 文件", "docx"),
])
def test_explicit_format_mentions_are_derived_from_fixture_registry(prose, expected):
    mentions = e2e._explicit_file_format_mentions([{"id": "R1", "text": prose}])
    assert list(mentions) == [expected]


def test_every_registered_format_exposes_registry_derived_evidence_terms():
    canonical_formats = {handler.canonical_format for handler in e2e.FILE_FIXTURE_FORMATS.values()}
    for fmt in canonical_formats:
        terms = e2e._file_format_evidence_terms(fmt)
        assert fmt in terms
        assert e2e._resolve_file_fixture_handler(fmt).extension in terms


def test_model_format_proposal_requires_literal_matching_evidence(monkeypatch):
    typed = _spec("uploads", "list[file_path]")
    requirement = e2e.RequirementItem(
        id="R1", target_file=typed.target_file,
        requirement="读取用户上传的 Markdown 文档并提取标题。",
    )
    plan = e2e._build_e2e_input_case_plan(
        [typed], requirements_by_file={typed.target_file: [requirement]},
    )
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    monkeypatch.setattr(
        e2e, "_complete_creator_json_object_once_sync_for_e2e",
        lambda **kwargs: {"inputs": [{
            "name": "uploads", "decision": "resolved", "format": "txt",
            "evidence": [{"requirement_id": "R1", "quote": "Markdown 文档"}],
            "reason": "Plain text is broadly compatible.",
        }]},
    )

    resolved = e2e._plan_unknown_e2e_file_formats(
        plan, requirements_by_file={typed.target_file: [requirement]}, requested_model=None,
    )

    assert resolved.inputs["uploads"].allowed_formats == ()
    assert resolved.inputs["uploads"].file_format_source == "unknown"


def test_model_may_abstain_when_file_format_is_not_explicit(monkeypatch):
    typed = _spec("upload", "file_path")
    requirement = e2e.RequirementItem(
        id="R1", target_file=typed.target_file, requirement="处理用户上传的文档。",
    )
    plan = e2e._build_e2e_input_case_plan(
        [typed], requirements_by_file={typed.target_file: [requirement]},
    )
    monkeypatch.setattr(e2e, "route_model", lambda *args, **kwargs: type("Route", (), {"model": "test"})())
    captured = {}
    def complete(**kwargs):
        captured.update(kwargs)
        return {"inputs": [{
            "name": "upload", "decision": "unknown", "format": "", "evidence": [],
            "reason": "No explicit format is stated.",
        }]}
    monkeypatch.setattr(e2e, "_complete_creator_json_object_once_sync_for_e2e", complete)

    resolved = e2e._plan_unknown_e2e_file_formats(
        plan, requirements_by_file={typed.target_file: [requirement]}, requested_model=None,
    )

    assert resolved == plan
    schema = json.dumps(captured["response_schema"])
    assert '"enum": ["unknown"]' in schema
    assert json.loads(captured["messages"][1]["content"])["unresolved_inputs"][0]["explicit_format_mentions"] == {}


def test_frozen_requirement_without_format_remains_unknown():
    resolved = e2e._resolve_e2e_file_input_spec(
        _spec("upload", "file_path"),
        [e2e.RequirementItem(target_file="scripts/analyze.py", requirement="处理用户上传的文件")],
    )
    assert resolved.allowed_formats == ()
    assert resolved.format_source == "unknown"


def test_requirement_prose_survives_production_graph_round_trip_for_model_planning():
    prose = "Skill 必须读取两个 CSV 文件作为输入数据源。"
    original = e2e.RequirementItem(
        id="R1", target_file="scripts/main.py", requirement=prose,
    )
    graph = e2e.RequirementGraph(requirements=[original])

    # Exercise the same model JSON transport and parser/normalizer used for a
    # persisted frozen ResponsibilityGraph, rather than resolving the original
    # in-memory object.
    serialized = json.dumps(graph.model_dump(mode="json"), ensure_ascii=False)
    loaded = e2e.normalize_requirement_graph(
        e2e.parse_requirement_graph_result(serialized),
    )
    assert loaded.requirements[0].requirement == prose

    payload = e2e.function_item_prompt_payload(original)
    assert payload["requirement"] == prose
    assert payload["text"] == ""

    resolved = e2e._resolve_e2e_file_input_spec(
        e2e.E2ETypedInputSpec(
            name="input_files", shape="list[file_path]", target_file="scripts/main.py",
        ),
        loaded.requirements,
    )
    assert resolved.allowed_formats == ()
    assert resolved.format_source == "unknown"


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
        e2e.RequirementItem(
            id="R1", target_file="scripts/analyze.py", purpose="Read CSV",
            constraints=[{"name": "input_file_contract", "kind": "input", "value": {
                "allowed_formats": ["csv"],
            }}],
        ),
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
            constraints=[{"name": "input_file_contract", "kind": "input", "value": {
                "allowed_formats": ["csv"],
            }}],
        )]},
        skill_plan_entries={"scripts/analyze.py": SimpleNamespace(default_values={})},
        external_context={}, requested_model=None, session=session,
    )
    assert accepted is not None
    assert session.trial_case_prepared is True
    assert session.trial_case_digest
    assert accepted["inputs"][0]["fixture"]["files"][0]["format"] == "csv"
    with pytest.raises(ValueError, match="format authority is unknown"):
        e2e._materialize_e2e_sample_value(_spec(), skill_dir=skill_dir)
