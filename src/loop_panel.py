"""实车调参回路：指定目标/反馈/输出和 PID 参数变量，在线写参数（可撤销），主动或被动阶跃评估，调参记录。"""
from __future__ import annotations

import csv
import math
import time

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QHeaderView, QInputDialog,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from analysis import advice, detect_steps, format_metrics, step_metrics
from elfvars import guess_roles

GAINS = ("kp", "ki", "kd", "max_out", "max_iout")
SIGNALS = ("target", "feedback", "output", "inject")
NONE = "（未设置）"


def _label(text, kind=""):
    widget = QLabel(text)
    widget.setObjectName(kind)
    widget.setWordWrap(True)
    return widget


def _button(text, kind=""):
    widget = QPushButton(text)
    widget.setObjectName(kind)
    return widget


def _row(*widgets):
    widget = QWidget()
    from PyQt5.QtWidgets import QHBoxLayout
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    for child in widgets:
        layout.addWidget(child)
    return widget


def _number(lo, hi, value, decimals=3):
    widget = QDoubleSpinBox()
    widget.setRange(lo, hi)
    widget.setDecimals(decimals)
    widget.setValue(value)
    return widget


def _same(a, b):
    return a is not None and b is not None and abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


class LoopPanel(QScrollArea):
    ROLE_NAMES = (("target", "目标"), ("feedback", "反馈"), ("output", "输出"), ("inject", "阶跃注入"), ("kp", "Kp"), ("ki", "Ki"),
                  ("kd", "Kd"), ("max_out", "输出限幅"), ("max_iout", "积分限幅"))

    def __init__(self, page):
        super().__init__()
        self.page = page
        self.loops = []
        self.history = []
        self.step = None
        self.step_token = 0
        self.passive_done = set()
        self.initial = {}
        self.undo = []
        self.step_confirmed = False
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("scroll-content")
        self.setWidget(body)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)

        self.loop_combo = QComboBox()
        self.new_loop, self.rename_loop, self.delete_loop = _button("新建"), _button("重命名"), _button("删除")
        layout.addWidget(_row(self.loop_combo, self.new_loop, self.rename_loop, self.delete_loop))
        layout.itemAt(0).widget().layout().setStretch(0, 1)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.signal_combos = {}
        for role in SIGNALS:
            combo = QComboBox()
            combo.setMinimumContentsLength(12)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            self.signal_combos[role] = combo
            form.addRow(dict(self.ROLE_NAMES)[role] + ("（可选）" if role in ("output", "inject") else ""), combo)
        self.signal_combos["inject"].setToolTip("主动阶跃改写的变量。目标由遥控每周期重写时，在代码里加一个偏置变量叠加到目标上，"
                                                "这里选它；不选则直接改写目标")
        layout.addLayout(form)

        layout.addWidget(_label("PID 参数", "section"))
        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        for column, title in enumerate(("参数", "变量", "板上当前", "新值")):
            grid.addWidget(_label(title, "muted"), 0, column)
        self.gain_combos, self.gain_current, self.gain_new = {}, {}, {}
        for r, role in enumerate(GAINS, 1):
            combo = QComboBox()
            combo.setMinimumContentsLength(8)
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            current = _label("—")
            new = QLineEdit()
            new.setPlaceholderText("—")
            new.setMaximumWidth(110)
            grid.addWidget(_label(dict(self.ROLE_NAMES)[role]), r, 0)
            grid.addWidget(combo, r, 1)
            grid.addWidget(current, r, 2)
            grid.addWidget(new, r, 3)
            self.gain_combos[role], self.gain_current[role], self.gain_new[role] = combo, current, new
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.write_gains = _button("写入新参数", "primary")
        self.copy_current = _button("新值 = 当前值")
        self.undo_button = _button("撤销上次写入")
        self.restore_button = _button("恢复初始值")
        layout.addWidget(_row(self.write_gains, self.copy_current))
        layout.addWidget(_row(self.undo_button, self.restore_button))
        self.gain_note = _label("改过的新值标黄；写入后会读回校验，并检查是否被程序改回。", "muted")
        layout.addWidget(self.gain_note)

        layout.addWidget(_label("阶跃评估", "section"))
        self.mode = QComboBox()
        self.mode.addItems(("主动：软件改写目标变量", "被动：检测遥控/程序给的阶跃"))
        self.amplitude = _number(-1e6, 1e6, 100, 3)
        self.hold = _number(0.2, 20, 1.0, 1)
        self.jump = _number(0.0001, 1e6, 20, 3)
        self.jump.setToolTip("目标在 0.1 s 内跳变超过此值、前后都平稳时，记为一次阶跃")
        step_form = QFormLayout()
        step_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        step_form.addRow("方式", self.mode)
        self.amplitude_row = ("阶跃幅值", self.amplitude)
        step_form.addRow("阶跃幅值", self.amplitude)
        step_form.addRow("保持时间 [s]", self.hold)
        step_form.addRow("最小跳变", self.jump)
        layout.addLayout(step_form)
        self.step_form = step_form
        self.start_step = _button("开始阶跃", "primary")
        layout.addWidget(self.start_step)
        self.result = _label("", "metric")
        layout.addWidget(self.result)
        self.tips = _label("", "inlineNote")
        self.tips.hide()
        layout.addWidget(self.tips)
        self.step_note = _label("", "muted")
        layout.addWidget(self.step_note)
        layout.addStretch()

        self.history_page = QWidget()
        history = QVBoxLayout(self.history_page)
        history.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 12)
        self.table.setHorizontalHeaderLabels(("时间", "回路", "方式", "幅值", "Kp", "Ki", "Kd", "上升ms", "超调%",
                                              "调节ms", "稳态%", "评价"))
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        history.addWidget(self.table, 1)
        self.apply_history = _button("把所选参数填入新值")
        self.export_history = _button("导出 CSV")
        self.clear_history = _button("清空")
        history.addWidget(_row(self.apply_history, self.export_history, self.clear_history))
        history.addWidget(_label("每次阶跃（主动或被动）都会记下当时的参数和指标，便于比较哪组更好。", "muted"))

        self.loop_combo.currentIndexChanged.connect(self.show_loop)
        self.new_loop.clicked.connect(lambda: self.create("回路"))
        self.rename_loop.clicked.connect(self.rename)
        self.delete_loop.clicked.connect(self.delete)
        for role, combo in {**self.signal_combos, **self.gain_combos}.items():
            combo.activated.connect(lambda _, r=role: self.choose(r))
        for role, edit in self.gain_new.items():
            edit.textChanged.connect(self.mark_dirty)
            edit.returnPressed.connect(self.write)
        self.write_gains.clicked.connect(self.write)
        self.copy_current.clicked.connect(lambda: self.seed(force=True))
        self.undo_button.clicked.connect(self.undo_last)
        self.restore_button.clicked.connect(self.restore_initial)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        for widget in (self.amplitude, self.hold, self.jump):
            widget.valueChanged.connect(self.store_step_settings)
        self.start_step.clicked.connect(self.toggle_step)
        self.apply_history.clicked.connect(self.fill_from_history)
        self.export_history.clicked.connect(self.export)
        self.clear_history.clicked.connect(self.clear)
        self.passive_timer = QTimer(self)
        self.passive_timer.timeout.connect(self.scan_passive)
        self.passive_timer.start(400)
        self.passive_on = False
        self.passive_since = 0.0
        self.mode_changed()

    # ================= 回路管理 =================
    @property
    def current(self):
        index = self.loop_combo.currentIndex()
        return self.loops[index] if 0 <= index < len(self.loops) else None

    def create(self, name, roles=None):
        names = {loop["name"] for loop in self.loops}
        base, n = name, 2
        while name in names:
            name, n = f"{base} {n}", n + 1
        self.loops.append({"name": name, "roles": dict(roles or {}),
                           "step": {"mode": 0, "amp": 100.0, "hold": 1.0, "jump": 20.0}})
        self.loop_combo.addItem(name)
        self.loop_combo.setCurrentIndex(len(self.loops) - 1)
        self.page.save_timer.start()
        return self.loops[-1]

    def rename(self):
        loop = self.current
        if loop is None:
            return
        name, ok = QInputDialog.getText(self, "重命名回路", "名称：", text=loop["name"])
        if ok and name.strip():
            loop["name"] = name.strip()
            self.loop_combo.setItemText(self.loop_combo.currentIndex(), loop["name"])
            self.page.save_timer.start()

    def delete(self):
        loop = self.current
        if loop and QMessageBox.question(self, "删除回路", f"删除回路「{loop['name']}」？（变量仍在监视里）") == QMessageBox.Yes:
            index = self.loop_combo.currentIndex()
            self.loops.pop(index)
            self.loop_combo.removeItem(index)
            self.page.save_timer.start()
            self.page.update_channels()

    def from_struct(self, path, address, ctype):
        members = self.page.image.struct_leaves(path, address, ctype)
        roles = guess_roles(list(members))
        if not roles:
            self.page.notify("没有识别出 PID 成员，请在「回路调参」里手动指定。", True)
        assigned = {}
        for role, member in roles.items():
            assigned[role] = self.page.add_entry(members[member], plot=False)
        short = path.split("->")[-1].split(".")[-1]
        self.create(short, assigned)
        self.page.mid_tabs.button(1).click()
        missing = [dict(self.ROLE_NAMES)[r] for r in ("target", "feedback", "kp", "ki", "kd") if r not in assigned]
        self.page.notify(f"已建回路「{short}」：识别到 {len(assigned)} 个成员"
                         + (f"，未识别 {'、'.join(missing)}，可在下拉框手动指定" if missing else ""), bool(missing))

    def assign(self, role, path):
        if not path:
            return
        loop = self.current or self.create("回路")
        loop["roles"][role] = path
        self.show_loop()
        self.page.mid_tabs.button(1).click()
        self.page.save_timer.start()

    def choose(self, role):
        loop = self.current or self.create("回路")
        combo = {**self.signal_combos, **self.gain_combos}[role]
        path = combo.currentData()
        if path:
            loop["roles"][role] = path
        else:
            loop["roles"].pop(role, None)
        if role in GAINS:
            self.gain_new[role].clear()
        self.show_loop()
        self.page.save_timer.start()

    def refresh_choices(self):
        paths = [e.path for e in self.page.entries if e.leaf is not None]
        for role, combo in {**self.signal_combos, **self.gain_combos}.items():
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(NONE, None)
            for path in paths:
                # 参数下拉只显示成员名，完整路径在提示里
                short = path.replace("->", ".").split(".")[-1] if role in GAINS else path
                combo.addItem(short, path)
            combo.blockSignals(False)
        self.sync_combos()

    def sync_combos(self):
        loop = self.current
        roles = loop["roles"] if loop else {}
        for role, combo in {**self.signal_combos, **self.gain_combos}.items():
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(roles.get(role)) if roles.get(role) else 0))
            combo.setToolTip(roles.get(role, ""))
            combo.blockSignals(False)
        self.seed()
        self.update_enabled()
        if hasattr(self.page, "loop"):
            self.page.update_state()

    def show_loop(self, *_):
        loop = self.current
        roles = loop["roles"] if loop else {}
        for role, combo in {**self.signal_combos, **self.gain_combos}.items():
            combo.blockSignals(True)
            index = combo.findData(roles.get(role)) if roles.get(role) else 0
            combo.setCurrentIndex(max(0, index))
            combo.setToolTip(roles.get(role, ""))
            combo.blockSignals(False)
        if loop:
            settings = loop["step"]
            for widget, key in ((self.amplitude, "amp"), (self.hold, "hold"), (self.jump, "jump")):
                widget.blockSignals(True)
                widget.setValue(float(settings.get(key, widget.value())))
                widget.blockSignals(False)
            self.mode.blockSignals(True)
            self.mode.setCurrentIndex(int(settings.get("mode", 0)))
            self.mode.blockSignals(False)
            self.mode_changed()
        for edit in self.gain_new.values():
            edit.clear()
        self.seed()
        self.page.update_channels()
        self.update_enabled()
        if hasattr(self.page, "loop"):
            self.page.update_state()

    def channel_roles(self):
        loop = self.current
        return {loop["roles"][r]: r for r in ("target", "feedback", "output") if loop and loop["roles"].get(r)}

    def ready(self):
        loop = self.current
        return bool(loop and loop["roles"].get("target") and loop["roles"].get("feedback"))

    def guide_hint(self):
        if not self.page.armed:
            return "看曲线确认目标/反馈正常。改参数前打开「允许写入」；用遥控给阶跃可选「被动」方式自动评估。"
        return "改新值后点「写入新参数」，再「开始阶跃」自动评估；记录页可对比每组参数。Esc 中止阶跃。"

    # ================= 参数 =================
    def path(self, role):
        loop = self.current
        return loop["roles"].get(role) if loop else None

    def gain_value(self, role):
        path = self.path(role)
        value = self.page.value_of(path) if path else None
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def refresh_values(self):
        for role in GAINS:
            value = self.gain_value(role)
            self.gain_current[role].setText("—" if value is None else f"{value:.6g}")
        loop = self.current
        if loop and loop["name"] not in self.initial and self.page.connected:
            values = {role: self.gain_value(role) for role in GAINS if self.path(role)}
            if values and all(v is not None for v in values.values()):
                self.initial[loop["name"]] = values
        self.seed()
        self.mark_dirty()

    def seed(self, force=False):
        for role, edit in self.gain_new.items():
            value = self.gain_value(role)
            if value is not None and (force or not edit.text()):
                edit.blockSignals(True)
                edit.setText(f"{value:.6g}")
                edit.blockSignals(False)
        self.mark_dirty()

    def new_value(self, role):
        try:
            return float(self.gain_new[role].text())
        except ValueError:
            return None

    def dirty_roles(self):
        return [role for role in GAINS if self.path(role) and self.new_value(role) is not None
                and not _same(self.new_value(role), self.gain_value(role))]

    def mark_dirty(self, *_):
        dirty = set(self.dirty_roles())
        for role, edit in self.gain_new.items():
            state = "true" if role in dirty else "false"
            if edit.property("dirty") != state:
                edit.setProperty("dirty", state)
                edit.style().unpolish(edit)
                edit.style().polish(edit)
        self.write_gains.setText(f"写入新参数（{len(dirty)}）" if dirty else "写入新参数")

    def write(self, values=None, remember=True):
        values = values or {role: self.new_value(role) for role in self.dirty_roles()}
        values = {r: v for r, v in values.items() if v is not None and self.path(r)}
        if not values or not self.page.can_write():
            return
        bad = [dict(self.ROLE_NAMES)[r] for r, v in values.items() if not math.isfinite(v) or (r in GAINS[:3] and v < 0)]
        if bad and QMessageBox.question(self, "确认写入", f"{'、'.join(bad)} 为负数或非法值，确定写入？") != QMessageBox.Yes:
            return
        before = {r: self.gain_value(r) for r in values}
        pending = {"left": len(values), "ok": 0}

        def done(ok, role, value):
            pending["left"] -= 1
            pending["ok"] += bool(ok)
            if ok:
                QTimer.singleShot(600, lambda: self.check_kept(role, value))
            if pending["left"] == 0:
                self.page.notify(f"已写入 {pending['ok']}/{len(values)} 个参数", pending["ok"] != len(values))

        if remember and all(v is not None for v in before.values()):
            self.undo.append((self.current["name"], before))
        for role, value in values.items():
            self.page.write_value(self.path(role), value, lambda ok, r=role, v=value: done(ok, r, v), quiet=True)

    def check_kept(self, role, value):
        current = self.gain_value(role)
        if current is not None and not _same(current, value):
            self.page.notify(f"{dict(self.ROLE_NAMES)[role]} 写入后又被程序改成 {current:.6g}："
                             "程序可能在循环里重复初始化 PID，需改代码只初始化一次。", True)

    def undo_last(self):
        loop = self.current
        for index in range(len(self.undo) - 1, -1, -1):
            if self.undo[index][0] == loop["name"]:
                _, values = self.undo.pop(index)
                self.write(values, remember=False)
                return
        self.page.notify("没有可撤销的写入")

    def restore_initial(self):
        loop = self.current
        values = self.initial.get(loop["name"]) if loop else None
        if not values:
            self.page.notify("还没有记录到初始值（连接后首次读到的值）", True)
            return
        if QMessageBox.question(self, "恢复初始值", "把参数写回本次连接时读到的初始值？") == QMessageBox.Yes:
            self.write(values)

    # ================= 阶跃 =================
    def mode_changed(self, *_):
        active = self.mode.currentIndex() == 0
        self.step_form.labelForField(self.amplitude).setVisible(active)
        self.amplitude.setVisible(active)
        self.step_form.labelForField(self.jump).setVisible(not active)
        self.jump.setVisible(not active)
        if not active and self.step is not None:
            self.abort_step("已切换方式", restore=True)
        if active:
            self.passive_on = False
        self.step_note.setText(
            "主动：把目标变量改为 当前值+幅值，保持后改回。若程序每周期都重写目标（如来自遥控），软件会检测到并自动停止。"
            if active else
            "被动：不写任何变量。用遥控或程序让目标跳变，软件识别阶跃并自动分析，适合目标由遥控决定的整车。")
        self.store_step_settings()
        self.update_enabled()

    def store_step_settings(self, *_):
        loop = self.current
        if loop:
            loop["step"] = {"mode": self.mode.currentIndex(), "amp": self.amplitude.value(), "hold": self.hold.value(),
                            "jump": self.jump.value()}
            self.page.save_timer.start()

    def update_enabled(self):
        ready = self.ready()
        writable = self.page.can_write(quiet=True)
        has_gain = any(self.path(r) for r in GAINS)
        self.write_gains.setEnabled(writable and has_gain)
        for widget in (self.undo_button, self.restore_button):
            widget.setEnabled(writable and has_gain)
        active = self.mode.currentIndex() == 0
        if active:
            self.start_step.setText("中止阶跃 (Esc)" if self.step else "开始阶跃")
            self.start_step.setEnabled(bool(self.step) or (ready and writable))
        else:
            self.start_step.setText("停止被动检测" if self.passive_on else "开始被动检测")
            self.start_step.setEnabled(ready and self.page.connected)
        for widget in (self.rename_loop, self.delete_loop):
            widget.setEnabled(self.current is not None)

    def toggle_step(self):
        if self.mode.currentIndex() == 1:
            self.passive_on = not self.passive_on
            self.passive_done = set()
            self.passive_since = self.page.scope.now() or 0.0
            self.result.setText("被动检测中：用遥控或程序给目标一个阶跃…" if self.passive_on else "")
            self.update_enabled()
            return
        if self.step is not None:
            self.abort_step("已中止，目标已写回原值", restore=True)
            return
        target = self.path("target")
        writer = self.path("inject") or target
        base, write_base = self.page.value_of(target), self.page.value_of(writer)
        if base is None or write_base is None or not self.page.can_write():
            return
        amplitude = self.amplitude.value()
        peak = float(base) + amplitude
        if not self.step_confirmed:
            text = (f"将把 {writer} 从 {float(write_base):.6g} 改为 {float(write_base) + amplitude:.6g}，"
                    f"保持 {self.hold.value():g} s 后改回（目标预计 {float(base):.6g} → {peak:.6g}）。\n"
                    "机构会动作，请确认安全。本次运行不再询问。")
            if QMessageBox.question(self, "开始阶跃", text) != QMessageBox.Yes:
                return
            self.step_confirmed = True
        self.step_token += 1
        self.step = {"token": self.step_token, "base": float(base), "peak": peak, "hold": self.hold.value(),
                     "phase": 0, "marks": [], "loop": self.current["name"], "writer": writer,
                     "write_base": float(write_base), "write_peak": float(write_base) + amplitude}
        self.tips.hide()
        self.result.setText(f"阶跃中：{float(base):.6g} → {peak:.6g}")
        self.page.write_value(writer, self.step["write_peak"], lambda ok: self.step_written(ok, self.step_token),
                              quiet=True)
        self.update_enabled()

    def step_written(self, ok, token):
        step = self.step
        if step is None or step["token"] != token:
            return
        if not ok:
            self.abort_step("写入目标失败", restore=step["phase"] == 0)
            return
        now = self.page.scope.now() or 0.0
        step["marks"].append(now)
        self.page.scope.add_marker(now, "阶跃" if step["phase"] == 0 else "回落")
        if step["phase"] == 0:
            QTimer.singleShot(250, lambda: self.check_override(token))
            QTimer.singleShot(round(step["hold"] * 1000), lambda: self.step_back(token))
        else:
            QTimer.singleShot(round(step["hold"] * 1000), lambda: self.step_finish(token))

    def check_override(self, token):
        step = self.step
        if step is None or step["token"] != token or step["phase"] != 0:
            return
        value = self.page.value_of(self.path("target"))
        tolerance = max(1e-6, abs(step["peak"] - step["base"]) * 0.05)
        if value is None or abs(float(value) - step["peak"]) > tolerance:
            self.abort_step("目标变量被程序每周期重写，主动阶跃无效。请改用「被动」方式（用遥控给阶跃），"
                            "或在代码里加一个调试偏置变量叠加到目标上（见使用说明）。", restore=True)

    def step_back(self, token):
        step = self.step
        if step is None or step["token"] != token:
            return
        step["phase"] = 1
        self.result.setText(f"回落：{step['peak']:.6g} → {step['base']:.6g}")
        self.page.write_value(step["writer"], step["write_base"], lambda ok: self.step_written(ok, token), quiet=True)

    def step_finish(self, token):
        step = self.step
        if step is None or step["token"] != token:
            return
        self.step = None
        marks = step["marks"]
        self.analyze(marks[0], marks[1], step["base"], step["peak"], "主动")
        self.update_enabled()

    def abort_step(self, message, restore=False):
        step = self.step
        if step is None:
            return
        self.step = None
        if restore and self.page.connected and self.page.armed:
            self.page.write_value(step["writer"], step["write_base"], quiet=True)
        self.result.setText(message)
        self.page.notify(message, True)
        self.update_enabled()

    def on_samples(self):
        pass

    def scan_passive(self):
        if not self.passive_on or not self.ready() or self.mode.currentIndex() != 1:
            return
        scope = self.page.scope
        now = scope.now()
        if now is None:
            return
        hold = self.hold.value()
        t, y = scope.series(self.path("target"), max(self.passive_since, now - max(15.0, hold * 4)))
        values = [None if not math.isfinite(v) else float(v) for v in y]
        for t0, old, new, end in detect_steps(list(t), values, self.jump.value(), quiet=0.15):
            key = round(t0, 2)
            if key in self.passive_done or end - t0 < min(hold, 0.3):
                continue
            if end - t0 < hold and now - end < 0.15:
                continue  # 新平稳段还在持续，等够保持时间
            self.passive_done.add(key)
            scope.add_marker(t0, "阶跃")
            self.analyze(t0, min(end, t0 + hold), old, new, "被动", edge_known=True)

    def analyze(self, t0, t1, base, peak, mode, edge_known=False):
        scope = self.page.scope
        target, feedback, output = self.path("target"), self.path("feedback"), self.path("output")
        start = t0 - 0.3
        tt, tg = scope.series(target, start, t1)
        edge = t0
        if not edge_known:
            tolerance = max(1e-9, abs(peak - base) * 0.02)
            hits = [ti for ti, v in zip(tt, tg) if ti >= t0 - 0.3 and math.isfinite(v) and abs(v - base) > tolerance]
            edge = hits[0] if hits else t0
        tf, fb = scope.series(feedback, edge, t1)
        out = scope.series(output, edge, t1)[1] if output else None
        limit = self.gain_value("max_out")
        metrics = step_metrics(list(tf), [None if not math.isfinite(v) else float(v) for v in fb], edge, base, peak,
                               list(out) if out is not None and len(out) == len(tf) else None, limit)
        name = self.current["name"] if self.current else ""
        tips = advice(metrics, name or "该回路")
        self.result.setText(f"{mode}阶跃 {base:.6g} → {peak:.6g}\n{format_metrics(metrics)}")
        marks = {"ok": "✔", "warn": "⚠", "bad": "✖"}
        self.tips.setText("\n".join(f"{marks[level]} {text}" for level, text in tips))
        self.tips.show()
        verdict = "✖" if any(l == "bad" for l, _ in tips) else "⚠" if any(l == "warn" for l, _ in tips) else "✔"
        entry = {
            "time": time.strftime("%H:%M:%S"), "loop": name, "mode": mode, "amp": peak - base,
            "kp": self.gain_value("kp"), "ki": self.gain_value("ki"), "kd": self.gain_value("kd"),
            "rise": None if metrics is None or metrics.rise_time is None else metrics.rise_time * 1000,
            "overshoot": None if metrics is None else metrics.overshoot,
            "settling": None if metrics is None or metrics.settling_time is None else metrics.settling_time * 1000,
            "error": None if metrics is None else metrics.steady_error, "verdict": verdict,
        }
        self.history.append(entry)
        self.history = self.history[-300:]
        self.add_history_row(entry)
        self.page.save_timer.start()

    # ================= 记录 =================
    def add_history_row(self, entry):
        r = self.table.rowCount()
        self.table.insertRow(r)

        def fmt(value, digits=".4g"):
            return "—" if value is None else format(value, digits)
        cells = (entry["time"], entry["loop"], entry["mode"], fmt(entry["amp"]), fmt(entry["kp"]), fmt(entry["ki"]),
                 fmt(entry["kd"]), fmt(entry["rise"], ".0f"), fmt(entry["overshoot"], ".1f"),
                 fmt(entry["settling"], ".0f"), fmt(entry["error"], ".1f"), entry["verdict"])
        for c, text in enumerate(cells):
            self.table.setItem(r, c, QTableWidgetItem(text))
        self.table.scrollToBottom()

    def fill_from_history(self):
        r = self.table.currentRow()
        if not 0 <= r < len(self.history):
            return
        entry = self.history[r]
        for role in ("kp", "ki", "kd"):
            if entry.get(role) is not None and self.path(role):
                self.gain_new[role].setText(f"{entry[role]:.6g}")
        self.page.mid_tabs.button(1).click()
        self.page.notify("已填入新值，确认后点「写入新参数」")

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出调参记录", time.strftime("调参记录_%Y%m%d_%H%M%S.csv"), "CSV (*.csv)")
        if not path:
            return
        keys = ("time", "loop", "mode", "amp", "kp", "ki", "kd", "rise", "overshoot", "settling", "error", "verdict")
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("时间", "回路", "方式", "幅值", "Kp", "Ki", "Kd", "上升ms", "超调%", "调节ms", "稳态%", "评价"))
            for entry in self.history:
                writer.writerow(["" if entry.get(k) is None else entry[k] for k in keys])
        self.page.notify(f"已导出 {len(self.history)} 条记录")

    def clear(self):
        if self.history and QMessageBox.question(self, "清空记录", "清空全部调参记录？") == QMessageBox.Yes:
            self.history = []
            self.table.setRowCount(0)
            self.page.save_timer.start()

    # ================= 保存 =================
    def dump(self):
        return {"loops": self.loops, "current": self.loop_combo.currentIndex(), "history": self.history}

    def load(self, data):
        self.loops = [loop for loop in data.get("loops", []) if isinstance(loop, dict) and "name" in loop]
        for loop in self.loops:
            loop.setdefault("roles", {})
            loop.setdefault("step", {"mode": 0, "amp": 100.0, "hold": 1.0, "jump": 20.0})
        self.history = list(data.get("history", []))[-300:]
        self.initial, self.undo = {}, []
        self.loop_combo.blockSignals(True)
        self.loop_combo.clear()
        self.loop_combo.addItems([loop["name"] for loop in self.loops])
        self.loop_combo.setCurrentIndex(min(max(0, int(data.get("current", 0))), len(self.loops) - 1))
        self.loop_combo.blockSignals(False)
        self.table.setRowCount(0)
        for entry in self.history:
            self.add_history_row(entry)
        self.show_loop()
