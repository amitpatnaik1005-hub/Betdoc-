#Requires -Version 5.1
# Run from anywhere. Windows blocks .ps1 files by default, so start it with:
#   powershell -ExecutionPolicy Bypass -File .\backend\setup.ps1
# Safe to re-run: every step skips itself if it's already done.

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

# $ErrorActionPreference doesn't catch failing native commands (python, pip, alembic),
# so check the exit code of each one explicitly
function Invoke-Native {
    param([string]$Description, [scriptblock]$Command)
    Write-Host "==> $Description" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Description failed (exit code $LASTEXITCODE)" }
}

# --- 1. Create virtual environment ---
if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Invoke-Native "Creating virtual environment" { python -m venv venv }
}

# --- 2. Activate it ---
Write-Host "==> Activating virtual environment" -ForegroundColor Cyan
. .\venv\Scripts\Activate.ps1

# Call the venv interpreter by path so nothing silently falls back to the global Python
$Python = Join-Path $PSScriptRoot "venv\Scripts\python.exe"

# --- 3. Install requirements ---
Invoke-Native "Upgrading pip" { & $Python -m pip install --upgrade pip }
Invoke-Native "Installing requirements" { & $Python -m pip install -r requirements.txt }

# --- Create .env with fresh keys on first run ---
if (-not (Test-Path ".env")) {
    Write-Host "==> Creating .env with freshly generated keys" -ForegroundColor Cyan

    $keys = @{}
    $output = & $Python generate_keys.py
    if ($LASTEXITCODE -ne 0) { throw "generate_keys.py failed (exit code $LASTEXITCODE)" }
    foreach ($line in $output) {
        $name, $value = $line -split "=", 2
        $keys[$name.Trim()] = $value.Trim()
    }

    $content = Get-Content ".env.example" -Raw
    $content = $content -replace '(?m)^SECRET_KEY=[^\r\n]*', "SECRET_KEY=$($keys['SECRET_KEY'])"
    $content = $content -replace '(?m)^ENCRYPTION_KEY=[^\r\n]*', "ENCRYPTION_KEY=$($keys['ENCRYPTION_KEY'])"
    $content = $content -replace '(?m)^INGESTION_API_KEY=[^\r\n]*', "INGESTION_API_KEY=$($keys['INGESTION_API_KEY'])"

    # Write UTF-8 WITHOUT a BOM. On PowerShell 5.1, Set-Content -Encoding UTF8 adds a BOM,
    # which corrupts the first key name so pydantic-settings can't find DATABASE_URL.
    [System.IO.File]::WriteAllText(
        (Join-Path $PSScriptRoot ".env"),
        $content,
        (New-Object System.Text.UTF8Encoding $false)
    )

    Write-Warning ".env created. Set the real password in DATABASE_URL, make sure the 'betdoc' database exists in PostgreSQL, then re-run this script."
    exit 0
}

# --- 4. Initialize Alembic (async template), keeping the custom env.py ---
if (-not (Test-Path "alembic.ini")) {
    $customEnv = Join-Path $PSScriptRoot "alembic\env.py"
    $backup = Join-Path $env:TEMP "betdoc_alembic_env.py.bak"

    if (-not (Test-Path $customEnv)) {
        throw "alembic\env.py (async BetDoc version) not found. Add it before running setup."
    }

    # `alembic init` refuses a non-empty folder, so back up env.py, clear the folder, init, restore
    Copy-Item $customEnv $backup -Force

    $existingMigrations = Get-ChildItem "alembic\versions" -Filter "*.py" -ErrorAction SilentlyContinue
    if ($existingMigrations) {
        throw "alembic\versions already contains migrations but alembic.ini is missing. Restore alembic.ini instead of re-initializing."
    }
    Remove-Item "alembic" -Recurse -Force

    Invoke-Native "Initializing Alembic (async template)" { & $Python -m alembic init -t async alembic }

    Copy-Item $backup $customEnv -Force
    Remove-Item $backup -Force
    Write-Host "==> Restored custom async env.py" -ForegroundColor Cyan
}

# --- 5. Generate the first migration (only once) ---
$migrations = Get-ChildItem "alembic\versions" -Filter "*.py" -ErrorAction SilentlyContinue
if (-not $migrations) {
    Invoke-Native "Generating initial migration" {
        & $Python -m alembic revision --autogenerate -m "Initial ledger schema"
    }
} else {
    Write-Host "==> Migrations already exist, skipping autogenerate" -ForegroundColor Yellow
}

# --- 6. Upgrade the database ---
Invoke-Native "Applying migrations" { & $Python -m alembic upgrade head }

# --- 7. Run the server (Ctrl+C to stop) ---
Write-Host "==> Starting BetDoc API at http://127.0.0.1:8000" -ForegroundColor Green
& $Python -m uvicorn app.main:app --reload
