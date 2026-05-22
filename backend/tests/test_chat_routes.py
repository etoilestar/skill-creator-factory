"""Route-level regression tests for decoupled chat routers."""

from backend.main import app


def _has_post_route(path: str) -> bool:
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        if "POST" in methods and getattr(route, "path", "") == path:
            return True
    return False


def test_creator_chat_route_is_registered():
    assert _has_post_route("/api/chat/creator")


def test_sandbox_chat_route_is_registered():
    assert _has_post_route("/api/chat/sandbox/{skill_name}")
