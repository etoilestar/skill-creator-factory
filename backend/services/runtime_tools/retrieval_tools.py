"""Controlled retrieval and read-only database helpers for generated Skills."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from typing import Any

_MAX_SEARCH_RESULTS = 10
_MAX_FETCH_CHARS = 12000
_FORBIDDEN_SQL = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|replace|grant|revoke|vacuum|attach|detach)\b", re.I)


def _trial() -> bool:
    return os.environ.get("SKILL_TRIAL_RUN") == "1"


def _timeout(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name) or default)
    except ValueError:
        return default


def _json_request(url: str, *, headers: dict[str, str] | None = None, timeout: float = 20.0) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec: platform-configured URL
        body = response.read(1024 * 1024).decode("utf-8", errors="replace")
    return json.loads(body)


def web_search(query: str, top_k: int = 5, language: str | None = None) -> dict[str, Any]:
    """Search with SearchXNG/SearXNG and return JSON-serializable results."""
    query = str(query or "").strip()
    if not query:
        raise ValueError("query is empty")
    top_k = max(1, min(int(top_k or 5), _MAX_SEARCH_RESULTS))
    if _trial():
        return {
            "query": query,
            "results": [
                {
                    "title": f"Mock result {index} for {query}",
                    "url": f"https://example.com/mock/{index}",
                    "snippet": "Mock search result returned during SKILL_TRIAL_RUN.",
                    "source": "trial",
                }
                for index in range(1, top_k + 1)
            ],
        }

    base_url = (os.environ.get("SEARCHXNG_BASE_URL") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("SEARCHXNG_BASE_URL is not set")
    params = {"q": query, "format": "json"}
    if language:
        params["language"] = language
    if os.environ.get("SEARCHXNG_ENGINE"):
        params["engines"] = os.environ["SEARCHXNG_ENGINE"]
    url = f"{base_url}/search?{urllib.parse.urlencode(params)}"
    headers = {"Accept": "application/json"}
    if os.environ.get("SEARCHXNG_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['SEARCHXNG_API_KEY']}"
    data = _json_request(url, headers=headers, timeout=_timeout("SEARCHXNG_TIMEOUT", 20.0))
    results = []
    for item in list(data.get("results") or [])[:top_k]:
        results.append({
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or item.get("snippet") or ""),
            "source": str(item.get("engine") or item.get("source") or "searchxng"),
        })
    return {"query": query, "results": results}


def fetch_url_text(url: str, max_chars: int = _MAX_FETCH_CHARS) -> dict[str, Any]:
    """Fetch URL text with bounded response size."""
    url = str(url or "").strip()
    if not url:
        raise ValueError("url is empty")
    max_chars = max(1, min(int(max_chars or _MAX_FETCH_CHARS), _MAX_FETCH_CHARS))
    if _trial():
        return {"url": url, "text": "Mock fetched page text during SKILL_TRIAL_RUN.", "truncated": False}
    request = urllib.request.Request(url, headers={"User-Agent": "superskills-runtime/1.0"})
    with urllib.request.urlopen(request, timeout=_timeout("SEARCHXNG_TIMEOUT", 20.0)) as response:  # nosec: caller-provided capability-gated URL
        raw = response.read(max_chars * 4 + 1)
    text = raw.decode("utf-8", errors="replace")[:max_chars]
    return {"url": url, "text": text, "truncated": len(raw) > len(text.encode("utf-8", errors="ignore"))}


def _database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dialect = os.environ.get("DB_DIALECT") or ""
    host = os.environ.get("DB_HOST") or ""
    name = os.environ.get("DB_NAME") or ""
    user = urllib.parse.quote(os.environ.get("DB_USER") or "")
    password = urllib.parse.quote(os.environ.get("DB_PASSWORD") or "")
    port = f":{os.environ.get('DB_PORT')}" if os.environ.get("DB_PORT") else ""
    auth = f"{user}:{password}@" if user or password else ""
    if dialect and host and name:
        return f"{dialect}://{auth}{host}{port}/{name}"
    return ""


def _validate_sql(sql: str) -> str:
    statement = str(sql or "").strip().rstrip(";")
    if not re.match(r"^(select|with|show|describe|explain)\b", statement, re.I):
        raise ValueError("Only readonly statements are allowed (SELECT/WITH/SHOW/DESCRIBE/EXPLAIN)")
    if ";" in statement or _FORBIDDEN_SQL.search(statement):
        raise ValueError("Only a single readonly statement is allowed")
    return statement


def _limited_sql(sql: str, limit: int) -> str:
    limit = max(1, min(int(limit or 100), 1000))
    if re.search(r"\blimit\s+\d+\b", sql, re.I):
        return sql
    # SHOW/DESCRIBE/EXPLAIN are metadata queries that don't support LIMIT
    if re.match(r"^(show|describe|explain)\b", sql, re.I):
        return sql
    return f"{sql} LIMIT {limit}"


def query_database_readonly(sql: str, params: dict | None = None, limit: int = 100) -> dict[str, Any]:
    """Run a bounded read-only query without exposing credentials."""
    limit = max(1, min(int(limit or 100), 1000))
    statement = _limited_sql(_validate_sql(sql), limit)
    if _trial():
        return {"columns": ["id", "name"], "rows": [{"id": 1, "name": "mock"}], "row_count": 1, "truncated": False}
    url = _database_url()
    if not url:
        return {
            "columns": [], "rows": [], "row_count": 0, "truncated": False,
            "error": "DATABASE_URL or DB_* environment variables are not set. 无法连接数据库，请配置数据库连接信息。",
        }
    try:
        if url.startswith("sqlite:///"):
            db_path = url.removeprefix("sqlite:///")
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(statement, params or {}).fetchmany(limit + 1)
                columns = list(rows[0].keys()) if rows else []
                data_rows = [dict(row) for row in rows[:limit]]
                return {"columns": columns, "rows": data_rows, "row_count": len(data_rows), "truncated": len(rows) > limit}
        from sqlalchemy import create_engine, text  # type: ignore

        engine = create_engine(url)
        with engine.connect() as conn:
            result = conn.execute(text(statement), params or {})
            rows = result.fetchmany(limit + 1)
            columns = list(result.keys())
        return {"columns": columns, "rows": [dict(row._mapping) for row in rows[:limit]], "row_count": min(len(rows), limit), "truncated": len(rows) > limit}
    except Exception as exc:
        return {
            "columns": [], "rows": [], "row_count": 0, "truncated": False,
            "error": f"数据库查询失败: {exc}",
        }


def list_database_tables() -> dict[str, Any]:
    if _trial():
        return {"tables": ["mock_table"]}
    return query_database_readonly("SELECT name FROM sqlite_master WHERE type='table'", limit=1000) if _database_url().startswith("sqlite:///") else {"tables": []}


def describe_database_table(table_name: str) -> dict[str, Any]:
    table_name = re.sub(r"[^A-Za-z0-9_]", "", str(table_name or ""))
    if not table_name:
        raise ValueError("table_name is empty")
    if _trial():
        return {"table_name": table_name, "columns": [{"name": "id", "type": "integer"}]}
    if _database_url().startswith("sqlite:///"):
        result = query_database_readonly(f"SELECT name, type FROM pragma_table_info('{table_name}')", limit=1000)
        return {"table_name": table_name, "columns": result.get("rows", [])}
    return {"table_name": table_name, "columns": []}


# ---------------------------------------------------------------------------
# Ollama Embedding helpers
# ---------------------------------------------------------------------------

def get_ollama_embedding(text: str, model: str | None = None) -> list[float]:
    """调用 Ollama /api/embeddings 端点获取文本向量。

    复用平台已有的 LLM_BASE_URL 和 EMBEDDING_MODEL 配置，
    无需额外安装 embedding 模型依赖。
    """
    import httpx

    text = str(text or "").strip()
    if not text:
        raise ValueError("text is empty")

    if _trial():
        return [0.0] * 1024  # bge-m3 维度

    base_url = (os.environ.get("LLM_BASE_URL") or "http://localhost:11434").rstrip("/")
    model = model or os.environ.get("EMBEDDING_MODEL") or "bge-m3:latest"
    resp = httpx.post(
        f"{base_url}/api/embeddings",
        json={"model": model, "prompt": text},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["embedding"]


# ---------------------------------------------------------------------------
# FAISS vector index helpers
# ---------------------------------------------------------------------------

def build_faiss_index(
    chunks: list[dict],
    index_path: str,
    *,
    batch_size: int = 8,
) -> dict[str, Any]:
    """构建 FAISS 向量索引并保存。

    Args:
        chunks: 文档块列表，每项需含 ``content``、``source``、``page`` 等字段。
        index_path: FAISS 索引保存路径。

    Returns:
        含 ``chunk_count``、``index_path``、``chunks_meta_path`` 的摘要字典。
    """
    if not chunks:
        return {"chunk_count": 0, "index_path": "", "chunks_meta_path": "", "error": "chunks 为空"}

    import numpy as np
    import faiss

    # 获取 embedding 向量
    dim = None
    embeddings = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        for chunk in batch:
            content = str(chunk.get("content") or "")
            try:
                emb = get_ollama_embedding(content)
            except Exception:
                # 零向量填充失败的 embedding
                dim = dim or 1024
                emb = [0.0] * dim
            if dim is None:
                dim = len(emb)
            embeddings.append(emb)

    vectors = np.array(embeddings, dtype=np.float32)
    faiss.normalize_L2(vectors)
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)

    # 保存索引
    from pathlib import Path

    index_file = Path(index_path)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    faiss.write_index(index, str(index_file))

    # 保存元数据
    meta_path = index_file.parent / "chunks_meta.json"
    meta = {
        "chunk_count": len(chunks),
        "dimension": dim,
        "chunks": chunks,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    return {
        "chunk_count": len(chunks),
        "index_path": str(index_file),
        "chunks_meta_path": str(meta_path),
    }


def search_faiss_index(
    query: str,
    index_path: str,
    *,
    top_k: int = 5,
) -> dict[str, Any]:
    """语义检索：query embedding → FAISS search → 返回相关文档块。

    Returns:
        含 ``query`` 和 ``results`` 列表的字典。每个 result 包含
        content、source、page、score 等字段。
    """
    query = str(query or "").strip()
    if not query:
        return {"query": "", "results": [], "error": "query is empty"}

    from pathlib import Path

    if not Path(index_path).exists():
        return {"query": query, "results": [], "error": "索引文件不存在"}

    import numpy as np
    import faiss

    index = faiss.read_index(index_path)

    meta_path = str(Path(index_path).parent / "chunks_meta.json")
    with open(meta_path, "r", encoding="utf-8") as f:
        meta = json.load(f)
    chunks = meta.get("chunks", [])

    if not chunks:
        return {"query": query, "results": [], "error": "索引中没有文档块"}

    # query embedding
    try:
        query_emb = get_ollama_embedding(query)
    except Exception as exc:
        return {"query": query, "results": [], "error": f"获取查询向量失败: {exc}"}

    query_vec = np.array([query_emb], dtype=np.float32)
    faiss.normalize_L2(query_vec)
    scores, indices = index.search(query_vec, min(top_k, len(chunks)))

    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(chunks):
            continue
        chunk = chunks[idx]
        results.append({
            "content": chunk.get("content", ""),
            "source": chunk.get("source", ""),
            "page": chunk.get("page", 0),
            "section": chunk.get("section", ""),
            "score": round(float(score), 4),
        })

    return {"query": query, "results": results}
