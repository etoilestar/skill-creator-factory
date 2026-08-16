import json

import pytest

from backend.routers.chat_models import Message
from backend.routers.sandbox.io_manifest import SandboxChatRequest, build_input_envelope
from backend.routers.sandbox.result_manifest import build_result_manifest
from backend.routers.sandbox.workflow_dataflow import _workflow_context_from_input_envelope


@pytest.mark.asyncio
async def test_instruction_analysis_receives_file_only_envelope(monkeypatch):
    from backend.routers.sandbox.instruction_analysis import _run_instruction_analysis_round

    captured = []

    async def complete(messages, _model):
        captured.extend(messages)
        return json.dumps({
            "intent": "use supplied input", "scope": "input_files", "constraints": [],
            "output_requirements": [], "complexity": "simple", "requires_script_execution": False,
        })

    monkeypatch.setattr("backend.routers.sandbox.instruction_analysis.complete_chat_once", complete)
    envelope = {"user_request": "", "input_files": [{"mime_type": "image/x-future"}], "fields": {"key": "value"}}
    request = SandboxChatRequest(messages=[Message(role="user", content="")])
    result = await _run_instruction_analysis_round(
        body_prompt="skill", request=request, model="model", input_envelope=envelope,
    )
    assert result["intent"] == "use supplied input"
    assert json.dumps(envelope, ensure_ascii=False) in captured[-1]["content"]


def test_file_only_text_preview_and_path_semantics(tmp_path):
    uploaded = tmp_path / "inputs" / "session" / "opaque.data"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_text("readable input", encoding="utf-8")
    request = SandboxChatRequest(
        messages=[Message(role="user", content="")],
        input_files=[{"path": "inputs/session/opaque.data", "mime_type": "application/x-future"}],
    )
    envelope = build_input_envelope(request, tmp_path)
    item = envelope["input_files"][0]
    assert envelope["user_request"] == ""
    assert item["model_access"] == "text_preview"
    assert item["text_preview"] == "readable input"
    assert item["relative_to_input_dir"] == "session/opaque.data"
    assert item["relative_to_input_session_dir"] == "opaque.data"


def test_fields_options_and_mime_image_without_input_suffix(tmp_path):
    image = tmp_path / "inputs" / "session" / "blob"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"binary-image")
    request = SandboxChatRequest(
        messages=[], fields={"subject": "value"}, options={"quality": 2},
        input_files=[{"path": "inputs/session/blob", "mime_type": "image/x-future"}],
    )
    envelope = build_input_envelope(request, tmp_path)
    assert envelope["fields"] == {"subject": "value"}
    assert envelope["options"] == {"quality": 2}
    assert envelope["input_files"][0]["media_family"] == "image/*"
    assert envelope["input_files"][0]["model_access"] == "inline_image"
    assert _workflow_context_from_input_envelope(envelope)["fields"] == {"subject": "value"}


def test_result_manifest_preserves_stdout_and_artifact_mime(tmp_path):
    artifact = tmp_path / "outputs" / "result.json"
    artifact.parent.mkdir()
    artifact.write_text("{}", encoding="utf-8")
    manifest = build_result_manifest({
        "stdout": json.dumps({"unrestricted_business_field": {"value": 1}}),
        "output_files": [{"path": "outputs/result.json", "url": "/download"}],
    }, tmp_path)
    assert manifest["structured_outputs"][0]["data"]["unrestricted_business_field"]["value"] == 1
    assert manifest["artifacts"][0]["mime_type"] == "application/json"


@pytest.mark.asyncio
async def test_frontloaded_decisions_receive_structured_envelope(monkeypatch):
    from backend.routers.sandbox.metadata_decisions import (
        _run_child_skill_selection_round, _run_metadata_round,
    )
    from backend.routers.sandbox.resource_catalog import _run_resource_selection_round

    calls = []

    async def complete(messages, _model):
        calls.append(messages)
        content = messages[-1]["content"]
        if "valid_child_refs" in content:
            return '{"need_child":false,"child_ref":"","reason":"ok"}'
        if "resource_catalog" in content:
            return '{"need_resources":false,"resource_handles":[],"reason":"ok"}'
        return '{"need_body":true}'

    monkeypatch.setattr("backend.routers.sandbox.metadata_decisions.complete_chat_once", complete)
    monkeypatch.setattr("backend.routers.sandbox.resource_catalog.complete_chat_once", complete)
    request = SandboxChatRequest(messages=[], fields={"field": "value"})
    envelope = build_input_envelope(request, None)
    envelope["options"] = {"mode": "value"}
    envelope["resources"] = [{"handle": "provided"}]

    assert await _run_metadata_round(
        metadata_prompt="metadata", request=request, model="model", input_envelope=envelope,
    )
    await _run_child_skill_selection_round(
        parent_metadata_prompt="## Child Skills Manifest\n- ref: `child`",
        request=request, model="model", input_envelope=envelope,
    )
    await _run_resource_selection_round(
        body_prompt="skill", request=request, model="model",
        resource_catalog=[{
            "resource_handle": "resource:0", "path": "references/item", "kind": "reference",
            "allowed_actions": ["read_resource"],
        }],
        input_envelope=envelope,
    )
    serialized = "\n".join(message["content"] for messages in calls for message in messages)
    assert '"fields": {"field": "value"}' in serialized
    assert '"options": {"mode": "value"}' in serialized
    assert '"resources": [{"handle": "provided"}]' in serialized


@pytest.mark.asyncio
async def test_cleanup_removes_input_and_output_session(monkeypatch, tmp_path):
    from backend.routers.sandbox.stream_pipeline import delete_sandbox_inputs

    for area in ("inputs", "outputs"):
        directory = tmp_path / area / "session"
        directory.mkdir(parents=True)
        (directory / "item").write_text("data", encoding="utf-8")
    monkeypatch.setattr("backend.routers.sandbox.stream_pipeline._skill_root_for_name", lambda _name: tmp_path)

    assert await delete_sandbox_inputs("skill", "session") == {"deleted": True}
    assert not (tmp_path / "inputs" / "session").exists()
    assert not (tmp_path / "outputs" / "session").exists()
    assert await delete_sandbox_inputs("skill", "session") == {"deleted": True}
