"""A fake model provider for the agent tests: real Claude Code, Codex, OMP and Pi binaries talk
to it instead of Anthropic/OpenAI, so no login and no tokens are needed.

Speaks the Anthropic Messages API (`POST /v1/messages`: Claude Code, OMP, Pi) and the OpenAI
Responses API (`POST /v1/responses`: Codex), streaming. What it answers is scripted by a marker
in the latest user message:

  HACP-SAY <text>          reply with <text>
  HACP-RUN <cmd>           call the agent's shell tool with <cmd>; once the tool result comes
                           back, reply "HACP-RAN"
  HACP-SLOW <secs> <text>  a slow model: nothing for <secs>, then <text> word by word

Anything else (title generation, quota probes, advisors) gets a short plain reply. The shell
tool and its argument names are read from the request's tool list, so a renamed tool or a new
schema in a daily agent update doesn't need a change here.

Run standalone to watch requests: `python tests/fakellm.py [port] [dump dir]` (logs to stderr).
"""

import itertools
import json
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MARKER = re.compile(r"HACP-(SAY|RUN|SLOW) (.+?)(?:\s*$|\n)", re.S)
SHELL_TOOLS = ("bash", "shell", "exec_command", "shell_command")  # compared without case or leading "_" (OMP: "_bash")
ids = itertools.count(1)


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") in ("text", "input_text")]


def _marker(texts: list[str]):
    for t in reversed(texts):
        found = MARKER.findall(t)
        if found:
            kind, arg = found[-1]
            return kind, arg.strip()
    return None


def _shell_args(schema: dict, cmd: str) -> dict:
    """Arguments for the agent's shell tool, from its JSON schema."""
    props = (schema or {}).get("properties") or {}
    key = "command" if "command" in props else "cmd" if "cmd" in props else "command"
    args = {key: ["bash", "-lc", cmd] if (props.get(key) or {}).get("type") == "array" else cmd}
    if "sandbox_permissions" in props:  # Codex: ask to leave the sandbox, which needs approval
        args["sandbox_permissions"] = "require_escalated"
    for name in (schema or {}).get("required") or []:  # e.g. Claude's description, Codex's justification, OMP's intent "i"
        if name not in args and (props.get(name) or {}).get("type") == "string":
            args[name] = "hacp test"
    if "justification" in props:
        args["justification"] = "hacp test needs to write a file"
    return args


def _shell_tool(tools: list) -> tuple[str, dict] | None:
    for t in tools or []:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") or ""
        if name.lstrip("_").lower() in SHELL_TOOLS:
            return name, fn.get("input_schema") or fn.get("parameters") or {}
    return None


def plan(user_texts: list[str], after_tool_result: bool, tools: list):
    """('text', str[, seconds]) or ('tool', name, args): the scripted answer to one request.
    `seconds`: wait that long before answering, then stream word by word (a slow model)."""
    if after_tool_result:
        return ("text", "HACP-RAN")
    found = _marker(user_texts)
    shell = _shell_tool(tools)
    if found and found[0] == "RUN" and shell:
        return ("tool", shell[0], _shell_args(shell[1], found[1]))
    if found and found[0] == "SAY":
        return ("text", found[1])
    if found and found[0] == "SLOW":
        secs, _, text = found[1].partition(" ")
        return ("text", text, float(secs)) if shell else ("text", text)  # side requests (titles) stay fast
    return ("text", "ok")


def _pieces(p) -> list[str]:
    """The text deltas: word by word for a slow answer, else in one piece."""
    if len(p) < 3:
        return [p[1]]
    words = p[1].split(" ")
    return [w if i == 0 else " " + w for i, w in enumerate(words)]


# ---- Anthropic Messages ----------------------------------------------------------------------

def anthropic_plan(body: dict):
    msgs = [m for m in body.get("messages") or [] if m.get("role") != "system"]  # Claude appends a system note
    last = msgs[-1] if msgs else {}
    content = last.get("content") if last.get("role") == "user" else None
    if not isinstance(content, list):
        return plan(_texts(content), False, body.get("tools"))
    # After a rejection Claude puts the next prompt in the same user message as the rejected tool's
    # result: whichever comes last, a marker or a tool result, is what this request answers.
    last_result = max((i for i, b in enumerate(content) if isinstance(b, dict) and b.get("type") == "tool_result"), default=-1)
    last_marker = max((i for i, b in enumerate(content) if _marker(_texts([b]))), default=-1)
    return plan(_texts(content), last_result > last_marker, body.get("tools"))


def anthropic_sse(body: dict, p) -> list[bytes]:
    n = next(ids)
    out = [("message_start", {"type": "message_start", "message": {
        "id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model", "fake"),
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})]
    if p[0] == "text":
        out += [("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})]
        out += [("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": t}})
                for t in _pieces(p)]
        out += [("content_block_stop", {"type": "content_block_stop", "index": 0})]
        stop = "end_turn"
    else:
        out += [("content_block_start", {"type": "content_block_start", "index": 0,
                                         "content_block": {"type": "tool_use", "id": f"toolu_hacp{n}", "name": p[1], "input": {}}}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                         "delta": {"type": "input_json_delta", "partial_json": json.dumps(p[2])}}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0})]
        stop = "tool_use"
    out += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"})]
    return [f"event: {e}\ndata: {json.dumps(d)}\n\n".encode() for e, d in out]


def anthropic_json(body: dict, p) -> dict:
    n = next(ids)
    block = {"type": "text", "text": p[1]} if p[0] == "text" else \
        {"type": "tool_use", "id": f"toolu_hacp{n}", "name": p[1], "input": p[2]}
    return {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model", "fake"),
            "content": [block], "stop_reason": "end_turn" if p[0] == "text" else "tool_use", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}}


# ---- OpenAI Responses ------------------------------------------------------------------------

def responses_plan(body: dict):
    items = body.get("input") or []
    if isinstance(items, str):
        return plan([items], False, body.get("tools"))
    last = items[-1] if items else {}
    after = last.get("type") in ("function_call_output", "custom_tool_call_output", "local_shell_call_output")
    user = [i for i in items if i.get("type", "message") == "message" and i.get("role") == "user"]
    return plan(_texts(user[-1].get("content")) if user else [], after, body.get("tools"))


def responses_sse(body: dict, p) -> list[bytes]:
    n = next(ids)
    resp = {"id": f"resp_{n}", "object": "response", "created_at": 0, "status": "in_progress",
            "model": body.get("model", "fake"), "output": []}
    if p[0] == "text":
        item = {"id": f"msg_{n}", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": p[1], "annotations": []}]}
        events = [("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "content": []}}),
                  ("response.content_part.added", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                                   "part": {"type": "output_text", "text": "", "annotations": []}})]
        events += [("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": t})
                   for t in _pieces(p)]
        events += [("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": p[1]}),
                   ("response.content_part.done", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                                   "part": item["content"][0]})]
    else:
        item = {"id": f"fc_{n}", "type": "function_call", "status": "completed", "call_id": f"call_hacp{n}",
                "name": p[1], "arguments": json.dumps(p[2])}
        events = [("response.output_item.added", {"output_index": 0, "item": {**item, "status": "in_progress", "arguments": ""}}),
                  ("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": 0, "delta": item["arguments"]}),
                  ("response.function_call_arguments.done", {"item_id": item["id"], "output_index": 0, "arguments": item["arguments"]})]
    events.append(("response.output_item.done", {"output_index": 0, "item": item}))
    done = {**resp, "status": "completed", "output": [item], "usage": {
        "input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 5, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 15}}
    events = [("response.created", {"response": resp}), ("response.in_progress", {"response": resp})] + events + \
             [("response.completed", {"response": done})]
    return [f"event: {e}\ndata: {json.dumps({'type': e, 'sequence_number': i, **d})}\n\n".encode()
            for i, (e, d) in enumerate(events)]


# ---- server ----------------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log = None  # file-like for request logging, set by serve()
    dump = None  # directory that receives every request body, set by serve()

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, chunks: list[bytes], slow: float = 0.0) -> None:
        """`slow`: seconds before the first byte, then chunks 0.1s apart (a slow model)."""
        time.sleep(slow)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for c in chunks:
            self.wfile.write(c)
            if slow:
                self.wfile.flush()
                time.sleep(0.1)
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        if self.log:
            print(f"GET {self.path}", file=self.log, flush=True)
        path = self.path.split("?")[0]
        self._reply(200, {"object": "list", "data": []} if path.endswith("/models") else {})

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        size = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")
        if self.dump:
            with open(f"{self.dump}/{next(ids):04d}{self.path.split('?')[0].replace('/', '_')}.json", "w") as f:
                json.dump(body, f, indent=1)
        path = self.path.split("?")[0]
        if path.endswith("/messages/count_tokens"):
            return self._reply(200, {"input_tokens": 10})
        if path.endswith("/messages"):
            p, stream, make = anthropic_plan(body), anthropic_sse, anthropic_json
        elif path.endswith("/responses"):
            p, stream, make = responses_plan(body), responses_sse, None
        else:
            if self.log:
                print(f"POST {self.path} (unhandled)", file=self.log, flush=True)
            return self._reply(404, {"error": {"type": "not_found", "message": self.path}})
        if self.log:
            print(f"POST {path} tools={len(body.get('tools') or [])} -> {p[0]} {p[1]}", file=self.log, flush=True)
        if body.get("stream") or make is None:
            return self._stream(stream(body, p), p[2] if p[0] == "text" and len(p) > 2 else 0.0)
        return self._reply(200, make(body, p))


def serve(port: int = 0, log=None, dump: str | None = None) -> ThreadingHTTPServer:
    """Start the fake provider on 127.0.0.1:<port> (0: any free port) in a daemon thread; `dump`
    names a directory that receives every request body."""
    Handler.log, Handler.dump = log, dump
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _selfcheck() -> None:
    claude_tools = [{"name": "Bash", "input_schema": {"properties": {"command": {"type": "string"}, "description": {"type": "string"}},
                                                      "required": ["command", "description"]}}]
    def claude(*content):  # Claude ends its messages with a system note
        return {"messages": [{"role": "user", "content": list(content)}, {"role": "system", "content": "note"}], "tools": claude_tools}
    result = {"type": "tool_result", "tool_use_id": "t", "content": "rejected"}
    assert anthropic_plan(claude({"type": "text", "text": "HACP-RUN touch a"})) == \
        ("tool", "Bash", {"command": "touch a", "description": "hacp test"})
    assert anthropic_plan(claude({"type": "text", "text": "HACP-RUN touch a"}, result)) == ("text", "HACP-RAN")
    # after a rejection Claude puts the next prompt into the rejected result's message
    assert anthropic_plan(claude(result, {"type": "text", "text": "HACP-RUN touch b"}))[0] == "tool"
    assert anthropic_plan({"messages": [{"role": "user", "content": "HACP-SAY hi"}], "tools": []}) == ("text", "hi")
    # a side request (a title) quoting the prompt, with no shell tool: plain text, never a tool call
    assert anthropic_plan({"messages": [{"role": "user", "content": "<session>HACP-RUN touch a</session>"}]}) == ("text", "ok")
    omp = [{"name": "_bash", "input_schema": {"properties": {"i": {"type": "string"}, "command": {"type": "string"}},
                                             "required": ["i", "command"]}}]
    assert plan(["HACP-RUN ls"], False, omp) == ("tool", "_bash", {"command": "ls", "i": "hacp test"})
    codex = [{"type": "function", "name": "exec_command", "parameters": {"properties": {
        "cmd": {"type": "string"}, "sandbox_permissions": {"type": "string"}, "justification": {"type": "string"}}, "required": ["cmd"]}}]
    assert responses_plan({"input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "HACP-RUN ls"}]}],
                           "tools": codex}) == ("tool", "exec_command", {"cmd": "ls", "sandbox_permissions": "require_escalated",
                                                                          "justification": "hacp test needs to write a file"})
    assert responses_plan({"input": [{"type": "function_call_output", "call_id": "c", "output": "x"}], "tools": codex}) == ("text", "HACP-RAN")
    # a slow model, for the agent's own request only; a side request (no shell tool) stays fast
    assert plan(["HACP-SLOW 2 one two"], False, omp) == ("text", "one two", 2.0)
    assert plan(["HACP-SLOW 2 one two"], False, []) == ("text", "one two")
    assert _pieces(("text", "one two", 2.0)) == ["one", " two"] and _pieces(("text", "one two")) == ["one two"]
    print("fakellm ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
        sys.exit()
    s = serve(int(sys.argv[1]) if len(sys.argv) > 1 else 0, sys.stderr, sys.argv[2] if len(sys.argv) > 2 else None)
    print(f"fake provider on http://127.0.0.1:{s.server_address[1]}", flush=True)
    threading.Event().wait()
