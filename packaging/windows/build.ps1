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

# Which Python builds the bundle decides which Python ends up inside it, so
# resolve it explicitly instead of inheriting whatever "python" means today.
if ($env:PYTHON) {
    $py = $env:PYTHON
} elseif ($env:VIRTUAL_ENV -and (Test-Path "$env:VIRTUAL_ENV\Scripts\python.exe")) {
    $py = "$env:VIRTUAL_ENV\Scripts\python.exe"
} elseif (Test-Path "$root\.venv\Scripts\python.exe") {
    $py = "$root\.venv\Scripts\python.exe"
} else {
    $py = "python"
}
Write-Host "building with $py ($(& $py --version))"

& $py -m pip install --upgrade pyinstaller
& $py -m pip install -e .

# The spec refuses to embed an icon it cannot open, so generate them first.
& $py packaging\make_icons.py

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
# -m PyInstaller, never the bare command: a pyinstaller earlier on PATH would
# bundle a different interpreter than the one holding the dependencies.
& $py -m PyInstaller packaging\tjiptemp.spec --noconfirm

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

# The deliverable is a single .exe. Inno Setup makes an installer that unpacks
# the bundle; without it, fall back to PyInstaller's one-file build so there is
# always exactly one .exe in dist\ either way.
$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if ($iscc) {
    & $iscc.Source "packaging\windows\tjiptemp.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
} else {
    Write-Host "Inno Setup (iscc.exe) not found; building a one-file executable instead."
    & $py -m PyInstaller (Join-Path $root "src\tjiptemp\__main__.py") `
        --noconfirm --onefile --windowed --name TjipTemp `
        --icon (Join-Path $root "packaging\icon.ico") `
        --distpath dist --workpath build\onefile
}

$produced = Get-ChildItem dist\*.exe -ErrorAction SilentlyContinue
if (-not $produced) { throw "no .exe was produced in dist\" }
foreach ($f in $produced) {
    Write-Host ("built: {0} ({1:N1} MB)" -f $f.Name, ($f.Length / 1MB))
}
