# Build the Windows executable and an installer.
#
#     powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
#
# Produces dist\tjiptemp\tjiptemp.exe and, if Inno Setup is installed,
# dist\TjipTemp-Setup-<version>.exe.
#
# Windows 10 and later ship the usbser.sys CDC driver, so there is nothing to
# install for the board itself — it appears as a COM port as soon as it is
# plugged in.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $root

python -m pip install --upgrade pyinstaller
python -m pip install -e .

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
pyinstaller packaging\tjiptemp.spec --noconfirm

if (-not (Test-Path "dist\tjiptemp\tjiptemp.exe")) {
    throw "PyInstaller did not produce dist\tjiptemp\tjiptemp.exe"
}

# Optional: sign the executable. Without this, SmartScreen will warn users on
# first run until the binary builds reputation.
if ($env:SIGN_CERT -and $env:SIGN_PASS) {
    & signtool sign /f $env:SIGN_CERT /p $env:SIGN_PASS `
        /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
        "dist\tjiptemp\tjiptemp.exe"
}

$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source "packaging\windows\tjiptemp.iss"
    Write-Host "installer built in dist\"
} else {
    Write-Host "Inno Setup (iscc.exe) not found; skipping the installer."
    Write-Host "The portable build is in dist\tjiptemp\"
}
