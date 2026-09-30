# Builds a standalone Windows executable: dist\Auto-Scapture.exe
#   Usage:  .\build_exe.ps1                      (uses "python" on PATH)
#           .\build_exe.ps1 -Python path\to\python.exe
param([string]$Python = "python")

# The app version lives in autocapture.py (APP_VERSION); version.txt must match it.
$appVersion = [regex]::Match((Get-Content autocapture.py -Raw), 'APP_VERSION = "([0-9.]+)"').Groups[1].Value
$versionTxt = Get-Content version.txt -Raw
$parts = $appVersion.Split(".")
$tuple = "($($parts[0]), $($parts[1]), $($parts[2]), 0)"
if (-not $appVersion -or
    -not $versionTxt.Contains("u'FileVersion', u'$appVersion'") -or
    -not $versionTxt.Contains("u'ProductVersion', u'$appVersion'") -or
    -not $versionTxt.Contains("filevers=$tuple") -or
    -not $versionTxt.Contains("prodvers=$tuple")) {
    Write-Error "Version mismatch: autocapture.py says '$appVersion' but version.txt differs. Update version.txt first."
    exit 1
}
Write-Host "Building Auto-Scapture v$appVersion"

& $Python -m pip install -r requirements.txt pyinstaller
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Auto-Scapture `
    --version-file version.txt `
    --icon assets\icon.ico `
    --add-data "assets\icon.png;assets" `
    --exclude-module tkinter `
    autocapture.py
