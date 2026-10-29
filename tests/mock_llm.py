"""Local stand-in for an OpenAI-compatible provider. Used only for e2e tests.

    python tests/mock_llm.py 8777

It reads the posts array out of the user message and answers with a JSON array
that marks the first two posts as matches and one as expired, so the whole
pipeline (fetch -> dedupe -> batch -> merge -> UI payload) can be exercised
without spending anyone's tokens.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    user_msg = next((m["content"] for m in body.get("messages", []) if m["role"] == "user"), "")

    match = re.search(r"\[.*\]", user_msg, re.S)
    posts = json.loads(match.group(0)) if match else []

    out = []
    for i, post in enumerate(posts):
        if i == 0:
            out.append({"id": str(post["id"]), "status": "high", "reason": "Mock: strong field match."})
        elif i == 1:
            out.append({"id": post["id"], "status": "medium", "reason": "Mock: partial match."})
        elif i == 2:
            out.append({"id": post["id"], "status": "expired", "reason": "Mock: deadline passed."})

    return {
        "choices": [{"message": {"role": "assistant", "content": json.dumps(out)}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@app.get("/v1/models")
async def models():
    """Pretend to be a provider's live model list (used by tests only)."""
    return {"data": [
        {"id": "gemini-3.8-flash"},
        {"id": "gemini-3.5-flash-lite"},
        {"id": "gemini-3.1-pro-preview"},
        {"id": "text-embedding-005"},
        {"id": "imagen-4"},
        {"id": "gemini-3.1-flash-image"},
    ]}


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8777
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
