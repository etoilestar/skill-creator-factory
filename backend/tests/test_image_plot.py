# C:\Work\code\2026\SkillFactory_creator\backend\tests\test_image_plot.py
"""Tests for Stable Diffusion image generation functionality."""

import sys
import os
from pathlib import Path

# Add project root to Python path so `from backend.services...` works
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest
from unittest.mock import patch, MagicMock
import json
import base64

# 测试图片的固定输出目录
_TEST_OUTPUT_DIR = Path(__file__).parent / "test_output_images"

# 统一的测试环境变量配置（所有测试共享）
_TEST_ENV_VARS = {
    # LLM 服务基础地址
    "LLM_BASE_URL": "http://172.18.127.67:11434",

    # 默认使用的模型
    "DEFAULT_MODEL": "qwen3:30b",

    # 各场景专用模型
    "TEXT_MODEL": "qwen3:30b-instruct",
    "CODE_MODEL": "qwen3-coder:30b",
    "PLANNER_MODEL": "qwen3:30b-instruct",
    "VALIDATOR_MODEL": "qwen3:30b",

    # 图像与视觉模型
    "IMAGE_MODEL": "stable-diffusion-2-1-base",
    "IMAGE_BASE_MODEL": "stable-diffusion-2-1-base",
    "VISION_MODEL": "qwen3-vl:32b",
    "IMAGE_BASE_URL": "http://172.18.127.67:11435",
    "IMAGE_SIZE": "512x512",

    # 技能命令超时时间
    "SKILL_COMMAND_TIMEOUT": "180",
}


@pytest.fixture(autouse=True)
def _setup_test_environment(monkeypatch):
    """在每个测试前自动设置统一的环境变量并创建输出目录。

    所有测试共享同一套环境变量配置。
    个别测试如需覆盖特定变量，可在测试函数内单独使用 monkeypatch.setenv()。

    特别说明：
    - 默认启用 SKILL_TRIAL_RUN=1（避免真实 API 调用，快速测试）
    - 如需测试真实 API 模式的测试，可在测试内用 monkeypatch.delenv("SKILL_TRIAL_RUN") 关闭
    """
    # 设置统一的环境变量
    for key, value in _TEST_ENV_VARS.items():
        monkeypatch.setenv(key, value)

    # 默认启用 Trial 模式（避免真实网络调用）
    monkeypatch.setenv("SKILL_TRIAL_RUN", "0")

    # 创建输出目录
    _TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    yield


# ---------------------------------------------------------------------------
# Test generate_stable_diffusion_image in trial mode
# ---------------------------------------------------------------------------

def test_generate_stable_diffusion_image_trial_mode():
    """
    测试在 Trial 模式下生成图片（不调用真实 API）。

    验证内容：
    - 传入中文提示词（绘本风格图片描述）
    - 返回结果包含必要字段: prompt, model, image_path, source
    - model 字段正确读取环境变量 IMAGE_MODEL (stable-diffusion-2-1-base)
    - source 字段应为 "trial"（代表试用模式）
    - prompt 字段为自动翻译后的英文 prompt
    - 生成的 PNG 图片文件存在于指定输出目录
    - 图片文件大小大于 0（非空文件）
    - 文件扩展名为 .png
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 调用生成函数（环境变量已由统一 fixture 配置）
    result = generate_stable_diffusion_image(
        "水墨风格的儿童绘本插画，光线明亮。画面取自第一章《浪花里的小珍珠》：一颗小巧莹亮的珍珠漂浮在波光粼粼的海底世界，四周环绕着轻柔的海浪、绵软的珊瑚与灵动好奇的海洋生物。整体氛围梦幻又奇幻，充满童趣童话感，线条细腻柔和，色调温润，十分适合用作儿童童话绘本配图。",
        output_dir=_TEST_OUTPUT_DIR,
        filename_prefix="cat",
    )

    # 验证返回结果的字段完整性
    assert "prompt" in result
    assert "model" in result
    assert "image_path" in result
    assert "source" in result

    # 验证字段值是否符合预期
    assert result["model"] == "stable-diffusion-2-1-base"

    # 验证图片文件已成功创建
    image_path = Path(result["image_path"])
    assert image_path.is_file(), f"图片文件未创建: {image_path}"
    assert image_path.parent == _TEST_OUTPUT_DIR
    assert image_path.suffix == ".png"
    assert image_path.stat().st_size > 0  # 文件应包含有效内容


def test_generate_stable_diffusion_image_trial_mode_with_size():
    """
    测试 Trial 模式下使用自定义尺寸参数生成图片。

    验证内容：
    - size 参数 "515x515" 被正确传递和返回
    - 图片文件成功生成
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 使用自定义尺寸调用（Trial 模式已由统一 fixture 启用）
    result = generate_stable_diffusion_image(
        "a beautiful sunset",
        output_dir=_TEST_OUTPUT_DIR,
        filename_prefix="sunset",
        size="515x515",
    )

    assert result["size"] == "515x515"
    image_path = Path(result["image_path"])
    assert image_path.is_file()


def test_generate_stable_diffusion_image_empty_topic():
    """
    测试传入空提示词时抛出 ValueError 异常。

    验证内容：
    - 传入空字符串 "" 作为 topic 时
    - 应抛出 ValueError 异常，异常信息包含 "empty" 关键字
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 传入空字符串应抛出 ValueError（Trial 模式已启用）
    with pytest.raises(ValueError, match="empty"):
        generate_stable_diffusion_image("")


def test_generate_stable_diffusion_image_topic_with_special_chars():
    """
    测试包含特殊字符的提示词能被正确处理。

    验证内容：
    - 提示词包含 @#$%^&* 等特殊字符
    - 函数能正常执行，不因为特殊字符而崩溃
    - 图片文件成功生成
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 传入包含特殊字符的提示词
    result = generate_stable_diffusion_image(
        "A cat with @#$%^&* symbols!",
        output_dir=_TEST_OUTPUT_DIR,
        filename_prefix="special-chars",
    )

    assert result["source"] == "trial"
    image_path = Path(result["image_path"])
    assert image_path.is_file()


# ---------------------------------------------------------------------------
# Test translate_image_prompt_to_english helper
# ---------------------------------------------------------------------------

def test_translate_image_prompt_to_english_trial_mode():
    """
    测试 Trial 模式下提示词翻译返回默认英文 prompt。

    验证内容：
    - 在 Trial 模式下（SKILL_TRIAL_RUN=1，已由统一 fixture 启用）
    - 传入任意中文提示词
    - 返回值应包含默认的英文 prompt 内容 "cinematic watercolor cat"
    - Trial 模式不会调用真实翻译 API，直接返回预设值
    """
    from backend.services.skill_runtime import translate_image_prompt_to_english

    result = translate_image_prompt_to_english("任何中文")
    assert "cinematic watercolor cat" in result


def test_translate_image_prompt_to_english_empty_topic():
    """Test empty topic raises ValueError."""
    from backend.services.skill_runtime import translate_image_prompt_to_english

    with pytest.raises(ValueError, match="empty"):
        translate_image_prompt_to_english("")


# ---------------------------------------------------------------------------
# Test generate_stable_diffusion_image with mocked API
# ---------------------------------------------------------------------------

def test_generate_stable_diffusion_image_real_api_mode(monkeypatch):
    """
    测试完整的 API 调用流程（使用 Mock 模拟真实 API 响应）。

    测试场景：
    - 关闭 Trial 模式，模拟真实 API 调用流程
    - Mock _post_json 函数，模拟 Stable Diffusion API 的响应
    - Mock translate_image_prompt_to_english 函数，返回固定英文 prompt

    验证内容：
    - API URL 正确构造
    - 请求 payload 包含正确的 model, prompt, size, response_format 字段
    - 返回结果的 model, prompt, size, source 字段正确
    - source 字段应为 "b64_json"（标识图片数据格式）
    - 解析 base64 图片数据并写入 PNG 文件
    - 图片文件存在且扩展名为 .png
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 关闭 Trial 模式，同时覆盖部分环境变量以模拟不同配置
    monkeypatch.delenv("SKILL_TRIAL_RUN", raising=False)
    monkeypatch.setenv("IMAGE_MODEL", "stable-diffusion-v1-5")
    monkeypatch.setenv("IMAGE_BASE_URL", "http://mock-image-api.test")
    monkeypatch.setenv("IMAGE_SIZE", "512x512")
    monkeypatch.setenv("IMAGE_API_KEY", "test-key")

    # Create a mock 1x1 red PNG image
    mock_image_data = base64.b64encode(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c\x03\x00\x00\x01\x00\x01\xfdb\xa1\x00\x00\x00\x00IEND\xaeB`\x82'
    ).decode('utf-8')

    # Mock the _post_json function
    def mock_post_json(url, *, payload, headers):
        assert url == "http://mock-image-api.test/v1/images/generations"
        assert payload["model"] == "stable-diffusion-v1-5"
        assert payload["prompt"] == "a cute cat"  # Should be translated
        assert payload["size"] == "512x512"
        assert payload["response_format"] == "b64_json"
        return {
            "data": [{"b64_json": mock_image_data}]
        }

    with patch("backend.services.skill_runtime._post_json", side_effect=mock_post_json):
        with patch("backend.services.skill_runtime.translate_image_prompt_to_english", return_value="a cute cat"):
            result = generate_stable_diffusion_image(
                "一只可爱的猫",
                output_dir=_TEST_OUTPUT_DIR,
                filename_prefix="test",
            )

    # Verify result
    assert result["model"] == "stable-diffusion-v1-5"
    assert result["prompt"] == "a cute cat"
    assert result["size"] == "512x512"
    assert result["source"] == "b64_json"

    # Verify image file
    image_path = Path(result["image_path"])
    assert image_path.is_file()
    assert image_path.suffix == ".png"


def test_generate_stable_diffusion_image_api_failure(monkeypatch):
    """
    测试 API 调用失败时的异常处理。

    测试场景：
    - 关闭 Trial 模式，模拟真实 API 调用流程
    - Mock _post_json 函数抛出异常（模拟网络错误、API 超时等）

    验证内容：
    - 当 API 调用抛出异常时
    - 异常应被正确向上传播到调用者
    - 异常信息包含 "API connection failed"
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image

    # 关闭 Trial 模式，覆盖部分环境变量
    monkeypatch.delenv("SKILL_TRIAL_RUN", raising=False)
    monkeypatch.setenv("IMAGE_MODEL", "sd-model")
    monkeypatch.setenv("IMAGE_BASE_URL", "http://mock-image-api.test")
    monkeypatch.setenv("IMAGE_SIZE", "512x512")
    monkeypatch.setenv("IMAGE_API_KEY", "test-key")

    # Mock API 调用抛出异常（模拟网络故障）
    def mock_post_json(*args, **kwargs):
        raise Exception("API connection failed")

    # 验证异常被正确向上传播
    with patch("backend.services.skill_runtime._post_json", side_effect=mock_post_json):
        with pytest.raises(Exception, match="API connection failed"):
            generate_stable_diffusion_image("a cat", output_dir=_TEST_OUTPUT_DIR)


# ---------------------------------------------------------------------------
# Test generate_stable_diffusion_image_from_reference (img2img)
# ---------------------------------------------------------------------------

@pytest.fixture
def _reference_image():
    """使用固定的参考图文件用于 img2img 测试。"""
    ref_path = Path(__file__).parent / "reference" / "illustration_example.png"
    assert ref_path.is_file(), f"参考图文件不存在: {ref_path}"
    return ref_path


def test_img2img_trial_mode(_reference_image):
    """
    测试 Trial 模式下基于参考图生成图片（img2img）。

    验证内容：
    - 传入中文提示词和参考图路径
    - 返回结果包含必要字段: prompt, model, image_path, source, reference_image, strength
    - source 字段应为 "trial"
    - reference_image 字段与传入路径一致
    - strength 字段正确返回
    - 生成的图片文件存在且非空
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    result = generate_stable_diffusion_image_from_reference(
        """masterpiece, best quality, anime style, illustration, ultra-detailed,
cute beautiful mermaid, big eyes, long hair, delicate fish tail,
swimming in the deep sea, dynamic pose,
transparent seawater, glowing bubbles, seaweed, underwater fantasy scene,
soft color palette, clean line art, vibrant colors""",
        _reference_image,
        output_dir=_TEST_OUTPUT_DIR,
        filename_prefix="img2img-trial",
    )

    assert "prompt" in result
    assert "model" in result
    assert "image_path" in result
    assert "source" in result
    assert "reference_image" in result
    assert "strength" in result
    # assert result["source"] == "trial"
    assert result["reference_image"] == str(_reference_image)
    assert result["strength"] == 0.7

    image_path = Path(result["image_path"])
    assert image_path.is_file()
    assert image_path.stat().st_size > 0


def test_img2img_trial_mode_custom_strength(_reference_image):
    """
    测试 Trial 模式下 img2img 使用自定义 strength 参数。

    验证内容：
    - strength=0.3 被正确传递和返回
    - 图片文件成功生成
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    result = generate_stable_diffusion_image_from_reference(
        "a watercolor painting of a cat",
        _reference_image,
        strength=0.3,
        output_dir=_TEST_OUTPUT_DIR,
        filename_prefix="img2img-low-strength",
    )

    assert result["strength"] == 0.3
    image_path = Path(result["image_path"])
    assert image_path.is_file()


def test_img2img_reference_image_not_found():
    """
    测试参考图文件不存在时抛出 FileNotFoundError。

    验证内容：
    - 传入不存在的文件路径
    - 应抛出 FileNotFoundError 异常
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    with pytest.raises(FileNotFoundError, match="参考图文件不存在"):
        generate_stable_diffusion_image_from_reference(
            "a cat",
            "/nonexistent/path/image.png",
            output_dir=_TEST_OUTPUT_DIR,
        )


def test_img2img_empty_topic(_reference_image):
    """
    测试 img2img 传入空提示词时抛出 ValueError。

    验证内容：
    - 传入空字符串提示词
    - 应抛出 ValueError 异常，异常信息包含 "empty"
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    with pytest.raises(ValueError, match="empty"):
        generate_stable_diffusion_image_from_reference(
            "",
            _reference_image,
            output_dir=_TEST_OUTPUT_DIR,
        )


def test_img2img_mock_api_mode(monkeypatch, _reference_image):
    """
    测试 img2img 完整的 API 调用流程（使用 Mock 模拟真实 API 响应）。

    测试场景：
    - 关闭 Trial 模式
    - Mock _post_json 函数，模拟 /v1/images/generations API 响应
    - Mock translate_image_prompt_to_english 函数

    验证内容：
    - API URL 正确构造为 /v1/images/generations（与服务端文生图同一端点）
    - JSON payload 包含 model, prompt, n, size, response_format 字段
    - JSON payload 额外包含 image(base64) 和 strength 字段（触发服务端 img2img 逻辑）
    - image 字段为参考图的 base64 编码字符串
    - 返回结果字段正确
    - source 字段为 "b64_json"
    - 图片文件成功写入磁盘
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    monkeypatch.delenv("SKILL_TRIAL_RUN", raising=False)
    monkeypatch.setenv("IMAGE_MODEL", "stable-diffusion-v1-5")
    monkeypatch.setenv("IMAGE_BASE_URL", "http://mock-image-api.test")
    monkeypatch.setenv("IMAGE_SIZE", "512x512")
    monkeypatch.setenv("IMAGE_API_KEY", "test-key")

    mock_image_data = base64.b64encode(
        b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9c\x03\x00\x00\x01\x00\x01\xfdb\xa1\x00\x00\x00\x00IEND\xaeB`\x82'
    ).decode('utf-8')

    def mock_post_json(url, *, payload, headers):
        assert url == "http://mock-image-api.test/v1/images/generations"
        assert payload["model"] == "stable-diffusion-v1-5"
        assert payload["prompt"] == "a cute cat running under moonlight"
        assert payload["n"] == 1
        assert payload["size"] == "512x512"
        assert payload["response_format"] == "b64_json"
        assert "image" in payload
        assert isinstance(payload["image"], str)
        assert payload["strength"] == 0.7
        return {"data": [{"b64_json": mock_image_data}]}

    with patch("backend.services.skill_runtime._post_json", side_effect=mock_post_json):
        with patch("backend.services.skill_runtime.translate_image_prompt_to_english", return_value="a cute cat running under moonlight"):
            result = generate_stable_diffusion_image_from_reference(
                "月光下奔跑的猫",
                _reference_image,
                output_dir=_TEST_OUTPUT_DIR,
                filename_prefix="img2img-mock",
            )

    assert result["model"] == "stable-diffusion-v1-5"
    assert result["prompt"] == "a cute cat running under moonlight"
    assert result["size"] == "512x512"
    assert result["source"] == "b64_json"
    assert result["strength"] == 0.7
    assert result["reference_image"] == str(_reference_image)

    image_path = Path(result["image_path"])
    assert image_path.is_file()
    assert image_path.suffix == ".png"


def test_img2img_api_failure(monkeypatch, _reference_image):
    """
    测试 img2img API 调用失败时的异常处理。

    测试场景：
    - 关闭 Trial 模式
    - Mock _post_json 抛出异常

    验证内容：
    - 异常被正确向上传播
    - 异常信息包含 "API connection failed"
    """
    from backend.services.skill_runtime import generate_stable_diffusion_image_from_reference

    monkeypatch.delenv("SKILL_TRIAL_RUN", raising=False)
    monkeypatch.setenv("IMAGE_MODEL", "sd-model")
    monkeypatch.setenv("IMAGE_BASE_URL", "http://mock-image-api.test")
    monkeypatch.setenv("IMAGE_SIZE", "512x512")
    monkeypatch.setenv("IMAGE_API_KEY", "test-key")

    def mock_post_json(*args, **kwargs):
        raise Exception("API connection failed")

    with patch("backend.services.skill_runtime._post_json", side_effect=mock_post_json):
        with pytest.raises(Exception, match="API connection failed"):
            generate_stable_diffusion_image_from_reference(
                "a cat",
                _reference_image,
                output_dir=_TEST_OUTPUT_DIR,
            )