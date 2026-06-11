from fastapi.testclient import TestClient

from backend.main import app


def test_creator_tools_endpoint_lists_registered_tools():
    client = TestClient(app)

    response = client.get("/api/creator/tools")

    assert response.status_code == 200
    body = response.json()
    assert body["override_persistence"] == "process_memory"
    tools = body["tools"]
    names = {tool["name"] for tool in tools}
    assert "text_generation" in names
    assert "wechat_publish" in names


def test_creator_tool_roles_endpoint_exposes_default_capabilities():
    client = TestClient(app)

    response = client.get("/api/creator/tool-roles")

    assert response.status_code == 200
    body = response.json()
    roles = {item["role"]: item for item in body["roles"]}
    assert roles["search_reader"]["required_capabilities"] == ["web_search"]
    assert roles["database_reader"]["required_capabilities"] == ["database_read"]
    assert "reference" not in roles
    assert "reference" in body["resource_roles"]


def test_creator_tool_patch_updates_creator_flags():
    client = TestClient(app)

    response = client.patch("/api/creator/tools/web_search", json={"enabled": False, "allow_creator_use": False})

    assert response.status_code == 200
    tool = response.json()["tool"]
    assert tool["enabled"] is False
    assert tool["allow_creator_use"] is False
    client.patch("/api/creator/tools/web_search", json={"enabled": True, "allow_creator_use": True})


def test_creator_tool_test_is_dry_run_and_does_not_echo_payload_values():
    client = TestClient(app)

    response = client.post(
        "/api/creator/tools/wechat_publish/test",
        json={"payload": {"secret": "do-not-leak", "draft_id": "abc"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["side_effect_performed"] is False
    assert body["payload_keys"] == ["draft_id", "secret"]
    assert "do-not-leak" not in str(body)
