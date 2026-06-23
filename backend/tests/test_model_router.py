"""Tests for capability-aware model routing."""

from unittest.mock import patch


def test_creator_scripts_route_to_code_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_generation_model

    with patch.object(settings, "code_model", "coder-model"):
        route = route_creator_file_generation_model(
            file_path="scripts/main.py",
            purpose="处理数据",
            requested_model="general-model",
        )

    assert route.task == "code"
    assert route.model == "coder-model"


def test_skill_md_illustration_intent_routes_to_text_model():
    from backend.config import settings
    from backend.services.model_router import infer_creator_file_task, route_creator_file_generation_model

    with patch.object(settings, "text_model", "text-model"), patch.object(settings, "image_model", "image-model"):
        route = route_creator_file_generation_model(
            file_path="SKILL.md",
            purpose="Protocol for image generation and illustration workflow",
            requested_model="general-model",
        )

    assert infer_creator_file_task("SKILL.md", "illustration / image generation") == "text"
    assert route.task == "text"
    assert route.model == "text-model"


def test_skill_md_chinese_image_intent_routes_to_text_model():
    from backend.services.model_router import infer_creator_file_task

    assert infer_creator_file_task("SKILL.md", "生成插图/图片，调用 Stable Diffusion") == "text"


def test_references_image_intent_routes_to_text_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_generation_model

    with patch.object(settings, "text_model", "text-model"), patch.object(settings, "image_model", "image-model"):
        route = route_creator_file_generation_model(
            file_path="references/illustration-guide.md",
            purpose="image prompt reference for illustrations",
            requested_model="general-model",
        )

    assert route.task == "text"
    assert route.model == "text-model"


def test_generate_image_script_intent_routes_to_code_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_generation_model

    with patch.object(settings, "code_model", "coder-model"), patch.object(settings, "image_model", "image-model"):
        route = route_creator_file_generation_model(
            file_path="scripts/generate_image.py",
            purpose="根据用户描述生成 image/插图",
            requested_model="general-model",
        )

    assert route.task == "code"
    assert route.model == "coder-model"


def test_creator_file_generation_never_returns_image_task_for_file_types():
    from backend.services.model_router import infer_creator_file_task

    cases = [
        ("SKILL.md", "illustration image 插图"),
        ("references/prompt.md", "generate image"),
        ("config/settings.yaml", "image model settings"),
        ("scripts/generate_image.py", "image 插图"),
    ]

    assert [infer_creator_file_task(path, purpose) for path, purpose in cases] == ["text", "text", "text", "code"]


def test_requested_text_model_is_fallback_when_no_specialized_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_model

    with patch.object(settings, "code_model", None), patch.object(settings, "image_model", None), patch.object(settings, "text_model", None):
        route = route_creator_file_model(
            file_path="SKILL.md",
            purpose="说明工作流",
            requested_model="selected-model",
        )

    assert route.task == "text"
    assert route.model == "selected-model"


def test_model_routing_json_can_override_creator_path_with_text_model():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_generation_model

    routing_json = '{"creator_paths": {"references/*.md": "doc-model"}}'
    with patch.object(settings, "model_routing_json", routing_json):
        route = route_creator_file_generation_model(
            file_path="references/spec.md",
            purpose="参考文档",
            requested_model="general-model",
        )

    assert route.task == "text"
    assert route.model == "doc-model"
    assert "override" in route.reason


def test_model_routing_json_image_task_override_is_ignored_for_creator_files():
    from backend.config import settings
    from backend.services.model_router import route_creator_file_generation_model

    routing_json = '{"creator_paths": {"SKILL.md": "image"}}'
    with patch.object(settings, "model_routing_json", routing_json), patch.object(settings, "text_model", "text-model"), patch.object(settings, "image_model", "image-model"):
        route = route_creator_file_generation_model(
            file_path="SKILL.md",
            purpose="image generation",
            requested_model="general-model",
        )

    assert route.task == "text"
    assert route.model == "text-model"


def test_sandbox_plan_code_action_keeps_final_response_on_text_model():
    from backend.config import settings
    from backend.services.model_router import infer_sandbox_response_task, route_model

    plan = {"tasks": [{"action": "run_command", "command": "python scripts/build.py"}]}
    task = infer_sandbox_response_task(body_prompt="", user_text="运行脚本", plan=plan)
    with patch.object(settings, "text_model", "text-a"):
        route = route_model(task, requested_model="general", reason="test")

    assert task == "text"
    assert route.model == "text-a"


def test_sandbox_image_upload_routes_to_vision_model():
    from backend.config import settings
    from backend.services.model_router import infer_sandbox_response_task, route_model

    task = infer_sandbox_response_task(
        body_prompt="",
        user_text="分析这张图片",
        plan={},
        input_files=[{"filename": "photo.png", "path": "inputs/session/photo.png"}],
    )
    with patch.object(settings, "vision_model", "qwen-vl"):
        route = route_model(task, requested_model="general", reason="test")

    assert task == "vision"
    assert route.model == "qwen-vl"


def test_generate_file_prompt_uses_tool_context_only_for_scripts(monkeypatch):
    from backend.routers import creator

    calls = {"count": 0}

    def fake_contract(*args, **kwargs):
        calls["count"] += 1
        return {}

    monkeypatch.setattr(creator, "_script_local_contract_payload", fake_contract)
    creator._build_generate_file_prompt(
        "references/image-guide.md",
        "demo_skill",
        "image illustration reference",
        "# blueprint",
        [],
    )

    assert calls["count"] == 0


def test_complete_chat_once_would_receive_text_model_for_skill_md(monkeypatch):
    import asyncio
    from backend.config import settings
    from backend.routers import creator
    from backend.services.model_router import route_creator_file_generation_model

    seen = {}

    async def fake_complete(messages, model):
        seen["model"] = model
        return "---\nname: demo\ndescription: demo\n---\nBody"

    monkeypatch.setattr(settings, "text_model", "text-model")
    monkeypatch.setattr(settings, "image_model", "image-model")
    monkeypatch.setattr(creator, "complete_chat_once", fake_complete)
    route = route_creator_file_generation_model(
        file_path="SKILL.md",
        purpose="生成图片/illustration workflow",
        requested_model="general-model",
    )

    content = asyncio.run(creator._complete_creator_file_generation(
        messages=[{"role": "user", "content": "write SKILL.md"}],
        model=route.model,
        skill_name="demo",
        file_path="SKILL.md",
        prompt_variant="standard",
        retry_index=0,
    ))

    assert content.startswith("---")
    assert seen["model"] == "text-model"
    assert seen["model"] != "image-model"
