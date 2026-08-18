from backend.services.creator.e2e import (
    _artifact_runtime_state,
    _e2e_behavior_fingerprint,
    _e2e_candidate_improved,
    _e2e_failure_position,
    _runtime_binding_trace,
    _verified_bindings_from_runtime_trace,
)
from backend.services.creator.common import E2EWorkflowCommand
from backend.services.creator import e2e


def _failure(filesystem_trace, *, code="artifact_not_created", layer=None, actual=""):
    filesystem_trace = dict(filesystem_trace)
    filesystem_trace.setdefault("failure_code", code)
    return (
        "E2E_REPAIR_TARGET=scripts/x.py\n"
        f"E2E_LAYER={layer or code}\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": 1,
            "target_file": "scripts/x.py",
            "layer": layer or code,
            "actual": actual,
            "details": {
                "failure_code": code,
                "filesystem_trace": filesystem_trace,
            },
        })
    )


def _positioned_failure(*, step, target, layer, code, actual):
    return (
        f"E2E_REPAIR_TARGET={target}\n"
        f"E2E_LAYER={layer}\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": step,
            "target_file": target,
            "target_region": "command JSON argv" if layer == "argv_schema_error" else "frozen terminal binding",
            "layer": layer,
            "actual": actual,
            "details": {"failure_code": code},
        })
    )


def _command_binding_to_script_exit_failures(*, after_boundary_invalid=False):
    before = (
        "E2E_REPAIR_TARGET=SKILL.md\n"
        "E2E_LAYER=command_binding\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": 1,
            "target_file": "SKILL.md",
            "layer": "command_binding",
            "actual": "command binding invalid",
            "details": {
                "failure_code": "command_binding_invalid",
                "argv_shape_valid": False,
                "command_script_interface_aligned": False,
            },
        })
    )
    after_details = {"failure_code": "script_exit"}
    if after_boundary_invalid:
        after_details["argv_shape_valid"] = False
    after = (
        "E2E_REPAIR_TARGET=scripts/overview_generator.py\n"
        "E2E_LAYER=script_exit\n"
        "E2E_STRUCTURED_FAILURE="
        + __import__("json").dumps({
            "failed_step_index": 1,
            "target_file": "scripts/overview_generator.py",
            "layer": "script_exit",
            "actual": "runtime failed",
            "stderr": (
                "Traceback (most recent call last):\n"
                '  File "scripts/overview_generator.py", line 12, in run\n'
                "    file_path = input_files[0]['path']\n"
                "TypeError: string indices must be integers\n"
            ),
            "details": after_details,
        })
    )
    return before, after


def test_fixed_command_binding_advances_to_real_script_failure():
    before, after = _command_binding_to_script_exit_failures()

    assert _e2e_candidate_improved([before], [after], target_file="SKILL.md") is True


def test_new_script_traceback_is_not_progress_when_after_boundary_is_invalid():
    before, after = _command_binding_to_script_exit_failures(after_boundary_invalid=True)

    assert _e2e_candidate_improved([before], [after], target_file="SKILL.md") is False


def test_command_binding_layer_precedes_script_exit():
    before, after = _command_binding_to_script_exit_failures()

    assert _e2e_failure_position(before) < _e2e_failure_position(after)


def test_collection_placeholder_boundary_rejects_double_wrap(tmp_path):
    source = tmp_path / "a.csv"
    source.write_text("name\nAda\n", encoding="utf-8")
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python",
        {"input_files": ["{{input_files}}"]},
    )
    payload = {"input_files": [str(source)]}
    rendered = e2e._render_e2e_command_payload(command, payload=payload)
    trace = _runtime_binding_trace(
        command=command, payload=payload, rendered_payload=rendered, value_provenance={},
    )
    facts = e2e._e2e_runtime_boundary_facts(
        command=command, payload=payload, rendered_payload=rendered,
        script_content='EXPECTED_TYPES = {"input_files": "list[str]"}\n',
        runtime_binding_trace=trace,
    )

    assert rendered == {"input_files": [[str(source)]]}
    assert facts["argv_shape_valid"] is False
    assert facts["command_script_interface_aligned"] is False
    assert facts["repair_target"] == "SKILL.md"
    assert facts["issues"][0]["error_code"] == "collection_placeholder_double_wrapped"


def test_whole_collection_placeholder_preserves_list_shape(tmp_path):
    source = tmp_path / "a.csv"
    source.write_text("name\nAda\n", encoding="utf-8")
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python",
        {"input_files": "{{input_files}}"},
    )
    payload = {"input_files": [str(source)]}
    rendered = e2e._render_e2e_command_payload(command, payload=payload)

    assert rendered == {"input_files": [str(source)]}
    assert isinstance(rendered["input_files"], list)
    assert not isinstance(rendered["input_files"][0], list)


def test_runtime_file_fixture_check_accepts_existing_platform_file(tmp_path):
    source = tmp_path / "input.csv"
    source.write_text("name\nAda\n", encoding="utf-8")
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python",
        {"input_files": "{{input_files}}"},
    )
    payload = {"input_files": [str(source)]}
    rendered = e2e._render_e2e_command_payload(command, payload=payload)
    trace = _runtime_binding_trace(
        command=command, payload=payload, rendered_payload=rendered, value_provenance={},
    )

    facts = e2e._e2e_runtime_boundary_facts(
        command=command, payload=payload, rendered_payload=rendered,
        script_content="", runtime_binding_trace=trace,
    )

    assert facts["fixture_valid"] is True


def test_non_runtime_file_absolute_strings_are_not_fixture_checked():
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python",
        {"paths": "{{paths}}"},
    )
    payload = {"paths": ["/api/v1/a", "/api/v1/b"]}
    rendered = e2e._render_e2e_command_payload(command, payload=payload)
    trace = _runtime_binding_trace(
        command=command, payload=payload, rendered_payload=rendered, value_provenance={},
    )

    facts = e2e._e2e_runtime_boundary_facts(
        command=command, payload=payload, rendered_payload=rendered,
        script_content="", runtime_binding_trace=trace,
    )

    assert facts["fixture_valid"] is True


def test_platform_runtime_files_seed_is_real_file_collection(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "SKILL.md").write_text("# CSV workflow\n", encoding="utf-8")
    (tmp_path / "scripts" / "run.py").write_text("EXPECTED_TYPES = {'input_files': 'list[str]'}\n", encoding="utf-8")
    command = E2EWorkflowCommand(
        1, "SKILL.md", "scripts/run.py", "python scripts/run.py ...", "python",
        {"input_files": "{{input_files}}"},
    )

    payload = e2e._seed_initial_e2e_payload([command], skill_dir=tmp_path)

    assert isinstance(payload["input_files"], list)
    assert payload["input_files"]
    assert all(__import__("pathlib").Path(path).is_file() for path in payload["input_files"])
    assert "sample value" not in payload["input_files"]


def test_new_breakpoint_is_false_progress_when_boundary_remains_invalid():
    before = _positioned_failure(step=1, target="scripts/x.py", layer="script_exit", code="script_exit", actual="AttributeError")
    after = _positioned_failure(step=1, target="scripts/x.py", layer="script_exit", code="script_exit", actual="KeyError")
    for name, value in (("before", before), ("after", after)):
        marker, data = value.split("E2E_STRUCTURED_FAILURE=", 1)
        parsed = __import__("json").loads(data)
        parsed["details"]["argv_shape_valid"] = False
        if name == "before":
            before = marker + "E2E_STRUCTURED_FAILURE=" + __import__("json").dumps(parsed)
        else:
            after = marker + "E2E_STRUCTURED_FAILURE=" + __import__("json").dumps(parsed)

    assert _e2e_candidate_improved([before], [after], target_file="scripts/x.py") is False


def test_terminal_phase_after_argv_failure_is_progress_but_same_breakpoint_is_not():
    argv_failure = _positioned_failure(
        step=1,
        target="SKILL.md",
        layer="argv_schema_error",
        code="argv_schema_error",
        actual="TypeError: argv must be a non-empty list",
    )
    repeated_argv_failure = _positioned_failure(
        step=1,
        target="SKILL.md",
        layer="argv_schema_error",
        code="argv_schema_error",
        actual="TypeError: argv must be a non-empty list",
    )
    terminal_failure = _positioned_failure(
        step=3,
        target="INTERFACE",
        layer="terminal_output_commit",
        code="upstream_interface_contract_conflict",
        actual="invalid emission value for sink: text",
    )

    improved = _e2e_candidate_improved([argv_failure], [terminal_failure], target_file="SKILL.md")
    assert improved is True
    assert {"candidate_retained": improved, "candidate_rolled_back": not improved} == {
        "candidate_retained": True,
        "candidate_rolled_back": False,
    }
    assert _e2e_failure_position(terminal_failure) > (2, 999)
    assert _e2e_candidate_improved([argv_failure], [repeated_argv_failure], target_file="SKILL.md") is False


def test_runtime_binding_trace_uses_source_root_provenance_and_verified_binding():
    command = E2EWorkflowCommand(
        ordinal=1,
        runner="python",
        script_path="scripts/x.py",
        argv_template={"content": "{{story_text}}"},
        raw_command="python scripts/x.py '{}'",
        source_path="SKILL.md",
    )
    trace = _runtime_binding_trace(
        command=command,
        payload={"story_text": "hello"},
        rendered_payload={"content": "hello"},
        value_provenance={"story_text": {"producer_step": 1, "producer_script": "scripts/a.py", "source_kind": "stdout"}},
    )

    assert trace["content"]["source_root"] == "story_text"
    assert trace["content"]["source_provenance"]["producer_step"] == 1
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=trace,
        script_content='def run(args):\n    return args["content"]\n',
        script_path="scripts/x.py",
    ) == {"content": "story_text"}


def test_artifact_progress_recognizes_creation_and_existing_reported_path():
    old = _failure({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})
    new = _failure({
        "created_files": [{"relative_path": "out.txt"}],
        "modified_files": [],
        "reported_paths": ["missing.txt"],
        "resolved_reported_paths": [{"raw_path": "missing.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is True

    old_missing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["a.txt"],
        "resolved_reported_paths": [{"raw_path": "a.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    new_missing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["b.txt"],
        "resolved_reported_paths": [{"raw_path": "b.txt", "exists": False}],
    }, code="artifact_return_path_missing")
    assert _e2e_candidate_improved([old_missing], [new_missing], target_file="scripts/x.py") is False

    new_existing = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": ["ok.txt"],
        "resolved_reported_paths": [{"raw_path": "ok.txt", "exists": True}],
    }, code="artifact_validation_failed")
    assert _e2e_candidate_improved([old_missing], [new_existing], target_file="scripts/x.py") is True


def test_dynamic_baseline_uses_recent_failure_not_initial_failure():
    initial = _failure({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})
    progressed = _failure({
        "created_files": [{"relative_path": "out.dat"}],
        "modified_files": [],
        "reported_paths": ["missing.dat"],
        "resolved_reported_paths": [{"raw_path": "missing.dat", "exists": False}],
    }, code="artifact_return_path_missing")

    assert _e2e_candidate_improved([initial], [progressed], target_file="scripts/x.py") is True
    assert _e2e_candidate_improved([progressed], [progressed], target_file="scripts/x.py") is False
    assert _e2e_behavior_fingerprint(progressed, target_file="scripts/x.py") == _e2e_behavior_fingerprint(progressed, target_file="scripts/x.py")
    assert _artifact_runtime_state({"created_files": [], "modified_files": [], "reported_paths": [], "resolved_reported_paths": []})["created_count"] == 0


def test_non_artifact_temp_file_does_not_count_as_progress():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="script_exit", layer="script_exit", actual="same traceback")
    new = _failure({
        "created_files": [{"relative_path": "tmp/cache.tmp"}],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="script_exit", layer="script_exit", actual="same traceback")

    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is False


def test_artifact_created_file_counts_as_progress_for_artifact_failure():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="artifact_not_created")
    new = _failure({
        "created_files": [{"relative_path": "outputs/result.dat"}],
        "modified_files": [],
        "reported_paths": ["missing/result.dat"],
        "resolved_reported_paths": [{"raw_path": "missing/result.dat", "exists": False}],
    }, code="artifact_return_path_mismatch")

    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is True


def test_stdout_contract_temp_file_is_not_artifact_progress():
    old = _failure({
        "created_files": [],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="stdout_contract", layer="stdout_contract", actual="stdout serialization failed")
    new = _failure({
        "created_files": [{"relative_path": "tmp/cache.tmp"}],
        "modified_files": [],
        "reported_paths": [],
        "resolved_reported_paths": [],
    }, code="stdout_contract", layer="stdout_contract", actual="stdout serialization failed")

    assert "artifact_" not in "stdout_contract"
    assert _e2e_candidate_improved([old], [new], target_file="scripts/x.py") is False


def test_synthetic_fixture_cannot_be_verified_but_external_context_can():
    command = E2EWorkflowCommand(
        ordinal=1,
        runner="python",
        script_path="scripts/x.py",
        argv_template={"topic": "{{user_request}}"},
        raw_command="python scripts/x.py '{}'",
        source_path="SKILL.md",
    )
    synthetic_trace = _runtime_binding_trace(
        command=command,
        payload={"user_request": "sample"},
        rendered_payload={"topic": "sample"},
        value_provenance={"user_request": {"producer_step": 0, "source_kind": "synthetic_fixture"}},
    )
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=synthetic_trace,
        script_content='def run(args):\n    return args["topic"]\n',
        script_path="scripts/x.py",
    ) == {}

    external_trace = _runtime_binding_trace(
        command=command,
        payload={"user_request": "real"},
        rendered_payload={"topic": "real"},
        value_provenance={"user_request": {"producer_step": 0, "source_kind": "external_context"}},
    )
    assert _verified_bindings_from_runtime_trace(
        runtime_binding_trace=external_trace,
        script_content='def run(args):\n    return args["topic"]\n',
        script_path="scripts/x.py",
    ) == {"topic": "user_request"}


def test_stdout_fields_final_contract_does_not_force_artifact_scope():
    from backend.services.creator.e2e import _allowed_edit_scope_for_failure, _is_artifact_validation_failure

    class Entry:
        artifact_contract = {"stdout_fields": ["text"], "final": True}

    assert _is_artifact_validation_failure(
        error="stdout JSON contains error field",
        reported_paths=[],
        entry=Entry(),
    ) is False
    assert _allowed_edit_scope_for_failure(
        target_path="scripts/text.py",
        failure_code="stdout_contract",
        failure_layer="stdout_contract",
        is_artifact_failure=False,
        created_count=1,
        missing_reported_count=0,
    ) == ["current script stdout serialization and return logic"]
