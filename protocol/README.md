# KNEIL live protocol (version 2)

How the KNEIL board, the XIAO bridge and the PC app talk to each other. There are two implementations that
**must stay identical**:
- C: `kneil_proto.h/.c` in this folder, used by the KNEIL firmware (`../src/kneil_live.c`) and the bridge
  (`../bridge/src/main.c`); unit test `test_kneil_proto.c`.
- Python: `../pc_app/kneil/protocol.py`; unit tests in `../pc_app/tests/`. Both tests use the same example packets.

Units: acceleration in **mg** (int16). Device frame: x anterior, y subject's left, z superior. Multi-byte values
are **little-endian**. `t_ms` = milliseconds since the device booted (uint32).

## Version 2 (17 Nov 2026, step E)
The model is 8 classes with a 4 s window (V4). Changes against version 1, which stays supported:
- **LIVE v2 is 22 bytes** (version byte = 2). Bytes 0-19 as v1, but byte 8 is the class **after the stairs rule**
  (0-7) and byte 10 the vote share of the **raw** class; byte 20 = raw class (argmax before the rule, 255 = none);
  byte 21 = event flags: bit0 sit-to-stand counted in that window, bit1 stand-to-sit, bit2 bed transfer, bits 4-5
  posture (0 unknown, 1 sitting, 2 standing). The decoder accepts a 20-byte v1 packet too.
- **SUMMARY packet** (`type 4`, 41 bytes): `uint8 type` + the 40-byte daily summary, format 2 (minutes per class,
  the still minutes split by posture, event counts, mean sit-to-stand time, steps, cadences, seated tilt). New
  characteristic `a8f50006-...` (notify); needs ATT MTU >= 44. Sent when the central subscribes, every 60 s while
  connected, and on CONTROL `0x13`.
- **Text lines**: the `L` line gains two values at the end for v2 messages (`<raw_class>,<event_flags>`); the new
  `D,<format>,...` line carries the summary. Commands `Q` (`0x13`, send the summary now) and `Z` (`0x14`, reset the
  daily metrics) join `S`, `P`, `T`.
- Classes in v2: 0 walking, 1 stairs_up, 2 stairs_down, 3 still, 4 lying, 5 sit_to_stand, 6 stand_to_sit,
  7 bed_transfer. The class table, the window length (4 s) and the window centre (2 s before `t_ms`) follow the LIVE
  version byte; v1 keeps 9 classes, a 2 s window and a 1 s centre.

## A. Bluetooth LE (binary)
Advertised name `KNEIL-xxxx` (last 4 hex digits of the device address). The 128-bit service UUID is in the scan
response.

| UUID | name | properties |
|---|---|---|
| `a8f50001-3e2c-4c6e-9b1a-6b4e45494c00` | KNEIL live service | |
| `a8f50002-3e2c-4c6e-9b1a-6b4e45494c00` | LIVE | notify |
| `a8f50003-3e2c-4c6e-9b1a-6b4e45494c00` | RAW | notify |
| `a8f50004-3e2c-4c6e-9b1a-6b4e45494c00` | CONTROL | write |
| `a8f50005-3e2c-4c6e-9b1a-6b4e45494c00` | INFO | notify (optional, added 16 Nov 2026) |

**LIVE** (20 bytes, once per second, one classified 2 s window):

| offset | type | field |
|---|---|---|
| 0 | uint8 | type = 0x01 |
| 1 | uint8 | protocol version = 1 |
| 2 | uint16 | sequence number (wraps) |
| 4 | uint32 | t_ms at the end of the window |
| 8 | uint8 | class (raw, 0-8 or 255 = no result yet) |
| 9 | uint8 | class after the majority-of-3 smoothing (0-8 or 255); it describes the previous window |
| 10 | uint8 | vote share of the winning class in % (forest only; 255 = not available) |
| 11 | uint8 | flags: bit0 self-test passed, bit1 input low-pass on, bit2 small model (single tree) |
| 12 | int16 x3 | mean x, y, z of the window (mg) |
| 18 | uint16 | battery in mV (0 = not measured) |

**RAW** (every 0.5 s while raw streaming is on): `uint8 type = 0x02`, `uint8 n`, `uint16 sequence`,
`uint32 t_ms of the first sample`, then n samples of `int16 x, y, z` (mg) at 50 Hz. Normally n = 25 (158 bytes,
needs ATT MTU >= 161); with a smaller MTU, n = floor((MTU - 3 - 8) / 6). Maximum n = 39 (MTU 247).

**INFO**: `uint8 type = 0x03`, then 1-100 characters of printable ASCII (no line end). Information lines (firmware,
model, self-test, sensor state, reset cause, drop counts). The device sends a summary when the central subscribes.

**CONTROL** writes (1 byte): `0x10` start raw streaming, `0x11` stop raw streaming, `0x12` run the self-test now
(the result comes as an INFO line, and in the flags of the next LIVE packets).

## B. Text lines (USB serial of the bridge; 115200 baud, 8N1)
One line per message, comma-separated, ending with `\n`. Numbers are decimal.
- `L,<seq>,<t_ms>,<class>,<class_smoothed>,<vote_pct>,<flags>,<mean_x_mg>,<mean_y_mg>,<mean_z_mg>,<battery_mV>`
- `R,<seq>,<t_ms_first>,<n>,x1,y1,z1,...,xn,yn,zn`
- `I,<free text>`: the text is everything after the first comma (it may contain commas)
- Commands from the PC: `S` start raw, `P` stop raw, `T` self-test (each on its own line)

Example: LIVE bytes `01 01 01 02 40 E2 01 00 00 04 55 03 F4 FF 22 00 DB 03 00 00` =
`L,513,123456,0,4,85,3,-12,34,987,0`; RAW bytes `02 02 07 00 E8 03 00 00 01 00 FE FF E8 03 00 80 FF 7F 00 00` =
`R,7,1000,2,1,-2,1000,-32768,32767,0`; INFO `03 'selftest PASS'` = `I,selftest PASS`.

## C. Bridge lines (`B,...`)
- PC -> bridge: `B,SCAN` (5 s), `B,CONNECT,<address>`, `B,CONNECT,AUTO` (strongest `KNEIL-*`), `B,DISCONNECT`
- bridge -> PC: `B,VERSION,<text>`, `B,FOUND,<name>,<address>,<rssi_dBm>`, `B,SCAN_DONE`,
  `B,STATE,<idle|scanning|connecting|connected|lost>,<name>,<address>,<rssi_dBm>,<mtu>` (on every change and
  every 2 s while connected; unknown fields empty or 0), `B,DROPS,<count>`

Details of the bridge's behaviour: [../bridge/README.md](../bridge/README.md).

## Changing the protocol
Change the C and the Python code together, update this file and both unit tests, and keep older parts working
(new characteristics optional, new lines ignored by older parsers). The LIVE version byte must change if the
LIVE layout changes.
