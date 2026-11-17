"""Recording of a session. One click makes three files with the date and time in their names:
  kneil_<date>_<time>_live.csv     every LIVE packet (classes, vote, flags, means) with the truth key
  kneil_<date>_<time>_raw.csv      every RAW sample (mg) with the truth key
  kneil_<date>_<time>_session.txt  every received line with its time, and the truth keys: used for replay
Times: pc_time = wall clock of the PC; t_rec_s = seconds since the recording started; t_ms = device clock."""
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from . import VERSION
from . import protocol as P
from .session import truth_label

# v2 adds the raw class (before the stairs rule), the event flags and the posture state at the end
LIVE_COLUMNS_V2_EXTRA = ['raw_class', 'raw_class_name', 'event_flags', 'sit_to_stand', 'stand_to_sit',
                       'bed_transfer', 'posture']

SESSION_SUFFIX = '_session.txt'
TRUTH_LINE = '#TRUTH,'           # truth key in the session file (not a protocol line)

LIVE_COLUMNS = ['pc_time', 't_rec_s', 'seq', 't_ms', 'class', 'class_name', 'class_smoothed',
                'class_smoothed_name', 'vote_pct', 'flags', 'selftest_ok', 'mean_x_mg', 'mean_y_mg', 'mean_z_mg',
                'battery_mv', 'truth_now', 'truth_now_name', 'truth_window', 'truth_window_name',
                'truth_window_smoothed', 'truth_window_smoothed_name']
RAW_COLUMNS = ['pc_time', 't_rec_s', 'seq', 't_ms', 'x_mg', 'y_mg', 'z_mg', 'truth', 'truth_name']


def _code(c: Optional[int]) -> str:
    return '' if c is None else str(c)


def _class_name(c: int, version: int = 1) -> str:
    names = P.class_names(version)
    return names[c] if 0 <= c < len(names) else ''


class Recorder:
    def __init__(self, folder: str, t_start: float):
        os.makedirs(folder, exist_ok=True)
        self.started = datetime.now()
        self.base = os.path.join(folder, 'kneil_' + self.started.strftime('%Y%m%d_%H%M%S'))
        self.t_start = t_start
        self.n_live = 0
        self.n_raw = 0
        self._live_f = open(self.base + '_live.csv', 'w', newline='', encoding='utf-8')
        self._raw_f = open(self.base + '_raw.csv', 'w', newline='', encoding='utf-8')
        self._lines_f = open(self.base + SESSION_SUFFIX, 'w', encoding='utf-8')
        self._live = csv.writer(self._live_f)
        self._raw = csv.writer(self._raw_f)
        self._live.writerow(LIVE_COLUMNS + LIVE_COLUMNS_V2_EXTRA)
        self._raw.writerow(RAW_COLUMNS)
        self._lines_f.write(f'# KNEIL live viewer {VERSION} session file (used for replay)\n'
                            f'# started {self.started.isoformat(timespec="seconds")}\n'
                            f'# each line: seconds since the start <TAB> received line (or {TRUTH_LINE}<key code>)\n')

    @property
    def name(self) -> str:
        return os.path.basename(self.base)

    def _times(self, t: float) -> Tuple[str, str]:
        return datetime.now().isoformat(timespec='milliseconds'), f'{t - self.t_start:.3f}'

    def line(self, t: float, text: str) -> None:
        self._lines_f.write(f'{t - self.t_start:.3f}\t{text}\n')

    def truth(self, t: float, code: int) -> None:
        self.line(t, f'{TRUTH_LINE}{code}')

    def live(self, p: P.Live, t: float, truth_now: Optional[int], truth_window: Optional[int],
             truth_window_smoothed: Optional[int]) -> None:
        pc_time, t_rec = self._times(t)
        sts, s2s, bed = P.event_flags(p.events)
        self._live.writerow([pc_time, t_rec, p.seq, p.t_ms, p.cls, _class_name(p.cls, p.version), p.cls_smooth,
                             _class_name(p.cls_smooth, p.version), p.vote_pct, p.flags, int(p.selftest_ok), *p.mean_mg,
                             p.battery_mv, _code(truth_now), truth_label(truth_now), _code(truth_window),
                             truth_label(truth_window), _code(truth_window_smoothed),
                             truth_label(truth_window_smoothed),
                             ('' if not p.v2 else p.raw_cls), ('' if not p.v2 else _class_name(p.raw_cls, p.version)),
                             ('' if not p.v2 else p.events), int(sts), int(s2s), int(bed),
                             ('' if not p.v2 else P.posture_name(p.still_posture))])
        self.n_live += 1

    def raw(self, p: P.Raw, t: float, truth_of_sample: Callable[[int], Optional[int]]) -> None:
        pc_time, t_rec = self._times(t)
        rows: List[list] = []
        for i, (x, y, z) in enumerate(p.samples):
            t_ms = p.t_ms_first + i * P.SAMPLE_MS
            truth = truth_of_sample(t_ms)
            rows.append([pc_time, t_rec, p.seq, t_ms, x, y, z, _code(truth), truth_label(truth)])
        self._raw.writerows(rows)
        self.n_raw += len(rows)

    def flush(self) -> None:
        for f in (self._live_f, self._raw_f, self._lines_f):
            f.flush()

    def close(self) -> None:
        for f in (self._live_f, self._raw_f, self._lines_f):
            f.close()


def read_session(path: str) -> List[Tuple[float, str]]:
    """Session file -> [(seconds since the start, line)]. Raises ValueError if it is not a session file."""
    entries: List[Tuple[float, str]] = []
    with open(path, encoding='utf-8') as f:
        for n, raw in enumerate(f, 1):
            raw = raw.rstrip('\r\n')
            if not raw or raw.startswith('# '):
                continue
            t, sep, line = raw.partition('\t')
            if not sep:
                raise ValueError(f'line {n} has no time column')
            try:
                entries.append((float(t), line))
            except ValueError:
                raise ValueError(f'line {n}: bad time {t!r}') from None
    if not entries:
        raise ValueError('no data in the file')
    return entries
