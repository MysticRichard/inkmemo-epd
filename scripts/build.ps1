$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing $python"
}

Push-Location $root
try {
    $caFile = & $python -c "import ssl, os; print(os.path.join(os.path.dirname(ssl.__file__), 'codex-rejected-cas.pem'))"
    $extra = @()
    if (Test-Path -LiteralPath $caFile) {
        $extra += @('--add-data', "$caFile;.")
    }
    # 只打包运行时真正读取的模板，避免把带真实样例的历史设计稿打进产物
    $assets = Join-Path $env:TEMP 'inkmemo-bundle-assets'
    if (Test-Path -LiteralPath $assets) {
        Remove-Item -LiteralPath $assets -Recurse -Force
    }
    foreach ($t in @('standard-v0.11\source.html', 'relaxed-v0.16\source.html', 'compact-v0.13\source24.html')) {
        $dest = Join-Path $assets ('design\approved\' + $t)
        New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
        Copy-Item -LiteralPath (Join-Path $root ('design\approved\' + $t)) -Destination $dest
    }
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --name InkMemo `
        --add-data "$assets\design\approved;design\approved" `
        --collect-all playwright `
        --collect-all pystray `
        --collect-all lunardate `
        --hidden-import winrt.windows.devices.geolocation `
        @extra `
        InkMemo.pyw
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    Write-Output "Built $root\dist\InkMemo\InkMemo.exe"
}
finally {
    Pop-Location
}
