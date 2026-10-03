# Sobe o servidor MCP na porta 7301. Exige REQUEST_STATE_SECRET no ambiente.
$ErrorActionPreference = "Stop"
if (-not $env:REQUEST_STATE_SECRET) {
    Write-Error 'Defina REQUEST_STATE_SECRET antes: $env:REQUEST_STATE_SECRET = (python -c "import secrets; print(secrets.token_hex(32))")'
}
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\servidor-mcp\servidor.py"
