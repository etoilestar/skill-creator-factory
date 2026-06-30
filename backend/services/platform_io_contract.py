"""Deterministic platform IO contract shared with Creator prompts.

The sandbox runtime is the source of truth.  Creator code imports this module
only to describe that runtime contract to generation and repair models.
"""

from __future__ import annotations

from typing import Any


def build_platform_io_contract() -> dict[str, Any]:
    """Return the immutable sandbox artifact IO contract for Creator prompts."""
    return {
        "environment": {
            "OUTPUT_DIR": "already points to the final outputs directory for the current skill workspace",
        },
        "platform_skill_boundary": {
            "input_envelope_fields": ["user_request", "input", "text", "payload", "fields", "options", "input_files", "files", "resources"],
            "preferred_structured_input_root": "fields",
            "final_output_fields": ["text", "markdown", "image_path", "image_paths", "pdf_path", "docx_path", "pptx_path", "html_path", "file_paths", "file_outputs"],
            "protocol_notes": [
                "These fields are the hard platform-to-generated-SKILL protocol and may be hardcoded at that boundary.",
                "Do not hardcode the platform field vocabulary for business fields passed between scripts inside a generated SKILL; internal fields are determined by the requirement graph, script argv schema, and actual stdout.",
                "If the first command needs structured business parameters, bind them through fields.<name> placeholders, for example {{fields.keywords}}.",
                "Subsequent commands may only reference previous stdout fields, for example {{story_text}} or {{image_paths}}.",
                "Command argv must not contain case sample values such as [\"童年\",\"分别\",\"重逢\"], \"用户输入的故事内容\", or \"/output/story_1.png\".",
            ],
        },
        "hard_rules": [
            "OUTPUT_DIR already points to final outputs directory.",
            "Do not append 'outputs' to OUTPUT_DIR.",
            "Do not use os.path.join(OUTPUT_DIR, 'outputs').",
            "Do not use os.path.join(output_dir, 'outputs').",
            "Do not replace '/tmp/' with 'outputs/'.",
            "Runtime artifact helpers write artifacts under OUTPUT_DIR.",
            "Helper filename parameters must be basenames only, for example filename='report.pdf'.",
            "Do not pass full paths or absolute paths to filename=.",
            "Prefer returning helper results unchanged, or forwarding result['pdf_path'] and result['file_outputs'] unchanged.",
            "Do not manually rewrite helper-returned pdf_path/file_outputs.",
            "Artifact allowed roots are outputs/ and assets/generated/.",
            "Absolute paths under the current skill workspace outputs/ or assets/generated/ directories are valid artifact paths when the files exist.",
        ],
        "allowed_artifact_roots": ["outputs/", "assets/generated/"],
        "helper_filename_rule": "basename only, e.g. 'report.pdf', never a full path",
        "valid_absolute_paths": [
            "<current_skill_workspace>/outputs/<file>",
            "<current_skill_workspace>/assets/generated/<file>",
        ],
        "forbidden_patterns": [
            "os.path.join(OUTPUT_DIR, 'outputs')",
            "os.path.join(output_dir, 'outputs')",
            ".replace('/tmp/', 'outputs/')",
            "filename=full_path",
            "filename=absolute_path",
        ],
    }


def platform_io_contract_prompt_text() -> str:
    """Render the platform IO contract as concise prompt text."""
    contract = build_platform_io_contract()
    lines = [
        "Platform IO Contract (deterministic, immutable; sandbox is source of truth):",
        "- OUTPUT_DIR already points to final outputs directory.",
        "- Do not append 'outputs' to OUTPUT_DIR; never use os.path.join(OUTPUT_DIR, 'outputs') or os.path.join(output_dir, 'outputs').",
        "- Do not replace '/tmp/' with 'outputs/'.",
        "- Runtime artifact helpers write artifacts under OUTPUT_DIR.",
        "- Helper filename= arguments must be basenames only, e.g. filename='report.pdf'; never filename=full_path or filename=absolute_path.",
        "- Prefer return helper result unchanged, or forward result['pdf_path'] and result['file_outputs'] unchanged.",
        "- Do not manually rewrite helper-returned pdf_path/file_outputs.",
        "- Artifact allowed roots: outputs/ and assets/generated/.",
        "- Absolute paths under current skill workspace outputs/ or assets/generated/ are valid if the files exist.",
        "- Platform ↔ generated SKILL boundary input fields are hard protocol: user_request, input, text, payload, fields, options, input_files, files, resources.",
        "- Prefer structured first-command business input via fields.<name>, e.g. {{fields.keywords}}; these boundary field names may be hardcoded.",
        "- Generated SKILL internal script fields must not hardcode the platform vocabulary; derive them from requirement graph, script argv schema, and previous stdout.",
        "- Subsequent commands may only reference previous stdout fields such as {{story_text}} or {{image_paths}}.",
        "- Command argv must not contain case sample values like [\"童年\",\"分别\",\"重逢\"], \"用户输入的故事内容\", or fixed downstream paths such as \"/output/story_1.png\".",
        "- Contract payload: " + str(contract),
    ]
    return "\n".join(lines)
