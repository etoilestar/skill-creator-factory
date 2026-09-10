import json
import os
import uuid

import httpx
from mcp.server.fastmcp import FastMCP


CREATOR_URL = os.getenv(
    "SUPERSKILLS_CREATOR_URL",
    "http://127.0.0.1:8000/api/chat/creator",
)

mcp = FastMCP(
    "superskills-creator",
    host="0.0.0.0",
    port=8001,
)

# 仅用于第一轮联调。
# DSH进程重启后会话消失没关系，先验证链路。
_sessions: dict[str, list[dict]] = {}


async def call_creator(messages: list[dict]) -> dict:
    payload = {
        "messages": messages,
        "execution_mode": "execute",
        "input_files": [],
    }

    contents = []
    quick_actions = []
    completed = False
    skill_name = None
    skill_path = None
    error = None

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            CREATOR_URL,
            json=payload,
        ) as response:
            response.raise_for_status()

            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue

                raw = line[5:].strip()

                if not raw:
                    continue

                if raw == "[DONE]":
                    break

                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                content = event.get("content")
                if isinstance(content, str):
                    contents.append(content)

                qa = event.get("quick_actions")
                if isinstance(qa, dict):
                    quick_actions = qa.get("actions") or []

                if event.get("type") == "completed":
                    completed = bool(event.get("success"))
                    skill_name = event.get("skill_name")
                    skill_path = event.get("skill_path")

                if event.get("type") == "error":
                    error = str(event.get("message") or "creator error")

                if event.get("error"):
                    error = str(event["error"])

    return {
        "reply": "".join(contents).strip(),
        "quick_actions": quick_actions,
        "completed": completed,
        "skill_name": skill_name,
        "skill_path": skill_path,
        "error": error,
    }


@mcp.tool()
async def creator_turn(
    message: str,
    session_id: str = "",
) -> dict:
    """
    Create a Skill through the SuperSkills Creator.

    Continue using the same session_id until creation is complete.

    If Creator asks a question, show that question to the user.
    Do not answer Creator's clarification questions yourself.

    Pass the user's response back without changing its meaning.
    """

    if not session_id:
        session_id = f"creator_{uuid.uuid4().hex}"

    messages = _sessions.setdefault(session_id, [])

    messages.append({
        "role": "user",
        "content": message,
    })

    result = await call_creator(messages)

    # Creator依赖完整多轮历史判断 Phase1 / Phase2 / Phase3。
    if result["reply"]:
        messages.append({
            "role": "assistant",
            "content": result["reply"],
        })

    return {
        "session_id": session_id,
        **result,
    }

if __name__ == "__main__":
    mcp.run(
        transport="streamable-http",
    )
