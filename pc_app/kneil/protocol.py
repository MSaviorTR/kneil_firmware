"""KNEIL live protocol, version 2 (with version 1 still accepted). The only place in the PC app that knows the protocol.

The KNEIL firmware and the XIAO bridge use the same protocol (C code: ../protocol/kneil_proto.h).
Do not change it on one side only.

Text lines (USB serial: the XIAO bridge, or a board's own serial port), one per message, ending with '\\n':
  L,<seq>,<t_ms>,<class>,<class_smoothed>,<vote_pct>,<flags>,<mean_x_mg>,<mean_y_mg>,<mean_z_mg>,<battery_mV>[,<raw_class>,<event_flags>]
  R,<seq>,<t_ms_first>,<n>,x1,y1,z1,...,xn,yn,zn         acceleration in mg, 50 Hz
  I,<free text>                                          the text is everything after the first comma
  D,<format>,<n_classes>,<min_0..min_7>,<min_sitting>,<min_standing>,<min_still_unknown>,
    <n_sts>,<n_s2s>,<n_bed>,<sts_x10>,<steps>,<cadence_x10>,<stairs_cadence_x10>,<sit_tilt_x10>
  B,...                                                  lines of the bridge itself (see _parse_bridge)
Commands to the device: S (start raw), P (stop raw), T (self-test), Q (daily summary now), Z (reset the
daily metrics); to the bridge: B,SCAN, B,CONNECT,<address|AUTO>, B,DISCONNECT.

Binary BLE packets (for a direct BLE link; the bridge converts them to the lines above), little-endian:
  LIVE  type 1, version 1 = 20 bytes, version 2 = 22 bytes (two extra bytes: raw class, event flags);
  RAW  type 2, 8 + 6n bytes; INFO  type 3, then 1-100 characters of text;
  SUMMARY  type 4, 41 bytes = type + the 40-byte daily summary (format 2).

Versions: v1 = 9 classes, 2 s window. v2 = 8 classes (sitting + standing -> 'still'), 4 s window, stairs rule,
transition events (event flags per window) and the daily summary. The class table, the window length and the
confusion logic depend on the version; Live.version says which one a packet is.

Device frame: x anterior, y subject's left, z superior (standing still: about (0, 0, +1000) mg).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

PROTOCOL_VERSION = 2             # the version the firmware writes; v1 packets are still decoded
BAUD_RATE = 115200
FS_HZ = 50
SAMPLE_MS = 1000 // FS_HZ
HOP_S = 1.0                      # both versions classify every second

# BLE (direct link)
UUID_SERVICE = 'a8f50001-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_LIVE = 'a8f50002-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_RAW = 'a8f50003-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_CONTROL = 'a8f50004-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_INFO = 'a8f50005-3e2c-4c6e-9b1a-6b4e45494c00'
UUID_SUMMARY = 'a8f50006-3e2c-4c6e-9b1a-6b4e45494c00'
NAME_PREFIX = 'KNEIL-'

# USB id of the XIAO bridge (Zephyr CDC ACM)
BRIDGE_USB_VID = 0x2FE3
BRIDGE_USB_PID = 0x0100

PKT_LIVE, PKT_RAW, PKT_INFO, PKT_SUMMARY = 0x01, 0x02, 0x03, 0x04
LIVE_LEN_V1 = 20
LIVE_LEN_V2 = 22
LIVE_LEN = LIVE_LEN_V2
RAW_HDR_LEN = 8
RAW_N_NORMAL = 25
RAW_N_MAX = 39
INFO_TEXT_MAX = 100
SUMMARY_LEN = 41
SUMMARY_FORMAT = 2

CLASS_NONE = 255
VOTE_NONE = 255
FLAG_SELFTEST_OK = 0x01
FLAG_LOWPASS = 0x02
FLAG_SMALL_MODEL = 0x04

# LIVE v2 event flags (byte 21)
EV_SIT_TO_STAND = 0x01
EV_STAND_TO_SIT = 0x02
EV_BED_TRANSFER = 0x04
EV_POSTURE_SHIFT = 4
EV_POSTURE_MASK = 0x30
POSTURE_NAMES = ('unknown', 'sitting', 'standing')

CMD_RAW_START = 'S'
CMD_RAW_STOP = 'P'
CMD_SELFTEST = 'T'
CMD_SUMMARY = 'Q'
CMD_RESET = 'Z'
CONTROL_BYTES = {CMD_RAW_START: 0x10, CMD_RAW_STOP: 0x11, CMD_SELFTEST: 0x12, CMD_SUMMARY: 0x13, CMD_RESET: 0x14}
CMD_SCAN = 'B,SCAN'
CMD_DISCONNECT = 'B,DISCONNECT'


def cmd_connect(address: str = 'AUTO') -> str:
    """B,CONNECT,<address> or B,CONNECT,AUTO (the strongest KNEIL-* device)."""
    return 'B,CONNECT,' + address


# ------------------------------------------------------------------------------------------------ classes
# v1: 9 classes, 2 s window (kept for recordings and for an old device)
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
# v2: 8 classes, 4 s window; sitting and standing are one class 'still', split by the posture state
CLASS_NAMES_V2 = ('walking', 'stairs_up', 'stairs_down', 'still', 'lying',
                  'sit_to_stand', 'stand_to_sit', 'bed_transfer')
CLASS_LABELS_V2 = ('walking', 'stairs up', 'stairs down', 'still', 'lying',
                   'sit-to-stand', 'stand-to-sit', 'bed transfer')
CLASS_COLOURS_V2 = ('#2e9e44', '#ff9a1f', '#c65102', '#2166d6', '#8e44ad', '#f4d03f', '#c9a227', '#8b5a2b')
N_CLASSES_V1 = len(CLASS_NAMES)
N_CLASSES_V2 = len(CLASS_NAMES_V2)
N_CLASSES = N_CLASSES_V1          # the truth space always has 9 ids (v1 classes)
# Device columns of the confusion table: the 9 v1 ids plus, for v2 only, a 10th column
# 'still (posture unknown)' so that a still window whose posture is unknown is not counted as sitting.
DEVICE_STILL_UNKNOWN = 9
N_DEVICE_COLUMNS = 10
NONE_COLOUR = '#d0d0d0'

# v1 groupings (also the truth groups)
UPRIGHT_AND_STILL = (3, 4)
SIT_STAND_TRANSITIONS = (6, 7)
GROUPS = (('walking', (0,)),
          ('stairs', (1, 2)),
          ('upright and still', (3, 4)),
          ('lying', (5,)),
          ('transitions', (6, 7, 8)))
GROUPS_V2 = (('walking', (0,)),
             ('stairs', (1, 2)),
             ('still', (3,)),
             ('lying', (4,)),
             ('transitions', (5, 6, 7)))

BRIDGE_STATES = ('idle', 'scanning', 'connecting', 'connected', 'lost')


def class_label(cls: int, version: int = 1) -> str:
    labels = CLASS_LABELS_V2 if version >= 2 else CLASS_LABELS
    return labels[cls] if 0 <= cls < len(labels) else '-'


def class_colour(cls: int, version: int = 1) -> str:
    cols = CLASS_COLOURS_V2 if version >= 2 else CLASS_COLOURS
    return cols[cls] if 0 <= cls < len(cols) else NONE_COLOUR


def class_names(version: int = 1):
    return CLASS_NAMES_V2 if version >= 2 else CLASS_NAMES


def groups(version: int = 1):
    return GROUPS_V2 if version >= 2 else GROUPS


def window_s(version: int = 1) -> float:
    """Classified window length: 2 s (v1) or 4 s (v2)."""
    return 4.0 if version >= 2 else 2.0


def centre_before_end_s(version: int = 1) -> float:
    """How far the window centre is before t_ms (the end of the window)."""
    return window_s(version) / 2.0


def posture_name(posture: int) -> str:
    return POSTURE_NAMES[posture] if 0 <= posture < len(POSTURE_NAMES) else 'unknown'


def event_flags(events: int) -> Tuple[bool, bool, bool]:
    """(sit_to_stand, stand_to_sit, bed_transfer) counted in that window."""
    return bool(events & EV_SIT_TO_STAND), bool(events & EV_STAND_TO_SIT), bool(events & EV_BED_TRANSFER)


def posture(events: int) -> int:
    """Posture state after that window: 0 unknown, 1 sitting, 2 standing."""
    return (events & EV_POSTURE_MASK) >> EV_POSTURE_SHIFT


def device_column(cls: int, events: int = 0, version: int = 1, unknown_col: int = DEVICE_STILL_UNKNOWN) -> int:
    """Device class in the confusion-table space. For v2 the still class is split by the posture state:
    still + posture sitting -> 3, still + posture standing -> 4, still + posture unknown -> `unknown_col`
    (the 10th column, DEVICE_STILL_UNKNOWN, so an unknown posture is never counted as sitting). The totals table
    passes unknown_col=3 (the still time still goes into its sitting/standing/unknown counters)."""
    if version < 2:
        return cls
    if cls == 3:
        p = posture(events)
        return 4 if p == 2 else (3 if p == 1 else unknown_col)
    return {4: 5, 5: 6, 6: 7, 7: 8}.get(cls, cls)


# ------------------------------------------------------------------------------------------------
# Messages
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Live:
    """One classified window (sent once per second)."""
    seq: int
    t_ms: int                       # end of the window, ms since the device booted
    cls: int                        # v2: after the stairs rule (0-7); v1: raw class (0-8); or CLASS_NONE
    cls_smooth: int                 # after the majority-of-3 smoothing (one window later), or CLASS_NONE
    vote_pct: int                   # v2: vote share of the raw class; v1: of the winning class; or VOTE_NONE
    flags: int
    mean_mg: Tuple[int, int, int]   # mean x, y, z of the window
    battery_mv: int                 # 0 = not measured
    raw_cls: int = CLASS_NONE       # v2 only: argmax before the stairs rule
    events: int = 0                 # v2 only: event flags + posture
    version: int = 1                # protocol version of this packet

    @property
    def selftest_ok(self) -> bool:
        return bool(self.flags & FLAG_SELFTEST_OK)

    @property
    def lowpass(self) -> bool:
        return bool(self.flags & FLAG_LOWPASS)

    @property
    def small_model(self) -> bool:
        return bool(self.flags & FLAG_SMALL_MODEL)

    @property
    def v2(self) -> bool:
        return self.version >= 2

    @property
    def still_posture(self) -> int:
        return posture(self.events) if self.v2 else 0


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
class Summary:
    """Daily summary, format 2 (40 bytes on the wire)."""
    format: int
    n_classes: int
    minutes_by_class: Tuple[int, ...]
    minutes_sitting: int
    minutes_standing: int
    minutes_still_unknown: int
    n_sit_to_stand: int
    n_stand_to_sit: int
    n_bed_transfer: int
    sts_seconds_x10: int
    steps: int
    cadence_x10: int
    stairs_cadence_x10: int
    sit_tilt_x10: int

    @property
    def minutes_still(self) -> int:
        return self.minutes_by_class[3] if len(self.minutes_by_class) > 3 else 0


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


Message = Union[Live, Raw, Info, Summary, BridgeVersion, BridgeFound, BridgeScanDone, BridgeState, BridgeDrops, BadLine]


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
    if value >= max(N_CLASSES_V1, N_CLASSES_V2) and value != CLASS_NONE:
        raise ValueError(f'{what} is not a class: {value}')
    return value


def _parse_live(fields: list) -> Live:
    if len(fields) == 12:                       # v2
        version, extra = 2, (fields[10], fields[11])
    elif len(fields) == 10:                     # v1
        version, extra = 1, (CLASS_NONE, 0)
    else:
        raise ValueError(f'L line needs 10 (v1) or 12 (v2) values, has {len(fields)}')
    vote = _int(fields[4], 0, 255, 'vote')
    if vote > 100 and vote != VOTE_NONE:
        raise ValueError(f'vote out of range: {vote}')
    live = Live(seq=_int(fields[0], 0, 0xFFFF, 'seq'),
                t_ms=_int(fields[1], 0, 0xFFFFFFFF, 't_ms'),
                cls=_class(fields[2], 'class'),
                cls_smooth=_class(fields[3], 'smoothed class'),
                vote_pct=vote,
                flags=_int(fields[5], 0, 255, 'flags'),
                mean_mg=tuple(_int(v, -32768, 32767, 'mean') for v in fields[6:9]),
                battery_mv=_int(fields[9], 0, 0xFFFF, 'battery'),
                raw_cls=_class(extra[0], 'raw class') if version == 2 else CLASS_NONE,
                events=_int(extra[1], 0, 255, 'event flags') if version == 2 else 0,
                version=version)
    return live


def _parse_summary(fields: list) -> Summary:
    if len(fields) != 21:
        raise ValueError(f'D line needs 21 values, has {len(fields)}')
    n = _int(fields[1], 1, 8, 'n_classes')
    if n != 8:
        raise ValueError(f'unknown class count {n}')
    minutes = tuple(_int(v, 0, 0xFFFF, 'minutes') for v in fields[2:10])
    vals = [_int(v, 0, 0xFFFF, 'summary') for v in fields[10:20]]
    return Summary(format=_int(fields[0], 1, 255, 'format'), n_classes=n, minutes_by_class=minutes,
                   minutes_sitting=vals[0], minutes_standing=vals[1], minutes_still_unknown=vals[2],
                   n_sit_to_stand=vals[3], n_stand_to_sit=vals[4], n_bed_transfer=vals[5],
                   sts_seconds_x10=vals[6], steps=vals[7], cadence_x10=vals[8], stairs_cadence_x10=vals[9],
                   sit_tilt_x10=_int(fields[20], -32768, 32767, 'seated tilt'))


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
        if kind == 'D':
            return _parse_summary(rest.split(','))
        if kind == 'B':
            return _parse_bridge(rest)
        raise ValueError('unknown line type')
    except ValueError as exc:
        return BadLine(line, str(exc))


def format_live(p: Live) -> str:
    base = 'L,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d' % (p.seq, p.t_ms, p.cls, p.cls_smooth, p.vote_pct, p.flags,
                                                p.mean_mg[0], p.mean_mg[1], p.mean_mg[2], p.battery_mv)
    if p.version >= 2:
        base += ',%d,%d' % (p.raw_cls, p.events)
    return base + '\n'


def format_raw(p: Raw) -> str:
    values = ','.join('%d,%d,%d' % s for s in p.samples)
    return 'R,%d,%d,%d,%s\n' % (p.seq, p.t_ms_first, p.n, values)


def format_info(text: str) -> str:
    return 'I,%s\n' % text.replace('\n', ' ').replace('\r', ' ')


def format_summary(s: Summary) -> str:
    return 'D,%d,%d,%s,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d\n' % (
        s.format, s.n_classes, ','.join(str(v) for v in s.minutes_by_class),
        s.minutes_sitting, s.minutes_standing, s.minutes_still_unknown,
        s.n_sit_to_stand, s.n_stand_to_sit, s.n_bed_transfer, s.sts_seconds_x10, s.steps,
        s.cadence_x10, s.stairs_cadence_x10, s.sit_tilt_x10)


# ------------------------------------------------------------------------------------------------
# Binary BLE packets
# ------------------------------------------------------------------------------------------------
_LIVE_V1 = struct.Struct('<BBHIBBBBhhhH')
_LIVE_V2 = struct.Struct('<BBHIBBBBhhhHBB')
_SUMMARY = struct.Struct('<BBB8H3H3H4Hh')


def encode_live(p: Live) -> bytes:
    if p.version >= 2:
        return _LIVE_V2.pack(PKT_LIVE, PROTOCOL_VERSION, p.seq, p.t_ms, p.cls, p.cls_smooth, p.vote_pct, p.flags,
                             *p.mean_mg, p.battery_mv, p.raw_cls, p.events)
    return _LIVE_V1.pack(PKT_LIVE, 1, p.seq, p.t_ms, p.cls, p.cls_smooth, p.vote_pct, p.flags,
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


def encode_summary(s: Summary) -> bytes:
    return _SUMMARY.pack(PKT_SUMMARY, s.format, s.n_classes, *s.minutes_by_class,
                         s.minutes_sitting, s.minutes_standing, s.minutes_still_unknown,
                         s.n_sit_to_stand, s.n_stand_to_sit, s.n_bed_transfer, s.sts_seconds_x10,
                         s.steps, s.cadence_x10, s.stairs_cadence_x10, s.sit_tilt_x10)


def decode_packet(data: bytes) -> Union[Live, Raw, Info, Summary]:
    """BLE notification (LIVE v1/v2, RAW, INFO or SUMMARY) -> message. Raises ValueError if it is not valid."""
    if not data:
        raise ValueError('empty packet')
    kind = data[0]
    if kind == PKT_LIVE:
        if len(data) == LIVE_LEN_V2 and data[1] == PROTOCOL_VERSION:
            f = _LIVE_V2.unpack(data)
            return Live(f[2], f[3], f[4], f[5], f[6], f[7], (f[8], f[9], f[10]), f[11], f[12], f[13], 2)
        if len(data) == LIVE_LEN_V1 and data[1] == 1:
            f = _LIVE_V1.unpack(data)
            return Live(f[2], f[3], f[4], f[5], f[6], f[7], (f[8], f[9], f[10]), f[11], CLASS_NONE, 0, 1)
        raise ValueError('bad LIVE packet')
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
    if kind == PKT_SUMMARY:
        if len(data) != SUMMARY_LEN:
            raise ValueError('bad SUMMARY packet')
        f = _SUMMARY.unpack(data)
        if f[1] != SUMMARY_FORMAT:
            raise ValueError(f'unknown summary format {f[1]}')
        return Summary(f[1], f[2], tuple(f[3:11]), f[11], f[12], f[13], f[14], f[15], f[16], f[17], f[18],
                       f[19], f[20], f[21])
    raise ValueError(f'unknown packet type {kind}')
