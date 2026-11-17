# KNEIL firmware: on-implant activity recognition with a live viewer

KNEIL is a research prototype of a sensing **tibial insert (knee implant)**, part of an engineering PhD project.
An accelerometer on a small custom board recognises, **on the chip**, what the person is doing (walking,
stairs, sitting, standing, lying, transitions) once per second, and sends the result over Bluetooth LE.
A small USB bridge and a PC app show it live and measure how often it is right.

```
 KNEIL board (es4l15ba1, nRF54L15)                XIAO nRF52840 bridge          PC (Windows)
 BMA580 accelerometer 50 Hz, +/-8 g   --BLE-->    BLE central, USB serial  -->  KNEIL live viewer
 kneil_tinyml classifier (model V4)               (text lines L/R/I/D/B)        (PySide6 app / .exe)
```

The same repository also holds the original **NeiL** firmware for the same board (a simple BLE sensor app with
RTC-controlled power cycling), from which the KNEIL work started.

## Repository map

| Path | What it is | README |
|---|---|---|
| `src/`, `CMakeLists.txt`, `Kconfig`, `*.conf` | Zephyr application with **two firmware programs** for the es4l15ba1 board: the NeiL BLE sensor app (`main.c`) and the **KNEIL live** app (`kneil_live.c`), chosen at build time | [src/README.md](src/README.md) |
| `include/`, `src/kneil_*.c`, `zephyr/`, `tests/`, `tools/` | **kneil_tinyml** library: features, tree-ensemble model (generated), classifier, daily metrics, self-test (also a Zephyr module) | [docs/kneil_tinyml.md](docs/kneil_tinyml.md) |
| `protocol/` | **KNEIL live protocol** (BLE packets and text lines), C code shared by the firmware and the bridge, with a unit test | [protocol/README.md](protocol/README.md) |
| `bridge/` | Firmware of the **XIAO nRF52840 BLE bridge** (separate Zephyr app) | [bridge/README.md](bridge/README.md) |
| `pc_app/` | **KNEIL live viewer**: Python/PySide6 PC app (live view, ground-truth keys, accuracy, recording, replay), builds to `KNEIL_viewer.exe` | [pc_app/README.md](pc_app/README.md) |
| `boards/` | Board definition of the custom **es4l15ba1** module (nRF54L15) | [boards/README.md](boards/README.md) |
| `docs/` | How to build and flash everything; library documentation | [docs/BUILD.md](docs/BUILD.md) |

## Status (17 Nov 2026)

| Part | State |
|---|---|
| kneil_tinyml 2.0 + model V4 | host test PASS (both models, 16 vectors); 8 classes, 4 s window from the model header, stairs rule, R1 events, 40-byte summary |
| Timing on nRF54L15 (128 MHz) | one classified window: mean 0.60 ms, max 1.0 ms (DWT cycle counter, 60 windows); a sample without classification: 1.4 us |
| BMA580 | 50 Hz, +/-8 g, 1024-byte FIFO read every 0.5 s (no interrupt wire); axis check done: sensor axes = device axes |
| KNEIL live firmware | advertises `KNEIL-xxxx`, LIVE v2 every 1 s, RAW every 0.5 s, CONTROL, INFO, SUMMARY; MTU 247; resets itself after a fault |
| Bridge | scan, connect (address or AUTO), reconnect after a link loss, LED states, B lines, 1200-baud-touch flashing |
| PC viewer | live view, timeline, totals, ground-truth keys, confusion table, daily-summary panel (Q/Z), CSV recording with the v2 fields, replay of v1 and v2 sessions; `KNEIL_viewer.exe` |
| Not done | accuracy tests with real movements (next step), z-axis offset calibration (about -45 mg), battery measurement, direct BLE from the PC (optional) |

Flash/RAM of the KNEIL live build (model V4, measured): 386,664 B of 1,428 KB flash (forest) and 51,632 B of 188 KB
RAM; single-tree build 238,856 B / 51,632 B. With the V3 model it was 409,668 B / 48,708 B.

## Activity classes

| index | name | notes |
|---|---|---|
| 0 | walking | |
| 1 | stairs_up | weak on real people (about 40 %) |
| 2 | stairs_down | weak on real people (about 53 %) |
| 3 | sitting | sitting and standing look alike at the tibia: report them together as "upright and still" (92-100 %) |
| 4 | standing | |
| 5 | lying | seated knee extensions can look like lying |
| 6 | sit_to_stand | |
| 7 | stand_to_sit | |
| 8 | bed_transfer | rolling, sit-up at the bed edge, lie-down |

255 = no result yet. Device frame: **x anterior, y = subject's left, z superior** (standing still reads about
(0, 0, +1000) mg). Model V3 was trained on simulated implant-site data plus real implant-site data of 265 people,
validated by 5-fold cross-validation by person (walking recognised 74-100 % on real people).

## Quick start
Full commands: [docs/BUILD.md](docs/BUILD.md).
1. Toolchain: nRF Connect SDK **v3.1.0** (VS Code with the nRF Connect extension, or `west`).
2. KNEIL live firmware: `west build -d build_kneil -b es4l15ba1/nrf54l15/cpuapp . -- -DBOARD_ROOT=. -DEXTRA_CONF_FILE=kneil.conf`, then `west flash -d build_kneil`.
3. Bridge: `west build -d bridge/build -b xiao_ble bridge`, then `bridge\flash_bridge.ps1`.
4. PC: `pc_app\dist\KNEIL_viewer\KNEIL_viewer.exe` after `pc_app\build_exe.ps1`, or `python pc_app\kneil_viewer.py`.

## Notes for contributors and AI assistants
Read this before changing code.
- **Two firmware programs, one Zephyr app folder.** `CONFIG_NEIL_APP_KNEIL_LIVE=y` (set in `kneil.conf`) builds
  `src/kneil_live.c`; otherwise `src/main.c` (NeiL app) is built. The app folder is also the `kneil_tinyml`
  Zephyr module (`zephyr/module.yml`, added with `ZEPHYR_EXTRA_MODULES` in `CMakeLists.txt`).
- **Never edit generated files:** `include/kneil_model_forest.h`, `include/kneil_model_tree.h`,
  `include/kneil_model_info.h`, `tests/kneil_test_vectors.h` (made by the training pipeline, emlearn).
  The model sizes in the INFO lines are counted from them by `CMakeLists.txt`.
- **Existing drivers are considered stable** (`src/bma580.c`, `src/rtc.c`, `src/TEMP118.c`): the owner wants to be
  asked before they are changed.
- **The protocol has two implementations that must stay identical:** `protocol/kneil_proto.c` (firmware and bridge)
  and `pc_app/kneil/protocol.py` (PC). Change both, update [protocol/README.md](protocol/README.md), and run both
  unit tests (`protocol/test_kneil_proto.c`, `pc_app/tests`). Optional additions must not break older parts.
- **Tests use the real data path:** board -> BLE -> XIAO bridge -> COM port -> PC. RTT (J-Link) is only for
  debugging; the board has no UART or USB.
- **Firmware rules:** no dynamic memory; nothing processed in interrupts; 50 Hz sampling must never stall;
  Bluetooth callbacks only set flags or copy data (the Bluetooth RX stack is small: a formatted log line in a
  callback once overflowed it and halted the board); BLE and USB work in their own lower-priority threads.
- **BMA580 start-up quirk:** after power-on or a soft reset the first I2C accesses are not acknowledged (the
  datasheet requires a dummy read; on this board two accesses fail). Read CHIP_ID (0xC4) until it answers. Both
  `bma580.c` and `bma580_fifo.c` do this.
- **Environment used so far:** Windows 11, NCS v3.1.0 (toolchain bundle b8b84efebd), J-Link on an nRF52 DK
  (PCA10040) as the programmer of the es4l15ba1 board, Python 3.13, WinLibs gcc for host tests.
- The user writes short, plain English: keep explanations short and simple.

## History
The KNEIL live work followed these steps: (1) library module and on-board self-test, (2) timing with the DWT
cycle counter, (3) BMA580 FIFO and axis check, (4) text stream (replaced by the bridge: the board has no USB),
(5) Bluetooth LE service and INFO packet, (6) vote share from the 20 trees; then the XIAO bridge and the PC
viewer (parsers, demo mode, live view, ground truth, accuracy, recording, replay, Windows .exe).
Step E (17 Nov 2026) moved to model V4: library 2.0, 8 classes, 4 s window, stairs rule, transition events, posture,
protocol v2 (LIVE 22 bytes, SUMMARY characteristic) and the PC app support for both protocol versions.
