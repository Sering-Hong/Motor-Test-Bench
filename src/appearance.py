"""外观设置对话框：浅色/深色、背景图、透明度、强调色。修改实时预览，关闭时由主窗口保存。"""
from __future__ import annotations

import os

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from theme import ACCENTS, Appearance


class PercentSlider(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, lo, hi, tip):
        super().__init__()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.slider.setToolTip(tip)
        self.text = QLabel()
        self.text.setMinimumWidth(44)
        self.text.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.text)
        self.slider.valueChanged.connect(self._changed)

    def _changed(self, value):
        self.text.setText(f"{value} %")
        self.valueChanged.emit(value)

    def value(self):
        return self.slider.value()

    def setValue(self, value):
        self.slider.blockSignals(True)
        self.slider.setValue(int(value))
        self.slider.blockSignals(False)
        self.text.setText(f"{self.slider.value()} %")


class AppearanceDialog(QDialog):
    changed = pyqtSignal(object)

    def __init__(self, appearance: Appearance, parent=None):
        super().__init__(parent)
        self.setWindowTitle("外观设置")
        self.setMinimumWidth(480)
        self.original = appearance.copy()
        self.a = appearance.copy()
        self.mode = QComboBox()
        self.mode.addItem("浅色（樱花）", "light")
        self.mode.addItem("深色", "dark")
        self.enabled = QCheckBox("显示背景图")
        self.path = QLineEdit()
        self.path.setReadOnly(True)
        self.path.setPlaceholderText("未选择图片")
        browse, clear = QPushButton("选择图片…"), QPushButton("清除")
        self.image_note = QLabel()
        self.image_note.setObjectName("muted")
        self.image_note.setWordWrap(True)
        self.bg = PercentSlider(0, 100, "背景图本身的不透明度")
        self.overlay = PercentSlider(0, 90, "浅色模式提亮、深色模式压暗，保证文字清楚")
        self.panel = PercentSlider(15, 100, "卡片和输入框的不透明度，越低越透出背景")
        self.accent = QPushButton()
        self.accent.setMinimumWidth(100)
        accents = QHBoxLayout()
        accents.setContentsMargins(0, 0, 0, 0)
        accents.addWidget(self.accent)
        for color in ACCENTS:
            swatch = QPushButton()
            swatch.setFixedSize(24, 24)
            swatch.setStyleSheet(f"QPushButton{{background:{color};border:1px solid rgba(0,0,0,60);"
                                 f"border-radius:12px;padding:0;min-height:0}}")
            swatch.clicked.connect(lambda _=False, c=color: self.set_accent(c))
            accents.addWidget(swatch)
        accents.addStretch()
        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.addWidget(self.path, 1)
        path_row.addWidget(browse)
        path_row.addWidget(clear)
        form = QFormLayout()
        form.addRow("模式", self.mode)
        form.addRow(self.enabled)
        form.addRow("图片", path_row)
        form.addRow("", self.image_note)
        form.addRow("背景不透明度", self.bg)
        form.addRow("遮罩强度", self.overlay)
        form.addRow("卡片不透明度", self.panel)
        form.addRow("强调色", accents)
        hint = QLabel("图片两种模式共用；透明度、遮罩和强调色按浅色 / 深色分别记忆，下次打开自动恢复。")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        defaults, revert, done = QPushButton("恢复默认"), QPushButton("撤销修改"), QPushButton("完成")
        done.setObjectName("primary")
        done.setDefault(True)
        bottom = QHBoxLayout()
        bottom.addWidget(defaults)
        bottom.addWidget(revert)
        bottom.addStretch()
        bottom.addWidget(done)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(hint)
        layout.addLayout(bottom)
        self.mode.currentIndexChanged.connect(self.change_mode)
        self.enabled.toggled.connect(lambda on: self.set_variant(bg_enabled=on))
        self.bg.valueChanged.connect(lambda v: self.set_variant(bg_opacity=v))
        self.overlay.valueChanged.connect(lambda v: self.set_variant(overlay=v))
        self.panel.valueChanged.connect(lambda v: self.set_variant(panel_opacity=v))
        self.accent.clicked.connect(self.pick_accent)
        browse.clicked.connect(self.browse)
        clear.clicked.connect(lambda: self.set_path(""))
        defaults.clicked.connect(self.defaults)
        revert.clicked.connect(self.revert)
        done.clicked.connect(self.accept)
        self.load()

    def load(self):
        v = self.a.variant()
        self.mode.blockSignals(True)
        self.mode.setCurrentIndex(max(0, self.mode.findData(self.a.mode)))
        self.mode.blockSignals(False)
        self.enabled.blockSignals(True)
        self.enabled.setChecked(v.bg_enabled)
        self.enabled.blockSignals(False)
        self.path.setText(self.a.bg_path)
        self.path.setToolTip(self.a.bg_path)
        self.bg.setValue(v.bg_opacity)
        self.overlay.setValue(v.overlay)
        self.panel.setValue(v.panel_opacity)
        self.show_accent(v.accent)
        self.show_image_note()

    def show_image_note(self, error=""):
        path = self.a.bg_path
        if not path:
            text = "未设置背景图"
        elif not os.path.isfile(path):
            text = "⚠ 文件不存在，请重新选择"
        else:
            text = f"⚠ {error}" if error else os.path.basename(path)
        self.image_note.setText(text)
        for widget in (self.bg, self.overlay):
            widget.setEnabled(self.enabled.isChecked() and bool(path))

    def show_accent(self, color):
        text = "#ffffff" if QColor(color).lightness() < 175 else "#2a1a22"
        self.accent.setText(color.upper())
        self.accent.setStyleSheet(f"QPushButton{{background:{color};color:{text};border-radius:6px}}")

    def emit(self):
        self.show_image_note()
        self.changed.emit(self.a.copy())

    def set_variant(self, **values):
        for key, value in values.items():
            setattr(self.a.variant(), key, value)
        self.emit()

    def set_accent(self, color):
        self.show_accent(color)
        self.set_variant(accent=color)

    def pick_accent(self):
        color = QColorDialog.getColor(QColor(self.a.variant().accent), self, "选择强调色")
        if color.isValid():
            self.set_accent(color.name())

    def set_path(self, path):
        self.a.bg_path = path
        self.path.setText(path)
        self.path.setToolTip(path)
        if path and not self.a.variant().bg_enabled:
            self.enabled.setChecked(True)
            return
        self.emit()

    def browse(self):
        start = os.path.dirname(self.a.bg_path) if self.a.bg_path else os.path.expanduser("~/Pictures")
        path, _ = QFileDialog.getOpenFileName(self, "选择背景图", start, "图片 (*.png *.jpg *.jpeg *.bmp *.webp)")
        if path:
            self.set_path(os.path.normpath(path))

    def change_mode(self):
        self.a.mode = self.mode.currentData()
        self.load()
        self.emit()

    def defaults(self):
        fresh = Appearance()
        fresh.mode, fresh.bg_path = self.a.mode, self.a.bg_path or fresh.bg_path
        self.a = fresh
        self.load()
        self.emit()

    def revert(self):
        self.a = self.original.copy()
        self.load()
        self.emit()

    def set_image_error(self, error):
        self.show_image_note(error)
