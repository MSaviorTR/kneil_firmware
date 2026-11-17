"""KNEIL live viewer: start the PC app.

    python kneil_viewer.py              normal start
    python kneil_viewer.py --demo       simulated data, no hardware needed
    python kneil_viewer.py --port COM4  open this port at start
(The same options work for KNEIL_viewer.exe.)
"""
import argparse
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

from PySide6 import QtWidgets

from kneil import VERSION
from kneil.ui import MainWindow

ERROR_LOG = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'KNEIL' / 'viewer_errors.log'


def _write_error(text: str) -> None:
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ERROR_LOG.open('a', encoding='utf-8') as f:
            f.write(f'--- {datetime.now().isoformat(timespec="seconds")} (viewer {VERSION})\n{text}\n')
    except OSError:
        pass


def _install_error_handlers() -> None:
    """The .exe has no console: unexpected errors are shown in a message box and saved in ERROR_LOG."""
    def on_error(exc_type, exc, tb):
        _write_error(''.join(traceback.format_exception(exc_type, exc, tb)))
        if QtWidgets.QApplication.instance() is not None:
            QtWidgets.QMessageBox.critical(None, 'KNEIL live viewer: unexpected error',
                                           f'{exc_type.__name__}: {exc}\n\nThe details are saved in\n{ERROR_LOG}')

    def on_thread_error(args):                      # no message box from a background thread
        _write_error(''.join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))

    sys.excepthook = on_error
    threading.excepthook = on_thread_error


def main() -> int:
    parser = argparse.ArgumentParser(description=f'KNEIL live viewer {VERSION}')
    parser.add_argument('--demo', action='store_true', help='start with simulated data (no hardware)')
    parser.add_argument('--port', help='open this COM port at start, for example COM4')
    args = parser.parse_args()

    _install_error_handlers()
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName('KNEIL live viewer')
    window = MainWindow(demo=args.demo, port=args.port)
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
