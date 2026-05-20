"""Tests for backend/services/output_validator.py."""

import json
import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# _strip_validator_fence
# ---------------------------------------------------------------------------

def test_strip_validator_fence_plain():
    from backend.services.output_validator import _strip_validator_fence
    assert _strip_validator_fence('{"valid": true}') == '{"valid": true}'


def test_strip_validator_fence_json_fence():
    from backend.services.output_validator import _strip_validator_fence
    result = _strip_validator_fence('```json\n{"valid": true}\n```')
    assert result == '{"valid": true}'


def test_strip_validator_fence_plain_fence():
    from backend.services.output_validator import _strip_validator_fence
    result = _strip_validator_fence('```\n{"valid": false, "reason": "bad"}\n```')
    assert result == '{"valid": false, "reason": "bad"}'


# ---------------------------------------------------------------------------
# validate_output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_output_returns_true_on_valid_response():
    from backend.services.output_validator import validate_output

    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(return_value='{"valid": true}'),
    ):
        valid, reason = await validate_output(
            [{"role": "system", "content": "Output JSON only."}],
            '{"foo": "bar"}',
            "test-model",
        )

    assert valid is True


@pytest.mark.asyncio
async def test_validate_output_returns_false_with_reason():
    from backend.services.output_validator import validate_output

    response = '{"valid": false, "reason": "contains markdown fence"}'
    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(return_value=response),
    ):
        valid, reason = await validate_output(
            [{"role": "system", "content": "No fences."}],
            "```json\n{}\n```",
            "test-model",
        )

    assert valid is False
    assert "fence" in reason


@pytest.mark.asyncio
async def test_validate_output_defaults_true_on_non_json_response():
    """Validator returning garbage should default to passing to avoid false blocks."""
    from backend.services.output_validator import validate_output

    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(return_value="I cannot evaluate this."),
    ):
        valid, reason = await validate_output(
            [{"role": "system", "content": "test"}],
            "some output",
            "test-model",
        )

    assert valid is True


@pytest.mark.asyncio
async def test_validate_output_strips_code_fence_from_validator_response():
    from backend.services.output_validator import validate_output

    # Validator wraps its own answer in a fence despite instructions.
    wrapped = '```json\n{"valid": false, "reason": "too short"}\n```'
    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(return_value=wrapped),
    ):
        valid, reason = await validate_output(
            [{"role": "system", "content": "test"}],
            "x",
            "test-model",
        )

    assert valid is False
    assert "short" in reason


# ---------------------------------------------------------------------------
# retry_with_validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_succeeds_on_first_attempt():
    from backend.services.output_validator import retry_with_validation

    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(side_effect=[
            "good output",           # generation attempt 1
            '{"valid": true}',       # validator for attempt 1
        ]),
    ):
        output, succeeded, log = await retry_with_validation(
            [{"role": "system", "content": "output something"}],
            "test-model",
        )

    assert succeeded is True
    assert output == "good output"
    assert len(log) == 1
    assert log[0].is_valid is True


@pytest.mark.asyncio
async def test_retry_succeeds_on_second_attempt():
    from backend.services.output_validator import retry_with_validation

    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(side_effect=[
            "bad output",                               # generation attempt 1
            '{"valid": false, "reason": "too short"}',  # validator attempt 1
            "good output",                              # generation attempt 2
            '{"valid": true}',                          # validator attempt 2
        ]),
    ):
        output, succeeded, log = await retry_with_validation(
            [{"role": "system", "content": "output something"}],
            "test-model",
        )

    assert succeeded is True
    assert output == "good output"
    assert len(log) == 2
    assert log[0].is_valid is False
    assert log[1].is_valid is True


@pytest.mark.asyncio
async def test_retry_fails_after_max_retries():
    from backend.services.output_validator import retry_with_validation

    # All 3 generation calls return bad output; all 3 validators reject it.
    validator_rejection = '{"valid": false, "reason": "always bad"}'
    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(side_effect=[
            "bad",  validator_rejection,
            "bad",  validator_rejection,
            "bad",  validator_rejection,
        ]),
    ):
        output, succeeded, log = await retry_with_validation(
            [{"role": "system", "content": "test"}],
            "test-model",
            max_retries=3,
        )

    assert succeeded is False
    assert len(log) == 3
    assert all(not r.is_valid for r in log)


@pytest.mark.asyncio
async def test_retry_attempt_log_records_chars():
    from backend.services.output_validator import retry_with_validation

    with patch(
        "backend.services.output_validator.complete_chat_once",
        new=AsyncMock(side_effect=[
            "hello world",      # 11 chars
            '{"valid": true}',
        ]),
    ):
        _, _, log = await retry_with_validation(
            [{"role": "system", "content": "test"}],
            "test-model",
        )

    assert log[0].output_chars == len("hello world")
    assert log[0].attempt == 1
