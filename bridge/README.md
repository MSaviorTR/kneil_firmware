# KNEIL BLE bridge (Seeed XIAO nRF52840)

Part of the KNEIL project (overview: [../README.md](../README.md)). The PC does not use its own Bluetooth: this
small USB board is the link between the KNEIL board and the PC viewer (`../pc_app/`). Separate Zephyr application
(nRF Connect SDK v3.1.0, board `xiao_ble`); files: `src/main.c` (all of the bridge), `prj.conf`, `CMakeLists.txt`
(also compiles the shared protocol code `../protocol/kneil_proto.c`), `flash_bridge.ps1`. Current version: 0.3.0.

The XIAO is a BLE central. It connects to a `KNEIL-xxxx` device, subscribes to LIVE, RAW and, when the device has
them, INFO and SUMMARY (`a8f50006-...`), and writes each packet to its USB serial port as the text lines of the
KNEIL live protocol (`L`, `R`, `I`, `D`). LIVE v1 (20 bytes) and v2 (22 bytes, step E) are both accepted, so an
older board still works. Commands `S`, `P`, `T`, `Q`, `Z` from the PC are written to CONTROL (`0x10`, `0x11`, `0x12`,
`0x13`, `0x14`). The bridge adds only its own `B` lines.

SUMMARY (UUID `a8f50006-3e2c-4c6e-9b1a-6b4e45494c00`, notify; added 17 Nov 2026) carries the 40-byte daily summary
(format 2); it is written as a `D,...` line. The bridge subscribes to it when the device has it and forwards it like
any other packet.

INFO (UUID `a8f50005-3e2c-4c6e-9b1a-6b4e45494c00`, notify; added 16 Nov 2026) carries the device's information
lines: a packet is `0x03` followed by 1-100 characters of text, written as `I,<text>`. The device sends a
summary (firmware, model, self-test, sensor, reset cause) when the bridge subscribes. The bridge keeps the
last 16 `I` lines of the current link and sends them again when the PC opens the port, so the PC app sees
them even if it was started later.

The protocol code is in `../protocol/kneil_proto.c` (shared with the KNEIL firmware, one place to change it).

## Build and flash
nRF Connect SDK v3.1.0, board target `xiao_ble` (also works on the Sense version).
```
west build -d build -b xiao_ble .          (in this folder; output: build\bridge\zephyr\zephyr.uf2)
.\flash_bridge.ps1                          (PowerShell)
```
`flash_bridge.ps1` uses the "1200 baud touch" to put a running bridge into its UF2 bootloader, then copies
the file to the `XIAO-SENSE` drive. The first time (other firmware on the XIAO), or if it does not work,
double-tap RESET on the XIAO and run the script again.

Windows shows the bridge as **USB Serial Device (COMx)** (USB ID 2FE3:0100). If that COM number is also
used by another device (for example a Bluetooth serial link), give the bridge a free number in Device
Manager (Port Settings, Advanced, COM Port Number), or the port cannot be opened.

## Commands and lines (115200 baud, lines end with `\n`)
| PC -> bridge | effect |
|---|---|
| `B,SCAN` | scan 5 s: `B,FOUND,<name>,<address>,<rssi_dBm>` once per KNEIL device, then `B,SCAN_DONE` |
| `B,CONNECT,<address>` | connect to that device (address as in `B,FOUND`, e.g. `C0:11:22:33:44:55`) |
| `B,CONNECT,AUTO` | listen 3 s, then connect to the strongest `KNEIL-*` |
| `B,DISCONNECT` | disconnect and stop reconnecting |
| `S` / `P` / `T` | start raw / stop raw / self-test (written to CONTROL while connected) |

| bridge -> PC | when |
|---|---|
| `B,VERSION,<text>` | when the PC opens the port |
| `B,STATE,<idle\|scanning\|connecting\|connected\|lost>,<name>,<address>,<rssi_dBm>,<mtu>` | when the PC opens the port, on every change, every 2 s while connected (RSSI of the link). Unknown fields are empty or 0. |
| `B,DROPS,<count>` | total lines/packets lost inside the bridge (the PC did not read fast enough), at most once per second when it changes |
| `L,...` `R,...` | every LIVE / RAW notification, converted to text |

After a link loss the bridge shows `lost` for 1 s, then `connecting`, and reconnects by itself to the same
device. If the device has no KNEIL service, the bridge disconnects, goes `idle` and the LED is red for 3 s.

LED: blue blinking = scanning or connecting, green = connected, red = link lost or error, off = idle.

## Design
- BLE callbacks only copy packets/events into message queues (never block, never format).
- A writer thread converts packets to lines and puts complete lines into an 8 KB USB buffer; a line
  that does not fit is dropped and counted (`B,DROPS`). A slow PC never blocks the BLE link.
- Lines are sent only while the PC has the port open with DTR on (pyserial does this by default), so
  the PC never receives old lines. On opening, the bridge first sends an empty line (it ends a line that
  was cut off when the port was last closed), then `B,VERSION` and `B,STATE`. The BLE link stays up
  while the port is closed.
- The main thread runs the commands, the connection state machine, the `B` lines and the LED.
- ATT MTU 247 is requested after connecting; data length extension and 2M PHY are requested
  automatically by the Bluetooth stack.

## Example packets (from `../protocol/test_kneil_proto.c`)
- LIVE `01 01 01 02 40 E2 01 00 00 04 55 03 F4 FF 22 00 DB 03 00 00`
  -> `L,513,123456,0,4,85,3,-12,34,987,0`
- RAW `02 02 07 00 E8 03 00 00 01 00 FE FF E8 03 00 80 FF 7F 00 00`
  -> `R,7,1000,2,1,-2,1000,-32768,32767,0`
