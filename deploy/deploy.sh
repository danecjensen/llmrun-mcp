#!/usr/bin/env bash
# Run from your laptop in this repo:  ./deploy/deploy.sh [vm-name]
# Ships the committed code (git HEAD) to the exe.dev VM and (re)starts the server.
set -euo pipefail
VM="${1:-llmrun}"
HOST="$VM.exe.xyz"
cd "$(git rev-parse --show-toplevel)"

echo "-> shipping $(git rev-parse --short HEAD) to $HOST"
git archive --format=tar HEAD | ssh "$HOST" 'mkdir -p ~/llmrun-mcp && tar -x -C ~/llmrun-mcp'
OUT="$(ssh "$HOST" 'bash ~/llmrun-mcp/deploy/setup.sh')"
echo "$OUT" | grep -v '^NEW_TOKEN=' || true

TOKEN="$(echo "$OUT" | sed -n 's/^NEW_TOKEN=//p')"
if [ -n "$TOKEN" ]; then
  cat <<MSG

First deploy: your server token is

  $TOKEN

Save it somewhere safe (it is also in ~/.config/llmrun/env on the VM), then:

  export LLMRUN_TOKEN=$TOKEN
  claude mcp add --transport http --scope user llmrun https://mcp.llmrun.com/mcp \\
    --header "Authorization: Bearer \$LLMRUN_TOKEN"
MSG
fi
