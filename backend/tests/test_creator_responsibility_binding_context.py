from backend.services.creator.common import E2EWorkflowCommand, responsibility_edges_by_to_node
from backend.services.creator.e2e import skill_md_command_value_source_mismatches


def test_responsibility_edges_group_by_to_node_without_overwriting_same_input():
    grouped = responsibility_edges_by_to_node([
        {"from_node": "scripts/a.py", "from_output": "items", "to_node": "scripts/b.py", "to_input": "items"},
        {"from_node": "scripts/c.py", "from_output": "items", "to_node": "scripts/d.py", "to_input": "items"},
    ])
    assert grouped["scripts/b.py"][0]["from_node"] == "scripts/a.py"
    assert grouped["scripts/d.py"][0]["from_node"] == "scripts/c.py"


def test_script_to_script_file_sentinel_mismatch_and_placeholder_passes():
    context = responsibility_edges_by_to_node([
        {"from_node": "scripts/generate_story.py", "from_output": "items", "to_node": "scripts/generate_images.py", "to_input": "items"},
    ])
    bad = [E2EWorkflowCommand(1, "SKILL.md", "scripts/generate_images.py", "python scripts/generate_images.py '{}'", "python", {"items": "__RUNTIME_INPUT_FILE__"})]
    issues = skill_md_command_value_source_mismatches(bad, context)
    assert issues[0]["issue_type"] == "command_value_source_mismatch"
    assert issues[0]["target_script"] == "scripts/generate_images.py"
    assert issues[0]["argv_key"] == "items"
    assert issues[0]["current_value"] == "__RUNTIME_INPUT_FILE__"
    assert issues[0]["from_node"] == "scripts/generate_story.py"
    assert issues[0]["from_output"] == "items"

    good = [E2EWorkflowCommand(1, "SKILL.md", "scripts/generate_images.py", "python scripts/generate_images.py '{}'", "python", {"items": "{{items}}"})]
    assert skill_md_command_value_source_mismatches(good, context) == []


def test_user_text_platform_input_cannot_use_file_sentinel():
    context = responsibility_edges_by_to_node([
        {"from_node": "platform_input_node", "from_output": "user_request", "to_node": "scripts/topic.py", "to_input": "topic"},
    ])
    commands = [E2EWorkflowCommand(1, "SKILL.md", "scripts/topic.py", "python scripts/topic.py '{}'", "python", {"topic": "__RUNTIME_INPUT_FILE__"})]
    issues = skill_md_command_value_source_mismatches(commands, context)
    assert issues[0]["issue_type"] == "command_value_source_mismatch"
    assert issues[0]["from_output"] == "user_request"


def test_platform_file_input_still_allows_official_file_sentinel():
    context = responsibility_edges_by_to_node([
        {"from_node": "platform_input_node", "from_output": "uploaded_file", "to_node": "scripts/read_upload.py", "to_input": "input_file"},
    ])
    commands = [E2EWorkflowCommand(1, "SKILL.md", "scripts/read_upload.py", "python scripts/read_upload.py '{}'", "python", {"input_file": "__RUNTIME_INPUT_FILES__"})]
    assert skill_md_command_value_source_mismatches(commands, context) == []


def test_bare_file_tokens_are_not_accepted_as_platform_sentinels():
    context = responsibility_edges_by_to_node([
        {"from_node": "platform_input_node", "from_output": "uploaded_file", "to_node": "scripts/read_upload.py", "to_input": "input_file"},
    ])
    commands = [E2EWorkflowCommand(1, "SKILL.md", "scripts/read_upload.py", "python scripts/read_upload.py '{}'", "python", {"input_file": "FILES"})]
    assert skill_md_command_value_source_mismatches(commands, context)
