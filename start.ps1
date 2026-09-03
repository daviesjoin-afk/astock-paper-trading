[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8600,
    [switch]$Local,
    [switch]$Docker,
    [switch]$SkipInstall,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

function Test-Executable {
    param([Parameter(Mandatory)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Open-Dashboard {
    if (-not $NoBrowser) {
        Start-Process ("http://localhost:{0}/" -f $Port) | Out-Null
    }
}

function Wait-ForHttp {
    param(
        [Parameter(Mandatory)][string]$Url,
        [System.Diagnostics.Process]$Process
    )
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        if ($null -ne $Process -and $Process.HasExited) {
            throw "The service process exited before the dashboard became ready."
        }
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
                return $true
            }
        } catch {
            # The server may still be starting; retry below.
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Warning "The service did not pass its health check yet. Open http://localhost:$Port/ manually in a moment."
    return $false
}

function Start-Docker {
    if ($Port -ne 8600) {
        if ($Docker) {
            throw "The Compose configuration uses port 8600. Use -Port 8600 for Docker mode."
        }
        return $false
    }
    if (-not (Test-Executable "docker")) {
        if ($Docker) { throw "Docker was not found. Install Docker Desktop and try again." }
        return $false
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($Docker) { throw "Docker Compose is unavailable. Make sure Docker Desktop is running." }
        return $false
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($Docker) { throw "The Docker daemon is not running. Start Docker Desktop and try again." }
        return $false
    }
    Write-Host "Building and starting the Docker service..."
    & docker compose up -d --build
    if ($LASTEXITCODE -ne 0) {
        if ($Docker) { throw "Docker Compose failed to start." }
        Write-Warning "Docker startup failed; falling back to local mode."
        return $false
    }
    [void](Wait-ForHttp -Url "http://localhost:$Port/api/health" -Process $null)
    Open-Dashboard
    Write-Host "Dashboard is running at http://localhost:$Port/"
    Write-Host "Stop the Docker service with: docker compose down"
    return $true
}

if (-not $Local -and (Start-Docker)) {
    exit 0
}

$pythonCommand = $null
$pythonArguments = @()
$pyLauncher = Get-Command "py" -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    $pythonCommand = $pyLauncher.Source
    $pythonArguments = @("-3")
} elseif (Test-Executable "python") {
    $pythonCommand = (Get-Command "python").Source
} elseif (Test-Executable "python3") {
    $pythonCommand = (Get-Command "python3").Source
}
if ($null -eq $pythonCommand) {
    throw "Python 3 was not found. Install Python 3.11 or newer and try again."
}

$venv = Join-Path $root ".venv"
if (-not (Test-Path -LiteralPath (Join-Path $venv "Scripts/python.exe"))) {
    Write-Host "Creating the local Python virtual environment..."
    & $pythonCommand @pythonArguments -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the virtual environment." }
}
$venvPython = Join-Path $venv "Scripts/python.exe"
$requirements = Join-Path $root "requirements.txt"
$stamp = Join-Path $venv ".requirements.sha256"
if (-not $SkipInstall) {
    $requirementsHash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
    $installedHash = if (Test-Path -LiteralPath $stamp) { (Get-Content -LiteralPath $stamp -Raw).Trim() } else { "" }
    if ($requirementsHash -ne $installedHash) {
        Write-Host "Installing or updating Python dependencies..."
        & $venvPython -m pip install -r $requirements
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
        Set-Content -LiteralPath $stamp -Value $requirementsHash -NoNewline
    }
}

Write-Host "Starting the local service..."
$server = Start-Process -FilePath $venvPython -ArgumentList @(
    "-m", "uvicorn", "backend.main:app", "--host", "localhost",
    "--port", $Port, "--workers", "1"
) -WorkingDirectory $root -NoNewWindow -PassThru
try {
    [void](Wait-ForHttp -Url "http://localhost:$Port/api/health" -Process $server)
    Open-Dashboard
    Write-Host "Dashboard is running at http://localhost:$Port/"
    Write-Host "Press Ctrl+C or close this window to stop the service."
    Wait-Process -Id $server.Id
} finally {
    if ($null -ne $server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id
    }
}
