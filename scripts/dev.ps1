# Atalhos para Windows/PowerShell (equivalente ao Makefile).
# Uso: .\scripts\dev.ps1 <tarefa>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("install", "up", "down", "logs", "test", "test-integration", "lint", "format")]
    [string]$Task
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

switch ($Task) {
    "install"          { python -m pip install -r requirements-dev.txt }
    "up"               { docker compose up --build }
    "down"             { docker compose down }
    "logs"             { docker compose logs -f }
    "test"             { pytest }
    "test-integration" { pytest -m integration }
    "lint"             { ruff check .; ruff format --check . }
    "format"           { ruff check --fix .; ruff format . }
}
