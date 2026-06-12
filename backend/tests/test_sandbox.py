"""Tests for sandbox skill loading and message assembly.

Verifies that after a skill is loaded in sandbox mode, user questions
are correctly assembled into messages (system prompt with body_prompt,
user messages, optional strict-mode instructions) before being sent to
the LLM model.
"""
import sys
import os
from pathlib import Path

# Add project root to Python path so `from backend.services...` works
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

from backend.routers.chat_models import ChatRequest


_REAL_SKILL_ROOT = Path(__file__).parent.parent.parent / "skills"
_REAL_ANIMAL_SKILL = _REAL_SKILL_ROOT / "animal-world-story-generator"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_skill_dir(tmp_path: Path, name: str = "test-skill", body: str = "# Test Skill Body") -> Path:
    skill_dir = tmp_path / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: A test skill for sandbox\n---\n{body}",
        encoding="utf-8",
    )
    return skill_dir


def _make_chat_request(user_text: str, **overrides) -> ChatRequest:
    return ChatRequest(
        messages=[{"role": "user", "content": user_text}],
        **overrides,
    )


# ---------------------------------------------------------------------------
# Test: build_skill_context assembles correct context dict
# ---------------------------------------------------------------------------

class TestBuildSkillContext:
    """Test that build_skill_context produces the expected context dict."""

    def test_context_has_required_keys(self, tmp_path):
        skill_dir = _make_skill_dir(tmp_path, "my-skill")
        with patch("backend.routers.sandbox.stream_pipeline._skill_root_for_name", return_value=skill_dir), \
             patch("backend.routers.sandbox.stream_pipeline.load_skill_metadata_prompt", return_value="metadata-prompt"):
            from backend.routers.sandbox.stream_pipeline import build_skill_context
            ctx = build_skill_context("my-skill")

        assert ctx["skill_name"] == "my-skill"
        assert ctx["metadata_prompt"] == "metadata-prompt"
        assert callable(ctx["body_loader"])
        assert callable(ctx["child_body_loader"])
        assert ctx["force_body"] is False
        assert ctx["enable_action_execution"] is True
        assert ctx["require_action_confirmation"] is False
        assert ctx["execution_root"] == skill_dir
        assert ctx["strict_skill_execution"] is True
        assert ctx["enable_resource_preload"] is True

    def test_context_with_real_animal_skill(self):
        if not _REAL_ANIMAL_SKILL.exists():
            pytest.skip(f"Real skill not found: {_REAL_ANIMAL_SKILL}")
        with patch("backend.routers.sandbox.stream_pipeline._skill_root_for_name", return_value=_REAL_ANIMAL_SKILL), \
             patch("backend.routers.sandbox.stream_pipeline.load_skill_metadata_prompt", return_value="metadata-prompt"):
            from backend.routers.sandbox.stream_pipeline import build_skill_context
            ctx = build_skill_context("animal-world-story-generator")

        assert ctx["skill_name"] == "animal-world-story-generator"
        assert ctx["execution_root"] == _REAL_ANIMAL_SKILL
        assert ctx["enable_action_execution"] is True
        assert ctx["strict_skill_execution"] is True

    def test_body_loader_returns_body_prompt(self, tmp_path):
        skill_dir = _make_skill_dir(tmp_path, "my-skill", "# Hello Skill")
        with patch("backend.routers.sandbox.stream_pipeline._skill_root_for_name", return_value=skill_dir), \
             patch("backend.routers.sandbox.stream_pipeline.load_skill_metadata_prompt", return_value="meta"), \
             patch("backend.routers.sandbox.stream_pipeline.load_skill_body_prompt", return_value="body-prompt-content"):
            from backend.routers.sandbox.stream_pipeline import build_skill_context
            ctx = build_skill_context("my-skill")

        assert ctx["body_loader"]() == "body-prompt-content"


# ---------------------------------------------------------------------------
# Test: final_messages assembly in _make_stream
# ---------------------------------------------------------------------------

class TestFinalMessagesAssembly:
    """Test that _make_stream assembles final_messages correctly before
    sending to the LLM.

    The core flow is:
    1. metadata round → need_body=True
    2. body loading → body_prompt loaded
    3. runtime planner → mode=direct_answer (no actions)
    4. final_messages = [system: body_prompt] + [system: strict_instruction] + user_messages
    5. stream_chat(final_messages, model) is called
    """

    @pytest.mark.asyncio
    async def test_body_prompt_as_system_message(self, tmp_path):
        """When skill is loaded and user asks a question, body_prompt should
        appear as the first system message in final_messages sent to the model."""
        skill_dir = _make_skill_dir(tmp_path, "demo-skill")
        body_prompt_text = "You are a demo skill assistant. Follow the SKILL.md strictly."

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "Hello from model"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "demo-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": True,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("帮我生成一个故事")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "test", "scope": "test", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        assert len(captured_messages) > 0
        first_msg = captured_messages[0]
        assert first_msg["role"] == "system"
        assert body_prompt_text in first_msg["content"]

    @pytest.mark.asyncio
    async def test_user_message_in_final_messages(self, tmp_path):
        """User question should appear in final_messages as a user-role message."""
        skill_dir = _make_skill_dir(tmp_path, "story-skill")
        body_prompt_text = "Skill body prompt"
        user_question = "请帮我写一个关于猫的故事"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "好的，这是一个关于猫的故事"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "story-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": True,
            "enable_resource_preload": False,
        }
        request = _make_chat_request(user_question)

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "story", "scope": "creative", "constraints": [], "output_requirements": [], "complexity": "moderate", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        user_msgs = [m for m in captured_messages if m["role"] == "user"]
        assert len(user_msgs) >= 1
        assert user_question in user_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_strict_mode_injection(self, tmp_path):
        """When strict_skill_execution=True, a strict-mode system message
        should be injected after the body_prompt system message."""
        body_prompt_text = "Skill body prompt"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "strict-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": True,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("执行任务")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "exec", "scope": "task", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        system_msgs = [m for m in captured_messages if m["role"] == "system"]
        strict_msgs = [m for m in system_msgs if "严格执行模式" in m["content"]]
        assert len(strict_msgs) == 1, "Expected exactly one strict-mode system message"

    @pytest.mark.asyncio
    async def test_no_strict_mode_when_disabled(self, tmp_path):
        """When strict_skill_execution=False, no strict-mode system message
        should be injected."""
        body_prompt_text = "Skill body prompt"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "loose-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("执行任务")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "exec", "scope": "task", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        system_msgs = [m for m in captured_messages if m["role"] == "system"]
        strict_msgs = [m for m in system_msgs if "严格执行模式" in m["content"]]
        assert len(strict_msgs) == 0, "Expected no strict-mode system message when strict_skill_execution=False"

    @pytest.mark.asyncio
    async def test_runtime_final_instruction_injection(self, tmp_path):
        """When runtime planner returns a final_instruction, it should be
        injected as an additional system message in final_messages."""
        body_prompt_text = "Skill body prompt"
        final_instruction = "请优先输出 Markdown 格式的结果"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "instruct-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": True,
            "require_action_confirmation": False,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("生成报告")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "report", "scope": "generate", "constraints": [], "output_requirements": [], "complexity": "moderate", "requires_script_execution": False}
            mock_planner.return_value = {
                "mode": "direct_answer",
                "tasks": [],
                "errors": [],
                "final_instruction": final_instruction,
            }

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        system_msgs = [m for m in captured_messages if m["role"] == "system"]
        instruction_msgs = [m for m in system_msgs if "运行时动作意图判断器" in m["content"]]
        assert len(instruction_msgs) == 1, "Expected one runtime instruction system message"
        assert final_instruction in instruction_msgs[0]["content"]


# ---------------------------------------------------------------------------
# Test: message ordering in final_messages
# ---------------------------------------------------------------------------

class TestFinalMessageOrdering:
    """Test that final_messages follow the correct ordering:
    1. system: body_prompt
    2. system: runtime final_instruction (if present)
    3. system: strict_skill_execution instruction (if enabled)
    4. user/assistant messages from conversation
    """

    @pytest.mark.asyncio
    async def test_message_order_with_all_system_messages(self, tmp_path):
        body_prompt_text = "Skill body prompt"
        final_instruction = "输出 JSON 格式"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "ordered-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": True,
            "require_action_confirmation": False,
            "execution_root": None,
            "strict_skill_execution": True,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("执行任务")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "exec", "scope": "task", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {
                "mode": "direct_answer",
                "tasks": [],
                "errors": [],
                "final_instruction": final_instruction,
            }

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        assert len(captured_messages) >= 4

        assert captured_messages[0]["role"] == "system"
        assert body_prompt_text in captured_messages[0]["content"]

        runtime_msg_indices = [i for i, m in enumerate(captured_messages) if m["role"] == "system" and "运行时动作意图判断器" in m["content"]]
        assert len(runtime_msg_indices) == 1

        strict_msg_indices = [i for i, m in enumerate(captured_messages) if m["role"] == "system" and "严格执行模式" in m["content"]]
        assert len(strict_msg_indices) == 1

        user_msg_indices = [i for i, m in enumerate(captured_messages) if m["role"] == "user"]
        assert len(user_msg_indices) >= 1

        body_idx = 0
        runtime_idx = runtime_msg_indices[0]
        strict_idx = strict_msg_indices[0]
        user_idx = user_msg_indices[0]

        assert body_idx < runtime_idx
        assert runtime_idx < strict_idx
        assert strict_idx < user_idx


# ---------------------------------------------------------------------------
# Test: multi-turn conversation messages
# ---------------------------------------------------------------------------

class TestMultiTurnMessages:
    """Test that multi-turn conversation history is correctly included
    in final_messages."""

    @pytest.mark.asyncio
    async def test_multi_turn_conversation(self, tmp_path):
        body_prompt_text = "Skill body prompt"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "multi-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = ChatRequest(
            messages=[
                {"role": "user", "content": "第一个问题"},
                {"role": "assistant", "content": "第一个回答"},
                {"role": "user", "content": "第二个问题"},
            ],
        )

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "follow-up", "scope": "multi", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        user_msgs = [m for m in captured_messages if m["role"] == "user"]
        assistant_msgs = [m for m in captured_messages if m["role"] == "assistant"]

        assert len(user_msgs) == 2
        assert len(assistant_msgs) == 1
        assert "第一个问题" in user_msgs[0]["content"]
        assert "第二个问题" in user_msgs[1]["content"]
        assert "第一个回答" in assistant_msgs[0]["content"]


# ---------------------------------------------------------------------------
# Test: force_body skips metadata round
# ---------------------------------------------------------------------------

class TestForceBody:
    """Test that force_body=True skips the metadata round and directly
    loads the body prompt."""

    @pytest.mark.asyncio
    async def test_force_body_skips_metadata(self, tmp_path):
        body_prompt_text = "Force-loaded body prompt"

        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "force"})

        skill_context = {
            "skill_name": "force-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": True,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("直接执行")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        mock_meta.assert_not_called()

        system_msgs = [m for m in captured_messages if m["role"] == "system"]
        body_msgs = [m for m in system_msgs if body_prompt_text in m["content"]]
        assert len(body_msgs) == 1


# ---------------------------------------------------------------------------
# Test: need_body=False sends fallback messages
# ---------------------------------------------------------------------------

class TestNeedBodyFallback:
    """Test that when need_body=False, a fallback response is generated
    instead of the skill body prompt."""

    @pytest.mark.asyncio
    async def test_need_body_false_uses_fallback(self, tmp_path):
        captured_messages = []

        async def fake_stream_chat(messages, model, **kwargs):
            captured_messages.extend(messages)
            yield "该 Skill 不适用当前请求"

        skill_context = {
            "skill_name": "mismatch-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: "should not be used",
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("完全不相关的请求")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta:

            mock_route.return_value = MagicMock(model="test-model", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = False

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        assert len(captured_messages) > 0
        first_msg = captured_messages[0]
        assert first_msg["role"] == "system"
        assert "不匹配" in first_msg["content"]

        body_in_messages = any("should not be used" in m.get("content", "") for m in captured_messages)
        assert not body_in_messages, "Body prompt should NOT appear when need_body=False"


# ---------------------------------------------------------------------------
# Test: model routing for final response
# ---------------------------------------------------------------------------

class TestModelRoutingForFinalResponse:
    """Test that the model is correctly routed for the final response."""

    @pytest.mark.asyncio
    async def test_text_task_uses_routed_model(self, tmp_path):
        body_prompt_text = "Skill body prompt"

        captured_model = None

        async def fake_stream_chat(messages, model, **kwargs):
            nonlocal captured_model
            captured_model = model
            yield "response"

        async def fake_complete_chat_once(messages, model):
            return json.dumps({"need_body": True, "reason": "match"})

        skill_context = {
            "skill_name": "route-skill",
            "metadata_prompt": "metadata prompt",
            "body_loader": lambda: body_prompt_text,
            "child_body_loader": None,
            "force_body": False,
            "enable_action_execution": False,
            "require_action_confirmation": True,
            "execution_root": None,
            "strict_skill_execution": False,
            "enable_resource_preload": False,
        }
        request = _make_chat_request("普通文本问题")

        with patch("backend.routers.sandbox.stream_pipeline.stream_chat", side_effect=fake_stream_chat), \
             patch("backend.routers.sandbox.stream_pipeline.complete_chat_once", side_effect=fake_complete_chat_once), \
             patch("backend.routers.sandbox.stream_pipeline.route_model") as mock_route, \
             patch("backend.routers.sandbox.stream_pipeline._run_metadata_round", new_callable=AsyncMock) as mock_meta, \
             patch("backend.routers.sandbox.stream_pipeline._run_instruction_analysis_round", new_callable=AsyncMock) as mock_ia, \
             patch("backend.routers.sandbox.stream_pipeline._run_skill_runtime_planner_round", new_callable=AsyncMock) as mock_planner:

            mock_route.return_value = MagicMock(model="routed-model-name", task="text", ack=MagicMock(return_value={}))
            mock_meta.return_value = True
            mock_ia.return_value = {"intent": "text", "scope": "simple", "constraints": [], "output_requirements": [], "complexity": "simple", "requires_script_execution": False}
            mock_planner.return_value = {"mode": "direct_answer", "tasks": [], "errors": []}

            from backend.routers.sandbox.stream_pipeline import _make_stream
            response = _make_stream(skill_context, request)

            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

        assert captured_model == "routed-model-name"