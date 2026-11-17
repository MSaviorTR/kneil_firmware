# Building, flashing and testing

All commands are for Windows 11 (paths may differ on your PC). Firmware: nRF Connect SDK **v3.1.0**.

## Toolchain
- VS Code with the **nRF Connect** extension: open the folder, add a build configuration (board, extra Kconfig
  fragment, build folder as below), build and flash from the extension.
- Or a terminal with the NCS toolchain environment (the extension's "nRF Connect terminal", or
  `nrfutil toolchain-manager launch --ncs-version v3.1.0 --shell`), then `west` as below.

## Firmware for the es4l15ba1 board (repository root)
The board definition is in `boards/`, so pass `-DBOARD_ROOT=<absolute path of this folder>`. A relative `.` does not
work with the sysbuild default in NCS 3.1.0 (CMake reports "Invalid BOARD"); use the full path.

| Build | Command (in the repository root) | Folder |
|---|---|---|
| NeiL app | `west build -d build -b es4l15ba1/nrf54l15/cpuapp . -- -DBOARD_ROOT=.` | `build` |
| NeiL app with RTT logs | `west build -d build_debug -b es4l15ba1/nrf54l15/cpuapp . -- -DBOARD_ROOT=. -DEXTRA_CONF_FILE=debug.conf` | `build_debug` |
| **KNEIL live** | `west build -d build_kneil -b es4l15ba1/nrf54l15/cpuapp . -- "-DBOARD_ROOT=<absolute path>" "-DEXTRA_CONF_FILE=kneil.conf"` | `build_kneil` |
| KNEIL live, single tree | the same with `"-DEXTRA_CONF_FILE=kneil.conf;<file with CONFIG_KNEIL_TINYML_MODEL_SMALL=y>"` | `build_kneil_tree` |

Add `--pristine=always` after changing Kconfig files. Flash: `west flash -d build_kneil` (runner nrfutil,
J-Link). The board is programmed through a J-Link (here: the debug-out of an nRF52 DK, PCA10040).

| Config file | Used by | Content |
|---|---|---|
| `prj.conf` | both apps | BLE peripheral, I2C, RC low-frequency clock (no crystal), power settings, no console |
| `debug.conf` | NeiL debug | RTT console, printk, logging |
| `kneil.conf` | KNEIL live | selects `kneil_live.c`; kneil_tinyml (50 Hz, self-test), FPU, MTU 247 and data length extension, Bluetooth RX stack 2048, reset on fatal error, minimal logging, RTT console |
| `boards/es4l15ba1_nrf54l15_cpuapp.overlay` | both | I2C at 100 kHz |

**RTT output** (debugging only): find the RTT block address with
`arm-zephyr-eabi-nm build_kneil/neilv2/zephyr/zephyr.elf | findstr _SEGGER_RTT`, then
`JLinkRTTLogger.exe -Device NRF54L15_M33 -If SWD -Speed 4000 -RTTAddress <address> -RTTChannel 0 rtt.txt`
(or use J-Link RTT Viewer).

**Flash/RAM reports:** `west build -d build_kneil --domain kneil_firmware_agent -t rom_report` (or `ram_report`).
The domain is the application folder name. Measured (17 Nov 2026, model V4): forest 386,664 B flash / 51,632 B RAM;
single tree 238,856 B / 51,632 B (V3 model: 409,668 B / 48,708 B).

## kneil_tinyml host test (PC, needs gcc, e.g. WinLibs MinGW-w64)
```
python tests\run_host_test.py        -> must print: HOST TEST: PASS (both models)
```

## Protocol unit test (PC, gcc)
```
gcc -std=c99 -Wall -Wextra -o test_kneil_proto protocol\test_kneil_proto.c protocol\kneil_proto.c
test_kneil_proto                      -> PROTOCOL TEST: PASS
```

## XIAO nRF52840 bridge (`bridge/`)
```
west build -d bridge/build -b xiao_ble bridge        -> bridge/build/bridge/zephyr/zephyr.uf2
powershell -ExecutionPolicy Bypass -File bridge\flash_bridge.ps1
```
The script puts a running bridge into its UF2 bootloader (1200-baud touch) and copies the file to the
`XIAO-SENSE` drive. First time (other firmware on the XIAO): double-tap RESET, then run the script.

## PC viewer (`pc_app/`)
```
cd pc_app
python -m pip install -r requirements.txt pyinstaller
python -m unittest discover -s tests                -> OK (25 tests)
python kneil_viewer.py [--demo] [--port COM4]
powershell -ExecutionPolicy Bypass -File build_exe.ps1   -> dist\KNEIL_viewer\KNEIL_viewer.exe
```

## Not in the repository
Build folders (`build*/`, `bridge/build/`), `pc_app/dist/` (the .exe), Python caches, recordings, the BMA580
datasheet and the model training data (the training scripts are in `tools/`).
