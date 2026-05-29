$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Remove-InWorkspace {
    param([string]$RelativePath)

    $target = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $target)) {
        return
    }

    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $resolvedTarget = (Resolve-Path -LiteralPath $target).Path
    if (-not $resolvedTarget.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove outside workspace: $resolvedTarget"
    }

    Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
}

function Clear-BuildArtifacts {
    Remove-InWorkspace ".venv"
    Remove-InWorkspace "build"
    Remove-InWorkspace ".pytest_cache"
    Remove-InWorkspace "reports"
    Remove-InWorkspace "DirDiffMaster.spec"
    Remove-InWorkspace "dist\reports"

    Get-ChildItem -LiteralPath $Root -Directory -Recurse -Force -Filter "__pycache__" |
        Where-Object { $_.FullName.StartsWith($Root, [System.StringComparison]::OrdinalIgnoreCase) } |
        Remove-Item -Recurse -Force

    if (Test-Path ".\dist") {
        Get-ChildItem ".\dist" -Force |
            Where-Object { $_.Name -ne "DirDiffMaster.exe" } |
            Remove-Item -Recurse -Force
    }
}

try {
    Clear-BuildArtifacts

    python -m venv .venv

    & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
    if (-not (Test-Path ".\assets\app.ico")) {
        throw "Missing icon file: assets\app.ico"
    }

    & ".\.venv\Scripts\pyinstaller.exe" `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "DirDiffMaster" `
        --icon ".\assets\app.ico" `
        --add-data ".\assets;assets" `
        --add-data ".\pyproject.toml;." `
        ".\app.py"

    if (-not (Test-Path ".\dist\DirDiffMaster.exe")) {
        throw "Build did not produce dist\DirDiffMaster.exe"
    }

    Write-Host "Build complete: $Root\dist\DirDiffMaster.exe"
}
finally {
    Clear-BuildArtifacts
}
