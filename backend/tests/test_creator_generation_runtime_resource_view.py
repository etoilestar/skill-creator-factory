from backend.services.creator.generation import _build_generate_file_prompt


def _skill_md_prompt_for_asset(source: str) -> str:
    asset_path = "assets/example-resource.bin"
    blueprint = f"""### SkillPlan / 文件职责计划
- path: `SKILL.md`
  role: skill_overview
  inputs: [user_request]
  outputs: [workflow]
  dependencies: [{asset_path}]
  required_capabilities: []
  forbidden_capabilities: [hidden_runtime_protocol]
  references: []
- path: `{asset_path}`
  role: asset
  source: {source}
  inputs: []
  outputs: []
  dependencies: []
  required_capabilities: []
  forbidden_capabilities: [runtime_execution]
  references: []
"""
    messages = _build_generate_file_prompt(
        file_path="SKILL.md",
        skill_name="resource-consumer",
        purpose="Use the confirmed static resource at runtime.",
        blueprint_text=blueprint,
        conversation_history=[],
    )
    return "\n".join(str(message.get("content") or "") for message in messages)


def test_skill_md_writer_prompt_keeps_user_upload_asset_identity_runtime_only():
    prompt = _skill_md_prompt_for_asset("user_upload")

    assert "SKILL.MD RUNTIME RESOURCE VIEW" in prompt
    assert (
        "Creation-stage materialization metadata is not part of the runtime user "
        "instructions"
    ) in prompt
    assert "do not describe source=user_upload" in prompt
    assert "do not instruct the user to upload" in prompt
    assert "assets/example-resource.bin" in prompt


def test_skill_md_writer_prompt_applies_same_runtime_view_to_bundled_asset():
    prompt = _skill_md_prompt_for_asset("bundled")

    assert "describe only what the already-present static resource is used for at runtime" in prompt
    assert "do not describe source=bundled" in prompt
    assert "do not describe Creator-stage upload/materialization workflow" in prompt
    assert "assets/example-resource.bin" in prompt
