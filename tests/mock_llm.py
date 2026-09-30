"""Offline OpenAI-compatible mock for tests.

Speaks pdfskill's own protocols (outline levels, anchored prose, math repair)
and retain-pdf's translation protocols (fake Chinese "translations" that keep
every $...$ span). Run standalone: ``python tests/mock_llm.py PORT``.
"""

from __future__ import annotations

import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MATH = re.compile(r"\$[^$]+\$")
ANCHOR = re.compile(r"^\[\[(b\d{4,})\]\]\s*$")


def fake_translation(src: str) -> str:
    maths = MATH.findall(src or "")
    base = "这是模拟译文，内容保持简洁" if len(src or "") > 20 else "模拟译文"
    return base + ("" if not maths else "：" + " ".join(maths)) + "。"


def outline_reply(user: str) -> str:
    payload = json.loads(user)
    levels = {}
    for c in payload["candidates"]:
        levels[c["id"]] = 0 if c["text"].startswith("Ada Example") else c["prior"]
    return json.dumps({"levels": levels})


def prose_reply(user: str, *, sabotage: bool = False) -> str:
    """Echo text blocks under their anchors, applying the fixes an LLM would make."""
    section = user.split("\nblocks:\n", 1)[1]
    section = section.split("\n\ncontext (next text", 1)[0]
    out, cur, body = [], None, []

    def flush():
        if cur is None:
            return
        text = "\n".join(body).strip()
        text = text.replace("let � be", "let $q$ be")
        text = re.sub(r"(\w)- (\w)", r"\1\2", text)
        m = re.match(r"^1\. (.+?) 2\. (.+)$", text)
        if m:
            text = f"1. {m.group(1)}\n2. {m.group(2)}"
        out.append(f"[[{cur}]]\n{text}")

    for line in section.split("\n"):
        m = ANCHOR.match(line)
        if m:
            flush()
            cur, body = m.group(1), []
        elif line.startswith("<<"):
            flush()
            cur, body = None, []
        elif cur is not None:
            body.append(line)
    flush()
    if sabotage and len(out) > 1:
        out = out[:-1]  # drop a block: the verifier must catch it
        out[0] += " This sentence was invented by the model and must be rejected by the fidelity gate."
    return "\n\n".join(out)


def retain_reply(body: dict, user: str) -> str:
    rf = body.get("response_format") or {}
    name = ((rf.get("json_schema") or {}).get("name")) or ""
    if name == "domain_context_response" or "判断其所属的学术或技术领域" in user:
        return json.dumps(
            {"domain": "测试领域", "summary": "模拟摘要", "translation_guidance": "保持术语一致。"}, ensure_ascii=False
        )
    if name == "continuation_review_response":
        ids = re.findall(r'"pair_id"\s*:\s*"([^"]+)"', user)
        return json.dumps({"decisions": [{"pair_id": i, "decision": "join"} for i in ids]})
    if name == "translation_group_member_response":
        members = json.loads(user)["group"]["members"]
        return json.dumps(
            {
                "member_translations": [
                    {"item_id": m["item_id"], "translated_text": fake_translation(m["source_text"])} for m in members
                ]
            },
            ensure_ascii=False,
        )
    m = re.search(r"【当前原文开始】\n(.*?)\n【当前原文结束】", user, re.S)
    if name == "translation_single_decision_response":
        return json.dumps(
            {"decision": "translate", "translated_text": fake_translation(m.group(1) if m else user)},
            ensure_ascii=False,
        )
    if name in {"translation_single_text_response", "garbled_reconstruction_response"}:
        return json.dumps({"translated_text": fake_translation(m.group(1) if m else user)}, ensure_ascii=False)
    if "<<<ITEM item_id=" in user:
        blocks = re.findall(r"原文 (\S+):\n(.*?)(?=\n\n原文 |\Z)", user, re.S)
        return "\n".join(f"<<<ITEM item_id={i}>>>\n{fake_translation(s)}\n<<<END>>>" for i, s in blocks)
    if m:
        return fake_translation(m.group(1))
    return "模拟译文。"


class State:
    sabotage_first_prose = False
    requests: list[dict] = []


def answer(body: dict) -> str:
    msgs = body.get("messages") or []
    system = "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system")
    user = next((str(m.get("content", "")) for m in msgs if m.get("role") == "user"), "")
    if "fix the heading hierarchy" in system:
        return outline_reply(user)
    if "clean Markdown prose" in system:
        retry = len(msgs) > 2
        sabotage = State.sabotage_first_prose and not retry
        return prose_reply(user, sabotage=sabotage)
    if "repair one LaTeX formula" in system:
        return json.dumps({"latex": json.loads(user)["latex"].replace("\\badcommand", "x")})
    if "Reply with the word" in user:
        return "ok"
    return retain_reply(body, "\n".join(str(m.get("content", "")) for m in msgs if m.get("role") == "user"))


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        State.requests.append({"path": self.path, "auth": bool(self.headers.get("Authorization")), "body": body})
        content = answer(body)
        out = json.dumps(
            {
                "id": "mock",
                "object": "chat.completion",
                "created": 0,
                "model": body.get("model"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            },
            ensure_ascii=False,
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


def start(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
    srv.serve_forever()
