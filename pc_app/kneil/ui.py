"""Main window of the KNEIL live viewer. The data sources run in their own threads and put items into a queue;
a 50 ms timer takes them out, so the window never freezes."""
from __future__ import annotations

import html
import os
import platform
import queue
import time
from pathlib import Path
from typing import Optional

import numpy as np
import PySide6
from PySide6 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from . import VERSION
from . import protocol as P
from .recording import Recorder
from .session import KEY_TO_TRUTH, TRUTH_LABELS, TRUTH_OTHER, Session, truth_label
from .transports import DemoTransport, ReplayTransport, SerialTransport, TransportError, list_ports

pg.setConfigOptions(antialias=True, background='w', foreground='k', imageAxisOrder='row-major')

TRANSPORTS = (('bridge', 'XIAO bridge (USB)'),
              ('serial', "Board's own serial port"),
              ('replay', 'Replay a recording'),
              ('demo', 'Demo (simulated data)'))
SPEEDS = (1, 2, 5, 10, 30)
STALE_S = 3.0           # no LIVE packet for this long: warn
AXES = (('x', '#d62728'), ('y', '#2ca02c'), ('z', '#1f77b4'), ('|a|', '#333333'))
STATE_COLOURS = {'connected': '#1e8e3e', 'connecting': '#1a73e8', 'scanning': '#1a73e8',
                 'lost': '#d93025', 'idle': '#777777'}
KEY_FOR_TRUTH = {code: key for key, code in KEY_TO_TRUTH.items()}
DEFAULT_REC_FOLDER = str(Path.home() / 'Documents' / 'KNEIL recordings')


def _rgba(colour: str):
    c = QtGui.QColor(colour)
    return c.red(), c.green(), c.blue(), 255


LUT_V1 = np.array([_rgba(c) for c in P.CLASS_COLOURS] + [_rgba(P.NONE_COLOUR)] * (256 - P.N_CLASSES_V1),
                   dtype=np.uint8)
LUT_V2 = np.array([_rgba(c) for c in P.CLASS_COLOURS_V2] + [_rgba(P.NONE_COLOUR)] * (256 - P.N_CLASSES_V2),
                   dtype=np.uint8)


def _lut(version: int):
    """Colour table of the timeline: v1 has 9 classes, v2 has 8 (still is one colour)."""
    return LUT_V2 if version >= 2 else LUT_V1


def _text_on(colour: str) -> str:
    c = QtGui.QColor(colour)
    return '#000000' if 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue() > 150 else '#ffffff'


def _truth_colour(code: Optional[int]) -> str:
    return P.class_colour(code) if code is not None and code < P.N_CLASSES else P.NONE_COLOUR


def _duration(seconds: float) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def _pct(value) -> str:
    pct, n = value
    return f'<b>{pct:.0f} %</b> ({n} windows)' if pct is not None else '- (no windows yet)'


def _bold(widget: QtWidgets.QWidget, size: int) -> None:
    f = widget.font()
    f.setPointSize(size)
    f.setBold(True)
    widget.setFont(f)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, demo: bool = False, port: str | None = None):
        super().__init__()
        self.setWindowTitle(f'KNEIL live viewer {VERSION}')
        self.settings = QtCore.QSettings('KNEIL', 'live viewer')
        self.session = Session()
        self.inbox: queue.Queue = queue.Queue()
        self.transport = None
        self.kind = None
        self.recorder: Optional[Recorder] = None
        self.link_up = False                 # data can flow from the device
        self.pending_auto_connect = False
        self.plot_dirty = False
        self.last_plot = 0.0
        self.last_status = 0.0

        self._build()
        self._restore()
        self.refresh_ports()
        self._update_controls()
        self._refresh_all()
        QtWidgets.QApplication.instance().installEventFilter(self)    # truth keys anywhere in the window
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(50)
        self.log(f'KNEIL live viewer {VERSION} (Python {platform.python_version()}, PySide6 {PySide6.__version__}, '
                 f'pyqtgraph {pg.__version__})')

        if demo:
            self.transport_combo.setCurrentIndex(self.transport_combo.findData('demo'))
            self.auto_connect.setChecked(True)
            self.open_transport()
        elif port:
            if self.port_combo.findData(port) < 0:
                self.port_combo.addItem(port, port)
            self.port_combo.setCurrentIndex(self.port_combo.findData(port))
            if self.transport_combo.currentData() not in ('bridge', 'serial'):
                self.transport_combo.setCurrentIndex(self.transport_combo.findData('bridge'))
            self.open_transport()

    # ------------------------------------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------------------------------------
    def _build(self) -> None:
        # Layout (fits a laptop screen): connection on top; activity | (signal plot over timeline);
        # tabs for accuracy, totals and log at the bottom. Drag the bars to resize.
        central = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 2)
        root.addWidget(self._build_connection())
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(self._build_accel(), stretch=1)
        rv.addWidget(self._build_timeline())
        mid = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        mid.addWidget(self._build_activity())
        mid.addWidget(right)
        mid.setStretchFactor(0, 0)
        mid.setStretchFactor(1, 1)
        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self._build_accuracy(), 'Accuracy (truth keys)')
        self.tabs.addTab(self._build_totals(), 'Session totals')
        self.tabs.addTab(self._build_log(), 'Log')
        self.vsplit = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        self.vsplit.addWidget(mid)
        self.vsplit.addWidget(self.tabs)
        self.vsplit.setChildrenCollapsible(False)
        self.vsplit.setStretchFactor(0, 3)
        self.vsplit.setStretchFactor(1, 2)
        self.vsplit.setSizes([440, 290])
        root.addWidget(self.vsplit, stretch=1)
        self.setCentralWidget(central)
        self.status_text = QtWidgets.QLabel('')
        self.rec_indicator = QtWidgets.QLabel('')
        self.statusBar().addWidget(self.status_text, 1)
        self.statusBar().addPermanentWidget(self.rec_indicator)
        self.resize(1300, 880)

    def _build_connection(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox('Connection')
        v = QtWidgets.QVBoxLayout(box)

        row = QtWidgets.QHBoxLayout()
        self.transport_combo = QtWidgets.QComboBox()
        for key, label in TRANSPORTS:
            self.transport_combo.addItem(label, key)
        self.transport_combo.currentIndexChanged.connect(self._update_controls)
        self.port_label = QtWidgets.QLabel('Port')
        self.port_combo = QtWidgets.QComboBox()
        self.port_combo.setMinimumWidth(240)
        self.refresh_btn = QtWidgets.QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self.refresh_ports)
        self.speed_label = QtWidgets.QLabel('Speed')
        self.speed_combo = QtWidgets.QComboBox()
        for s in SPEEDS:
            self.speed_combo.addItem(f'{s}x', float(s))
        self.open_btn = QtWidgets.QPushButton('Open')
        self.open_btn.setMinimumWidth(90)
        self.open_btn.clicked.connect(self.toggle_open)
        self.auto_connect = QtWidgets.QCheckBox('Connect to the device when the port opens')
        for w in (QtWidgets.QLabel('Source'), self.transport_combo, self.port_label, self.port_combo,
                  self.refresh_btn, self.speed_label, self.speed_combo, self.open_btn, self.auto_connect):
            row.addWidget(w)
        row.addStretch(1)
        v.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.device_combo = QtWidgets.QComboBox()
        self.device_combo.setMinimumWidth(340)
        self.scan_btn = QtWidgets.QPushButton('Scan')
        self.scan_btn.clicked.connect(self.scan)
        self.connect_btn = QtWidgets.QPushButton('Connect')
        self.connect_btn.clicked.connect(self.connect_device)
        self.disconnect_btn = QtWidgets.QPushButton('Disconnect')
        self.disconnect_btn.clicked.connect(lambda: self.send(P.CMD_DISCONNECT))
        self.state_label = QtWidgets.QLabel()
        self.state_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        for w in (QtWidgets.QLabel('Device'), self.device_combo, self.scan_btn, self.connect_btn,
                  self.disconnect_btn):
            row.addWidget(w)
        row.addSpacing(16)
        row.addWidget(self.state_label, stretch=1)
        v.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        self.stats_label = QtWidgets.QLabel()
        self.record_btn = QtWidgets.QPushButton('Start recording')
        self.record_btn.setMinimumWidth(140)
        self.record_btn.clicked.connect(self.toggle_recording)
        self.folder_btn = QtWidgets.QPushButton('Folder...')
        self.folder_btn.clicked.connect(self.choose_folder)
        self.summary_btn = QtWidgets.QPushButton('Daily summary')
        self.summary_btn.setToolTip('Ask the device for the daily summary (command Q)')
        self.summary_btn.clicked.connect(lambda: self.send(P.CMD_SUMMARY))
        self.reset_day_btn = QtWidgets.QPushButton('Reset day')
        self.reset_day_btn.setToolTip('Reset the daily metrics in the device (command Z)')
        self.reset_day_btn.clicked.connect(lambda: self.send(P.CMD_RESET))
        self.rec_label = QtWidgets.QLabel('')
        _bold(self.rec_label, 10)
        row.addWidget(self.stats_label)
        row.addStretch(1)
        row.addWidget(self.rec_label)
        row.addWidget(self.summary_btn)
        row.addWidget(self.reset_day_btn)
        row.addWidget(self.record_btn)
        row.addWidget(self.folder_btn)
        v.addLayout(row)
        return box

    def _build_activity(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox('Current activity')
        v = QtWidgets.QVBoxLayout(box)
        v.setSpacing(4)
        self.big = QtWidgets.QLabel('no data')
        _bold(self.big, 28)
        self.big.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.big.setMinimumSize(320, 76)
        self.big.setToolTip('Smoothed class: majority of 3 windows, about 2 s behind the movement.\n'
                            'Sitting and standing look alike at the tibia: judge them together as '
                            '"upright and still".')
        self._set_big('no data', P.NONE_COLOUR)
        self.upright = QtWidgets.QLabel('')
        _bold(self.upright, 14)
        self.upright.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.upright.setMinimumHeight(24)
        self.raw_label = QtWidgets.QLabel('')
        _bold(self.raw_label, 11)
        self.vote_label = QtWidgets.QLabel('')
        self.mean_label = QtWidgets.QLabel('')
        self.flags_label = QtWidgets.QLabel('')
        self.flags_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.age_label = QtWidgets.QLabel('')
        self.age_label.setStyleSheet('color: #d93025; font-weight: bold;')
        self.summary_label = QtWidgets.QLabel('daily summary: not received yet')
        self.summary_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)
        for w in (self.big, self.upright, self.raw_label, self.vote_label, self.mean_label, self.flags_label,
                  self.age_label, self.summary_label):
            v.addWidget(w)

        truth = QtWidgets.QGroupBox('Ground truth: what are you really doing? (keys)')
        tv = QtWidgets.QVBoxLayout(truth)
        tv.setSpacing(4)
        self.truth_label = QtWidgets.QLabel('')
        _bold(self.truth_label, 12)
        self.truth_label.setMinimumHeight(30)
        self.match_label = QtWidgets.QLabel('')
        self.match_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        self.match_label.setMinimumHeight(20)
        keys = QtWidgets.QLabel('<b>1</b> walk &nbsp;<b>2</b> stairs up &nbsp;<b>3</b> stairs down &nbsp;'
                                '<b>4</b> sit &nbsp;<b>5</b> stand<br><b>6</b> lie &nbsp;<b>7</b> sit-to-stand '
                                '&nbsp;<b>8</b> stand-to-sit &nbsp;<b>9</b> bed transfer &nbsp;<b>0</b> other')
        keys.setTextFormat(QtCore.Qt.TextFormat.RichText)
        keys.setStyleSheet('color: #888888;')
        for w in (self.truth_label, self.match_label, keys):
            tv.addWidget(w)
        v.addWidget(truth)
        v.addStretch(1)
        row = QtWidgets.QHBoxLayout()
        self.raw_check = QtWidgets.QCheckBox('Raw stream (for the plot)')
        self.raw_check.toggled.connect(self._raw_toggled)
        self.selftest_btn = QtWidgets.QPushButton('Run self-test')
        self.selftest_btn.clicked.connect(self.run_selftest)
        row.addWidget(self.raw_check)
        row.addStretch(1)
        row.addWidget(self.selftest_btn)
        v.addLayout(row)
        return box

    def _build_accel(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox('Live acceleration: last 10 s (device frame: x anterior, y left, z superior)')
        v = QtWidgets.QVBoxLayout(box)
        self.plot = pg.PlotWidget()
        self.plot.setLabel('left', 'acceleration', units='g')
        self.plot.setLabel('bottom', 'seconds before the newest sample')
        self.plot.showGrid(x=True, y=True, alpha=0.25)
        self.plot.setXRange(-10, 0, padding=0.01)
        self.plot.addLegend(offset=(10, 5), colCount=4)
        self.curves = [self.plot.plot(pen=pg.mkPen(c, width=1.2), name=n) for n, c in AXES]
        self.lp_curves = [self.plot.plot(pen=pg.mkPen(c, width=2.5, style=QtCore.Qt.PenStyle.DashLine))
                          for _, c in AXES]
        self.no_data = pg.TextItem('No raw data yet: tick "Raw stream"', color='#888888', anchor=(0.5, 0.5))
        self.plot.addItem(self.no_data)
        self.no_data.setPos(-5, 0.5)
        v.addWidget(self.plot)
        row = QtWidgets.QHBoxLayout()
        self.lp_check = QtWidgets.QCheckBox('Overlay the signal after the 6 Hz low-pass (dashed, as on the chip)')
        self.mag_check = QtWidgets.QCheckBox('Show |a|')
        self.lp_check.toggled.connect(self._curve_visibility)
        self.mag_check.toggled.connect(self._curve_visibility)
        row.addWidget(self.lp_check)
        row.addWidget(self.mag_check)
        row.addStretch(1)
        v.addLayout(row)
        return box

    def _build_timeline(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox('Timeline: last 5 minutes, one block per second (smoothed class)')
        v = QtWidgets.QVBoxLayout(box)
        self.tl_plot = pg.PlotWidget()
        self.tl_plot.setFixedHeight(66)
        self.tl_plot.hideAxis('left')
        self.tl_plot.setLabel('bottom', 'seconds ago')
        self.tl_plot.setMouseEnabled(x=False, y=False)
        self.tl_plot.setMenuEnabled(False)
        self.tl_plot.hideButtons()
        self.tl_img = pg.ImageItem()
        self.tl_plot.addItem(self.tl_img)
        self.tl_plot.setXRange(-self.session.timeline.seconds, 0, padding=0)
        self.tl_plot.setYRange(0, 1, padding=0)
        v.addWidget(self.tl_plot)
        legend = QtWidgets.QHBoxLayout()
        self.legend_layout = legend
        self.legend_version = 0
        self._update_legend()
        v.addLayout(legend)
        return box

    def _update_legend(self) -> None:
        """The legend follows the protocol version of the last LIVE packet (9 classes in v1, 8 in v2)."""
        version = self.session.version
        if version == self.legend_version:
            return
        self.legend_version = version
        while self.legend_layout.count():
            item = self.legend_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for c in range(len(P.class_names(version))):
            self.legend_layout.addWidget(self._chip(P.class_label(c, version), P.class_colour(c, version)))
        self.legend_layout.addWidget(self._chip('no data', P.NONE_COLOUR))
        self.legend_layout.addStretch(1)

    @staticmethod
    def _chip(text: str, colour: str) -> QtWidgets.QLabel:
        chip = QtWidgets.QLabel(text)
        chip.setStyleSheet(f'background: {colour}; color: {_text_on(colour)}; padding: 2px 6px; '
                           f'border-radius: 4px;')
        return chip

    def _build_totals(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(box)
        v.addWidget(QtWidgets.QLabel('Time per class (smoothed class, 1 s per window):'))
        rows = [(name, None) for name, _ in P.GROUPS] + [(P.class_label(c), c) for c in range(P.N_CLASSES)]
        self.totals_table = QtWidgets.QTableWidget(len(rows), 3)
        self.totals_table.setHorizontalHeaderLabels(['', 'time', 'share'])
        self.totals_table.verticalHeader().setVisible(False)
        self.totals_table.verticalHeader().setDefaultSectionSize(20)
        self.totals_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.totals_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.totals_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        for r, (name, cls) in enumerate(rows):
            item = QtWidgets.QTableWidgetItem(name)
            if cls is None:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            else:
                item.setBackground(QtGui.QColor(P.class_colour(cls)))
                item.setForeground(QtGui.QColor(_text_on(P.class_colour(cls))))
            self.totals_table.setItem(r, 0, item)
            for col in (1, 2):
                cell = QtWidgets.QTableWidgetItem('')
                cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter)
                self.totals_table.setItem(r, col, cell)
        v.addWidget(self.totals_table)
        row = QtWidgets.QHBoxLayout()
        self.transitions_label = QtWidgets.QLabel('')
        _bold(self.transitions_label, 10)
        reset = QtWidgets.QPushButton('Reset totals')
        reset.clicked.connect(self._reset_totals)
        row.addWidget(self.transitions_label)
        row.addStretch(1)
        row.addWidget(reset)
        v.addLayout(row)
        return box

    def _build_accuracy(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Compare the truth keys with the'))
        self.compare_combo = QtWidgets.QComboBox()
        self.compare_combo.addItem('smoothed class (big label)', 'smoothed')
        self.compare_combo.addItem('raw class (newest window)', 'raw')
        self.compare_combo.currentIndexChanged.connect(self._update_confusion)
        row.addWidget(self.compare_combo)
        row.addStretch(1)
        reset = QtWidgets.QPushButton('Reset table')
        reset.clicked.connect(self._reset_confusion)
        row.addWidget(reset)
        v.addLayout(row)
        self.acc_label = QtWidgets.QLabel('')
        self.acc_label.setTextFormat(QtCore.Qt.TextFormat.RichText)
        v.addWidget(self.acc_label)
        self.conf_table = QtWidgets.QTableWidget(len(TRUTH_LABELS), P.N_DEVICE_COLUMNS)
        self.conf_table.setHorizontalHeaderLabels([P.class_label(c) for c in range(P.N_CLASSES)]
                                                 + ['still (posture ?)'])
        self.conf_table.setVerticalHeaderLabels([f'{KEY_FOR_TRUTH[c]} {TRUTH_LABELS[c]}'
                                                 for c in range(len(TRUTH_LABELS))])
        self.conf_table.verticalHeader().setDefaultSectionSize(20)
        self.conf_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.conf_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.NoSelection)
        self.conf_table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        for r in range(len(TRUTH_LABELS)):
            for c in range(P.N_DEVICE_COLUMNS):
                cell = QtWidgets.QTableWidgetItem('')
                cell.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.conf_table.setItem(r, c, cell)
        v.addWidget(self.conf_table)
        note = QtWidgets.QLabel('Rows: your truth key at the centre of the classified window (2 s before the end for '
                                'v2, 1 s for v1). Columns: what the device said. Each cell: windows (share of the '
                                'row). Green: correct; blue: sitting and standing swapped (correct as "upright and '
                                'still"); violet: the device said still but its posture was unknown (v2 only); red: '
                                'wrong.')
        note.setWordWrap(True)
        note.setStyleSheet('color: #888888;')
        v.addWidget(note)
        return w

    def _build_log(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        self.log_view = QtWidgets.QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont))
        v.addWidget(self.log_view)
        clear = QtWidgets.QPushButton('Clear log')
        clear.clicked.connect(self.log_view.clear)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        row.addWidget(clear)
        v.addLayout(row)
        return w

    # ------------------------------------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------------------------------------
    def _restore(self) -> None:
        s = self.settings
        i = self.transport_combo.findData(s.value('transport', 'bridge'))
        self.transport_combo.setCurrentIndex(max(i, 0))
        self.speed_combo.setCurrentIndex(max(0, self.speed_combo.findData(s.value('replay_speed', 1.0, type=float))))
        self.auto_connect.setChecked(s.value('auto_connect', True, type=bool))
        self.raw_check.blockSignals(True)
        self.raw_check.setChecked(s.value('raw', True, type=bool))
        self.raw_check.blockSignals(False)
        self.lp_check.setChecked(s.value('lowpass_overlay', False, type=bool))
        self.mag_check.setChecked(s.value('show_mag', True, type=bool))
        self.compare_combo.setCurrentIndex(max(0, self.compare_combo.findData(s.value('compare', 'smoothed'))))
        self._curve_visibility()
        self._reset_device_combo()
        self.folder_btn.setToolTip('Recordings are saved in ' + self.rec_folder())
        geometry = s.value('geometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        split = s.value('vsplit')
        if split is not None:
            self.vsplit.restoreState(split)

    def _save(self) -> None:
        s = self.settings
        if self.transport_combo.currentData() != 'demo':   # next start: the real source again
            s.setValue('transport', self.transport_combo.currentData())
        if self.port_combo.currentData():
            s.setValue('port', self.port_combo.currentData())
        s.setValue('replay_speed', self.speed_combo.currentData())
        s.setValue('auto_connect', self.auto_connect.isChecked())
        s.setValue('raw', self.raw_check.isChecked())
        s.setValue('lowpass_overlay', self.lp_check.isChecked())
        s.setValue('show_mag', self.mag_check.isChecked())
        s.setValue('compare', self.compare_combo.currentData())
        s.setValue('geometry', self.saveGeometry())
        s.setValue('vsplit', self.vsplit.saveState())

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self._save()
        self.stop_recording()
        self.close_transport()
        super().closeEvent(event)

    def rec_folder(self) -> str:
        return str(self.settings.value('rec_folder', DEFAULT_REC_FOLDER))

    # ------------------------------------------------------------------------------------------------
    # Ports and devices
    # ------------------------------------------------------------------------------------------------
    def refresh_ports(self) -> None:
        current = self.port_combo.currentData() or self.settings.value('port', '')
        self.port_combo.clear()
        ports = list_ports()
        for p in ports:
            self.port_combo.addItem(f'{p.device}  {p.description}', p.device)
        i = self.port_combo.findData(current)
        if i < 0 and ports and ports[0].is_bridge:
            i = 0                                   # a bridge is plugged in: take it
        if i >= 0:
            self.port_combo.setCurrentIndex(i)
        if not ports:
            self.port_combo.addItem('no COM port found', None)

    def _reset_device_combo(self) -> None:
        self.device_combo.clear()
        self.device_combo.addItem('AUTO: the strongest KNEIL-* device', 'AUTO')
        last = self.settings.value('device', 'AUTO')
        if last and last != 'AUTO':
            self.device_combo.addItem(f'last used: {self.settings.value("device_name", "KNEIL")}   {last}', last)
            self.device_combo.setCurrentIndex(1)

    def _add_found(self, f: P.BridgeFound) -> None:
        text = f'{f.name}   {f.address}   {f.rssi} dBm'
        i = self.device_combo.findData(f.address)
        if i >= 0:
            self.device_combo.setItemText(i, text)
        else:
            self.device_combo.addItem(text, f.address)

    def scan(self) -> None:
        self.session.found.clear()
        self._reset_device_combo()
        self.send(P.CMD_SCAN)

    def connect_device(self) -> None:
        address = self.device_combo.currentData() or 'AUTO'
        if self.kind != 'demo':                     # never remember the simulated device
            self.settings.setValue('device', address)
        self.send(P.cmd_connect(address))

    # ------------------------------------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------------------------------------
    def now(self) -> float:
        """Session clock of the data source (the replay's own time during a replay)."""
        return self.transport.now() if self.transport is not None else time.monotonic()

    def toggle_open(self) -> None:
        if self.transport is not None:
            self.close_transport()
        else:
            self.open_transport()

    def open_transport(self) -> None:
        kind = self.transport_combo.currentData()
        if kind == 'demo':
            t = DemoTransport(self.inbox)
        elif kind == 'replay':
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                self, 'Replay a recording', self.rec_folder(), 'KNEIL session (*_session.txt);;All files (*)')
            if not path:
                return
            t = ReplayTransport(path, self.inbox, speed=self.speed_combo.currentData())
        else:
            port = self.port_combo.currentData()
            if not port:
                QtWidgets.QMessageBox.warning(self, 'No port', 'No COM port selected. Plug in the bridge and '
                                                               'press "Refresh".')
                return
            t = SerialTransport(port, self.inbox)
        try:
            t.open()
        except TransportError as exc:
            self.log(str(exc), error=True)
            QtWidgets.QMessageBox.warning(self, 'Cannot open', str(exc))
            return
        if kind == 'replay':
            self.session = Session()                # a recording is a session of its own
            self._refresh_all()
        else:
            self.session.reset_link()
        self.transport, self.kind = t, kind
        self.link_up = False
        self.log(f'opened {t.port}' + (f' (replay at {self.speed_combo.currentText()})' if kind == 'replay' else ''))
        if kind == 'serial':
            self.link_up = True                     # the board itself: no bridge in between
            if self.raw_check.isChecked():
                self.send(P.CMD_RAW_START)
        elif kind in ('bridge', 'demo'):
            self.pending_auto_connect = self.auto_connect.isChecked()
        self._update_controls()

    def close_transport(self) -> None:
        if self.transport is None:
            return
        self.transport.close()
        self.log(f'closed {self.transport.port}')
        self._transport_gone()

    def _transport_gone(self) -> None:
        self.transport = None
        self.link_up = False
        self.session.bridge_state = None
        self._update_controls()
        self._update_status(time.monotonic())

    def send(self, line: str) -> None:
        if self.transport is None or self.kind == 'replay':
            return
        self.transport.send(line)
        self.log('-> ' + line)

    def _update_controls(self) -> None:
        is_open = self.transport is not None
        kind = self.kind if is_open else self.transport_combo.currentData()
        replay = kind == 'replay'
        self.transport_combo.setEnabled(not is_open)
        for w in (self.port_label, self.port_combo, self.refresh_btn):
            w.setVisible(not replay)
        self.port_combo.setEnabled(not is_open and kind != 'demo')
        self.refresh_btn.setEnabled(not is_open and kind != 'demo')
        self.speed_label.setVisible(replay)
        self.speed_combo.setVisible(replay)
        self.speed_combo.setEnabled(not is_open)
        self.auto_connect.setVisible(not replay)
        self.open_btn.setText('Close' if is_open else ('Choose file...' if replay else 'Open'))
        bridge = is_open and kind in ('bridge', 'demo')
        for w in (self.device_combo, self.scan_btn, self.connect_btn, self.disconnect_btn):
            w.setEnabled(bridge)
        self.selftest_btn.setEnabled(is_open and not replay)
        self.record_btn.setEnabled(self.recorder is not None or not (is_open and replay))

    # ------------------------------------------------------------------------------------------------
    # Incoming data
    # ------------------------------------------------------------------------------------------------
    def _poll(self) -> None:
        for _ in range(2000):
            try:
                item = self.inbox.get_nowait()
            except queue.Empty:
                break
            kind = item[0]
            if kind == 'msg':
                self._handle(item[1], item[2], item[3])
            elif kind == 'truth':
                self.set_truth(item[1], t=item[2], replayed=True)
            elif kind == 'info':
                self.log(item[1])
            elif kind == 'error':
                self.log(item[1], error=True)
            elif kind == 'closed' and self.transport is not None:
                self.transport.close()              # port lost or replay finished: release it
                self._transport_gone()
        now = self.now()
        if time.monotonic() - self.last_status > 0.25:
            self._update_status(now)
        if self.plot_dirty and time.monotonic() - self.last_plot > 0.1:
            self._update_plot()

    def _handle(self, msg: P.Message, t: float, line: str) -> None:
        previous = self.session.bridge_state
        for text in self.session.handle(msg, t):
            self.log(text)
        if self.recorder is not None and t >= self.recorder.t_start:   # not lines from before the click
            self.recorder.line(t, line)
            if isinstance(msg, P.Live):
                self.recorder.live(msg, t, self.session.truth.at(t), self.session.last_truth_raw,
                                   self.session.last_truth_smooth)
            elif isinstance(msg, P.Raw):
                self.recorder.raw(msg, t, lambda t_ms: self.session.sample_truth(t_ms, t))
        if isinstance(msg, P.Live):
            self._show_live(msg)
            self._show_match()
            self._update_timeline()
            self._update_totals()
            self._update_confusion()
        elif isinstance(msg, P.Raw):
            self.plot_dirty = True
        elif isinstance(msg, P.BridgeState):
            self._bridge_state(msg, previous)
        elif isinstance(msg, P.BridgeFound):
            self._add_found(msg)

    def _bridge_state(self, msg: P.BridgeState, previous) -> None:
        was_up = self.link_up
        self.link_up = msg.state == 'connected'
        if self.link_up and not was_up:
            if self.raw_check.isChecked():
                self.send(P.CMD_RAW_START)          # the device forgets it when it restarts
            if msg.address and self.kind not in ('demo', 'replay'):
                self.settings.setValue('device', msg.address)
                self.settings.setValue('device_name', msg.name)
        if self.pending_auto_connect:
            self.pending_auto_connect = False
            if msg.state == 'idle':
                self.connect_device()

    def _show_live(self, p: P.Live) -> None:
        s = p.cls_smooth
        v = p.version
        if s == P.CLASS_NONE:
            self._set_big('starting', P.NONE_COLOUR)
        else:
            label = P.class_label(s, v)
            if v >= 2 and s == 3:
                label = f'still ({P.posture_name(p.still_posture)})'
            self._set_big(label, P.class_colour(s, v))
        if v >= 2:
            self.upright.setText(f'protocol v{v}, 4 s window; posture: {P.posture_name(p.still_posture)}')
        else:
            self.upright.setText('upright and still' if s in P.UPRIGHT_AND_STILL else '')
        raw = P.class_label(p.raw_cls if v >= 2 else p.cls, v)
        self.raw_label.setText(f'raw class (newest window, before the stairs rule): {raw}' if v >= 2
                               else f'raw class (newest window): {raw}')
        self.vote_label.setText('vote share: ' + (f'{p.vote_pct} % of the trees' if p.vote_pct != P.VOTE_NONE
                                                   else 'not available'))
        mag = float(np.linalg.norm(p.mean_mg))
        self.mean_label.setText(f'mean of the window: x {p.mean_mg[0]:+d}  y {p.mean_mg[1]:+d}  '
                                f'z {p.mean_mg[2]:+d} mg   (|a| {mag:.0f} mg)')
        selftest = 'passed' if p.selftest_ok else '<b style="color:#d93025">FAILED</b>'
        self.flags_label.setText(f'self-test: {selftest} &nbsp; input low-pass: {"on" if p.lowpass else "off"} '
                                 f'&nbsp; model: {"single tree" if p.small_model else "forest"}')
        self._show_summary()

    def _show_summary(self) -> None:
        """The latest daily summary (format 2), decoded."""
        s = self.session.summary
        if s is None:
            self.summary_label.setText('daily summary: not received yet')
            return
        ev = (f'sit-to-stand {s.n_sit_to_stand}, stand-to-sit {s.n_stand_to_sit}, '
              f'bed transfer {s.n_bed_transfer}')
        self.summary_label.setText(
            f'<b>Daily summary</b> (format {s.format}, {s.n_classes} classes): '
            f'walking {s.minutes_by_class[0]} min, stairs {s.minutes_by_class[1]}/{s.minutes_by_class[2]} min, '
            f'still {s.minutes_still} min (sit {s.minutes_sitting} / stand {s.minutes_standing} / '
            f'unknown {s.minutes_still_unknown}), lying {s.minutes_by_class[4]} min<br>{ev}, '
            f'mean sit-to-stand {s.sts_seconds_x10 / 10:.1f} s, steps {s.steps}, '
            f'cadence {s.cadence_x10 / 10:.1f}/min, seated tilt {s.sit_tilt_x10 / 10:.1f} deg')

    def _set_big(self, text: str, colour: str) -> None:
        self.big.setText(text)
        self.big.setStyleSheet(f'background: {colour}; color: {_text_on(colour)}; border-radius: 10px; '
                               f'padding: 12px;')

    def _update_status(self, now: float) -> None:
        self.last_status = time.monotonic()
        s = self.session
        st = s.bridge_state
        if self.transport is None:
            state = '<span style="color:#777777">closed</span>'
        elif self.kind == 'serial':
            state = f'reading {self.transport.port} directly'
        elif self.kind == 'replay':
            state = f'replaying {html.escape(self.transport.port)} at {self.speed_combo.currentText()}'
        elif st is None:
            state = 'waiting for the bridge...'
        else:
            colour = STATE_COLOURS.get(st.state, '#000000')
            who = f'&nbsp; {html.escape(st.name)} &nbsp; {st.address}' if st.address else ''
            rssi = f'&nbsp; RSSI {st.rssi} dBm' if st.rssi else ''
            mtu = f'&nbsp; MTU {st.mtu}' if st.mtu else ''
            state = f'<b style="color:{colour}">{st.state}</b>{who}{rssi}{mtu}'
        self.state_label.setText(state)
        self.stats_label.setText(
            f'LIVE {s.live_rate.rate(now):.1f}/s   RAW {s.raw_rate.rate(now):.1f}/s   '
            f'lost packets: LIVE {s.live_seq.lost}, RAW {s.raw_seq.lost}   bridge drops: {s.bridge_drops}')
        if self.transport is not None and s.last_live_time is not None and now - s.last_live_time > STALE_S:
            self.age_label.setText(f'no new data for {now - s.last_live_time:.0f} s')
        else:
            self.age_label.setText('')
        if self.recorder is not None:
            self.recorder.flush()
            elapsed = _duration(time.monotonic() - self.rec_started)
            self.rec_label.setText(f'<span style="color:#d93025">&#9679; recording {elapsed} '
                                   f'({self.recorder.n_live} windows)</span>')
            self.rec_indicator.setText(f'<span style="color:#d93025">&#9679; REC {elapsed}</span>')
        else:
            self.rec_label.setText('')
            self.rec_indicator.setText('')

    def _update_plot(self) -> None:
        self.plot_dirty = False
        self.last_plot = time.monotonic()
        view = self.session.raw.view()
        if view is None:
            for c in self.curves + self.lp_curves:
                c.setData([], [])
            self.no_data.show()
            return
        t, xyz, mag, lp, lp_mag = view
        self.no_data.hide()
        for k in range(3):
            self.curves[k].setData(t, xyz[:, k], connect='finite')
            self.lp_curves[k].setData(t, lp[:, k], connect='finite')
        self.curves[3].setData(t, mag, connect='finite')
        self.lp_curves[3].setData(t, lp_mag, connect='finite')

    def _curve_visibility(self) -> None:
        for k, (c, lp) in enumerate(zip(self.curves, self.lp_curves)):
            show = k < 3 or self.mag_check.isChecked()
            c.setVisible(show)
            lp.setVisible(show and self.lp_check.isChecked())

    def _update_timeline(self) -> None:
        classes = self.session.timeline.array()
        rgba = _lut(self.session.version)[classes][np.newaxis, :, :]
        self.tl_img.setImage(rgba, autoLevels=False, levels=(0, 255))
        n = len(classes)
        self.tl_img.setRect(QtCore.QRectF(-n, 0, n, 1))
        self._update_legend()

    def _update_totals(self) -> None:
        t = self.session.totals
        total = t.total
        values = [t.group_seconds(classes) for _, classes in P.GROUPS] + list(t.seconds)
        for r, sec in enumerate(values):
            self.totals_table.item(r, 1).setText(_duration(sec))
            self.totals_table.item(r, 2).setText(f'{100.0 * sec / total:.0f} %' if total else '')
        self.transitions_label.setText(f'Sit/stand transitions: {t.transitions}   (total {_duration(total)})')

    def _update_confusion(self) -> None:
        conf = self.session.conf_raw if self.compare_combo.currentData() == 'raw' else self.session.conf_smooth
        m = conf.m
        self.conf_table.setColumnHidden(P.DEVICE_STILL_UNKNOWN, self.session.version < 2)
        self.acc_label.setText(
            f'walking: {_pct(conf.walking())} &nbsp;&nbsp; upright and still: {_pct(conf.upright_and_still())}'
            f'<br>sitting: {_pct(conf.sitting())} &nbsp;&nbsp; standing: {_pct(conf.standing())} '
            f'(known posture only) &nbsp;&nbsp; posture unknown: {_pct(conf.posture_unknown())} of the still windows'
            f'<br>overall: {_pct(conf.overall())}; with sitting and standing as one: '
            f'{_pct(conf.overall(merge_sit_stand=True))}')
        for r in range(m.shape[0]):
            row_total = int(m[r].sum())
            for c in range(m.shape[1]):
                n = int(m[r, c])
                cell = self.conf_table.item(r, c)
                if n == 0:
                    cell.setText('')
                    cell.setData(QtCore.Qt.ItemDataRole.BackgroundRole, None)
                    cell.setData(QtCore.Qt.ItemDataRole.ForegroundRole, None)
                    continue
                cell.setText(f'{n} ({100.0 * n / row_total:.0f}%)')
                if r == c:
                    colour = '#c8f0c8'
                elif r in P.UPRIGHT_AND_STILL and c in P.UPRIGHT_AND_STILL:
                    colour = '#cfe5fb'
                elif c == P.DEVICE_STILL_UNKNOWN and r in P.UPRIGHT_AND_STILL:
                    colour = '#e8e0f8'
                elif r == TRUTH_OTHER:
                    colour = '#e6e6e6'
                else:
                    colour = '#f8d0cc'
                cell.setBackground(QtGui.QColor(colour))
                cell.setForeground(QtGui.QColor('#000000'))

    def _refresh_all(self) -> None:
        self._set_big('no data', P.NONE_COLOUR)
        for w in (self.upright, self.raw_label, self.vote_label, self.mean_label, self.flags_label, self.age_label):
            w.setText('')
        self._show_truth()
        self._update_timeline()
        self._update_totals()
        self._update_confusion()
        self.plot_dirty = True

    def _reset_totals(self) -> None:
        self.session.totals.reset()
        self._update_totals()
        self.log('totals reset')

    def _reset_confusion(self) -> None:
        self.session.conf_raw.reset()
        self.session.conf_smooth.reset()
        self._update_confusion()
        self.log('accuracy table reset')

    # ------------------------------------------------------------------------------------------------
    # Ground truth
    # ------------------------------------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if (event.type() == QtCore.QEvent.Type.KeyPress and not event.isAutoRepeat()
                and QtWidgets.QApplication.activeWindow() is self and event.text() in KEY_TO_TRUTH
                and not (event.modifiers() & (QtCore.Qt.KeyboardModifier.ControlModifier |
                                              QtCore.Qt.KeyboardModifier.AltModifier))):
            self.set_truth(KEY_TO_TRUTH[event.text()])
            return True
        return super().eventFilter(obj, event)

    def set_truth(self, code: int, t: Optional[float] = None, replayed: bool = False) -> None:
        t = self.now() if t is None else t
        if code == self.session.truth.current:
            return
        self.session.truth.set(code, t)
        if self.recorder is not None and not replayed:
            self.recorder.truth(t, code)
        self.log(f'truth: {truth_label(code)}' + (' (recorded)' if replayed else ''))
        self._show_truth()
        self._show_match()

    def _show_truth(self) -> None:
        code = self.session.truth.current
        if code is None:
            self.truth_label.setText('truth: not set yet')
            self.truth_label.setStyleSheet('')
            return
        colour = _truth_colour(code)
        self.truth_label.setText(f'truth: {truth_label(code)}  (key {KEY_FOR_TRUTH[code]})')
        self.truth_label.setStyleSheet(f'background: {colour}; color: {_text_on(colour)}; padding: 4px 8px; '
                                       f'border-radius: 6px;')

    def _show_match(self) -> None:
        truth = self.session.truth.current
        p = self.session.last_live
        if truth is None or p is None or p.cls_smooth == P.CLASS_NONE:
            self.match_label.setText('')
        elif truth == TRUTH_OTHER:
            self.match_label.setText('truth is "other": not compared')
        elif p.cls_smooth == truth:
            self.match_label.setText('<b style="color:#1e8e3e">&#10003; the device agrees</b>')
        elif truth in P.UPRIGHT_AND_STILL and p.cls_smooth in P.UPRIGHT_AND_STILL:
            self.match_label.setText('<b style="color:#1a73e8">&#10003; upright and still</b> '
                                     '(sitting and standing swapped)')
        else:
            self.match_label.setText(f'<b style="color:#d93025">&#10007; the device says '
                                     f'{P.class_label(p.cls_smooth)}</b> (it is about 2 s behind)')

    # ------------------------------------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------------------------------------
    def toggle_recording(self) -> None:
        if self.recorder is not None:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self) -> None:
        try:
            self.recorder = Recorder(self.rec_folder(), self.now())
        except OSError as exc:
            QtWidgets.QMessageBox.warning(self, 'Cannot record', f'Cannot create the files in {self.rec_folder()}: '
                                                                 f'{exc}')
            return
        self.rec_started = time.monotonic()
        if self.session.truth.current is not None:
            self.recorder.truth(self.now(), self.session.truth.current)
        self.record_btn.setText('Stop recording')
        self.record_btn.setStyleSheet('color: #d93025; font-weight: bold;')
        self.log(f'recording started: {self.recorder.base}_live.csv, _raw.csv, _session.txt')
        self._update_controls()

    def stop_recording(self) -> None:
        if self.recorder is None:
            return
        rec, self.recorder = self.recorder, None
        rec.close()
        self.record_btn.setText('Start recording')
        self.record_btn.setStyleSheet('')
        self.log(f'recording saved: {rec.name} ({rec.n_live} LIVE rows, {rec.n_raw} RAW samples) in '
                 f'{os.path.dirname(rec.base)}')
        self._update_controls()

    def choose_folder(self) -> None:
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, 'Folder for the recordings', self.rec_folder())
        if folder:
            self.settings.setValue('rec_folder', folder)
            self.folder_btn.setToolTip('Recordings are saved in ' + folder)
            self.log(f'recordings will be saved in {folder}')

    # ------------------------------------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------------------------------------
    def _raw_toggled(self, on: bool) -> None:
        if self.transport is not None and self.link_up:
            self.send(P.CMD_RAW_START if on else P.CMD_RAW_STOP)
        if not on:
            self.session.raw.clear()
            self.plot_dirty = True

    def run_selftest(self) -> None:
        self.send(P.CMD_SELFTEST)
        self.log('self-test requested: the device answers with an I line, and the LIVE flags show the result')

    def log(self, text: str, error: bool = False) -> None:
        stamp = time.strftime('%H:%M:%S')
        colour = '#d93025' if error else ''
        style = f' style="color:{colour}"' if colour else ''
        self.log_view.appendHtml(f'<span{style}>{stamp}  {html.escape(text)}</span>')
        self.status_text.setText(f'{stamp}  {text}')
        self.status_text.setStyleSheet(f'color: {colour};' if colour else '')
