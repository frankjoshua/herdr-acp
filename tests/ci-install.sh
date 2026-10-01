#!/usr/bin/env bash
# Install the latest Herdr, Claude Code, Codex, OMP and Pi (plus tmux) on a fresh Ubuntu, for
# tests/agents.py. Official installers only, no logins. Everything lands in ~/.local/bin; under
# GitHub Actions that is added to $GITHUB_PATH for later steps. Safe to re-run (re-runs update).
set -euo pipefail

bin="$HOME/.local/bin"
mkdir -p "$bin"
export PATH="$bin:$HOME/.local/node/bin:$PATH"
sudo=$([ "$(id -u)" = 0 ] || echo sudo)

# Release downloads (GitHub's CDN especially) sometimes answer 504 for a minute; an installer's own
# retries come in quick succession, so run the whole installer again after a pause.
retry() {
  for pause in 20 60 120; do
    "$@" && return 0
    echo "ci-install: '$*' failed; again in ${pause}s" >&2
    sleep "$pause"
  done
  "$@"
}

$sudo apt-get update -qq
$sudo apt-get install -y -qq --no-install-recommends tmux curl ca-certificates xz-utils >/dev/null

# Pi needs Node >= 22.19; take the current LTS from nodejs.org when the system one is older.
if ! node -e 'const [a, b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a == 22 && b >= 19) ? 0 : 1)' 2>/dev/null; then
  arch=$(uname -m | sed 's/x86_64/x64/; s/aarch64/arm64/')
  tarball=$(curl -fsSL https://nodejs.org/dist/latest-v24.x/SHASUMS256.txt | grep -o "node-v[0-9.]*-linux-$arch.tar.xz" | head -n1)
  rm -rf "$HOME/.local/node" && mkdir -p "$HOME/.local/node"
  curl -fsSL "https://nodejs.org/dist/latest-v24.x/$tarball" | tar -xJ --strip-components=1 -C "$HOME/.local/node"
fi

retry bash -o pipefail -c 'curl -fsSL https://herdr.dev/install.sh | sh'                                  # herdr.dev/docs/install
retry bash -o pipefail -c 'curl -fsSL https://claude.ai/install.sh | bash'                                # code.claude.com/docs/en/setup
retry bash -o pipefail -c 'curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_NON_INTERACTIVE=1 sh'  # github.com/openai/codex
retry bash -o pipefail -c 'curl -fsSL https://omp.sh/install | sh -s -- --binary'                         # github.com/can1357/oh-my-pi
retry npm install -g --silent --prefix "$HOME/.local" @earendil-works/pi-coding-agent

if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$bin" >>"$GITHUB_PATH"
  [ -d "$HOME/.local/node/bin" ] && echo "$HOME/.local/node/bin" >>"$GITHUB_PATH"
fi

tmux -V
for tool in node herdr claude codex omp pi; do
  printf '%-6s %s\n' "$tool" "$("$tool" --version 2>&1 | head -n1)"
done
