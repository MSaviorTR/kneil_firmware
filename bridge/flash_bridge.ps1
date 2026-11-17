# Flash the KNEIL bridge firmware (build\bridge\zephyr\zephyr.uf2) to the Seeed XIAO nRF52840.
# Usage (PowerShell, in the bridge folder):   .\flash_bridge.ps1
# 1. If the XIAO drive (XIAO-SENSE) is already there, the file is copied at once.
# 2. Otherwise the running bridge is put into its bootloader with the "1200 baud touch".
# 3. If that is not possible (other firmware, or no bridge found), double-tap RESET on the XIAO.
param([string]$Uf2 = "$PSScriptRoot\build\bridge\zephyr\zephyr.uf2")

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $Uf2)) { throw "Firmware not found: $Uf2 (build it first)" }

function Find-XiaoDrive {
    Get-Volume | Where-Object { $_.DriveType -eq 'Removable' -and $_.FileSystemLabel -match '^XIAO' } |
        Select-Object -First 1
}

$drive = Find-XiaoDrive
if (-not $drive) {
    $dev = Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_2FE3&PID_0100&MI_00' } |
        Select-Object -First 1
    if ($dev -and $dev.Name -match '\((COM\d+)\)') {
        $port = $Matches[1]
        Write-Host "Bridge found on ${port}: 1200 baud touch"
        try {
            $sp = New-Object System.IO.Ports.SerialPort $port, 1200
            $sp.DtrEnable = $true
            $sp.Open(); Start-Sleep -Milliseconds 300
            # closing the port triggers the restart; the device may already be gone, which is fine
            try { $sp.DtrEnable = $false; $sp.Close() } catch { }
        } catch {
            Write-Host "Could not open ${port}: $($_.Exception.Message)"
            Write-Host "If another device uses the same COM number (for example a Bluetooth link), give the bridge a free number in Device Manager."
        }
    } else {
        Write-Host 'No running bridge found: double-tap RESET on the XIAO now.'
    }
    for ($i = 0; $i -lt 60 -and -not $drive; $i++) { Start-Sleep -Milliseconds 500; $drive = Find-XiaoDrive }
    if (-not $drive) { throw 'The XIAO drive did not appear. Double-tap RESET on the XIAO and run this script again.' }
}

Write-Host "Copying $(Split-Path $Uf2 -Leaf) to $($drive.DriveLetter): ($($drive.FileSystemLabel))"
# The XIAO restarts as soon as the file is written, so Windows may report an error at the end of the copy.
try { Copy-Item $Uf2 "$($drive.DriveLetter):\" } catch { }
Start-Sleep -Seconds 3
$dev = Get-CimInstance Win32_PnPEntity | Where-Object { $_.DeviceID -match 'VID_2FE3&PID_0100&MI_00' } | Select-Object -First 1
if ($dev) { Write-Host "Done: $($dev.Name)" } else { Write-Host 'Copied. The bridge did not appear yet: check Device Manager.' }
