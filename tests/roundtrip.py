"""ACP-layer self-check: drive herdr-acp over stdio against a real pane.
Usage: .venv/bin/python tests/roundtrip.py <pane-id> "<prompt>"  (shell pane: try "pwd")"""
import json, subprocess, sys, time
pane, text = sys.argv[1], sys.argv[2]
p = subprocess.Popen([".venv/bin/herdr-acp", "--pane", pane, "-v"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def call(i, method, params):
    p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}) + "\n"); p.stdin.flush()
    while True:
        line = p.stdout.readline()
        if not line: raise SystemExit("agent died")
        m = json.loads(line)
        if m.get("id") == i: return m
        u = m.get("params", {}).get("update", {})
        print(f"  << {u.get('sessionUpdate')}: {json.dumps(u)[:200]}")
print(call(1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}))
sid = call(2, "session/new", {"cwd": "/tmp", "mcpServers": []})["result"]["sessionId"]
t = time.time()
print(call(3, "session/prompt", {"sessionId": sid, "prompt": [{"type": "text", "text": text}]}), f"{time.time()-t:.1f}s")
p.stdin.close(); p.wait(timeout=5)
