"""KNEIL live protocol, version 1. The only place in the PC app that knows the protocol.

The KNEIL firmware and the XIAO bridge use the same protocol (C code: ../protocol/kneil_proto.h).
Do not change it on one side only.

Text lines (USB serial: the XIAO bridge, or a board's own serial port), one per message, ending with '\\n':
  L,<seq>,<t_ms>,<class>,<class_smoothed>,<vote_pct>,<flags>,<mean_x_mg>,<mean_y_mg>,<mean_z_mg>,<battery_mV>
  R,<seq>,<t_ms_first>,<n>,x1,y1,z1,...,xn,yn,zn         acceleration in mg, 50 Hz
  I,<free text>                                          the text is everything after the first comma
  B,...                                                  lines of the bridge itself (see _parse_bridge)
Commands to the device: S (start raw), P (stop raw), T (self-test); to the bridge: B,SCAN, B,CONNECT,<address|AUTO>,
B,DISCONNECT.

Binary BLE packets (for a direct BLE link; the bridge converts them to the lines above), little-endian:
  LIVE  type 1, 20 bytes; RAW  type 2, 8 + 6n bytes; INFO  type 3, then 1-100 characters of text.

Device frame: x anterior, y subject's left, z superior (standing still: about (0, 0, +1000) mg).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional, Tuple, Union

PROTOCOL_VERSION = 1
BAUD_RATE = 115200
FS_HZ = 50
SAMPLE_MS = 1000 // FS_HZ

# BLE (direct link)
UUID_SERVICE = 'a8f50001-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_LIVE = 'a8f50002-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_RAW = 'a8f50003-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_CONTROL = 'a8f50004-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_INFO = 'a8f50005-3e2c-4c6e-9b1a-6b4e45494c00'
NAME_PREFIX = 'KNEIL-'

# USB id of the XIAO bridge (Zephyr CDC ACM)
BRIDGE_USB_VID = 0x2FE3
BRIDGE_USB_PID = 0x0100

PKT_LIVE, PKT_RAW, PKT_INFO = 0x01, 0x02, 0x03
LIVE_LEN = 20
RAW_HDR_LEN = 8
RAW_N_NORMAL = 25
RAW_N_MAX = 39
INFO_TEXT_MAX = 100

CLASS_NONE = 255
VOTE_NONE = 255
FLAG_SELFTEST_OK = 0x01
FLAG_LOWPASS = 0x02
FLAG_SMALL_MODEL = 0x04

CMD_RAW_START = 'S'
CMD_RAW_STOP = 'P'
CMD_SELFTEST = 'T'
CONTROL_BYTES = {CMD_RAW_START: 0x10, CMD_RAW_STOP: 0x11, CMD_SELFTEST: 0x12}
CMD_SCAN = 'B,SCAN'
CMD_DISCONNECT = 'B,DISCONNECT'


def cmd_connect(address: str = 'AUTO') -> str:
    """B,CONNECT,<address> or B,CONNECT,AUTO (the strongest KNEIL-* device)."""
    return 'B,CONNECT,' + address


# Classes: index = classifier output
CLASS_NAMES = ('walking', 'stairs_up', 'stairs_down', 'sitting', 'standing', 'lying',
               'sit_to_stand', 'stand_to_sit', 'bed_transfer')
CLASS_LABELS = ('walking', 'stairs up', 'stairs down', 'sitting', 'standing', 'lying',
                'sit-to-stand', 'stand-to-sit', 'bed transfer')
CLASS_COLOURS = ('#2e9e44',   # walking: green
                 '#ff9a1f',   # stairs up: orange
                 '#c65102',   # stairs down: dark orange
                 '#2166d6',   # sitting: blue
                 '#7fc4ef',   # standing: light blue
                 '#8e44ad',   # lying: purple
                 '#f4d03f',   # sit-to-stand: yellow
                 '#c9a227',   # stand-to-sit: gold
                 '#8b5a2b')   # bed transfer: brown
NONE_COLOUR = '#d0d0d0'
N_CLASSES = len(CLASS_NAMES)

# Sitting and standing are hard to tell apart at the tibia: also shown together.
UPRIGHT_AND_STILL = (3, 4)
SIT_STAND_TRANSITIONS = (6, 7)
GROUPS = (('walking', (0,)),
          ('stairs', (1, 2)),
          ('upright and still', (3, 4)),
          ('lying', (5,)),
          ('transitions', (6, 7, 8)))

BRIDGE_STATES = ('idle', 'scanning', 'connecting', 'connected', 'lost')


def class_label(cls: int) -> str:
    return CLASS_LABELS[cls] if 0 <= cls < N_CLASSES else '-'


def class_colour(cls: int) -> str:
    return CLASS_COLOURS[cls] if 0 <= cls < N_CLASSES else NONE_COLOUR


# ------------------------------------------------------------------------------------------------
# Messages
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Live:
    """One classified 2 s window (sent once per second)."""
    seq: int
    t_ms: int                       # end of the window, ms since the device booted
    cls: int                        # raw class 0-8, or CLASS_NONE
    cls_smooth: int                 # after the majority-of-3 smoothing (one window later), or CLASS_NONE
    vote_pct: int                   # vote share of the winning class (%), or VOTE_NONE
    flags: int
    mean_mg: Tuple[int, int, int]   # mean x, y, z of the window
    battery_mv: int                 # 0 = not measured

    @property
    def selftest_ok(self) -> bool:
        return bool(self.flags & FLAG_SELFTEST_OK)

    @property
    def lowpass(self) -> bool:
        return bool(self.flags & FLAG_LOWPASS)

    @property
    def small_model(self) -> bool:
        return bool(self.flags & FLAG_SMALL_MODEL)


@dataclass(frozen=True)
class Raw:
    """Raw acceleration samples (mg, device frame, 50 Hz)."""
    seq: int
    t_ms_first: int
    samples: Tuple[Tuple[int, int, int], ...]

    @property
    def n(self) -> int:
        return len(self.samples)


@dataclass(frozen=True)
class Info:
    text: str


@dataclass(frozen=True)
class BridgeVersion:
    text: str


@dataclass(frozen=True)
class BridgeFound:
    name: str
    address: str
    rssi: int


@dataclass(frozen=True)
class BridgeScanDone:
    pass


@dataclass(frozen=True)
class BridgeState:
    state: str          # one of BRIDGE_STATES
    name: str           # '' if unknown
    address: str        # '' if unknown
    rssi: int           # dBm, 0 if unknown
    mtu: int            # 0 if not connected


@dataclass(frozen=True)
class BridgeDrops:
    count: int          # total lines/packets lost inside the bridge


@dataclass(frozen=True)
class BadLine:
    """A line that does not follow the protocol (shown in the log, otherwise ignored)."""
    line: str
    reason: str


Message = Union[Live, Raw, Info, BridgeVersion, BridgeFound, BridgeScanDone, BridgeState, BridgeDrops, BadLine]


# ------------------------------------------------------------------------------------------------
# Text lines
# ------------------------------------------------------------------------------------------------
def _int(text: str, lo: int, hi: int, what: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise ValueError(f'{what} is not a number: {text!r}') from None
    if not lo <= value <= hi:
        raise ValueError(f'{what} out of range: {value}')
    return value


def _class(text: str, what: str) -> int:
    value = _int(text, 0, 255, what)
    if value >= N_CLASSES and value != CLASS_NONE:
        raise ValueError(f'{what} is not a class: {value}')
    return value


def _parse_live(fields: list) -> Live:
    if len(fields) != 10:
        raise ValueError(f'L line needs 10 values, has {len(fields)}')
    vote = _int(fields[4], 0, 255, 'vote')
    if vote > 100 and vote != VOTE_NONE:
        raise ValueError(f'vote out of range: {vote}')
    return Live(seq=_int(fields[0], 0, 0xFFFF, 'seq'),
                t_ms=_int(fields[1], 0, 0xFFFFFFFF, 't_ms'),
                cls=_class(fields[2], 'class'),
                cls_smooth=_class(fields[3], 'smoothed class'),
                vote_pct=vote,
                flags=_int(fields[5], 0, 255, 'flags'),
                mean_mg=tuple(_int(v, -32768, 32767, 'mean') for v in fields[6:9]),
                battery_mv=_int(fields[9], 0, 0xFFFF, 'battery'))


def _parse_raw(fields: list) -> Raw:
    if len(fields) < 6:
        raise ValueError('R line too short')
    n = _int(fields[2], 1, 1000, 'n')
    if len(fields) != 3 + 3 * n:
        raise ValueError(f'R line with n={n} needs {3 + 3 * n} values, has {len(fields)}')
    values = [_int(v, -32768, 32767, 'sample') for v in fields[3:]]
    samples = tuple((values[3 * i], values[3 * i + 1], values[3 * i + 2]) for i in range(n))
    return Raw(seq=_int(fields[0], 0, 0xFFFF, 'seq'), t_ms_first=_int(fields[1], 0, 0xFFFFFFFF, 't_ms'),
               samples=samples)


def _parse_bridge(rest: str) -> Message:
    kind, _, args = rest.partition(',')
    fields = args.split(',') if args else []
    if kind == 'VERSION':
        return BridgeVersion(args)
    if kind == 'SCAN_DONE':
        return BridgeScanDone()
    if kind == 'FOUND':
        if len(fields) != 3:
            raise ValueError('B,FOUND needs name, address, rssi')
        return BridgeFound(fields[0], fields[1], _int(fields[2], -128, 127, 'rssi'))
    if kind == 'STATE':
        if len(fields) != 5:
            raise ValueError('B,STATE needs state, name, address, rssi, mtu')
        if fields[0] not in BRIDGE_STATES:
            raise ValueError(f'unknown bridge state {fields[0]!r}')
        return BridgeState(fields[0], fields[1], fields[2], _int(fields[3], -128, 127, 'rssi'),
                           _int(fields[4], 0, 65535, 'mtu'))
    if kind == 'DROPS':
        if len(fields) != 1:
            raise ValueError('B,DROPS needs one count')
        return BridgeDrops(_int(fields[0], 0, 2 ** 63, 'drops'))
    raise ValueError(f'unknown bridge line B,{kind}')


def parse_line(line: str) -> Optional[Message]:
    """One received text line -> message. Empty lines -> None. Other lines -> BadLine."""
    line = line.rstrip('\r\n')
    if not line.strip():
        return None
    kind, sep, rest = line.partition(',')
    try:
        if kind == 'I':
            return Info(rest)
        if not sep:
            raise ValueError('no comma')
        if kind == 'L':
            return _parse_live(rest.split(','))
        if kind == 'R':
            return _parse_raw(rest.split(','))
        if kind == 'B':
            return _parse_bridge(rest)
        raise ValueError('unknown line type')
    except ValueError as exc:
        return BadLine(line, str(exc))


def format_live(p: Live) -> str:
    return 'L,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n' % (p.seq, p.t_ms, p.cls, p.cls_smooth, p.vote_pct, p.flags,
                                                 p.mean_mg[0], p.mean_mg[1], p.mean_mg[2], p.battery_mv)


def format_raw(p: Raw) -> str:
    values = ','.join('%d,%d,%d' % s for s in p.samples)
    return 'R,%d,%d,%d,%s\n' % (p.seq, p.t_ms_first, p.n, values)


def format_info(text: str) -> str:
    return 'I,%s\n' % text.replace('\n', ' ').replace('\r', ' ')


# ------------------------------------------------------------------------------------------------
# Binary BLE packets
# ------------------------------------------------------------------------------------------------
_LIVE = struct.Struct('<BBHIBBBBhhhH')


def encode_live(p: Live) -> bytes:
    return _LIVE.pack(PKT_LIVE, PROTOCOL_VERSION, p.seq, p.t_ms, p.cls, p.cls_smooth, p.vote_pct, p.flags,
                      *p.mean_mg, p.battery_mv)


def encode_raw(p: Raw) -> bytes:
    if not 1 <= p.n <= RAW_N_MAX:
        raise ValueError('RAW packet needs 1-%d samples' % RAW_N_MAX)
    return struct.pack('<BBHI', PKT_RAW, p.n, p.seq, p.t_ms_first) + \
        b''.join(struct.pack('<hhh', *s) for s in p.samples)


def encode_info(text: str) -> bytes:
    data = ''.join(c if ' ' <= c <= '~' else '?' for c in text)[:INFO_TEXT_MAX].encode('ascii')
    if not data:
        raise ValueError('INFO needs text')
    return bytes([PKT_INFO]) + data


def decode_packet(data: bytes) -> Union[Live, Raw, Info]:
    """BLE notification (LIVE, RAW or INFO) -> message. Raises ValueError if it is not a valid packet."""
    if not data:
        raise ValueError('empty packet')
    kind = data[0]
    if kind == PKT_LIVE:
        if len(data) != LIVE_LEN or data[1] != PROTOCOL_VERSION:
            raise ValueError('bad LIVE packet')
        f = _LIVE.unpack(data)
        return Live(f[2], f[3], f[4], f[5], f[6], f[7], (f[8], f[9], f[10]), f[11])
    if kind == PKT_RAW:
        if len(data) < RAW_HDR_LEN:
            raise ValueError('bad RAW packet')
        n = data[1]
        if not 1 <= n <= RAW_N_MAX or len(data) != RAW_HDR_LEN + 6 * n:
            raise ValueError('bad RAW packet length')
        _, _, seq, t_ms = struct.unpack_from('<BBHI', data)
        samples = tuple(struct.unpack_from('<hhh', data, RAW_HDR_LEN + 6 * i) for i in range(n))
        return Raw(seq, t_ms, samples)
    if kind == PKT_INFO:
        if not 2 <= len(data) <= 1 + INFO_TEXT_MAX:
            raise ValueError('bad INFO packet')
        return Info(''.join(chr(b) if 0x20 <= b <= 0x7E else '?' for b in data[1:]))
    raise ValueError(f'unknown packet type {kind}')
