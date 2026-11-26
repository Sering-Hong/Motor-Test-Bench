"""运行日志与未捕获异常处理。日志位于 %LOCALAPPDATA%\\MotorBench\\logs，按大小轮转。"""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path
import sys
import threading
import traceback

log = logging.getLogger("motorbench")


def log_dir():
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    path = base / "MotorBench" / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def setup_logging(level=logging.INFO):
    path = log_dir() / "motorbench.log"
    if not log.handlers:
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
        log.addHandler(handler)
    log.setLevel(level)
    return path


def install_excepthook():
    def show(text):
        from PyQt5.QtCore import QThread, QTimer
        from PyQt5.QtWidgets import QApplication, QMessageBox
        app = QApplication.instance()
        if app is None or QThread.currentThread() is not app.thread():
            return

        def popup():
            box = QMessageBox(QMessageBox.Critical, "程序错误",
                              "发生未处理的错误，已写入日志。\n若电机正在输出，请先按 Esc 停止输出并确认安全。",
                              QMessageBox.Ok, app.activeWindow())
            box.setDetailedText(text)
            box.exec()
        QTimer.singleShot(0, popup)

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error("未捕获异常\n%s", text)
        if sys.__stderr__:
            sys.__stderr__.write(text)
        show(text)

    def thread_hook(args):
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log.error("线程 %s 未捕获异常\n%s", getattr(args.thread, "name", "?"), text)

    sys.excepthook = hook
    threading.excepthook = thread_hook
