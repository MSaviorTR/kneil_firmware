"""Data sources. Each one reads in its own thread and puts items into the queue given to it; the window takes
them out with a timer (it never blocks). Items:
  ('msg', message, t, line)    a protocol line (t = session time when it arrived, line = the text as received)
  ('truth', code, t, line)     a truth key from a replayed session
  ('info', text) / ('error', text)
  ('closed', reason)           'error' (port lost) or 'end' (replay finished); not sent when the user closes
Each source has now(): its session clock in seconds (the PC's monotonic clock, or the replay time)."""
from __future__ import annotations

import os
import queue
import threading
import time
from typing import List, NamedTuple

try:                    # pyserial: only needed for a real COM port
    import serial
    import serial.tools.list_ports
except ImportError:     # replay, demo and the unit tests run without it
    serial = None

SerialException = getattr(serial, 'SerialException', OSError)

from . import protocol as P
from .recording import TRUTH_LINE, read_session


class TransportError(Exception):
    """Opening failed; the text is meant for the user."""


class PortInfo(NamedTuple):
    device: str
    description: str
    is_bridge: bool


def list_ports() -> List[PortInfo]:
    if serial is None:
        return []
    ports = []
    for p in serial.tools.list_ports.comports():
        bridge = p.vid == P.BRIDGE_USB_VID and p.pid == P.BRIDGE_USB_PID
        ports.append(PortInfo(p.device, 'KNEIL bridge' if bridge else (p.description or ''), bridge))
    return sorted(ports, key=lambda p: (not p.is_bridge, p.device))


def _open_error(port: str, exc: Exception) -> str:
    text = str(exc)
    if 'PermissionError' in text or 'Access is denied' in text:
        return (f'{port} is busy: another program has it open (a terminal, another viewer, '
                f'or a test script). Close it and try again.')
    if 'FileNotFoundError' in text or 'cannot find' in text or 'could not open port' in text:
        return f'{port} was not found. Is the bridge plugged in? Press "Refresh" to update the port list.'
    if 'semaphore timeout' in text:
        return (f'{port} does not answer. If it is a Bluetooth serial port with the same number, '
                f'give the bridge another COM number in Device Manager.')
    return f'{port} cannot be opened: {text}'


class SerialTransport:
    """Text lines from a COM port (the XIAO bridge, or a board's own serial port)."""

    def __init__(self, port: str, out: queue.Queue):
        self.port = port
        self.out = out
        self._tx: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self._ser = None

    @staticmethod
    def now() -> float:
        return time.monotonic()

    def open(self) -> None:
        if serial is None:
            raise TransportError('pyserial is not installed: pip install pyserial (only needed for a COM port)')
        try:
            # pyserial sets DTR when it opens the port: the bridge only sends while DTR is on
            self._ser = serial.Serial(self.port, P.BAUD_RATE, timeout=0.1, write_timeout=1.0)
        except (SerialException, OSError, ValueError) as exc:
            raise TransportError(_open_error(self.port, exc)) from None
        self._thread = threading.Thread(target=self._run, name='serial-reader', daemon=True)
        self._thread.start()

    def send(self, line: str) -> None:
        self._tx.put(line)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self._ser is not None:
            try:
                self._ser.close()
            except (SerialException, OSError):
                pass

    def _run(self) -> None:
        buf = bytearray()
        try:
            while not self._stop.is_set():
                while not self._tx.empty():
                    self._ser.write((self._tx.get_nowait() + '\n').encode('ascii'))
                chunk = self._ser.read(max(1, self._ser.in_waiting))
                if not chunk:
                    continue
                t = time.monotonic()
                buf += chunk
                while True:
                    i = buf.find(b'\n')
                    if i < 0:
                        break
                    line = buf[:i].decode('ascii', errors='replace').rstrip('\r')
                    del buf[:i + 1]
                    msg = P.parse_line(line)
                    if msg is not None:
                        self.out.put(('msg', msg, t, line))
                if len(buf) > 4096:             # no line end for a long time: not our device
                    self.out.put(('error', f'{self.port}: data without line ends, discarded'))
                    buf.clear()
        except (SerialException, OSError) as exc:
            if not self._stop.is_set():
                self.out.put(('error', f'{self.port} stopped working ({exc}). Was the bridge unplugged?'))
                self.out.put(('closed', 'error'))


class DemoTransport:
    """Simulated bridge and KNEIL device (no hardware). Same lines and commands as the real ones."""

    def __init__(self, out: queue.Queue):
        from .demo import DemoDevice
        self.out = out
        self.device = DemoDevice()
        self._tx: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread = None
        self.port = 'demo'

    @staticmethod
    def now() -> float:
        return time.monotonic()

    def open(self) -> None:
        self._thread = threading.Thread(target=self._run, name='demo', daemon=True)
        self._thread.start()

    def send(self, line: str) -> None:
        self._tx.put(line)

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        self.device.reset(time.monotonic())
        while not self._stop.is_set():
            while not self._tx.empty():
                self.device.command(self._tx.get_nowait(), time.monotonic())
            t = time.monotonic()
            for line in self.device.poll(t):
                msg = P.parse_line(line)        # through the real parser, like a real port
                if msg is not None:
                    self.out.put(('msg', msg, t, line.rstrip('\n')))
            time.sleep(0.05)


class ReplayTransport:
    """Plays a recorded session file (…_session.txt) at 1x or faster, with its truth keys."""

    def __init__(self, path: str, out: queue.Queue, speed: float = 1.0):
        self.path = path
        self.out = out
        self.speed = speed
        self.port = os.path.basename(path)
        self.entries = []
        self._stop = threading.Event()
        self._thread = None
        self._v0 = 0.0          # session time at the start of the replay
        self._m0 = None         # monotonic time at the start of the replay

    def now(self) -> float:
        if self._m0 is None:
            return self._v0
        return self._v0 + (time.monotonic() - self._m0) * self.speed

    def open(self) -> None:
        try:
            self.entries = read_session(self.path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            raise TransportError(f'{self.port} cannot be replayed: {exc}') from None
        self._v0 = self.entries[0][0]
        self._m0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, name='replay', daemon=True)
        self._thread.start()

    def send(self, line: str) -> None:
        pass                    # nothing to command in a recording

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        for t, line in self.entries:
            while not self._stop.is_set() and self.now() < t:
                time.sleep(min(0.05, max(0.001, (t - self.now()) / self.speed)))
            if self._stop.is_set():
                return
            if line.startswith(TRUTH_LINE):
                try:
                    self.out.put(('truth', int(line[len(TRUTH_LINE):]), t, line))
                except ValueError:
                    pass
                continue
            msg = P.parse_line(line)
            if msg is not None:
                self.out.put(('msg', msg, t, line))
        self.out.put(('info', f'replay of {self.port} finished'))
        self.out.put(('closed', 'end'))
