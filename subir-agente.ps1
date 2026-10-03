# Sobe o agente A2A na porta 7300. Espera o servidor MCP em http://127.0.0.1:7301/mcp.
$ErrorActionPreference = "Stop"
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\agente\agente.py"
