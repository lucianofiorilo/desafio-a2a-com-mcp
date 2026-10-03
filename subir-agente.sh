#!/usr/bin/env sh
# Sobe o agente A2A na porta 7300. Espera o servidor MCP em http://127.0.0.1:7301/mcp.
set -e
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=.venv/Scripts/python.exe; fi
exec "$PY" agente/agente.py
