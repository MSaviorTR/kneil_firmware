# KNEIL live viewer (PC app)

Shows in real time what the KNEIL board recognises, and measures how often it is right.
Data path: KNEIL board → Bluetooth LE → XIAO nRF52840 bridge → USB (COM port) → this app.

## Start
- **Program:** double-click `dist\KNEIL_viewer\KNEIL_viewer.exe` (no Python needed; keep the whole
  `KNEIL_viewer` folder together, also when you copy it to another PC). The `dist` folder is not in the
  repository: make it with `.\build_exe.ps1` (see below).
- **From Python** (Python 3.12 or newer, once: `python -m pip install -r requirements.txt`):
  ```
  python kneil_viewer.py              normal start
  python kneil_viewer.py --demo       simulated data, no hardware needed
  python kneil_viewer.py --port COM4  open this port at start
  ```
  The same options work for `KNEIL_viewer.exe`.

## Use
1. Plug in the XIAO bridge. Switch on the KNEIL board.
2. **Source**: XIAO bridge (USB). **Port**: the one called "KNEIL bridge" (chosen automatically). **Open**.
3. The app connects by itself (to the last device, or **AUTO** = the strongest KNEIL-* device). To choose a
   device: **Scan**, pick it in **Device**, **Connect**.
4. Keep **Raw stream** ticked to see the acceleration plot.
5. **Ground truth:** while you test, press the key for what you are really doing:
   `1` walking, `2` stairs up, `3` stairs down, `4` sitting, `5` standing, `6` lying, `7` sit-to-stand,
   `8` stand-to-sit, `9` bed transfer, `0` other. The truth is shown next to the device's answer.
6. **Accuracy** tab: the confusion table (rows = your key, columns = the device) and the accuracy for walking,
   for "upright and still" (sitting and standing together) and overall, updated every second.
   The device classifies the 2 s window that ends at the packet's time, so each window is compared with the key
   you held at the **centre** of that window (the smoothed class: one window earlier still). The PC time of a
   window comes from the device clock, corrected by the least delayed packet.
7. **Start recording** saves three files (default folder: `Documents\KNEIL recordings`, change with **Folder...**):
   - `kneil_<date>_<time>_live.csv`: every LIVE packet (time, classes, vote, flags, means) with the truth key at
     that moment and at the centre of the window;
   - `kneil_<date>_<time>_raw.csv`: every RAW sample (mg) with the truth key;
   - `kneil_<date>_<time>_session.txt`: everything received, with the keys, used for replay.
8. **Replay:** Source = **Replay a recording**, choose the speed, **Choose file...**, pick a `_session.txt`.
   The window, the keys and the accuracy table play again exactly as recorded.

Only one program can open the COM port at a time: close other terminals or test scripts first.
The app remembers the port, the device, the folder and the options. Unexpected errors are shown in a message
box and saved in `%LOCALAPPDATA%\KNEIL\viewer_errors.log`.

## What the window shows
- **Connection**: bridge state, device name and address, signal strength (RSSI), MTU; packets per second,
  packets lost (from the sequence numbers), lines dropped by the bridge; recording status.
- **Current activity**: the smoothed class in large letters (majority of 3 windows, about 2 s behind the
  movement), the raw class of the newest window, the vote share of the 20 trees, and "upright and still" for
  sitting or standing (the two look alike at the tibia); the truth key and whether the device agrees.
- **Live acceleration**: x, y, z and |a| of the last 10 s in g (device frame: x anterior, y left, z superior).
  Option: the same signal after the chip's 6 Hz low-pass (dashed; same filter, tested against the C code).
- **Timeline**: the class of every second over the last 5 minutes (grey = no data).
- Tabs: **Accuracy**, **Session totals** (time per class and group, sit/stand transitions) and **Log** (device
  information such as firmware, model, self-test, sensor and reset cause; bridge events; errors).
  The bars between the areas can be dragged.

## Build the program
```
.\build_exe.ps1          (PowerShell, in this folder; needs: pip install pyinstaller)
```
Result: `dist\KNEIL_viewer\KNEIL_viewer.exe` (about 135 MB with the Qt libraries).

## Tests
```
python -m unittest discover -s tests
```
The protocol tests use the same example packets as the firmware's C test (`../protocol/test_kneil_proto.c`).

## Files
```
kneil_viewer.py        start (also the entry point of the .exe)
kneil/protocol.py      the protocol (only place that knows it): lines, packets, classes, colours
kneil/session.py       state: lost packets, rates, signal, timeline, totals, truth, accuracy (no Qt, unit-tested)
kneil/recording.py     CSV recording and the session file for replay
kneil/transports.py    COM port reader thread, demo and replay sources
kneil/demo.py          simulated bridge and device
kneil/lowpass.py       the chip's 6 Hz low-pass
kneil/ui.py            the window
tests/                 unit tests
build_exe.ps1          builds KNEIL_viewer.exe
```

## Not yet
Direct Bluetooth from the PC without the bridge (optional).
