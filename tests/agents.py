"""Live end-to-end suite: the installed Claude Code, Codex and OMP (and a bare shell), each in a pane
of a private Herdr server, talking to tests/fakellm.py instead of a model provider. herdr-acp drives
every pane over ACP the way a client does. No login, no tokens; the user's own Herdr session,
agent configs, MCP servers and skills are not touched (each agent gets its own HOME and config).

Usage: .venv/bin/python tests/agents.py [claude] [codex] [omp] [shell] [-k <scenario>] [-v]
Exit status 0 when every selected scenario passed. A failure prints the pane's screen and keeps the
work directory (fake-provider request log included) for a look.

Agents update daily: run this after an update. A scenario failing here means herdr-acp no longer
reads that agent right (dialog shape, transcript format, turn end), not that the fake is wrong:
the fake reads tool names and argument schemas from each request.
"""

import argparse
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import fakellm  # noqa: E402
from herdr_acp.reader import parse_dialog  # noqa: E402

TURN_LIMIT = 60.0  # a turn that hasn't ended by then failed (a hang), whatever the reason
HERDR_VARS = ("HERDR_SOCKET_PATH", "HERDR_SESSION", "HERDR_ENV", "HERDR_PANE_ID", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID")


class Failed(AssertionError):
    pass


def check(cond, msg):
    if not cond:
        raise Failed(msg)


# ---- the private Herdr server ----------------------------------------------------------------

class Herdr:
    """`herdr --session <name> server`, headless; every CLI call here targets it."""

    def __init__(self, name: str):
        self.name = name
        self.env = {k: v for k, v in os.environ.items() if k not in HERDR_VARS}
        self.proc = subprocess.Popen(["herdr", "--session", name, "server"], env=self.env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.socket = None
        for line in self.proc.stdout:  # "api socket: <path>" once it listens
            if line.startswith("api socket:"):
                self.socket = line.split(":", 1)[1].strip()
                break
        check(self.socket, f"herdr test server didn't start (session {name})")
        self.env["HERDR_SOCKET_PATH"] = self.socket
        threading.Thread(target=lambda: self.proc.stdout.read(), daemon=True).start()

    def cli(self, *args: str) -> dict | str:
        r = subprocess.run(["herdr", *args], env=self.env, capture_output=True, text=True, timeout=60)
        out = r.stdout.strip()
        if r.returncode:
            raise Failed(f"herdr {' '.join(args[:3])}: {(r.stderr or out).strip()}")
        try:
            return json.loads(out)["result"]
        except (ValueError, KeyError):
            return out

    def screen(self, pane: str) -> str:
        try:
            return self.cli("pane", "read", pane, "--source", "visible")
        except Failed as e:
            return str(e)

    def stop(self) -> None:
        for args in (["session", "stop", self.name], ["session", "delete", self.name]):
            subprocess.run(["herdr", *args], env=self.env, capture_output=True, timeout=30)
        self.proc.terminate()


# ---- an ACP client over herdr-acp's stdio ----------------------------------------------------

class Client:
    """One herdr-acp process on one pane: one shared session for every scenario of that pane."""

    def __init__(self, herdr: Herdr, pane: str, log: Path):
        env = {**herdr.env, "PYTHONUNBUFFERED": "1"}
        self.proc = subprocess.Popen([sys.executable, "-m", "herdr_acp.main", "--pane", pane, "-v"], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open(log, "w"), text=True,
                                     cwd=Path(__file__).resolve().parent.parent / "src")
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.next_id, self.updates = 1, []
        self.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        self.session = self.call("session/new", {"cwd": "/tmp", "mcpServers": []})["sessionId"]

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))
        self.lines.put(None)

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", **msg}) + "\n")
        self.proc.stdin.flush()

    def _handle(self, m: dict, answer) -> None:
        if m.get("method") == "session/update":
            self.updates.append(m["params"]["update"])
        elif m.get("method") == "session/request_permission":
            self.asked.append(m["params"])
            self._send({"id": m["id"], "result": {"outcome": answer(m["params"]["options"])}})

    def call(self, method: str, params: dict, answer=None, limit: float = TURN_LIMIT) -> dict:
        i, self.next_id, self.asked = self.next_id, self.next_id + 1, []
        self._send({"id": i, "method": method, "params": params})
        deadline = time.monotonic() + limit
        while True:
            try:
                m = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise Failed(f"{method}: no answer within {limit:.0f}s")
            check(m is not None, f"{method}: herdr-acp exited")
            if m.get("id") == i and "method" not in m:
                check("error" not in m, f"{method}: {m.get('error')}")
                return m["result"]
            self._handle(m, answer or (lambda opts: {"outcome": "cancelled"}))

    def prompt(self, text: str, answer=None) -> tuple[str, list, float]:
        """(stop reason, updates during the turn, seconds)."""
        start, t = len(self.updates), time.monotonic()
        r = self.call("session/prompt", {"sessionId": self.session, "prompt": [{"type": "text", "text": text}]}, answer)
        return r["stopReason"], self.updates[start:], time.monotonic() - t

    def wait_for(self, pred, limit: float = TURN_LIMIT) -> list:
        """Updates streamed outside any prompt, until `pred(updates)` holds."""
        start, deadline = len(self.updates), time.monotonic() + limit
        while not pred(self.updates[start:]):
            try:
                m = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise Failed(f"nothing matching within {limit:.0f}s; got {said(self.updates[start:])}")
            check(m is not None, "herdr-acp exited")
            self._handle(m, lambda opts: {"outcome": "cancelled"})
        return self.updates[start:]

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def said(ups: list, kind: str = "agent_message_chunk") -> list[str]:
    return [u["content"]["text"].strip() for u in ups if u.get("sessionUpdate") == kind]


def pick(kind: str):
    """A permission answer: the first option of `kind`, or `cancelled` (the client dismisses it)."""
    def answer(options):
        if kind == "cancelled":
            return {"outcome": "cancelled"}
        chosen = next((o for o in options if o["kind"] == kind), None)
        check(chosen, f"no {kind} option among {[(o['name'], o['kind']) for o in options]}")
        return {"outcome": "selected", "optionId": chosen["optionId"]}
    return answer


# ---- the agents under test -------------------------------------------------------------------

def setup_claude(root: Path, url: str) -> tuple[dict, list]:
    key = "hacp-fake-anthropic-key-for-the-local-fake-provider"  # not a key; any string works against the fake
    (root / "cfg").mkdir()
    (root / "cfg" / ".claude.json").write_text(json.dumps({  # no onboarding, key approved, folder trusted
        "hasCompletedOnboarding": True, "lastOnboardingVersion": "99.0.0", "autoUpdates": False,
        "customApiKeyResponses": {"approved": [key[-20:]], "rejected": []},
        "projects": {str(root / "work"): {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}}}))
    return ({"CLAUDE_CONFIG_DIR": str(root / "cfg"), "ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": key,
             "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            ["--permission-mode", "default"])


def setup_codex(root: Path, url: str) -> tuple[dict, list]:
    (root / "codex").mkdir()
    (root / "codex" / "config.toml").write_text(f"""model = "hacp-fake"
model_provider = "hacp"
check_for_update_on_startup = false

[model_providers.hacp]
name = "hacp fake"
base_url = "{url}/v1"
env_key = "HACP_FAKE_KEY"
wire_api = "responses"

[projects."{root / 'work'}"]
trust_level = "trusted"
""")
    return {"CODEX_HOME": str(root / "codex"), "HACP_FAKE_KEY": "fake"}, \
        ["--ask-for-approval", "on-request", "--sandbox", "read-only"]


def setup_omp(root: Path, url: str) -> tuple[dict, list]:
    (root / "omp").mkdir()
    (root / "omp" / "config.yml").write_text("setupVersion: 2\n")  # no first-run login wizard
    (root / "omp" / "models.yml").write_text(f"""providers:
  hacp:
    baseUrl: {url}
    apiKey: hacp-fake-key
    api: anthropic-messages
    models:
      - id: hacp-fake
        name: HACP fake
        reasoning: false
        input: [text]
        contextWindow: 200000
        maxTokens: 8192
""")
    return {"PI_CODING_AGENT_DIR": str(root / "omp")}, ["--model", "hacp/hacp-fake", "--approval-mode", "always-ask"]


def setup_pi(root: Path, url: str) -> tuple[dict, list]:
    (root / "pi").mkdir()
    (root / "pi" / "models.json").write_text(json.dumps({"providers": {"hacp": {
        "baseUrl": url, "api": "anthropic-messages", "apiKey": "hacp-fake-key",
        "models": [{"id": "hacp-fake", "name": "HACP fake", "reasoning": False, "input": ["text"],
                    "contextWindow": 200000, "maxTokens": 8192}]}}}))
    return {"PI_CODING_AGENT_DIR": str(root / "pi"), "PI_OFFLINE": "1"}, ["--model", "hacp/hacp-fake"]


TMUX = f"hacp-agents-{os.getpid()}"  # the private tmux server codex-tmux runs in

# What runs in each pane. `bin`: the agent binary (skipped when not on PATH). `approvals`: it asks
# before running a tool (Pi never does). `tmux`: started inside a tmux client, the way the Codex
# desktop harness runs it, so herdr-acp has to see through tmux. `slash`: a built-in command that
# starts no turn.
PANES = {
    "claude": {"setup": setup_claude, "bin": "claude", "slash": "/cost"},
    "codex": {"setup": setup_codex, "bin": "codex", "slash": "/status"},
    "omp": {"setup": setup_omp, "bin": "omp", "slash": "/session"},
    "pi": {"setup": setup_pi, "bin": "pi", "approvals": False, "slash": "/session"},
    "codex-tmux": {"setup": setup_codex, "bin": "codex", "tmux": True},
    "shell": {},
}


def available(name: str) -> bool:
    p = PANES[name]
    return all(shutil.which(b) for b in [p.get("bin")] + (["tmux"] if p.get("tmux") else []) if b)


def version(name: str) -> str:
    def run(*argv):
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout.strip().splitlines()[0]
        except (OSError, IndexError, subprocess.TimeoutExpired):
            return "?"
    p = PANES[name]
    if not p:
        return "bash"
    return run(p["bin"], "--version") + (f" in {run('tmux', '-V')}" if p.get("tmux") else "")


# ---- scenarios -------------------------------------------------------------------------------
# Each takes (client, work dir, herdr, pane) and raises Failed; run in order on one pane.

def say(c, work, h, pane):
    stop, ups, _ = c.prompt("HACP-SAY hello from the fake")
    check(stop == "end_turn", f"stop reason {stop}")
    check("hello from the fake" in said(ups), f"reply not streamed: {said(ups)}")
    check(not any("HACP-SAY" in t for t in said(ups, "user_message_chunk")), "the prompt's own echo was streamed")


def run_and(kind: str, expect_file: bool, expect_stop: str):
    def scenario(c, work, h, pane):
        target = work / f"{kind}.txt"
        stop, ups, _ = c.prompt(f"HACP-RUN touch {target}", pick(kind))
        check(len(c.asked) == 1, f"{len(c.asked)} permission requests, expected 1")
        kinds = {o["kind"] for o in c.asked[0]["options"]}
        check({"allow_once", "reject_once"} <= kinds, f"option kinds {kinds}")
        check(stop == expect_stop, f"stop reason {stop}, expected {expect_stop}")
        check(target.exists() == expect_file, f"{target.name} {'missing' if expect_file else 'created'}")
        if expect_file:
            check("HACP-RAN" in said(ups), f"no reply after the tool ran: {said(ups)}")
        no_dialog_left(h, pane)
    return scenario


def no_dialog_left(h, pane, limit: float = 5.0):
    """The turn is over, so its dialog must be closed (the agent takes a moment to redraw)."""
    first = h.screen(pane)
    deadline = time.monotonic() + limit
    while parse_dialog(h.screen(pane)):
        check(time.monotonic() < deadline, f"a dialog is still open {limit:.0f}s after the turn ended; "
                                           f"screen when it ended:\n{first}")
        time.sleep(0.1)


def still_here(c, work, h, pane):
    """After a cancelled turn: the next turn waits for its own end (a late marker of the cancelled
    turn doesn't end it early)."""
    stop, ups, _ = c.prompt("HACP-SAY still here")
    check(stop == "end_turn" and "still here" in said(ups), f"stop {stop}, said {said(ups)}")


def human(c, work, h, pane):
    """Shared session: what a human types at the pane streams to the client, with no prompt."""
    h.cli("agent", "prompt", pane, "HACP-SAY typed at the pane")  # Herdr submits it, as a person at the pane would
    ups = c.wait_for(lambda ups: "typed at the pane" in said(ups))
    check(any("typed at the pane" in t for t in said(ups, "user_message_chunk")), f"human input not streamed: {ups}")


def slash(command: str):
    def scenario(c, work, h, pane):
        """A built-in command starts no turn and writes nothing: it ends on Herdr's stall verdict
        (or, for an agent Herdr doesn't track, the transcript staying silent)."""
        stop, _, _ = c.prompt(command)
        check(stop == "end_turn", f"stop reason {stop}")
    return scenario


def slow(c, work, h, pane):
    """A slow model: nothing for 3s, then the reply word by word. The turn holds until the reply."""
    stop, ups, secs = c.prompt("HACP-SLOW 3 slow words arrive late")
    check(stop == "end_turn" and secs >= 3, f"stop {stop} after {secs:.1f}s")
    check("slow words arrive late" in said(ups), f"reply not streamed: {said(ups)}")


def slow_tool(answer):
    """A tool that runs 3 silent seconds (asked about first, if the agent asks): the turn holds."""
    def scenario(c, work, h, pane):
        target = work / "slow-tool.txt"
        stop, ups, secs = c.prompt(f"HACP-RUN sleep 3 && touch {target}", answer)
        check(stop == "end_turn" and secs >= 3, f"stop {stop} after {secs:.1f}s")
        check(target.exists() and "HACP-RAN" in said(ups), f"tool didn't finish: {said(ups)}")
    return scenario


def run_unasked(c, work, h, pane):
    """An agent without approvals (Pi) runs the tool straight away; nothing is asked."""
    target = work / "unasked.txt"
    stop, ups, _ = c.prompt(f"HACP-RUN touch {target}")
    check(stop == "end_turn" and not c.asked, f"stop {stop}, asked {c.asked}")
    check(target.exists() and "HACP-RAN" in said(ups), f"tool didn't run: {said(ups)}")


def shell_echo(c, work, h, pane):
    stop, ups, _ = c.prompt("echo hacp-shell-out")
    check(stop == "end_turn" and any("hacp-shell-out" in t for t in said(ups)), f"stop {stop}, said {said(ups)}")


def shell_silent(c, work, h, pane):
    stop, _, secs = c.prompt("sleep 3")
    check(stop == "end_turn" and secs >= 3, f"stop {stop} after {secs:.1f}s (a silent command must hold the turn)")


def shell_builtin(c, work, h, pane):
    stop, _, secs = c.prompt(f"cd {work}")
    check(stop == "end_turn", f"stop {stop}")


SHELL_SCENARIOS = [("echo", shell_echo), ("silent", shell_silent), ("builtin", shell_builtin)]


def scenarios(name: str) -> list:
    p = PANES[name]
    if not p:
        return SHELL_SCENARIOS
    out = [("say", say), ("slow", slow)]
    if p.get("approvals", True):
        out += [("allow", run_and("allow_once", True, "end_turn")), ("slow-tool", slow_tool(pick("allow_once"))),
                ("reject", run_and("reject_once", False, "end_turn")),
                ("dismiss", run_and("cancelled", False, "cancelled")), ("after-cancel", still_here)]
    else:
        out += [("run", run_unasked), ("slow-tool", slow_tool(None))]
    if not p.get("tmux"):  # Herdr types for the "human"; it doesn't see an agent behind a tmux client
        out.append(("human", human))
    if p.get("slash"):
        out.append(("slash", slash(p["slash"])))
    return out


# ---- runner ----------------------------------------------------------------------------------

def run_pane(name: str, h: Herdr, url: str, root: Path, only: str | None, results: dict) -> None:
    rows = results[name] = []
    work, home = root / name / "work", root / name / "home"
    work.mkdir(parents=True)
    home.mkdir()
    p = PANES[name]
    env, args = p["setup"](root / name, url) if p else ({}, [])
    env = {"HOME": str(home), **env}
    flags = [x for k, v in env.items() for x in ("--env", f"{k}={v}")]
    pane = None
    try:
        pane = h.cli("workspace", "create", "--cwd", str(work), "--label", name, "--no-focus", *flags)["root_pane"]["pane_id"]
        if p.get("tmux"):
            agent = shlex.join([p["bin"], *args])
            h.cli("pane", "run", pane, shlex.join(["tmux", "-L", TMUX, "-f", "/dev/null", "new-session", "-s", name, agent]))
            h.cli("pane", "wait-output", pane, "--source", "visible", "--match", "Ask Codex", "--timeout", "60000")
        elif p:
            h.cli("agent", "start", f"t{name}", "--kind", p["bin"], "--pane", pane, "--", *args)
        client = Client(h, pane, root / name / "herdr-acp.log")
        results[name + ":herdr"] = h.cli("pane", "get", pane)["pane"].get("agent") or "none"
    except Failed as e:
        rows.append(("start", False, f"{e}\n{h.screen(pane) if pane else ''}"))
        return
    for sname, fn in scenarios(name):
        if only and sname != only:
            continue
        t = time.monotonic()
        try:
            fn(client, work, h, pane)
            rows.append((sname, True, f"{time.monotonic() - t:.1f}s"))
        except Failed as e:
            rows.append((sname, False, f"{e}\n--- pane ---\n{h.screen(pane)}"))
    client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("panes", nargs="*", default=list(PANES))
    ap.add_argument("-k", dest="only", help="run only this scenario")
    ap.add_argument("-v", action="store_true", help="print the fake provider's request log")
    a = ap.parse_args()
    panes = [p for p in a.panes if available(p)]
    skipped = [p for p in a.panes if p not in panes]
    root = Path(tempfile.mkdtemp(prefix="hacp-agents-"))
    log = open(root / "fakellm.log", "w")
    server = fakellm.serve(0, log, None)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    h = Herdr(f"hacp-agents-{os.getpid()}")
    results, t = {}, time.monotonic()
    try:
        threads = [threading.Thread(target=run_pane, args=(p, h, url, root, a.only, results)) for p in panes]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        h.stop()
        if shutil.which("tmux"):
            subprocess.run(["tmux", "-L", TMUX, "kill-server"], capture_output=True)
        server.shutdown()
        log.close()
    ok = all(passed for k, rows in results.items() if not k.endswith(":herdr") for _, passed, _ in rows)
    for p in panes:
        print(f"\n{p}: {version(p)} (Herdr detects: {results.get(p + ':herdr', '?')})")
        for sname, passed, note in results.get(p, []):
            print(f"  {'PASS' if passed else 'FAIL'} {sname:<13} {note if passed else ''}")
            if not passed:
                print("    " + note.replace("\n", "\n    "))
    for p in skipped:
        print(f"\n{p}: SKIP (not on PATH)")
    print(f"\n{'all passed' if ok else 'FAILED'} in {time.monotonic() - t:.0f}s")
    if a.v or not ok:
        print(f"kept {root} (fakellm.log, <agent>/herdr-acp.log)")
    else:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
