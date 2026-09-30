"""ACP-layer self-check: drive herdr-acp over stdio against a real pane.
Usage: .venv/bin/python tests/roundtrip.py <pane-id> "<prompt>" [option kind to pick, e.g. allow_once]
(shell pane: try "pwd"). A dialog in the pane arrives as session/request_permission; the first
option of the given kind is chosen (default: the first option)."""
import json, subprocess, sys, time
pane, text, kind = sys.argv[1], sys.argv[2], (sys.argv[3:] or [None])[0]
p = subprocess.Popen([".venv/bin/herdr-acp", "--pane", pane, "-v"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def send(m):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", **m}) + "\n"); p.stdin.flush()
def call(i, method, params):
    send({"id": i, "method": method, "params": params})
    while True:
        line = p.stdout.readline()
        if not line: raise SystemExit("agent died")
        m = json.loads(line)
        if m.get("id") == i and "method" not in m: return m
        if m.get("method") == "session/request_permission":
            opts = m["params"]["options"]
            pick = next((o for o in opts if o["kind"] == kind), opts[0])
            print(f"  ?? {m['params']['toolCall']} {[(o['name'], o['kind']) for o in opts]} -> {pick['name']}")
            send({"id": m["id"], "result": {"outcome": {"outcome": "selected", "optionId": pick["optionId"]}}})
            continue
        u = m.get("params", {}).get("update", {})
        print(f"  << {u.get('sessionUpdate')}: {json.dumps(u)[:200]}")
print(call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}))
sid = call(2, "session/new", {"cwd": "/tmp", "mcpServers": []})["result"]["sessionId"]
t = time.time()
print(call(3, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}), f"{time.time()-t:.1f}s")
p.stdin.close(); p.wait(timeout=5)
