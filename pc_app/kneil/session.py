"""State of a viewing session, built from the protocol messages (no Qt here, so it can be unit-tested).
All times are in seconds on the session clock of the data source (the PC's monotonic clock for a live source,
the recorded time for a replay)."""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
from typing import Dict, List, Optional

import numpy as np

from . import protocol as P
from .lowpass import LowPass

# Ground truth: the keys the user presses for what they are really doing
TRUTH_OTHER = 9                                     # key 0: something else
TRUTH_LABELS = P.CLASS_LABELS + ('other',)
KEY_TO_TRUTH = {'1': 0, '2': 1, '3': 2, '4': 3, '5': 4, '6': 5, '7': 6, '8': 7, '9': 8, '0': TRUTH_OTHER}


def truth_label(code: Optional[int]) -> str:
    return TRUTH_LABELS[code] if code is not None and 0 <= code < len(TRUTH_LABELS) else ''


class TruthTrack:
    """When which truth key was pressed."""

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.times: List[float] = []
        self.codes: List[int] = []

    def set(self, code: int, t: float) -> None:
        if self.times and t < self.times[-1]:
            t = self.times[-1]
        self.times.append(t)
        self.codes.append(code)

    def at(self, t: Optional[float]) -> Optional[int]:
        """The truth at time t (None before the first key)."""
        if t is None:
            return None
        i = bisect_right(self.times, t) - 1
        return self.codes[i] if i >= 0 else None

    @property
    def current(self) -> Optional[int]:
        return self.codes[-1] if self.codes else None


class ClockMap:
    """Device time (t_ms) -> session time. The offset is the smallest (arrival - t_ms) of the recent LIVE
    packets, i.e. the least delayed one (the BLE/USB delay varies; the smallest is closest to the truth)."""

    def __init__(self, n: int = 60):
        self.offsets: deque = deque(maxlen=n)

    def reset(self) -> None:
        self.offsets.clear()

    def update(self, t_ms: int, arrival: float) -> None:
        self.offsets.append(arrival - t_ms / 1000.0)

    def to_session(self, t_ms: float) -> Optional[float]:
        return t_ms / 1000.0 + min(self.offsets) if self.offsets else None


class Confusion:
    """Truth rows (9 classes + other) x device columns (9 v1 classes + 'still, posture unknown' for v2)."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.m = np.zeros((len(TRUTH_LABELS), P.N_DEVICE_COLUMNS), dtype=int)

    def add(self, truth: Optional[int], cls: int) -> None:
        if truth is not None and 0 <= truth < len(TRUTH_LABELS) and 0 <= cls < P.N_DEVICE_COLUMNS:
            self.m[truth, cls] += 1

    @staticmethod
    def _ratio(hit: int, n: int):
        return (100.0 * hit / n if n else None), int(n)

    def walking(self):
        return self._ratio(self.m[0, 0], self.m[0].sum())

    def upright_and_still(self):
        """Truth sitting or standing vs the device still class with any posture (the old, posture-free view)."""
        rows = list(P.UPRIGHT_AND_STILL)
        cols = rows + [P.DEVICE_STILL_UNKNOWN]
        return self._ratio(self.m[np.ix_(rows, cols)].sum(), self.m[rows].sum())

    def sitting(self):
        """Truth sitting vs device still + posture sitting, over the windows with a known posture."""
        known = int(self.m[3].sum() - self.m[3, P.DEVICE_STILL_UNKNOWN])
        return self._ratio(int(self.m[3, 3]), known)

    def standing(self):
        known = int(self.m[4].sum() - self.m[4, P.DEVICE_STILL_UNKNOWN])
        return self._ratio(int(self.m[4, 4]), known)

    def posture_unknown(self):
        """Share of the device's still windows whose posture is unknown (v2 only)."""
        still_cols = list(P.UPRIGHT_AND_STILL) + [P.DEVICE_STILL_UNKNOWN]
        return self._ratio(int(self.m[:, P.DEVICE_STILL_UNKNOWN].sum()), int(self.m[:, still_cols].sum()))

    def overall(self, merge_sit_stand: bool = False):
        classes = self.m[:P.N_CLASSES, :P.N_CLASSES]           # 'other' and the 10th column are not counted
        hit = int(np.trace(classes))
        if merge_sit_stand:
            hit += int(classes[3, 4] + classes[4, 3])
        return self._ratio(hit, int(self.m[:P.N_CLASSES].sum()))


class SeqTracker:
    """Counts lost packets from the 16-bit sequence numbers."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.last: Optional[int] = None
        self.received = 0
        self.lost = 0

    def update(self, seq: int) -> int:
        """Returns the number of packets lost just before this one."""
        self.received += 1
        if self.last is None:
            self.last = seq
            return 0
        gap = (seq - self.last) & 0xFFFF
        self.last = seq
        if gap == 0 or gap > 1000:      # repeated packet, or the device restarted: not counted
            return 0
        self.lost += gap - 1
        return gap - 1


class RateMeter:
    """Messages per second over the last few seconds."""

    def __init__(self, window_s: float = 5.0):
        self.window_s = window_s
        self.times: deque = deque()

    def tick(self, now: float) -> None:
        self.times.append(now)

    def rate(self, now: float) -> float:
        while self.times and self.times[0] < now - self.window_s:
            self.times.popleft()
        return len(self.times) / self.window_s


class RawBuffer:
    """The last `seconds` of RAW samples in g (device frame), plus the same signal after the chip's 6 Hz low-pass."""

    def __init__(self, seconds: float = 10.0, fs: int = P.FS_HZ):
        self.seconds = seconds
        self.capacity = int(seconds * fs * 1.5)
        self.lp = [LowPass(fs, 6.0) for _ in range(3)]
        self.clear()

    def clear(self) -> None:
        self.t = np.empty(0)
        self.xyz = np.empty((0, 3))
        self.xyz_lp = np.empty((0, 3))
        self.last_t: Optional[float] = None
        for f in self.lp:
            f.reset()

    def add(self, raw: P.Raw) -> None:
        dt = 1.0 / P.FS_HZ
        t0 = raw.t_ms_first / 1000.0
        g = np.asarray(raw.samples, dtype=float) / 1000.0
        t = t0 + dt * np.arange(raw.n)
        rows_t, rows_xyz, rows_lp = [], [], []
        if self.last_t is not None and (t0 - self.last_t > 3 * dt or t0 <= self.last_t - dt):
            # gap (lost packets or raw stream restarted): break the line and restart the filter
            rows_t.append(np.array([self.last_t + dt / 2]))
            rows_xyz.append(np.full((1, 3), np.nan))
            rows_lp.append(np.full((1, 3), np.nan))
            for f in self.lp:
                f.reset()
            if t0 <= self.last_t - dt:          # time went back: the device restarted
                self.clear()
                rows_t, rows_xyz, rows_lp = [], [], []
        lp = np.column_stack([self.lp[k].run(g[:, k]) for k in range(3)])
        rows_t.append(t)
        rows_xyz.append(g)
        rows_lp.append(lp)
        self.t = np.concatenate([self.t] + rows_t)[-self.capacity:]
        self.xyz = np.concatenate([self.xyz] + rows_xyz)[-self.capacity:]
        self.xyz_lp = np.concatenate([self.xyz_lp] + rows_lp)[-self.capacity:]
        self.last_t = float(t[-1])

    def view(self):
        """(t relative to the newest sample, xyz, |a|, xyz low-passed, |a| low-passed) for the last `seconds`."""
        if self.t.size == 0:
            return None
        rel = self.t - self.t[-1]
        keep = rel >= -self.seconds
        xyz, lp = self.xyz[keep], self.xyz_lp[keep]
        return rel[keep], xyz, np.linalg.norm(xyz, axis=1), lp, np.linalg.norm(lp, axis=1)


class Timeline:
    """Class of each second (one per LIVE packet) over the last `seconds`; CLASS_NONE where data is missing."""

    def __init__(self, seconds: int = 300):
        self.seconds = seconds
        self.classes: deque = deque(maxlen=seconds)

    def add(self, cls: int, lost_before: int = 0) -> None:
        for _ in range(min(lost_before, self.seconds)):
            self.classes.append(P.CLASS_NONE)
        self.classes.append(cls)

    def array(self) -> np.ndarray:
        out = np.full(self.seconds, P.CLASS_NONE, dtype=np.uint8)
        if self.classes:
            out[-len(self.classes):] = np.fromiter(self.classes, dtype=np.uint8, count=len(self.classes))
        return out


class Totals:
    """Time per class (one second per LIVE packet, smoothed class), the sit/stand/bed events and the posture time.
    v1 counts the events from runs of transition windows; v2 takes them from the LIVE event flags (one per event)."""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.seconds = [0] * P.N_CLASSES
        self.transitions = 0
        self.n_sit_to_stand = 0
        self.n_stand_to_sit = 0
        self.n_bed_transfer = 0
        self.seconds_sitting = 0
        self.seconds_standing = 0
        self.seconds_still_unknown = 0
        self._in_transition = False

    def add(self, cls: int, events: int = 0, version: int = 1) -> None:
        if version >= 2:
            col = P.device_column(cls, events, 2, unknown_col=3)   # totals keep the old sitting/standing/unknown split
            if col < P.N_CLASSES:
                self.seconds[col] += 1
            if cls == 3 and P.posture(events) == 0:
                self.seconds_still_unknown += 1
            elif col == 3:
                self.seconds_sitting += 1
            elif col == 4:
                self.seconds_standing += 1
            sts, s2s, bed = P.event_flags(events)
            self.n_sit_to_stand += int(sts)
            self.n_stand_to_sit += int(s2s)
            self.n_bed_transfer += int(bed)
            self.transitions = self.n_sit_to_stand + self.n_stand_to_sit
            return
        if not 0 <= cls < P.N_CLASSES:
            return
        self.seconds[cls] += 1
        tr = cls in P.SIT_STAND_TRANSITIONS
        if tr and not self._in_transition:      # a run of transition windows is one transition
            self.transitions += 1
        self._in_transition = tr

    @property
    def total(self) -> int:
        return sum(self.seconds)

    def group_seconds(self, classes) -> int:
        return sum(self.seconds[c] for c in classes)


class Session:
    """Everything the window shows, updated by handle(message)."""

    def __init__(self):
        self.live_seq = SeqTracker()
        self.raw_seq = SeqTracker()
        self.live_rate = RateMeter()
        self.raw_rate = RateMeter()
        self.raw = RawBuffer()
        self.timeline = Timeline()
        self.totals = Totals()
        self.version = 1                 # protocol version of the last LIVE packet (v1: 9 classes, v2: 8 classes)
        self.summary: Optional[P.Summary] = None
        self.last_live: Optional[P.Live] = None
        self.last_live_time: Optional[float] = None
        self.bridge_state: Optional[P.BridgeState] = None
        self.bridge_version = ''
        self.bridge_drops = 0
        self.found: Dict[str, P.BridgeFound] = {}
        # ground truth and accuracy
        self.truth = TruthTrack()
        self.clock = ClockMap()
        self.conf_raw = Confusion()          # raw class vs the truth at the centre of its window
        self.conf_smooth = Confusion()       # smoothed class vs the truth at the centre of its window
        self.last_truth_raw: Optional[int] = None
        self.last_truth_smooth: Optional[int] = None
        self.prev_events = 0                 # event flags of the window before (for the smoothed class)

    def reset_link(self) -> None:
        """New port or new device: the sequence numbers and the device clock start again."""
        self.live_seq.reset()
        self.raw_seq.reset()
        self.raw.clear()
        self.clock.reset()
        self.last_live = None
        self.bridge_state = None

    def window_truth(self, t_ms: int, delay_windows: int) -> Optional[int]:
        """Truth at the centre of the classified window that ended at t_ms, `delay_windows` windows earlier.
        v1: 2 s window, centre 1 s before the end. v2: 4 s window, centre 2 s before the end."""
        centre = self.clock.to_session(t_ms - 1000 * (P.centre_before_end_s(self.version) + delay_windows * P.HOP_S))
        return self.truth.at(centre)

    def sample_truth(self, t_ms: int, fallback: float) -> Optional[int]:
        t = self.clock.to_session(t_ms)
        return self.truth.at(t if t is not None else fallback)

    def handle(self, msg: P.Message, now: float) -> List[str]:
        """Update the state; returns the lines for the log."""
        log: List[str] = []
        if isinstance(msg, P.Live):
            if self.last_live is not None and msg.t_ms + 5000 < self.last_live.t_ms:
                log.append('device restarted (its clock went back)')
                self.live_seq.reset()
                self.raw_seq.reset()
                self.clock.reset()
            if msg.version != self.version:
                self.version = msg.version
                log.append(f'protocol v{msg.version}: {len(P.class_names(msg.version))} classes, '
                           f'{P.window_s(msg.version):g} s window')
            lost = self.live_seq.update(msg.seq)
            if lost:
                log.append(f'LIVE: {lost} packet(s) lost')
            self.timeline.add(msg.cls_smooth, lost)
            self.totals.add(msg.cls_smooth, msg.events, msg.version)
            # accuracy: the raw class is about the window that ended at t_ms (centre half a window earlier); the
            # smoothed class is about the window before it. For v2 the device class is mapped into the 9-id
            # truth space, splitting 'still' by the posture state.
            self.clock.update(msg.t_ms, now)
            self.last_truth_raw = self.window_truth(msg.t_ms, 0)
            self.last_truth_smooth = self.window_truth(msg.t_ms, 1)
            self.conf_raw.add(self.last_truth_raw, P.device_column(msg.cls, msg.events, msg.version))
            self.conf_smooth.add(self.last_truth_smooth,
                                 P.device_column(msg.cls_smooth, self.prev_events, msg.version))
            self.live_rate.tick(now)
            self.prev_events = msg.events
            self.last_live = msg
            self.last_live_time = now
        elif isinstance(msg, P.Summary):
            self.summary = msg
            log.append(f'daily summary: walking {msg.minutes_by_class[0]} min, still {msg.minutes_still} min '
                       f'(sit {msg.minutes_sitting} / stand {msg.minutes_standing} / unknown '
                       f'{msg.minutes_still_unknown}), sit-to-stand {msg.n_sit_to_stand}, stand-to-sit '
                       f'{msg.n_stand_to_sit}, bed transfer {msg.n_bed_transfer}, steps {msg.steps}')
        elif isinstance(msg, P.Raw):
            lost = self.raw_seq.update(msg.seq)
            if lost:
                log.append(f'RAW: {lost} packet(s) lost')
            self.raw.add(msg)
            self.raw_rate.tick(now)
        elif isinstance(msg, P.Info):
            log.append('device: ' + msg.text)
        elif isinstance(msg, P.BridgeState):
            old = self.bridge_state
            if old is None or (old.state, old.address) != (msg.state, msg.address):
                who = f' {msg.name} {msg.address}'.rstrip() if msg.address else ''
                log.append(f'bridge: {msg.state}{who}')
            if msg.state == 'connected' and (old is None or old.state != 'connected'):
                self.live_seq.reset()       # new link: the device may have restarted meanwhile
                self.raw_seq.reset()
            self.bridge_state = msg
        elif isinstance(msg, P.BridgeFound):
            self.found[msg.address] = msg
            log.append(f'bridge: found {msg.name} {msg.address} {msg.rssi} dBm')
        elif isinstance(msg, P.BridgeScanDone):
            log.append(f'bridge: scan done, {len(self.found)} KNEIL device(s)')
        elif isinstance(msg, P.BridgeDrops):
            if msg.count != self.bridge_drops:
                log.append(f'bridge: {msg.count} line(s) dropped in total (the PC did not read fast enough)')
            self.bridge_drops = msg.count
        elif isinstance(msg, P.BridgeVersion):
            self.bridge_version = msg.text
            log.append('bridge: ' + msg.text)
        elif isinstance(msg, P.BadLine):
            log.append(f'ignored line ({msg.reason}): {msg.line[:80]}')
        return log
