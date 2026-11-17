# Firmware sources (es4l15ba1 board, nRF54L15)

This folder holds **two firmware programs** for the same board, built from the same Zephyr application
(`../CMakeLists.txt`), and the sources of the `kneil_tinyml` library.

| Build | Main file | Config | Purpose |
|---|---|---|---|
| NeiL app (default) | `main.c` | `../prj.conf` (+ `../debug.conf` for RTT logs) | original NeiL BLE sensor firmware |
| KNEIL live app | `kneil_live.c` | `../prj.conf` + `../kneil.conf` | on-chip activity recognition, live results over BLE |

`kneil.conf` sets `CONFIG_NEIL_APP_KNEIL_LIVE=y`; `../CMakeLists.txt` then compiles `kneil_live.c`,
`bma580_fifo.c` and `../protocol/kneil_proto.c` instead of `main.c` and its drivers. Build commands:
[../docs/BUILD.md](../docs/BUILD.md).

## Files

| File | Program | What it does |
|---|---|---|
| `main.c` | NeiL | BLE peripheral "NeiL V3": a 128-bit sensor service with one notify characteristic (20-byte packet: TMP118 temperature at bytes 3-4, BMA580 raw x/y/z at bytes 14-19, every 100 ms). Runs 20 s, then arms the external RTC alarm that drives a load switch which cuts the board's power (the board restarts when the RTC switches it on again). |
| `bma580.c/.h` | NeiL | BMA580 driver: soft reset (waits for CHIP_ID after the reset), wake-up, INT2 as output, 50 Hz, +/-8 g, single-sample reads. |
| `rtc.c`, `RTC.h` | NeiL | external RTC (alarm, countdown, time) on the same I2C bus. |
| `TEMP118.c/.h` | NeiL | TMP118 temperature sensor. |
| `kneil_live.c` | KNEIL | the KNEIL live application (below). |
| `bma580_fifo.c/.h` | KNEIL | BMA580 start-up following the datasheet (dummy read, CHIP_ID 0xC4, health check, INT2 output), 50 Hz, +/-8 g, feature engine off, 1024-byte FIFO (x/y/z, 16 bit, header 0xCE + 6 bytes per frame), FIFO read-out and overflow detection. |
| `kneil_features.c`, `kneil_classifier.c`, `kneil_metrics.c`, `kneil_selftest.c` | library | the `kneil_tinyml` library, built by the Zephyr module (`../zephyr/`), see [../docs/kneil_tinyml.md](../docs/kneil_tinyml.md). Not listed in `../CMakeLists.txt` on purpose. |

## KNEIL live app (`kneil_live.c`)

**Boot:** prints the I lines (firmware, reset cause, model, RAM), runs the library self-test (27 vectors) and,
in self-test builds, a timing test of `kneil_classifier_push()` with the DWT cycle counter (60 windows played from
the test vectors), then starts Bluetooth and two threads.

**Threads** (no dynamic memory, nothing processed in interrupts):
- `kneil_acq` (priority 5): every 0.5 s reads the BMA580 FIFO (no interrupt wire), maps the samples to the
  device frame (`axis_map`: identity, checked on 16 Nov 2026), converts to mg and g, pushes each sample to the
  classifier (2 s window, one result per second), builds LIVE packets and, while raw streaming is on, RAW packets
  of 25 samples. A FIFO overflow or read error restarts the classifier (non-contiguous data is never glued).
  It also writes the I lines for Bluetooth events and runs the self-test when asked.
- `kneil_ble` (priority 8): sends queued packets as notifications: LIVE first, then INFO, then one RAW. If the
  link is too slow, RAW packets are dropped and counted, LIVE packets are not.

**Per window:** raw class; smoothed class with the library's majority-of-3 rule (the middle window changes only
if both neighbours agree on another class; it refers to the previous window, 1 s delay); vote share = share of
the 20 forest trees voting for the class (`kneil_model_predict_proba()` from the generated header); flags
(self-test passed, input low-pass on, small model); mean x/y/z of the window from the classifier's features.

**Bluetooth:** advertises `KNEIL-xxxx` (last 4 hex digits of the identity address; the 128-bit service UUID is in
the scan response), service `a8f50001-...` with LIVE, RAW, CONTROL and INFO, see
[../protocol/README.md](../protocol/README.md). Accepts ATT MTU 247 and data length extension. On INFO
subscription it sends a summary (firmware, model with node counts, dataset, last reset cause, self-test, timing,
sensor state, MTU and drop counts).

**Robustness:** Bluetooth callbacks only set atomic flags (the Bluetooth RX stack is small; it is 2048 bytes in
`kneil.conf`). `CONFIG_RESET_ON_FATAL_ERROR=y`: a fault restarts the board (the fault report goes to RTT with
minimal logging) and the next INFO summary shows "last reset: software".

**Debugging:** RTT (J-Link) shows the I lines and a copy of every L line. The board has no UART or USB.

## NeiL app notes
- Built by default (no extra config). Power-optimised: no console, no logging; `debug.conf` adds RTT logging.
- The board has no 32.768 kHz crystal: the low-frequency clock uses the internal RC oscillator (`prj.conf`).
- The data the NeiL app sends is raw BMA580 data at +/-8 g (4096 LSB/g) since 16 Nov 2026 (it was +/-2 g before).
