$ErrorActionPreference = 'Stop'

$toolDirectory = $PSScriptRoot
$projectDirectory = Split-Path -Parent (Split-Path -Parent $toolDirectory)
$venvDirectory = Join-Path $toolDirectory '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
$requirementsFile = Join-Path $toolDirectory 'requirements.txt'
$localUrl = 'http://127.0.0.1:8000'

function Write-Step([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Test-OllamaReady {
    try {
        $null = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
        return $true
    }
    catch {
        return $false
    }
}

Set-Location -LiteralPath $toolDirectory
Write-Host 'LPT Reviewer - One-click local setup' -ForegroundColor Green
Write-Host "Project: $projectDirectory"

if (-not (Get-Command py.exe -ErrorAction SilentlyContinue) -and -not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    throw 'Python 3 was not found. Install Python from https://www.python.org/downloads/ and enable "Add Python to PATH".'
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Step 'Creating the Python virtual environment'
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3 -m venv $venvDirectory
    }
    else {
        & python.exe -m venv $venvDirectory
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python could not create the virtual environment.' }
}

Write-Step 'Checking Python packages'
$previousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $venvPython -c 'import fastapi, uvicorn, youtube_transcript_api' 2>$null
    $packagesReady = ($LASTEXITCODE -eq 0)
}
finally {
    $ErrorActionPreference = $previousErrorActionPreference
}

if (-not $packagesReady) {
    Write-Host 'Installing required packages. This is needed only on the first run.'
    $ErrorActionPreference = 'Continue'
    try {
        & $venvPython -m pip install -r $requirementsFile
        $pipExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($pipExitCode -ne 0) { throw 'Required Python packages could not be installed.' }
}
else {
    Write-Host 'Required Python packages are ready.' -ForegroundColor Green
}

$ollamaCommand = Get-Command ollama.exe -ErrorAction SilentlyContinue
if (-not $ollamaCommand) {
    throw 'Ollama was not found. Install it from https://ollama.com/download and run this launcher again.'
}

if (-not (Test-OllamaReady)) {
    Write-Step 'Starting Ollama'
    Start-Process -FilePath $ollamaCommand.Source -ArgumentList 'serve' -WindowStyle Hidden
    $ollamaStarted = $false
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        Start-Sleep -Seconds 1
        if (Test-OllamaReady) {
            $ollamaStarted = $true
            break
        }
    }
    if (-not $ollamaStarted) { throw 'Ollama did not become ready. Start Ollama manually, then run this launcher again.' }
}

Write-Step 'Checking the qwen3:8b model'
$ollamaTags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 5
$installedModels = @($ollamaTags.models | ForEach-Object { if ($_.name) { $_.name } else { $_.model } })
if ('qwen3:8b' -notin $installedModels) {
    Write-Host 'qwen3:8b is not installed.' -ForegroundColor Yellow
    Write-Host 'For safety, the launcher will not download models automatically.'
    Write-Host 'Run this command once, then launch the reviewer again:' -ForegroundColor Yellow
    Write-Host '  ollama pull qwen3:8b' -ForegroundColor White
    exit 1
}
Write-Host 'Ollama and qwen3:8b are ready.' -ForegroundColor Green

Write-Step 'Starting the LPT Reviewer'
Write-Host "Opening $localUrl"
Write-Host 'Keep this window open. Press Ctrl+C here to stop the reviewer.' -ForegroundColor Yellow

$browserCommand = "Start-Sleep -Seconds 2; Start-Process '$localUrl'"
Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile', '-WindowStyle', 'Hidden', '-Command', $browserCommand -WindowStyle Hidden

$ErrorActionPreference = 'Continue'
& $venvPython -m uvicorn app:app --host 127.0.0.1 --port 8000
$serverExitCode = $LASTEXITCODE
$ErrorActionPreference = $previousErrorActionPreference
if ($serverExitCode -ne 0) { throw "The reviewer server stopped with exit code $serverExitCode." }
