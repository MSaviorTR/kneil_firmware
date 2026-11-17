# Build the Windows program KNEIL_viewer.exe with PyInstaller.
# Usage (PowerShell, in the pc_app folder):   .\build_exe.ps1
# Result: dist\KNEIL_viewer\KNEIL_viewer.exe  (keep the whole KNEIL_viewer folder together; copy the folder
# to another PC to use it there, no Python needed). Needs: pip install -r requirements.txt pyinstaller
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

python -m PyInstaller --noconfirm --clean --windowed --name KNEIL_viewer `
    --exclude-module pyqtgraph.opengl --exclude-module pyqtgraph.examples --exclude-module pyqtgraph.jupyter `
    --exclude-module tkinter `
    kneil_viewer.py
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }

$exe = Join-Path $PSScriptRoot 'dist\KNEIL_viewer\KNEIL_viewer.exe'
$size = (Get-ChildItem (Split-Path $exe) -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("Done: {0}  (folder {1:N0} MB)" -f $exe, $size)
