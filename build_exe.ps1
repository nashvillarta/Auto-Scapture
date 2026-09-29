# Builds a standalone Windows executable: dist\Auto-Scapture.exe
#   Usage:  .\build_exe.ps1                      (uses "python" on PATH)
#           .\build_exe.ps1 -Python path\to\python.exe
param([string]$Python = "python")

& $Python -m pip install -r requirements.txt pyinstaller
& $Python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name Auto-Scapture `
    --version-file version.txt `
    --icon assets\icon.ico `
    --add-data "assets\icon.png;assets" `
    --exclude-module tkinter `
    autocapture.py
