"""实车调参页：SWD 附加到正在运行的整车固件，按 ELF 变量名监视、画曲线、在线改 PID 并做阶跃评估。

安全约定：
- 默认只读。写内存必须先打开「允许写入」并确认；板上程序与 ELF 不一致时禁止写入。
- 断开连接自动关闭写入。Esc 只能中止本软件发起的阶跃并把目标写回原值，不能代替整车急停。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import time

from PyQt5.QtCore import QFileSystemWatcher, QSettings, Qt, QTimer
from PyQt5.QtGui import QColor, QIcon, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView, QButtonGroup, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton,
    QStackedWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

import applog
from elfvars import BASE_TYPES, CType, ElfImage, Leaf, decode, encode, manual_type
from live import LIVE_RATES_HZ
from loop_panel import LoopPanel
from model import BOARDS, SWD_RATES_KHZ
from scope import Scope

log = logging.getLogger("motorbench.live")
COLORS = ("#e0457b", "#3f8fd8", "#13a08f", "#e08a2e", "#9c6ade", "#6b8e23", "#d9467b", "#008b8b", "#b8860b", "#5c6bc0")
STEPS = ("连接探针", "加载 ELF", "选变量", "建调参回路", "调参与阶跃")


def label(text, kind=""):
    widget = QLabel(text)
    widget.setObjectName(kind)
    return widget


def button(text, kind=""):
    widget = QPushButton(text)
    widget.setObjectName(kind)
    return widget


def box(name, layout_type=QVBoxLayout, margin=12):
    widget = QFrame()
    widget.setObjectName(name)
    layout = layout_type(widget)
    layout.setContentsMargins(*[margin] * 4)
    layout.setSpacing(8)
    return widget, layout


def row(*widgets, stretch=None):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for index, child in enumerate(widgets):
        layout.addWidget(child, 1 if index == stretch else 0)
    return widget


def tabs(names):
    widget, layout = box("nav", QHBoxLayout, 4)
    group = QButtonGroup(widget)
    for index, name in enumerate(names):
        tab = button(name)
        tab.setProperty("tab", True)
        tab.setCheckable(True)
        group.addButton(tab, index)
        layout.addWidget(tab)
    group.button(0).setChecked(True)
    return widget, group


def repolish(widget, name, value):
    if widget.property(name) != value:
        widget.setProperty(name, value)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def format_value(leaf, value):
    if value is None:
        return "—"
    ctype = leaf.ctype
    if ctype.kind == "pointer":
        return f"0x{int(value):08X}"
    if ctype.kind == "enum" and int(value) in ctype.enumerators:
        return f"{ctype.enumerators[int(value)]} ({int(value)})"
    if isinstance(value, float):
        return f"{value:.6g}" if math.isfinite(value) else str(value)
    return str(value)


def color_icon(color):
    pixmap = QPixmap(12, 12)
    pixmap.fill(QColor(color))
    return QIcon(pixmap)


@dataclass
class WatchEntry:
    path: str
    leaf: Leaf | None
    plot: bool
    color: str
    manual: str = ""        # 手动添加时的类型名


class LivePage(QWidget):
    def __init__(self, worker, preferences: QSettings, notify):
        super().__init__()
        self.worker = worker
        self.preferences = preferences
        self.notify_main = notify
        self.image = None
        self.entries: list[WatchEntry] = []
        self.latest = {}
        self.generation = 0
        self.connected = False
        self.firmware_ok = None
        self.armed = False
        self.write_confirmed = False
        self.requests = 0
        self.write_callbacks = {}
        self.peek_callbacks = {}
        self.scale = 1.0
        self.save_timer = QTimer(self, singleShot=True, interval=600)
        self.save_timer.timeout.connect(self.save_project)
        self.search_timer = QTimer(self, singleShot=True, interval=220)
        self.search_timer.timeout.connect(self.run_search)
        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.elf_changed)
        self.build()
        self.bind()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_values)
        self.refresh_timer.start(100)
        last = self.preferences.value("live/elf", "", type=str)
        if last and os.path.isfile(last):
            QTimer.singleShot(0, lambda: self.load_elf(last, quiet=True))
        self.update_state()

    # ================= 界面 =================
    def build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)
        bar, grid = box("toolbar", QGridLayout, 10)
        self.board = QComboBox()
        self.board.addItems([b.name for b in BOARDS])
        self.board.setCurrentIndex(min(len(BOARDS) - 1, self.preferences.value("live/board", 0, type=int)))
        self.probe = QComboBox()
        self.probe.addItem("未检测到探针")
        self.probe.setMinimumWidth(150)
        self.refresh = button("刷新探针")
        self.swd = QComboBox()
        for khz in SWD_RATES_KHZ:
            self.swd.addItem(f"SWD {khz} kHz", khz)
        self.swd.setCurrentIndex(max(0, self.swd.findData(self.preferences.value("live/swd", 4000, type=int))))
        self.rate = QComboBox()
        for hz in LIVE_RATES_HZ:
            self.rate.addItem(f"采样 {hz} Hz", hz)
        self.rate.setCurrentIndex(max(0, self.rate.findData(self.preferences.value("live/rate", 200, type=int))))
        self.rate.setToolTip("上位机读取频率。实际值见底部状态；受 SWD 时钟和变量数量限制")
        self.connect_button = button("连接", "primary")
        self.connect_button.setToolTip("只读附加：不烧录、不复位、不暂停整车程序")
        grid.addWidget(row(label("板型", "muted"), self.board, label("探针", "muted"), self.probe, stretch=3), 0, 0)
        grid.addWidget(row(self.refresh, self.swd, self.rate, self.connect_button), 0, 1)
        self.open_elf = button("打开 ELF / AXF…")
        self.reload_elf = button("重新加载")
        self.elf_label = label("未加载固件 ELF", "muted")
        self.firmware_label = label("", "muted")
        self.arm = button("允许写入")
        self.arm.setCheckable(True)
        self.arm.setToolTip("打开后才能改变量；断开连接时自动关闭")
        grid.addWidget(row(self.open_elf, self.reload_elf, self.elf_label, stretch=2), 1, 0)
        grid.addWidget(row(self.firmware_label, self.arm, stretch=0), 1, 1)
        layout.addWidget(bar)

        self.guide, guide = box("guide", QHBoxLayout, 10)
        self.guide_steps = []
        for index, name in enumerate(STEPS):
            if index:
                guide.addWidget(label("›", "muted"))
            step = label(f"{'①②③④⑤'[index]} {name}", "guideStep")
            self.guide_steps.append(step)
            guide.addWidget(step)
        guide.addSpacing(10)
        self.guide_hint = label("", "guideHint")
        self.guide_hint.setWordWrap(True)
        guide.addWidget(self.guide_hint, 1)
        self.guide_action = button("", "guideAction")
        guide.addWidget(self.guide_action)
        layout.addWidget(self.guide)

        self.banner = label("", "inlineNote")
        self.banner.setWordWrap(True)
        self.banner_button = button("重新加载 ELF", "primary")
        self.banner_row = row(self.banner, self.banner_button, stretch=0)
        self.banner_row.layout().setStretch(0, 1)
        self.banner_row.hide()
        layout.addWidget(self.banner_row)

        columns = QHBoxLayout()
        columns.setSpacing(10)
        self.left = self.variable_card()
        self.middle = self.middle_card()
        self.right = self.scope_card()
        columns.addWidget(self.left)
        columns.addWidget(self.middle)
        columns.addWidget(self.right, 1)
        layout.addLayout(columns, 1)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

    def variable_card(self):
        card, layout = box("card")
        layout.addWidget(label("变量", "section"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索：空格分隔关键词，如 yaw kp")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(("名称", "类型"))
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setStretchLastSection(False)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)
        self.add_button = button("加入监视")
        self.manual_button = button("按地址添加…")
        layout.addWidget(row(self.add_button, self.manual_button))
        self.tree_note = label("双击变量加入监视；右键 PID 结构体可一键建调参回路。", "muted")
        self.tree_note.setWordWrap(True)
        layout.addWidget(self.tree_note)
        return card

    def middle_card(self):
        card, layout = box("card")
        nav, self.mid_tabs = tabs(("监视", "回路调参", "调参记录"))
        layout.addWidget(nav)
        self.mid_stack = QStackedWidget()
        watch = QWidget()
        watch_layout = QVBoxLayout(watch)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(("曲线", "名称", "当前值", "类型", "地址"))
        self.table.verticalHeader().hide()
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        watch_layout.addWidget(self.table, 1)
        self.edit_value = button("修改值…")
        self.remove_watch = button("移除所选")
        self.clear_watch = button("清空")
        watch_layout.addWidget(row(self.edit_value, self.remove_watch, self.clear_watch))
        note = label("勾选「曲线」画到下方监视图；双击当前值可修改（需允许写入）。", "muted")
        note.setWordWrap(True)
        watch_layout.addWidget(note)
        self.mid_stack.addWidget(watch)
        self.loop = LoopPanel(self)
        self.mid_stack.addWidget(self.loop)
        self.mid_stack.addWidget(self.loop.history_page)
        layout.addWidget(self.mid_stack, 1)
        return card

    def scope_card(self):
        card, layout = box("card")
        self.window = QComboBox()
        for seconds in (5, 10, 30, 60):
            self.window.addItem(f"最近 {seconds} 秒", seconds)
        self.window.setCurrentIndex(1)
        self.pause = button("暂停")
        self.pause.setCheckable(True)
        self.pause.setToolTip("暂停后可用鼠标拖动、滚轮缩放查看")
        self.auto_y = button("自动 Y")
        self.mark = button("打标记")
        self.export = button("导出 CSV")
        self.clear_scope = button("清空")
        layout.addWidget(row(label("示波器", "section"), QWidget(), self.window, self.pause, self.auto_y, self.mark,
                             self.export, self.clear_scope, stretch=1))
        self.scope = Scope(("回路：目标 / 反馈", "回路：输出", "监视"))
        layout.addWidget(self.scope, 1)
        return card

    def set_scale(self, scale):
        self.scale = scale
        self.left.setFixedWidth(round(330 * scale))
        self.middle.setFixedWidth(round(440 * scale))

    def bind(self):
        sig = self.worker.signals
        sig.probes.connect(self.on_probes)
        sig.link.connect(self.on_link)
        sig.notice.connect(self.notify)
        sig.firmware.connect(self.on_firmware)
        sig.samples.connect(self.on_samples)
        sig.stats.connect(self.on_stats)
        sig.written.connect(self.on_written)
        sig.peeked.connect(self.on_peeked)
        self.refresh.clicked.connect(lambda: self.worker.send("list"))
        self.connect_button.clicked.connect(self.toggle_connection)
        self.rate.currentIndexChanged.connect(self.change_rate)
        self.open_elf.clicked.connect(self.choose_elf)
        self.reload_elf.clicked.connect(lambda: self.image and self.load_elf(self.image.path))
        self.banner_button.clicked.connect(lambda: self.image and self.load_elf(self.image.path))
        self.arm.toggled.connect(self.toggle_arm)
        self.guide_action.clicked.connect(lambda: self.guide_callback and self.guide_callback())
        self.search.textChanged.connect(lambda: self.search_timer.start())
        self.tree.itemExpanded.connect(self.expand_item)
        self.tree.itemDoubleClicked.connect(lambda item, _: self.add_item(item))
        self.tree.customContextMenuRequested.connect(self.tree_menu)
        self.add_button.clicked.connect(lambda: [self.add_item(i) for i in self.tree.selectedItems()])
        self.manual_button.clicked.connect(self.manual_add)
        self.mid_tabs.idClicked.connect(self.mid_stack.setCurrentIndex)
        self.table.itemChanged.connect(self.table_changed)
        self.table.cellDoubleClicked.connect(lambda r, c: c == 2 and self.edit_row(r))
        self.table.customContextMenuRequested.connect(self.table_menu)
        self.edit_value.clicked.connect(lambda: self.table.currentRow() >= 0 and self.edit_row(self.table.currentRow()))
        self.remove_watch.clicked.connect(self.remove_selected)
        self.clear_watch.clicked.connect(self.clear_entries)
        self.window.currentIndexChanged.connect(lambda: setattr(self.scope, "window", float(self.window.currentData())))
        self.pause.toggled.connect(self.scope.set_paused)
        self.auto_y.clicked.connect(self.scope.auto_y)
        self.mark.clicked.connect(lambda: self.scope.now() is not None and self.scope.add_marker(self.scope.now(), "标记"))
        self.export.clicked.connect(self.export_csv)
        self.clear_scope.clicked.connect(self.scope.clear)

    # ================= 连接 =================
    def toggle_connection(self):
        if self.connected:
            self.worker.send("disconnect")
            return
        if self.probe.currentData() is None:
            self.worker.send("list")
            return
        self.preferences.setValue("live/board", self.board.currentIndex())
        self.preferences.setValue("live/swd", self.swd.currentData())
        self.preferences.setValue("live/rate", self.rate.currentData())
        self.connect_button.setEnabled(False)
        self.connect_button.setText("连接中…")
        self.worker.send("connect", self.board.currentIndex(), self.probe.currentData(), self.swd.currentData(),
                         self.rate.currentData())

    def change_rate(self):
        self.preferences.setValue("live/rate", self.rate.currentData())
        self.worker.send("rate", self.rate.currentData())

    def on_probes(self, devices):
        current = self.probe.currentText()
        self.probe.clear()
        for description, uid in devices:
            self.probe.addItem(description, uid)
        if not devices:
            self.probe.addItem("未检测到探针")
        index = self.probe.findText(current)
        self.probe.setCurrentIndex(max(0, index))
        self.update_state()

    def on_link(self, connected, message):
        was = self.connected
        self.connected = connected
        self.connect_button.setEnabled(True)
        self.connect_button.setText("断开" if connected else "连接")
        for widget in (self.board, self.probe, self.refresh, self.swd):
            widget.setEnabled(not connected)
        if not connected:
            self.firmware_ok = None
            self.arm.setChecked(False)
            self.loop.abort_step("连接已断开")
        if connected and not was:
            self.send_watch()
        self.notify(message)
        self.update_state()

    def on_firmware(self, ok):
        self.firmware_ok = ok
        if ok is False:
            self.arm.setChecked(False)
        self.update_state()

    def on_stats(self, rate, read_ms):
        self.status.setText(f"实际采样 {rate:.0f} Hz · 单次读取 {read_ms:.1f} ms · 监视 {len(self.entries)} 个变量"
                            + ("  ·  写入已允许" if self.armed else "  ·  只读"))

    # ================= ELF =================
    def choose_elf(self):
        start = os.path.dirname(self.image.path) if self.image else self.preferences.value("live/elfDir", "", type=str)
        path, _ = QFileDialog.getOpenFileName(self, "选择整车固件", start, "固件调试文件 (*.axf *.elf *.out);;所有文件 (*)")
        if path:
            self.load_elf(path)

    def load_elf(self, path, quiet=False):
        from PyQt5.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            image = ElfImage(path)
        except Exception as error:  # noqa: BLE001
            if not quiet:
                self.notify(f"解析 ELF 失败：{error}", True)
            log.exception("解析 ELF 失败")
            return
        finally:
            QApplication.restoreOverrideCursor()
        reload = self.image is not None and os.path.normcase(self.image.path) == os.path.normcase(image.path)
        if not reload:
            self.save_project()
        self.image = image
        self.preferences.setValue("live/elf", image.path)
        self.preferences.setValue("live/elfDir", os.path.dirname(image.path))
        for watched in self.watcher.files():
            self.watcher.removePath(watched)
        self.watcher.addPath(image.path)
        self.banner_row.hide()
        self.elf_label.setText(f"{Path(image.path).name} · {len(image.variables)} 个变量"
                               + ("" if image.has_dwarf else " · 无调试信息，只有符号表"))
        self.elf_label.setToolTip(image.path)
        self.populate_tree()
        if reload:
            missing = self.reresolve()
            self.notify("已重新加载 ELF" + (f"，{len(missing)} 个变量找不到：{', '.join(missing[:4])}" if missing else "，变量地址已按名字更新"),
                        bool(missing))
        else:
            self.load_project()
            self.notify(f"已加载 {Path(image.path).name}")
        self.worker.send("flash_chunks", image.flash_chunks)
        self.send_watch()

    def elf_changed(self, path):
        if os.path.isfile(path) and path not in self.watcher.files():
            self.watcher.addPath(path)
        elif not os.path.isfile(path):
            QTimer.singleShot(1000, lambda: os.path.isfile(path) and self.watcher.addPath(path))
        self.banner.setText("检测到固件 ELF 已重新编译。下载到板子后点「重新加载 ELF」，监视变量会按名字重新定位。")
        self.banner_row.show()

    def reresolve(self):
        missing = []
        for entry in self.entries:
            if entry.manual:
                continue
            if "->" in entry.path or entry.path.startswith("*"):
                continue  # 指针路径保留添加时解析出的地址
            leaf = self.image.resolve(entry.path)
            entry.leaf = leaf
            if leaf is None:
                missing.append(entry.path)
        self.rebuild_table()
        return missing

    # ================= 变量树 =================
    def populate_tree(self):
        self.tree.clear()
        if self.image is None:
            return
        if self.search.text().strip():
            self.run_search()
            return
        for variable in self.image.variables:
            self.tree.addTopLevelItem(self.make_item(variable.name, variable.address, variable.ctype, variable.ctype.leaf))

    def make_item(self, path, address, ctype, watchable):
        name = path if "." not in path and "->" not in path and "[" not in path else \
            path.split("->")[-1].split(".")[-1] if not path.endswith("]") else path[path.rfind("["):]
        item = QTreeWidgetItem((name, ctype.display))
        item.setData(0, Qt.UserRole, (path, address, ctype, watchable))
        item.setToolTip(0, f"{path}\n地址 0x{address:08X} · {ctype.display} · {ctype.size} 字节")
        expandable = ctype.kind in ("struct", "union", "array") or (ctype.kind == "pointer" and ctype.element is not None)
        if expandable:
            item.addChild(QTreeWidgetItem(("…",)))
        if not watchable and not expandable:
            item.setForeground(0, QColor("#999999"))
        return item

    def expand_item(self, item):
        if item.childCount() != 1 or item.child(0).data(0, Qt.UserRole) is not None or item.child(0).text(0) not in ("…", "连接后可展开指针"):
            return
        path, address, ctype, _ = item.data(0, Qt.UserRole)
        if ctype.kind == "pointer":
            if not self.connected:
                item.child(0).setText(0, "连接后可展开指针")
                return
            self.peek(address, 4, lambda raw, it=item: self.fill_pointer(it, raw))
            item.child(0).setText(0, "读取指针…")
            return
        item.takeChildren()
        for child in ElfImage.children(path, address, ctype):
            item.addChild(self.make_item(child[0], child[1], child[2], child[3]))

    def fill_pointer(self, item, raw):
        item.takeChildren()
        path, address, ctype, _ = item.data(0, Qt.UserRole)
        pointer = int.from_bytes(raw, "little") if raw else 0
        if not pointer:
            item.addChild(QTreeWidgetItem(("空指针（程序可能还没初始化）",)))
            return
        for child in ElfImage.deref_children(path, pointer, ctype):
            item.addChild(self.make_item(child[0], child[1], child[2], child[3]))

    def run_search(self):
        text = self.search.text().strip()
        if self.image is None:
            return
        if not text:
            self.tree.clear()
            for variable in self.image.variables:
                self.tree.addTopLevelItem(self.make_item(variable.name, variable.address, variable.ctype, variable.ctype.leaf))
            return
        results = self.image.search(text)
        self.tree.clear()
        for leaf in results:
            item = self.make_item(leaf.path, leaf.address, leaf.ctype, True)
            item.setText(0, leaf.path)
            self.tree.addTopLevelItem(item)
        if not results:
            self.tree.addTopLevelItem(QTreeWidgetItem(("没有匹配的变量",)))

    def tree_menu(self, position):
        item = self.tree.itemAt(position)
        data = item.data(0, Qt.UserRole) if item else None
        if not data:
            return
        path, address, ctype, watchable = data
        menu = QMenu(self)
        if watchable:
            menu.addAction("加入监视", lambda: self.add_item(item))
            roles = menu.addMenu("设为当前回路的")
            for role, name in self.loop.ROLE_NAMES:
                roles.addAction(name, lambda r=role: self.loop.assign(r, self.add_item(item, quiet=True)))
        if ctype.kind in ("struct", "union"):
            menu.addAction("新建调参回路（自动识别成员）", lambda: self.loop.from_struct(path, address, ctype))
            menu.addAction("全部标量成员加入监视", lambda: self.add_struct(path, address, ctype))
        if menu.actions():
            menu.exec(self.tree.viewport().mapToGlobal(position))

    def add_item(self, item, quiet=False):
        data = item.data(0, Qt.UserRole) if item else None
        if not data:
            return None
        path, address, ctype, watchable = data
        if not watchable:
            if not quiet and ctype.kind in ("struct", "union", "array"):
                item.setExpanded(True)
            return None
        return self.add_entry(Leaf(path, address, ctype))

    def add_struct(self, path, address, ctype):
        for child in ElfImage.children(path, address, ctype):
            if child[3]:
                self.add_entry(Leaf(child[0], child[1], child[2]), plot=False)

    def add_entry(self, leaf, plot=True, manual=""):
        for entry in self.entries:
            if entry.path == leaf.path:
                return entry.path
        used = {e.color for e in self.entries}
        color = next((c for c in COLORS if c not in used), COLORS[len(self.entries) % len(COLORS)])
        self.entries.append(WatchEntry(leaf.path, leaf, plot, color, manual))
        self.rebuild_table()
        self.send_watch()
        self.save_timer.start()
        return leaf.path

    def manual_add(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("按地址添加变量")
        form = QFormLayout(dialog)
        name, address, kind = QLineEdit("var"), QLineEdit("0x20000000"), QComboBox()
        kind.addItems(BASE_TYPES)
        form.addRow("名称", name)
        form.addRow("地址", address)
        form.addRow("类型", kind)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if not dialog.exec():
            return
        try:
            value = int(address.text().strip(), 0)
        except ValueError:
            self.notify("地址格式不对，例如 0x20001234", True)
            return
        ctype = manual_type(kind.currentText())
        if value % min(4, ctype.size):
            self.notify("地址需按类型对齐", True)
            return
        self.add_entry(Leaf(f"@{name.text().strip() or hex(value)}", value, ctype), manual=kind.currentText())

    # ================= 监视表 =================
    def rebuild_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.entries))
        for r, entry in enumerate(self.entries):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            check.setCheckState(Qt.Checked if entry.plot else Qt.Unchecked)
            name = QTableWidgetItem(color_icon(entry.color), entry.path)
            name.setToolTip(entry.path)
            leaf = entry.leaf
            cells = (check, name, QTableWidgetItem("—"),
                     QTableWidgetItem(leaf.ctype.display if leaf else "未找到"),
                     QTableWidgetItem(f"0x{leaf.address:08X}" if leaf else "—"))
            for c, cell in enumerate(cells):
                if leaf is None and c:
                    cell.setForeground(QColor("#c62828"))
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        self.loop.refresh_choices()
        self.update_channels()
        self.update_state()

    def table_changed(self, item):
        if item.column() == 0 and 0 <= item.row() < len(self.entries):
            self.entries[item.row()].plot = item.checkState() == Qt.Checked
            self.update_channels()
            self.save_timer.start()

    def table_menu(self, position):
        r = self.table.rowAt(position.y())
        if r < 0:
            return
        entry = self.entries[r]
        menu = QMenu(self)
        menu.addAction("修改值…", lambda: self.edit_row(r))
        roles = menu.addMenu("设为当前回路的")
        for role, name in self.loop.ROLE_NAMES:
            roles.addAction(name, lambda ro=role: self.loop.assign(ro, entry.path))
        menu.addAction("移除", lambda: self.remove_paths([entry.path]))
        menu.exec(self.table.viewport().mapToGlobal(position))

    def remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedItems()})
        self.remove_paths([self.entries[r].path for r in rows])

    def remove_paths(self, paths):
        self.entries = [e for e in self.entries if e.path not in paths]
        self.rebuild_table()
        self.send_watch()
        self.save_timer.start()

    def clear_entries(self):
        if self.entries and QMessageBox.question(self, "清空监视", "移除全部监视变量？") == QMessageBox.Yes:
            self.remove_paths([e.path for e in self.entries])

    def edit_row(self, r):
        entry = self.entries[r]
        if entry.leaf is None:
            return
        current = self.latest.get(entry.path)
        text, ok = QInputDialog.getText(self, "修改变量", f"{entry.path}（{entry.leaf.ctype.display}）新值：",
                                        text="" if current is None else format_value(entry.leaf, current).split(" ")[0])
        if ok and text.strip():
            raw = text.strip()
            try:
                value = float(raw) if entry.leaf.encoding == "float" else int(raw, 0) if "." not in raw else int(float(raw))
            except ValueError:
                self.notify("数值格式不对", True)
                return
            self.write_value(entry.path, value)

    def refresh_values(self):
        if not self.entries:
            return
        for r, entry in enumerate(self.entries):
            item = self.table.item(r, 2)
            if item is not None and entry.leaf is not None:
                text = format_value(entry.leaf, self.latest.get(entry.path))
                if item.text() != text:
                    item.setText(text)
        self.loop.refresh_values()

    # ================= 采样 =================
    def watched(self):
        return [e for e in self.entries if e.leaf is not None]

    def send_watch(self):
        self.generation += 1
        entries = self.watched()
        self.watch_order = [e.path for e in entries]
        self.watch_leaves = [e.leaf for e in entries]
        self.worker.send("watch", self.generation, [(leaf.address, leaf.size) for leaf in self.watch_leaves])
        self.update_channels()

    def update_channels(self):
        if not hasattr(self, "watch_order"):
            return
        roles = self.loop.channel_roles()
        channels = []
        for entry in self.watched():
            role = roles.get(entry.path)
            plot = 0 if role in ("target", "feedback") else 1 if role == "output" else 2 if entry.plot else -1
            name = {"target": "目标", "feedback": "反馈", "output": "输出"}.get(role, "") or entry.path
            if role:
                name = f"{name} · {entry.path.split('.')[-1]}"
            channels.append((entry.path, name, entry.color, plot, role == "target"))
        if [c[0] for c in channels] == self.watch_order:
            self.scope.set_channels(channels)

    def on_samples(self, generation, rows):
        if generation != self.generation or not rows:
            return
        leaves = self.watch_leaves
        times, values = [], []
        for stamp, raws in rows:
            decoded = []
            for leaf, raw in zip(leaves, raws):
                try:
                    decoded.append(decode(leaf.ctype, raw))
                except (ValueError, IndexError):
                    decoded.append(float("nan"))
            times.append(stamp)
            values.append([float(v) if v is not None else float("nan") for v in decoded])
        for path, value in zip(self.watch_order, decoded):
            self.latest[path] = value
        self.scope.append(times, values)
        self.loop.on_samples()

    def value_of(self, path):
        return self.latest.get(path)

    # ================= 读写 =================
    def peek(self, address, size, callback):
        self.requests += 1
        self.peek_callbacks[self.requests] = callback
        self.worker.send("peek", self.requests, address, size)

    def on_peeked(self, request, raw):
        callback = self.peek_callbacks.pop(request, None)
        if callback:
            callback(raw)

    def can_write(self, quiet=False):
        reason = None
        if not self.connected:
            reason = "未连接"
        elif not self.armed:
            reason = "请先打开右上方「允许写入」"
        elif self.firmware_ok is False:
            reason = "板上程序与 ELF 不一致，已禁止写入"
        if reason and not quiet:
            self.notify(reason, True)
        return reason is None

    def write_value(self, path, value, callback=None, quiet=False):
        entry = next((e for e in self.entries if e.path == path), None)
        leaf = entry.leaf if entry else (self.image.resolve(path) if self.image else None)
        if leaf is None:
            self.notify(f"找不到变量 {path}", True)
            return False
        if not self.can_write():
            return False
        try:
            data = encode(leaf.ctype, value)
        except ValueError as error:
            self.notify(str(error), True)
            return False
        self.requests += 1
        self.write_callbacks[self.requests] = (path, value, callback, quiet)
        log.info("写入 %s = %s", path, value)
        self.worker.send("write", self.requests, leaf.address, data)
        return True

    def on_written(self, request, ok, message):
        path, value, callback, quiet = self.write_callbacks.pop(request, (None, None, None, False))
        if path is None:
            return
        if not quiet or not ok:
            self.notify(f"{path} = {value}：{message}", not ok)
        if callback:
            callback(ok)

    def toggle_arm(self, checked):
        if not checked:
            self.armed = False
            self.update_state()
            return
        if not self.connected or self.firmware_ok is False:
            self.arm.blockSignals(True)
            self.arm.setChecked(False)
            self.arm.blockSignals(False)
            self.notify("先连接；板上程序与 ELF 一致时才能写入" if self.connected else "先连接整车", True)
            return
        if not self.write_confirmed:
            extra = "\n\n⚠ 没有加载 ELF，无法核对板上程序。" if self.firmware_ok is None else ""
            answer = QMessageBox.warning(
                self, "允许写入内存",
                "写入会立刻改变正在运行的整车程序，可能让机构动作。\n\n请确认：\n"
                "· 机构固定好、周围无人，遥控器急停/断电随手可用；\n· 清楚要改的每个变量的含义和合理范围。"
                + extra + "\n\n本次连接内不再询问。", QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                self.arm.blockSignals(True)
                self.arm.setChecked(False)
                self.arm.blockSignals(False)
                return
            self.write_confirmed = True
        self.armed = True
        self.update_state()

    # ================= 项目保存 =================
    def project_file(self):
        key = hashlib.sha1(os.path.normcase(self.image.path).encode("utf-8")).hexdigest()[:16]
        folder = applog.log_dir().parent / "projects"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{key}.json"

    def save_project(self):
        if self.image is None:
            return
        data = {
            "elf": self.image.path,
            "watch": [{"path": e.path, "plot": e.plot, "color": e.color, "manual": e.manual,
                       "address": e.leaf.address if e.leaf else None,
                       "ctype": [e.leaf.ctype.kind, e.leaf.ctype.name, e.leaf.ctype.size, e.leaf.ctype.encoding]
                       if e.leaf and ("->" in e.path or e.path.startswith("*")) else None} for e in self.entries],
            **self.loop.dump(),
        }
        try:
            self.project_file().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as error:
            log.warning("保存调参项目失败：%s", error)

    def load_project(self):
        self.entries = []
        data = {}
        path = self.project_file()
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        for item in data.get("watch", []):
            if item.get("manual"):
                leaf = Leaf(item["path"], int(item["address"]), manual_type(item["manual"]))
            elif item.get("ctype"):
                kind, name, size, encoding = item["ctype"]  # 指针路径：按保存的地址和类型恢复
                leaf = Leaf(item["path"], int(item["address"]), CType(kind, name, int(size), encoding))
            else:
                leaf = self.image.resolve(item["path"])
            self.entries.append(WatchEntry(item["path"], leaf, bool(item.get("plot", True)),
                                           item.get("color", COLORS[0]), item.get("manual", "")))
        self.loop.load(data)
        self.rebuild_table()

    def export_csv(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出曲线", time.strftime("实车曲线_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
        if path:
            try:
                count = self.scope.export_csv(path)
                self.notify(f"已导出 {count} 行")
            except OSError as error:
                self.notify(str(error), True)

    # ================= 状态与引导 =================
    def notify(self, message, error=False):
        self.notify_main(message, error)

    def update_state(self):
        self.reload_elf.setEnabled(self.image is not None)
        repolish(self.arm, "armed", "true" if self.armed else "false")
        self.arm.setText("写入已允许" if self.armed else "允许写入")
        self.arm.setObjectName("stop" if self.armed else "")
        self.arm.style().unpolish(self.arm)
        self.arm.style().polish(self.arm)
        if self.firmware_ok is True:
            self.firmware_label.setText("✓ 板上程序与 ELF 一致")
        elif self.firmware_ok is False:
            self.firmware_label.setText("✗ 板上程序与 ELF 不一致，写入已禁用")
        else:
            self.firmware_label.setText("未核对板上程序" if self.connected else "")
        self.firmware_label.setObjectName("alert" if self.firmware_ok is False else "muted")
        self.firmware_label.style().unpolish(self.firmware_label)
        self.firmware_label.style().polish(self.firmware_label)
        if self.firmware_ok is False:
            self.banner.setText("板上运行的程序和加载的 ELF 不一致：监视到的值可能是错的，写入已禁用。"
                                "请下载与 ELF 对应的程序，或打开正确的 ELF。")
            self.banner_row.show()
        step, hint, action, callback = self.guide_state()
        for index, widget in enumerate(self.guide_steps):
            repolish(widget, "stage", "done" if index < step else "now" if index == step else "todo")
        self.guide_hint.setText(hint)
        self.guide_action.setText(action or "请稍候")
        self.guide_action.setEnabled(callback is not None)
        self.guide_callback = callback
        self.loop.update_enabled()

    def guide_state(self):
        if not self.connected:
            if self.probe.currentData() is None:
                return 0, "插上 J-Link / DAPLink，接整车主控的 SWD，然后刷新探针。", "刷新探针", lambda: self.worker.send("list")
            return 0, "选板型后连接。只读附加：不烧录、不复位、不暂停整车程序。", "连接", self.toggle_connection
        if self.image is None:
            return 1, "打开整车工程编译出的调试文件（Keil 在 MDK-ARM\\工程名\\*.axf，GCC 为 *.elf）。", "打开 ELF…", self.choose_elf
        if self.firmware_ok is False:
            return 1, "板上程序与 ELF 不一致：重新编译并下载，或打开正确的 ELF。", "重新加载 ELF", lambda: self.load_elf(self.image.path)
        if not self.entries:
            return 2, "在左侧搜索变量（如 yaw speed），双击加入监视；右键 PID 结构体可一键建回路。", "搜索 pid", \
                lambda: (self.search.setText("pid"), self.search.setFocus())
        if not self.loop.ready():
            return 3, "右键变量树里的 PID 结构体选「新建调参回路」，或在「回路调参」里指定目标和反馈。", "去建回路", \
                lambda: self.mid_tabs.button(1).click()
        return 4, self.loop.guide_hint(), "打开回路调参", lambda: self.mid_tabs.button(1).click()

    def escape(self):
        if self.loop.step is not None:
            self.loop.abort_step("已按 Esc 中止，目标已写回原值", restore=True)
        else:
            self.notify("实车模式下 Esc 只中止本软件发起的阶跃；整车急停请用遥控器或断电。")

    def shutdown(self):
        self.save_project()
        self.worker.send("quit")
