# Bootstrap do ambiente de desenvolvimento do SAI.
#
# Faz tudo que NÃO depende de credenciais:
#   1. venv + dependências Python
#   2. dependências do frontend
#   3. Postgres via Docker + extensão pgvector
#   4. migrations
#   5. .env criado a partir do .env.example, com APP_SECRET_KEY já gerada
#   6. diagnóstico do que ainda falta preencher
#
# Idempotente: pode rodar quantas vezes quiser.
#
# Uso:  .\scripts\bootstrap.ps1

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg)       { Write-Host "    OK  $msg" -ForegroundColor Green }
function Warn($msg)     { Write-Host "    !   $msg" -ForegroundColor Yellow }

Write-Host "`n=== Bootstrap do SAI ===" -ForegroundColor White

# --- 1. Python -------------------------------------------------------------
Step 1 "Ambiente Python"
if (-not (Test-Path ".venv")) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { throw "Python não encontrado. Instale o Python 3.12+ e rode de novo." }
    python -m venv .venv
    Ok "venv criado"
} else {
    Ok "venv já existe"
}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
Ok "dependências instaladas"

# --- 2. Frontend -----------------------------------------------------------
Step 2 "Frontend"
if (Get-Command npm -ErrorAction SilentlyContinue) {
    Push-Location frontend
    npm install --no-audit --no-fund --silent
    Pop-Location
    Ok "dependências do frontend instaladas"
} else {
    Warn "npm não encontrado — o frontend não será preparado (instale o Node.js 20+)"
}

# --- 3. Postgres -----------------------------------------------------------
Step 3 "Postgres (Docker)"
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker compose up -d postgres | Out-Null

    Write-Host "    aguardando o banco aceitar conexões..." -NoNewline
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        docker exec automacaoinfra-postgres-1 pg_isready -U sai *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 2
    }
    Write-Host ""
    if (-not $ready) { throw "Postgres não ficou pronto a tempo." }
    Ok "Postgres no ar"

    docker exec automacaoinfra-postgres-1 psql -U sai -d sai -c "CREATE EXTENSION IF NOT EXISTS vector;" *> $null
    Ok "extensão pgvector habilitada"
} else {
    Warn "Docker não encontrado — suba um Postgres 16 com pgvector manualmente e ajuste DATABASE_URL"
}

# --- 4. .env ---------------------------------------------------------------
Step 4 "Arquivo .env"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    # A chave de assinatura de sessão não é um segredo de terceiros: dá para
    # gerar aqui e poupar um passo manual.
    $secret = & ".\.venv\Scripts\python.exe" -c "import secrets; print(secrets.token_urlsafe(48))"
    (Get-Content ".env") -replace '^APP_SECRET_KEY=$', "APP_SECRET_KEY=$secret" |
        Set-Content ".env" -Encoding utf8
    Ok ".env criado com APP_SECRET_KEY gerada"
} else {
    Ok ".env já existe (mantido como está)"
}

# --- 5. Migrations ---------------------------------------------------------
Step 5 "Migrations"
$env:DATABASE_URL = ((Get-Content ".env" | Select-String '^DATABASE_URL=') -split '=', 2)[1]
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = "postgresql+asyncpg://sai:sai@localhost:5432/sai" }
& ".\.venv\Scripts\python.exe" -m alembic upgrade head
Ok "schema aplicado"

# --- 6. Diagnóstico --------------------------------------------------------
Step 6 "O que ainda falta"
& ".\.venv\Scripts\python.exe" "scripts\check_setup.py"

Write-Host "`nPróximo passo: preencha as credenciais no .env e rode de novo:" -ForegroundColor White
Write-Host "  .\.venv\Scripts\python.exe scripts\check_setup.py --connect`n" -ForegroundColor White
