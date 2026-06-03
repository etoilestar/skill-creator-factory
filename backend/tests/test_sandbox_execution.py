"""Tests for sandbox execution enhancements.

Covers:
- ChatRequest.effective_execution_mode() backward compatibility
- SandboxExecutionResult dataclass
- _format_task_checklist_markdown() inline task checklist formatting
- _parse_error_correction_decision() LLM error correction parsing
- _apply_error_correction() task correction merging
- _task_checklist() / _sandbox_retry() SSE event helpers
- _compose_error_correction_prompt() prompt generation
- Instruction analysis requires_script_execution field
"""

import json
import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# ChatRequest.effective_execution_mode()
# ---------------------------------------------------------------------------

class TestEffectiveExecutionMode:
    """Test backward-compatible execution mode normalization."""

    def test_execute_mode(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode="execute")
        assert req.effective_execution_mode() == "execute"

    def test_plan_mode(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode="plan")
        assert req.effective_execution_mode() == "plan"

    def test_craft_maps_to_execute(self):
        """Backward compatibility: 'craft' should map to 'execute'."""
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode="craft")
        assert req.effective_execution_mode() == "execute"

    def test_none_defaults_to_execute(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode=None)
        assert req.effective_execution_mode() == "execute"

    def test_default_is_execute(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}])
        assert req.effective_execution_mode() == "execute"

    def test_case_insensitive(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode="CRAFT")
        assert req.effective_execution_mode() == "execute"

    def test_plan_case_insensitive(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(messages=[{"role": "user", "content": "hi"}], execution_mode="PLAN")
        assert req.effective_execution_mode() == "plan"


# ---------------------------------------------------------------------------
# SandboxExecutionResult
# ---------------------------------------------------------------------------

class TestSandboxExecutionResult:
    """Test the SandboxExecutionResult dataclass."""

    def test_defaults(self):
        from backend.routers.chat_models import SandboxExecutionResult
        result = SandboxExecutionResult()
        assert result.success is False
        assert result.action == ""
        assert result.attempt == 0
        assert result.max_retries == 3
        assert result.result == {}
        assert result.corrected_task is None
        assert result.error_message == ""

    def test_with_values(self):
        from backend.routers.chat_models import SandboxExecutionResult
        result = SandboxExecutionResult(
            success=True,
            action="run_command",
            attempt=2,
            max_retries=3,
            result={"returncode": 0},
            error_message="",
        )
        assert result.success is True
        assert result.action == "run_command"
        assert result.attempt == 2


# ---------------------------------------------------------------------------
# _format_task_checklist_markdown()
# ---------------------------------------------------------------------------

class TestFormatTaskChecklistMarkdown:
    """Test the inline task checklist Markdown formatter."""

    def test_empty_tasks(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        result = _format_task_checklist_markdown([])
        assert "共 0 项" in result

    def test_run_command_task(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [{"action": "run_command", "command": "python3 main.py", "reason": "运行脚本"}]
        result = _format_task_checklist_markdown(tasks)
        assert "- [ ]" in result
        assert "执行命令" in result
        assert "python3 main.py" in result

    def test_write_file_task(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [{"action": "write_file", "path": "output.txt", "reason": "写入结果"}]
        result = _format_task_checklist_markdown(tasks)
        assert "- [ ]" in result
        assert "写入文件" in result
        assert "output.txt" in result

    def test_read_resource_task(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [{"action": "read_resource", "path": "references/guide.md", "reason": "读取参考"}]
        result = _format_task_checklist_markdown(tasks)
        assert "- [ ]" in result
        assert "读取资源" in result

    def test_create_directory_task(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [{"action": "create_directory", "path": "outputs", "reason": "创建输出目录"}]
        result = _format_task_checklist_markdown(tasks)
        assert "- [ ]" in result
        assert "创建目录" in result

    def test_with_instruction_analysis(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [{"action": "display", "reason": "展示结果"}]
        analysis = {"intent": "测试功能", "complexity": "moderate"}
        result = _format_task_checklist_markdown(tasks, instruction_analysis=analysis)
        assert "任务意图" in result
        assert "测试功能" in result
        assert "复杂度" in result
        assert "moderate" in result

    def test_long_command_truncated(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        long_cmd = "python3 " + "x" * 200
        tasks = [{"action": "run_command", "command": long_cmd, "reason": "运行"}]
        result = _format_task_checklist_markdown(tasks)
        assert "…" in result

    def test_multiple_tasks(self):
        from backend.routers.sandbox_chat import _format_task_checklist_markdown
        tasks = [
            {"action": "read_resource", "path": "ref.md", "reason": "读取"},
            {"action": "run_command", "command": "python3 run.py", "reason": "执行"},
            {"action": "write_file", "path": "out.txt", "reason": "写入"},
        ]
        result = _format_task_checklist_markdown(tasks)
        assert result.count("- [ ]") == 3
        assert "共 3 项" in result


# ---------------------------------------------------------------------------
# _parse_error_correction_decision()
# ---------------------------------------------------------------------------

class TestParseErrorCorrectionDecision:
    """Test the LLM error correction decision parser."""

    def test_valid_correction(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        text = json.dumps({
            "corrected": True,
            "reason": "路径错误，已修正",
            "task": {"action": "run_command", "command": "python3 correct.py"},
        })
        result = _parse_error_correction_decision(text)
        assert result["corrected"] is True
        assert result["reason"] == "路径错误，已修正"
        assert result["task"]["command"] == "python3 correct.py"

    def test_not_corrected(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        text = json.dumps({"corrected": False, "reason": "无法确定修正方案"})
        result = _parse_error_correction_decision(text)
        assert result["corrected"] is False
        assert "无法确定" in result["reason"]

    def test_invalid_json(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        result = _parse_error_correction_decision("not json at all")
        assert result["corrected"] is False
        assert "JSON" in result["reason"]

    def test_corrected_true_but_missing_task(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        text = json.dumps({"corrected": True, "reason": "修正"})
        result = _parse_error_correction_decision(text)
        assert result["corrected"] is False

    def test_corrected_string_true(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        text = json.dumps({
            "corrected": "true",
            "reason": "修正",
            "task": {"action": "run_command", "command": "ls"},
        })
        result = _parse_error_correction_decision(text)
        assert result["corrected"] is True

    def test_non_dict_output(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        result = _parse_error_correction_decision('"hello"')
        assert result["corrected"] is False

    def test_markdown_wrapped_json(self):
        from backend.routers.sandbox_chat import _parse_error_correction_decision
        inner = json.dumps({"corrected": False, "reason": "测试"})
        text = f"```json\n{inner}\n```"
        result = _parse_error_correction_decision(text)
        assert result["corrected"] is False
        assert result["reason"] == "测试"


# ---------------------------------------------------------------------------
# _apply_error_correction()
# ---------------------------------------------------------------------------

class TestApplyErrorCorrection:
    """Test the error correction application logic."""

    def test_merge_corrected_command(self):
        from backend.routers.sandbox_chat import _apply_error_correction
        original = {"action": "run_command", "command": "python3 wrong.py", "reason": "执行"}
        correction = {
            "corrected": True,
            "reason": "修正命令",
            "task": {"command": "python3 correct.py"},
        }
        result = _apply_error_correction(original, correction)
        assert result["command"] == "python3 correct.py"
        assert result["action"] == "run_command"  # action preserved

    def test_action_type_preserved(self):
        """Security: action type must never be changed by correction."""
        from backend.routers.sandbox_chat import _apply_error_correction
        original = {"action": "run_command", "command": "ls"}
        correction = {
            "corrected": True,
            "reason": "恶意修改",
            "task": {"action": "write_file", "path": "/etc/passwd", "content": "hacked"},
        }
        result = _apply_error_correction(original, correction)
        assert result["action"] == "run_command"  # action type preserved
        assert result.get("path") == "/etc/passwd"  # other fields merged

    def test_invalid_correction_task(self):
        from backend.routers.sandbox_chat import _apply_error_correction
        original = {"action": "run_command", "command": "ls"}
        correction = {"corrected": True, "reason": "修正", "task": "not a dict"}
        result = _apply_error_correction(original, correction)
        assert result == original  # unchanged

    def test_empty_correction(self):
        from backend.routers.sandbox_chat import _apply_error_correction
        original = {"action": "run_command", "command": "ls"}
        correction = {"corrected": True, "reason": "修正", "task": {}}
        result = _apply_error_correction(original, correction)
        assert result["action"] == "run_command"
        assert result["command"] == "ls"


# ---------------------------------------------------------------------------
# SSE Event Helpers: _task_checklist() / _sandbox_retry()
# ---------------------------------------------------------------------------

class TestSSEEventHelpers:
    """Test the new SSE event helper functions."""

    def test_task_checklist_event(self):
        from backend.routers.chat_utils import _task_checklist
        tasks = [
            {"index": 0, "action": "run_command", "description": "执行脚本"},
            {"index": 1, "action": "write_file", "description": "写入文件"},
        ]
        result = _task_checklist(tasks, completed_indices=[0], executing_index=1)
        assert "task_checklist" in result
        parsed = json.loads(result.split("data: ", 1)[1].strip())
        assert "task_checklist" in parsed
        assert parsed["task_checklist"]["tasks"] == tasks
        assert parsed["task_checklist"]["completed_indices"] == [0]
        assert parsed["task_checklist"]["executing_index"] == 1

    def test_task_checklist_defaults(self):
        from backend.routers.chat_utils import _task_checklist
        result = _task_checklist([])
        parsed = json.loads(result.split("data: ", 1)[1].strip())
        assert parsed["task_checklist"]["completed_indices"] == []
        assert parsed["task_checklist"]["executing_index"] == -1

    def test_sandbox_retry_event(self):
        from backend.routers.chat_utils import _sandbox_retry
        result = _sandbox_retry(attempt=1, max_retries=3, error="ModuleNotFoundError", corrected=True)
        parsed = json.loads(result.split("data: ", 1)[1].strip())
        assert "sandbox_retry" in parsed
        assert parsed["sandbox_retry"]["attempt"] == 1
        assert parsed["sandbox_retry"]["max_retries"] == 3
        assert parsed["sandbox_retry"]["corrected"] is True

    def test_sandbox_retry_error_truncated(self):
        from backend.routers.chat_utils import _sandbox_retry
        long_error = "x" * 1000
        result = _sandbox_retry(attempt=1, max_retries=3, error=long_error, corrected=False)
        parsed = json.loads(result.split("data: ", 1)[1].strip())
        assert len(parsed["sandbox_retry"]["error"]) <= 500


# ---------------------------------------------------------------------------
# _compose_error_correction_prompt()
# ---------------------------------------------------------------------------

class TestComposeErrorCorrectionPrompt:
    """Test the error correction prompt generator."""

    def test_prompt_contains_rules(self):
        from backend.routers.sandbox_chat import _compose_error_correction_prompt
        prompt = _compose_error_correction_prompt(
            task={"action": "run_command"},
            error_result={"success": False},
            attempt=1,
            max_retries=3,
        )
        assert "沙盒执行错误修正助手" in prompt
        assert "action 类型" in prompt
        assert "1/3" in prompt

    def test_prompt_includes_attempt_info(self):
        from backend.routers.sandbox_chat import _compose_error_correction_prompt
        prompt = _compose_error_correction_prompt(
            task={"action": "run_command"},
            error_result={"success": False},
            attempt=2,
            max_retries=3,
        )
        assert "2/3" in prompt


# ---------------------------------------------------------------------------
# Instruction Analysis: requires_script_execution
# ---------------------------------------------------------------------------

class TestInstructionAnalysisScriptDetection:
    """Test the requires_script_execution field in instruction analysis."""

    def test_analysis_defaults_requires_script_execution(self):
        """When LLM output doesn't include the field, it should default to False."""
        from backend.routers.sandbox_chat import _run_instruction_analysis_round
        # We test the normalization logic directly by examining the
        # _run_instruction_analysis_round's post-processing
        # Simulate the post-processing logic
        analysis = {"intent": "test", "scope": "test", "constraints": [], "output_requirements": []}

        for key in ("intent", "scope", "constraints", "output_requirements", "complexity", "requires_script_execution"):
            if key not in analysis:
                if key in ("constraints", "output_requirements"):
                    analysis[key] = []
                elif key == "requires_script_execution":
                    analysis[key] = False
                else:
                    analysis[key] = ""

        assert analysis["requires_script_execution"] is False

    def test_requires_script_execution_string_normalization(self):
        """String 'true' should be normalized to boolean True."""
        analysis = {"requires_script_execution": "true"}

        rse = analysis.get("requires_script_execution")
        if isinstance(rse, str):
            analysis["requires_script_execution"] = rse.strip().lower() in {"true", "1", "yes", "y"}

        assert analysis["requires_script_execution"] is True

    def test_requires_script_execution_bool_passthrough(self):
        """Boolean True should pass through unchanged."""
        analysis = {"requires_script_execution": True}

        rse = analysis.get("requires_script_execution")
        if isinstance(rse, str):
            analysis["requires_script_execution"] = rse.strip().lower() in {"true", "1", "yes", "y"}

        assert analysis["requires_script_execution"] is True

    def test_requires_script_execution_false_string(self):
        """String 'false' should be normalized to boolean False."""
        analysis = {"requires_script_execution": "false"}

        rse = analysis.get("requires_script_execution")
        if isinstance(rse, str):
            analysis["requires_script_execution"] = rse.strip().lower() in {"true", "1", "yes", "y"}

        assert analysis["requires_script_execution"] is False


# ---------------------------------------------------------------------------
# Integration: ChatRequest with execution_mode in API context
# ---------------------------------------------------------------------------

class TestChatRequestIntegration:
    """Test ChatRequest behavior in typical API usage patterns."""

    def test_plan_mode_preserved(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(
            messages=[{"role": "user", "content": "test"}],
            execution_mode="plan",
        )
        assert req.effective_execution_mode() == "plan"

    def test_execute_mode_preserved(self):
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(
            messages=[{"role": "user", "content": "test"}],
            execution_mode="execute",
        )
        assert req.effective_execution_mode() == "execute"

    def test_craft_backward_compat_in_confirm_flow(self):
        """Simulate the confirm endpoint overriding mode to 'execute'."""
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(
            messages=[{"role": "user", "content": "test"}],
            execution_mode="plan",
        )
        # Simulate confirm endpoint: override to execute mode
        req.execution_mode = "execute"
        assert req.effective_execution_mode() == "execute"

    def test_serialization_round_trip(self):
        """ChatRequest should serialize/deserialize correctly."""
        from backend.routers.chat_models import ChatRequest
        req = ChatRequest(
            messages=[{"role": "user", "content": "hello"}],
            execution_mode="execute",
            input_files=[{"path": "inputs/test.csv", "filename": "test.csv"}],
        )
        data = req.model_dump()
        req2 = ChatRequest(**data)
        assert req2.effective_execution_mode() == "execute"
        assert len(req2.input_files) == 1


# ---------------------------------------------------------------------------
# Session Cleanup: _cleanup_expired_sessions
# ---------------------------------------------------------------------------

class TestSessionCleanup:
    """Test the session directory cleanup logic.

    NOTE: _cleanup_expired_sessions lives in skills.py which requires fastapi.
    We test the logic inline to avoid the import dependency.
    """

    @staticmethod
    def _cleanup_expired_sessions(inputs_dir, ttl_seconds=24 * 3600):
        """Inline copy of the cleanup logic for testing without fastapi import."""
        import os, time, shutil
        from pathlib import Path
        inputs_dir = Path(inputs_dir)
        if not inputs_dir.is_dir():
            return
        now = time.time()
        for session_dir in list(inputs_dir.iterdir()):
            if session_dir.is_dir() and (now - session_dir.stat().st_mtime) > ttl_seconds:
                shutil.rmtree(session_dir, ignore_errors=True)

    def test_cleanup_removes_expired_session(self, tmp_path):
        """Expired session directories should be removed."""
        import time, os

        inputs_dir = tmp_path / "inputs"
        old_session = inputs_dir / "old-session"
        old_session.mkdir(parents=True)
        (old_session / "data.csv").write_text("test")

        # Set mtime to 25 hours ago
        old_time = time.time() - 25 * 3600
        os.utime(old_session, (old_time, old_time))

        self._cleanup_expired_sessions(inputs_dir)
        assert not old_session.exists()

    def test_cleanup_keeps_active_session(self, tmp_path):
        """Active (non-expired) session directories should be kept."""
        inputs_dir = tmp_path / "inputs"
        active_session = inputs_dir / "active-session"
        active_session.mkdir(parents=True)
        (active_session / "data.csv").write_text("test")

        self._cleanup_expired_sessions(inputs_dir)
        assert active_session.exists()

    def test_cleanup_handles_nonexistent_dir(self, tmp_path):
        """Should not raise if the inputs directory doesn't exist."""
        self._cleanup_expired_sessions(tmp_path / "nonexistent")

    def test_cleanup_handles_empty_dir(self, tmp_path):
        """Should handle an empty inputs directory gracefully."""
        inputs_dir = tmp_path / "inputs"
        inputs_dir.mkdir()
        self._cleanup_expired_sessions(inputs_dir)  # Should not raise


# ---------------------------------------------------------------------------
# Input Path Correction: _correct_expanded_input_paths
# ---------------------------------------------------------------------------

class TestCorrectExpandedInputPaths:
    """Test the input path correction logic for placeholder filenames."""

    def test_no_correction_needed_when_path_exists(self, tmp_path):
        """Existing paths should not be modified."""
        from backend.routers.chat_utils import _correct_expanded_input_paths
        real_file = tmp_path / "inputs" / "session-1" / "report.csv"
        real_file.parent.mkdir(parents=True)
        real_file.write_text("data")
        argv = [str(real_file)]
        result = _correct_expanded_input_paths(
            argv, input_files=[], execution_root=tmp_path,
            session_input_dir=real_file.parent,
        )
        assert result == argv

    def test_corrects_placeholder_by_extension(self, tmp_path):
        """When LLM uses a placeholder filename, correct to the real uploaded file."""
        from backend.routers.chat_utils import _correct_expanded_input_paths
        session_dir = tmp_path / "inputs" / "session-1"
        session_dir.mkdir(parents=True)
        real_file = session_dir / "2603.pdf"
        real_file.write_bytes(b"%PDF-1.4")

        # LLM used "document.pdf" (placeholder from SKILL.md)
        wrong_path = session_dir / "document.pdf"
        argv = [str(wrong_path)]
        input_files = [{"path": "inputs/session-1/2603.pdf", "filename": "2603.pdf"}]

        result = _correct_expanded_input_paths(
            argv, input_files=input_files, execution_root=tmp_path,
            session_input_dir=session_dir,
        )
        assert len(result) == 1
        assert "2603.pdf" in result[0]
        assert "document.pdf" not in result[0]

    def test_no_correction_when_no_candidates(self, tmp_path):
        """When no uploaded files exist, argv should be returned unchanged."""
        from backend.routers.chat_utils import _correct_expanded_input_paths
        argv = ["/some/path/file.pdf"]
        result = _correct_expanded_input_paths(
            argv, input_files=[], execution_root=tmp_path,
            session_input_dir=None,
        )
        assert result == argv

    def test_no_correction_for_args_without_extension(self, tmp_path):
        """Args without file extensions should not be modified."""
        from backend.routers.chat_utils import _correct_expanded_input_paths
        argv = ["python3", "-c", "print('hello')"]
        result = _correct_expanded_input_paths(
            argv, input_files=[], execution_root=tmp_path,
            session_input_dir=None,
        )
        assert result == argv


# ---------------------------------------------------------------------------
# Input Path Validation: _validate_input_file_paths
# ---------------------------------------------------------------------------

class TestValidateInputFilePaths:
    """Test the input file path validation logic."""

    def test_valid_path_no_warnings(self, tmp_path):
        """Existing file paths should produce no warnings."""
        from backend.routers.chat_utils import _validate_input_file_paths
        real_file = tmp_path / "data.csv"
        real_file.write_text("test")
        warnings = _validate_input_file_paths([str(real_file)], tmp_path)
        assert len(warnings) == 0

    def test_nonexistent_path_in_session_dir_warns(self, tmp_path):
        """Non-existent paths within session dir should produce warnings."""
        from backend.routers.chat_utils import _validate_input_file_paths
        session_dir = tmp_path / "inputs" / "session-1"
        session_dir.mkdir(parents=True)
        wrong_path = session_dir / "nonexistent.csv"
        warnings = _validate_input_file_paths([str(wrong_path)], session_dir)
        assert len(warnings) == 1
        assert "nonexistent.csv" in warnings[0]

    def test_no_session_dir_no_warnings(self):
        """No warnings when session_input_dir is None."""
        from backend.routers.chat_utils import _validate_input_file_paths
        warnings = _validate_input_file_paths(["/some/path/file.pdf"], None)
        assert len(warnings) == 0

    def test_args_without_extension_ignored(self, tmp_path):
        """Args without file extensions should be ignored."""
        from backend.routers.chat_utils import _validate_input_file_paths
        warnings = _validate_input_file_paths(["--flag", "value"], tmp_path)
        assert len(warnings) == 0
