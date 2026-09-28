# The Makefile targets, for Windows machines without make.
#
#   .\scripts\demo.ps1 install
#   .\scripts\demo.ps1 up
#   .\scripts\demo.ps1 demo -Corpus globex-v1
#   .\scripts\demo.ps1 serve        # then open http://localhost:8000

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('install', 'up', 'down', 'reset', 'test', 'demo', 'serve', 'mutants')]
    [string]$Task,
    [string]$Corpus = 'acme-v1',
    [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$py = '.venv\Scripts\python.exe'
if (-not $env:DOCTASK_DSN) {
    $env:DOCTASK_DSN = 'postgresql://doctask:doctask@localhost:5433/doctask'
}

function Invoke-Checked {
    & $args[0] $args[1..($args.Length - 1)]
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

switch ($Task) {
    'install' {
        Invoke-Checked python -m venv .venv
        Invoke-Checked $py -m pip install -e '.[dev]'
    }
    'up'      { Invoke-Checked docker compose up -d --wait }
    'down'    { Invoke-Checked docker compose down }
    'reset'   { Invoke-Checked $py -m doctask.cli reset }
    'test'    { Invoke-Checked $py -m pytest -q }
    'demo'    { Invoke-Checked $py -m doctask.cli demo --corpus $Corpus }
    'serve'   { Invoke-Checked $py -m uvicorn doctask.asgi:app --port $Port }
    'mutants' { Invoke-Checked $py scripts/mutation_check.py }
}
