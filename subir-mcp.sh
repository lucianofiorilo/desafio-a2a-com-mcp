#!/usr/bin/env sh
# Sobe o servidor MCP na porta 7301. Exige REQUEST_STATE_SECRET no ambiente.
set -e
cd "$(dirname "$0")"
if [ -z "$REQUEST_STATE_SECRET" ]; then
  echo 'Defina REQUEST_STATE_SECRET antes: export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")' >&2
  exit 2
fi
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python.exe; fi
exec "$PY" servidor-mcp/servidor.py
