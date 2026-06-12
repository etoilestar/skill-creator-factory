"""Runtime helpers exposed to generated Skill scripts.

These helpers keep platform-specific model contracts out of generated Skills:
- text/translation requests use the LLM endpoint and TEXT_MODEL;
- Stable Diffusion image requests use the image endpoint and IMAGE_MODEL;
- image responses are normalized to files under OUTPUT_DIR.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")

# A valid 1x1 transparent PNG used only during Creator trial runs.  Real
# sandbox execution never uses this branch unless SKILL_TRIAL_RUN is set.
_TRIAL_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMB/"
    "gL+X2cAAAAASUVORK5CYII="
)


def _timeout_seconds() -> float:
    raw = _env("LLM_TIMEOUT_SECONDS", "6000")
    try:
        return float(raw)
    except ValueError:
        return 6000.0


def _build_chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _build_image_generations_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/images/generations"):
        return base
    if base.endswith("/v1"):
        return f"{base}/images/generations"
    return f"{base}/v1/images/generations"


def _post_json(url: str, *, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:  # nosec: platform-configured URL
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def _required_env(name: str) -> str:
    value = _env(name).strip()
    if not value:
        raise RuntimeError(
            f"Required platform environment variable {name} is not set. "
            "Skill scripts must run through the platform sandbox/runtime."
        )
    return value


def _llm_api_key() -> str:
    return (
        _env("LLM_API_KEY")
        or _env("OPENAI_API_KEY")
        or "ollama"
    )


def _image_api_key() -> str:
    return (
        _env("IMAGE_API_KEY")
        or _llm_api_key()
    )


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }


def _strip_model_text(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip().strip('"').strip("'").strip()


def translate_image_prompt_to_english(
    topic: str,
    *,
    style_context: str = "",
    style_keywords: list[str] | None = None,
) -> str:
    """Silently translate/rewrite an image topic into an English SD prompt.
    Automatically supplement quality, detail, light and composition for stable-diffusion-2-1-base.

    Args:
        topic: 图片描述提示词（支持中文）。
        style_context: 风格上下文描述（如从参考图通过 describe_image_with_vision 提取的风格描述）。
        style_keywords: 风格关键词列表（如从参考图提取的 SD 英文关键词）。
    """
    topic = str(topic or "").strip()
    if not topic:
        raise ValueError("image topic/prompt is empty")

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        return "a cinematic watercolor cat under a warm sunset"

    url = _build_chat_completions_url(_required_env("LLM_BASE_URL"))
    model = _required_env("TEXT_MODEL")

    # 构建风格增强的系统提示
    style_section = ""
    if style_context or style_keywords:
        style_section = "\n\n8. The user provides a reference style description and keywords. "
        "You MUST incorporate these style elements into the final prompt. "
        "The style keywords should be seamlessly blended, not just appended."
        if style_context:
            style_section += f"\n\nReference style description: {style_context}"
        if style_keywords:
            style_section += f"\n\nReference style keywords: {', '.join(style_keywords)}"

    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a professional Stable Diffusion prompt engineer for stable-diffusion-2-1-base. "
                    "Your task: rewrite user's image description into a high-quality, detailed, standard SD English prompt. "
                    "Strict rules:\n"
                    "1. 100% preserve the user's core subject and scene intent.\n"
                    "2. Automatically add universal quality keywords: masterpiece, best quality, ultra-detailed, sharp focus.\n"
                    "3. Automatically supplement reasonable visual details: texture, lighting, atmosphere, background, depth of field.\n"
                    "4. For creatures/characters (mermaid, cat, human), add perfect anatomy, clean features, complete limbs.\n"
                    "5. For underwater scenes, add underwater light rays, clear seawater, fine bubbles, soft water haze.\n"
                    "6. Output ONLY one concise, fluent prompt, no explanation, no markdown, no extra text.\n"
                    "7. Do not use overly exaggerated words, keep stable for SD 2.1-base generation."
                    + style_section
                ),
            },
            {
                "role": "user",
                "content": (
                    "Rewrite this Chinese image topic into a professional Stable Diffusion English prompt. "
                    "Enrich scene details, lighting, texture and atmosphere automatically.\n\n"
                    f"Topic: {topic}"
                ),
            },
        ],
    }
    try:
        data = _post_json(url, payload=payload, headers=_headers(_llm_api_key()))
    except Exception:
        if _CJK_RE.search(topic):
            raise
        logger.warning("image prompt rewrite failed; using original English prompt", exc_info=True)
        return topic

    choices = data.get("choices") or []
    if not choices:
        if _CJK_RE.search(topic):
            raise ValueError("TEXT_MODEL returned no prompt rewrite choices")
        return topic

    message = choices[0].get("message") or {}
    rewritten = _strip_model_text(message.get("content") or choices[0].get("text") or "")
    if not rewritten:
        if _CJK_RE.search(topic):
            raise ValueError("TEXT_MODEL returned an empty prompt rewrite")
        return topic
    return rewritten


def _write_image_bytes(*, image_bytes: bytes, output_dir: Path, filename_prefix: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_prefix = _SAFE_FILENAME_RE.sub("-", filename_prefix.strip() or "image").strip("-._") or "image"
    path = output_dir / f"{safe_prefix}-{int(time.time() * 1000)}.png"
    path.write_bytes(image_bytes)
    return path


def _decode_image_response(data: dict[str, Any]) -> tuple[bytes, str]:
    items = data.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise ValueError("image API response missing data[0]")

    first = items[0]
    b64_json = first.get("b64_json")
    if b64_json:
        return base64.b64decode(str(b64_json)), "b64_json"

    raise ValueError(f"Image API did not return b64_json: {data}")



def generate_text_with_llm(prompt: str, *, system: str = "", temperature: float = 0.7) -> str:
    """Generate text with the platform configured LLM/TEXT_MODEL.

    Generated Skill scripts use this helper instead of embedding API details.
    During Creator trial runs it returns deterministic non-empty text so script
    validation can verify plumbing without network access.
    """
    prompt = str(prompt or "").strip()
    if not prompt:
        raise ValueError("text generation prompt is empty")

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        return f"Generated text for: {prompt}"

    url = _build_chat_completions_url(_required_env("LLM_BASE_URL"))
    payload = {
        "model": _required_env("TEXT_MODEL"),
        "stream": False,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system or "You are a helpful writing assistant."},
            {"role": "user", "content": prompt},
        ],
    }
    data = _post_json(url, payload=payload, headers=_headers(_llm_api_key()))
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("TEXT_MODEL returned no choices")
    message = choices[0].get("message") or {}
    text = _strip_model_text(message.get("content") or choices[0].get("text") or "")
    if not text:
        raise ValueError("TEXT_MODEL returned empty text")
    return text

def generate_stable_diffusion_image(
    topic: str,
    *,
    output_dir: str | os.PathLike[str] | None = None,
    filename_prefix: str = "image",
    size: str | None = None,
) -> dict[str, Any]:
    """Generate an image with the platform Stable Diffusion model.

    Returns a JSON-serializable dict containing the English prompt, image model,
    image path, and response source.  Generated scripts can print this dict as
    stdout JSON so the sandbox can expose the image as an output file.
    """
    out_dir = Path(output_dir or _env("OUTPUT_DIR", "outputs"))
    english_prompt = translate_image_prompt_to_english(topic)

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        image_path = _write_image_bytes(
            image_bytes=base64.b64decode(_TRIAL_PNG_B64),
            output_dir=out_dir,
            filename_prefix=filename_prefix,
        )
        return {
            "prompt": english_prompt,
            "model": _required_env("IMAGE_MODEL"),
            "image_path": str(image_path),
            "source": "trial",
        }

    # IMAGE_MODEL is injected by the platform from settings.image_model.
    # Do not hardcode Stable Diffusion model names in generated Skills.
    image_model = _required_env("IMAGE_MODEL")
    image_base_url = _required_env("IMAGE_BASE_URL")
    image_size = size or _required_env("IMAGE_SIZE")
    url = _build_image_generations_url(image_base_url)

    # 全局默认负面提示词（适配SD2.1-base、人鱼、动物、人像）
    negative_prompt = (
        "lowres, blurry, worst quality, low quality, text, watermark, signature, "
        "deformed, disfigured, distorted, ugly, bad anatomy, "
        "extra limbs, extra arms, extra legs, extra fingers, extra heads, "
        "multiple heads, multiple faces, multiple bodies, multiple characters, "
        "bad hands, bad paws, bad face, bad proportions, malformed limbs, "
        "floating limbs, disconnected limbs, disconnected head, "
        "cropped, out of frame, cut off, border, frame, "
        "mutated, mutation, deformed face, fused fingers, too many fingers, "
        "long neck, huge head, giant head, disproportionate, asymmetric, "
        "grainy, noisy, jpeg artifacts, pixelated, overexposed, underexposed, "
        "bad tail, multiple tails, deformed tail, ugly tail, "
        "duplicate, cloned, copy, repeat, repetition"
    )

    payload = {
        "model": image_model,
        "prompt": english_prompt,
        "negative_prompt": negative_prompt,  # 新增：默认负面词
        "n": 1,
        "size": image_size,
        "response_format": "b64_json",
    }

    data = _post_json(url, payload=payload, headers=_headers(_image_api_key()))

    image_bytes, source = _decode_image_response(data)
    image_path = _write_image_bytes(
        image_bytes=image_bytes,
        output_dir=out_dir,
        filename_prefix=filename_prefix,
    )
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
    return {
        "prompt": english_prompt,
        "model": image_model,
        "size": image_size,
        "image_path": str(image_path),
        "mime_type": mime_type,
        "source": source,
    }


def generate_stable_diffusion_image_from_reference(
    topic: str,
    reference_image: str | os.PathLike[str],
    *,
    strength: float = 0.45,
    output_dir: str | os.PathLike[str] | None = None,
    filename_prefix: str = "image",
    size: str | None = None,
    num_inference_steps: int | None = None,
    guidance_scale: float | None = None,
    analyze_reference: bool = True,
) -> dict[str, Any]:
    """基于参考图生成新图片（img2img），使用 /v1/images/generations 接口。

    与文生图共用同一端点，通过额外传递 image(base64) 和 strength 字段
    触发服务端 img2img 逻辑。服务端需在 /v1/images/generations 中检测
    image 字段以切换到 StableDiffusionImg2ImgPipeline。

    当 analyze_reference=True 时，会先调用 VISION_MODEL 分析参考图的
    风格特征，自动提取风格关键词并融入生图提示词，提升 img2img 输出质量。

    Args:
        topic: 图片描述提示词（支持中文，内部自动翻译为英文）。
        reference_image: 参考图文件路径。
        strength: 参考图影响强度，0.0~1.0。默认 0.45，较小的值保留更多参考图风格，但让提示词主导主体构图。
        output_dir: 图片输出目录。
        filename_prefix: 输出文件名前缀。
        size: 图片尺寸。
        num_inference_steps: 推理步数（默认由服务端决定，建议 30-50）。
        guidance_scale: 引导系数（默认由服务端决定，建议 7-12）。
        analyze_reference: 是否使用 VISION_MODEL 分析参考图风格。

    Returns:
        包含 prompt, model, image_path, source 等字段的字典。
    """
    ref_path = Path(reference_image)
    if not ref_path.is_file():
        raise FileNotFoundError(f"参考图文件不存在: {reference_image}")

    out_dir = Path(output_dir or _env("OUTPUT_DIR", "outputs"))

    # 使用 VISION_MODEL 分析参考图风格（可选）
    style_context = ""
    style_keywords: list[str] = []
    vision_result: dict[str, Any] = {}
    if analyze_reference:
        try:
            vision_result = describe_image_with_vision(reference_image)
            style_context = vision_result.get("description") or ""
            style_keywords = vision_result.get("style_keywords") or []
            logger.info(
                "Reference image analysis: description=%s keywords=%s",
                style_context[:100], style_keywords,
            )
        except Exception as exc:
            logger.warning("Reference image style analysis failed (continuing without): %s", exc)

    english_prompt = translate_image_prompt_to_english(
        topic,
        style_context=style_context,
        style_keywords=style_keywords if style_keywords else None,
    )

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        image_path = _write_image_bytes(
            image_bytes=ref_path.read_bytes(),
            output_dir=out_dir,
            filename_prefix=filename_prefix,
        )
        return {
            "prompt": english_prompt,
            "model": _required_env("IMAGE_MODEL"),
            "image_path": str(image_path),
            "source": "trial",
            "reference_image": str(ref_path),
            "strength": strength,
        }

    image_model = _required_env("IMAGE_MODEL")
    image_base_url = _required_env("IMAGE_BASE_URL")
    image_size = size or _required_env("IMAGE_SIZE")
    url = _build_image_generations_url(image_base_url)

    ref_b64 = base64.b64encode(ref_path.read_bytes()).decode("utf-8")

    # 全局负面提示词：显式压制多主体、畸形肢体、低质量输出
    negative_prompt = (
        "lowres, blurry, worst quality, low quality, text, watermark, signature, "
        "deformed, disfigured, distorted, ugly, bad anatomy, "
        "extra limbs, extra arms, extra legs, extra fingers, extra heads, "
        "multiple heads, multiple faces, multiple bodies, multiple characters, "
        "bad hands, bad paws, bad face, bad proportions, malformed limbs, "
        "floating limbs, disconnected limbs, disconnected head, "
        "cropped, out of frame, cut off, border, frame, "
        "mutated, mutation, deformed face, fused fingers, too many fingers, "
        "long neck, huge head, giant head, disproportionate, asymmetric, "
        "grainy, noisy, jpeg artifacts, pixelated, overexposed, underexposed, "
        "bad tail, multiple tails, deformed tail, ugly tail, "
        "duplicate, cloned, copy, repeat, repetition"
    )

    payload: dict[str, Any] = {
        "model": image_model,
        "prompt": english_prompt,
        "negative_prompt": negative_prompt,
        "n": 1,
        "size": image_size,
        "response_format": "b64_json",
        "image": ref_b64,
        "strength": strength,
    }
    if num_inference_steps is not None:
        payload["num_inference_steps"] = num_inference_steps
    if guidance_scale is not None:
        payload["guidance_scale"] = guidance_scale

    data = _post_json(url, payload=payload, headers=_headers(_image_api_key()))
    result_bytes, source = _decode_image_response(data)
    image_path = _write_image_bytes(
        image_bytes=result_bytes,
        output_dir=out_dir,
        filename_prefix=filename_prefix,
    )
    result_mime = mimetypes.guess_type(str(image_path))[0] or "image/png"
    result: dict[str, Any] = {
        "prompt": english_prompt,
        "model": image_model,
        "size": image_size,
        "image_path": str(image_path),
        "mime_type": result_mime,
        "source": source,
        "reference_image": str(ref_path),
        "strength": strength,
    }
    if vision_result:
        result["vision_analysis"] = vision_result
    return result


def describe_image_with_vision(
    image_path: str | os.PathLike[str],
    *,
    prompt: str = "请详细描述这张图片的视觉风格、色彩、构图和内容。重点关注：1) 艺术风格（如水墨、油画、卡通等）2) 色调与亮度 3) 画面主体与构图 4) 线条与笔触特点",
) -> dict[str, Any]:
    """使用 VISION_MODEL（如 qwen3-vl:32b）分析图片，提取风格描述和视觉特征。

    典型用途：
    - 在 img2img 之前分析参考图，自动提取风格关键词
    - 用提取的风格描述优化生图提示词
    - 验证生成图片是否符合预期

    Args:
        image_path: 图片文件路径。
        prompt: 发送给视觉模型的分析提示词。

    Returns:
        包含 description（风格描述）、style_keywords（风格关键词列表）的字典。
    """
    img_path = Path(image_path)
    if not img_path.is_file():
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    if os.environ.get("SKILL_TRIAL_RUN") == "1":
        return {
            "description": "水墨风格插画，高亮度，温暖色调，简洁线条，儿童绘本风格",
            "style_keywords": ["ink wash", "high brightness", "warm tones", "simple lines", "children illustration"],
            "model": _env("VISION_MODEL", "qwen3-vl:32b"),
            "source": "trial",
        }

    vision_model = _required_env("VISION_MODEL")
    url = _build_chat_completions_url(_required_env("LLM_BASE_URL"))

    # 读取图片并 base64 编码
    img_b64 = base64.b64encode(img_path.read_bytes()).decode("utf-8")
    # 推断 MIME 类型
    mime_type = mimetypes.guess_type(str(img_path))[0] or "image/png"

    payload = {
        "model": vision_model,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": prompt,
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{img_b64}",
                        },
                    },
                ],
            },
            {
                "role": "system",
                "content": (
                    "你是一个专业的视觉风格分析师。分析图片后，请输出 JSON 格式：\n"
                    '{"description": "对图片视觉风格的详细描述", '
                    '"style_keywords": ["关键词1", "关键词2", ...]}\n'
                    "style_keywords 应包含适用于 Stable Diffusion 提示词的英文关键词，"
                    "如：ink wash, watercolor, high brightness, warm tones, soft lines, "
                    "children illustration, storybook style 等。"
                    "只输出 JSON，不要输出其他内容。"
                ),
            },
        ],
    }

    try:
        data = _post_json(url, payload=payload, headers=_headers(_llm_api_key()))
    except Exception as exc:
        logger.warning("Vision model call failed: %s", exc)
        return {
            "description": "",
            "style_keywords": [],
            "model": vision_model,
            "source": "error",
            "error": str(exc),
        }

    choices = data.get("choices") or []
    if not choices:
        return {
            "description": "",
            "style_keywords": [],
            "model": vision_model,
            "source": "empty",
        }

    message = choices[0].get("message") or {}
    content = _strip_model_text(message.get("content") or "")

    # 尝试解析 JSON 响应
    try:
        result = json.loads(content)
        if isinstance(result, dict):
            return {
                "description": str(result.get("description") or ""),
                "style_keywords": list(result.get("style_keywords") or []),
                "model": vision_model,
                "source": "vision",
            }
    except json.JSONDecodeError:
        pass

    # JSON 解析失败，将整个内容作为描述
    return {
        "description": content,
        "style_keywords": [],
        "model": vision_model,
        "source": "vision_raw",
    }


def print_json(data: dict[str, Any]) -> None:
    """Print compact UTF-8 JSON for generated scripts."""
    print(json.dumps(data, ensure_ascii=False))