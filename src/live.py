"""实车调参的探针线程：SWD 只读附加到正在运行的整车固件（不烧录、不复位、不暂停内核），
按设定频率批量读取监视变量，并执行带读回校验的内存写入。"""
from __future__ import annotations

import logging
import queue
import threading
import time

from PyQt5.QtCore import QObject, pyqtSignal

from elfvars import extract, plan_blocks
from model import BOARDS, SWD_RATES_KHZ
from probe import BUNDLE
from probe_backend import ProbeBackend

log = logging.getLogger("motorbench.live")
LIVE_RATES_HZ = (50, 100, 200, 500, 1000)
EMIT_INTERVAL = 0.04


class LiveSignals(QObject):
    probes = pyqtSignal(list)
    link = pyqtSignal(bool, str)             # 已连接, 说明
    notice = pyqtSignal(str, bool)           # 文本, 是否错误
    firmware = pyqtSignal(object)            # None=未比对 / True / False
    samples = pyqtSignal(int, object)        # 监视代号, [(t, [原始字节...])]
    stats = pyqtSignal(float, float)         # 实际采样率 Hz, 单次读取耗时 ms
    written = pyqtSignal(int, bool, str)     # 请求号, 成功, 说明
    peeked = pyqtSignal(int, object)         # 请求号, 原始字节或 None


class LiveWorker(threading.Thread):
    def __init__(self, executable=None):
        super().__init__(daemon=True, name="LiveProbe")
        self.signals = LiveSignals()
        self.requests = queue.Queue()
        self.backend = ProbeBackend(executable or BUNDLE / "motor-bench-probe.exe")
        self.connected = False
        self.board = None
        self.interval = 1 / 200
        self.items = []
        self.blocks = []
        self.placement = []
        self.generation = 0
        self.pending_rows = []
        self.last_emit = 0.0
        self.last_poll = 0.0
        self.rate_window = []
        self.read_ms = 0.0
        self.flash_chunks = []

    def send(self, action, *data):
        self.requests.put((action, data))

    # ---------------- 主循环 ----------------
    def run(self):
        while True:
            wait = None
            if self.connected and self.items:
                wait = max(0.0, self.last_poll + self.interval - time.monotonic())
            try:
                action, data = self.requests.get(timeout=wait)
            except queue.Empty:
                action, data = None, ()
            try:
                if action == "quit":
                    self.close()
                    self.backend.stop()
                    return
                if action is not None:
                    getattr(self, f"do_{action}")(*data)
                if self.connected and self.items and time.monotonic() >= self.last_poll + self.interval:
                    self.poll()
            except Exception as error:  # noqa: BLE001
                log.exception("实车调参 %s 失败", action or "采样")
                self.signals.notice.emit(str(error), True)
                if action in (None, "connect") or not self.backend.process:
                    self.close()

    # ---------------- 动作 ----------------
    def do_list(self):
        self.backend.start()
        self.signals.probes.emit(self.backend.request("list", timeout=10))

    def do_connect(self, board_index, probe_index, swd_khz, rate_hz):
        if swd_khz not in SWD_RATES_KHZ:
            raise ValueError("无效的 SWD 频率")
        self.close()
        self.board = BOARDS[board_index]
        self.set_rate(rate_hz)
        self.backend.start()
        try:
            info = self.backend.request("open", timeout=15, index=probe_index, chip=self.board.target, speed=swd_khz)
        except RuntimeError as error:
            raise RuntimeError(f"连接 {self.board.name} 失败：{error}。确认探针没被 Keil/Ozone/MemRW3 占用。") from error
        self.connected = True
        chip = self.backend.read32(self.board.id_register) & 0xfff
        if chip != self.board.device_id:
            self.close()
            raise RuntimeError(f"芯片 ID 0x{chip:03X} 与 {self.board.name} 不符，请核对板型")
        self.signals.link.emit(True, f"已附加 {self.board.name} · SWD {info['speed']} kHz（未复位、未暂停）")
        self.check_firmware()

    def do_disconnect(self):
        self.close()

    def do_rate(self, hz):
        self.set_rate(hz)

    def do_flash_chunks(self, chunks):
        self.flash_chunks = list(chunks)
        if self.connected:
            self.check_firmware()

    def do_watch(self, generation, items):
        """items: [(地址, 字节数)]。换监视列表时 generation 递增，旧批次样本被界面丢弃。"""
        self.flush(force=True)
        self.generation = generation
        self.items = list(items)
        self.blocks, self.placement = plan_blocks(self.items) if self.items else ([], [])
        self.rate_window.clear()

    def do_write(self, request, address, data):
        if not self.connected:
            self.signals.written.emit(request, False, "未连接")
            return
        self.backend.request("write_bytes", address=address, bytes=list(data))
        start = address & ~3
        words = ((address + len(data) + 3) & ~3) - start
        raw = self.read_bytes(start, words // 4)
        readback = raw[address - start: address - start + len(data)]
        ok = readback == bytes(data)
        self.signals.written.emit(request, ok, "已写入并读回一致" if ok else "读回值与写入值不同（可能被程序立即改写）")

    def do_peek(self, request, address, size):
        if not self.connected:
            self.signals.peeked.emit(request, None)
            return
        start = address & ~3
        raw = self.read_bytes(start, (((address + size + 3) & ~3) - start) // 4)
        self.signals.peeked.emit(request, raw[address - start: address - start + size])

    # ---------------- 内部 ----------------
    def set_rate(self, hz):
        if hz not in LIVE_RATES_HZ:
            raise ValueError("无效的采样率")
        self.interval = 1 / hz

    def read_bytes(self, address, words):
        values = self.backend.request("read", address=address, count=words)
        return b"".join(int(v).to_bytes(4, "little") for v in values)

    def check_firmware(self):
        if not self.connected or not self.flash_chunks:
            self.signals.firmware.emit(None)
            return
        for address, expected in self.flash_chunks:
            if self.read_bytes(address, len(expected) // 4) != expected:
                self.signals.firmware.emit(False)
                return
        self.signals.firmware.emit(True)

    def poll(self):
        now = time.monotonic()
        self.last_poll = now if now - self.last_poll > self.interval * 3 else self.last_poll + self.interval
        begin = time.perf_counter()
        raw = [self.read_bytes(address, words) for address, words in self.blocks]
        stamp = time.monotonic()
        self.read_ms = (time.perf_counter() - begin) * 1000
        self.pending_rows.append((stamp, extract(raw, self.placement, self.items)))
        self.rate_window.append(stamp)
        while self.rate_window and stamp - self.rate_window[0] > 1.0:
            self.rate_window.pop(0)
        self.flush()

    def flush(self, force=False):
        now = time.monotonic()
        if self.pending_rows and (force or now - self.last_emit >= EMIT_INTERVAL):
            self.signals.samples.emit(self.generation, self.pending_rows)
            self.pending_rows = []
            self.last_emit = now
            rate = (len(self.rate_window) - 1) / max(1e-3, self.rate_window[-1] - self.rate_window[0]) \
                if len(self.rate_window) > 1 else 0.0
            self.signals.stats.emit(rate, self.read_ms)

    def close(self):
        self.pending_rows = []
        if self.connected:
            try:
                if self.backend.process is not None:
                    self.backend.request("close")
            except RuntimeError:
                self.backend.stop()
        self.connected = False
        self.signals.link.emit(False, "未连接")
