"""多通道示波器：环形缓冲 + pyqtgraph 联动 X 轴的多张图，悬停读数、阶跃标记、导出 CSV。
所有通道共用同一时间列（同一次 SWD 批量读取得到），列数变化时清空缓冲。"""
from __future__ import annotations

import csv
import math

import numpy as np
import pyqtgraph as pg
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont
from PyQt5.QtWidgets import QVBoxLayout, QWidget

import theme

pg.setConfigOptions(antialias=True, foreground="k")


class Ring:
    """二维环形缓冲：第 0 列为时间，其余为各通道值。"""

    def __init__(self, columns, capacity=240_000):
        self.data = np.full((capacity, columns + 1), np.nan)
        self.capacity = capacity
        self.count = 0
        self.head = 0

    def append(self, rows):
        rows = np.asarray(rows, dtype=float)
        n = len(rows)
        if n == 0:
            return
        if n >= self.capacity:
            rows, n = rows[-self.capacity:], self.capacity
        end = self.head + n
        if end <= self.capacity:
            self.data[self.head:end] = rows
        else:
            first = self.capacity - self.head
            self.data[self.head:] = rows[:first]
            self.data[:n - first] = rows[first:]
        self.head = end % self.capacity
        self.count = min(self.capacity, self.count + n)

    def view(self):
        """按时间顺序返回全部有效行（必要时拷贝拼接）。"""
        if self.count < self.capacity:
            return self.data[:self.count]
        return np.concatenate((self.data[self.head:], self.data[:self.head]))

    def last(self):
        return self.data[(self.head - 1) % self.capacity] if self.count else None


class Scope(QWidget):
    def __init__(self, plot_titles=("回路", "输出", "监视")):
        super().__init__()
        self.layout_widget = pg.GraphicsLayoutWidget()
        self.layout_widget.setBackground(None)
        self.layout_widget.setStyleSheet("background: transparent; border: none;")
        self.layout_widget.viewport().setAutoFillBackground(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.layout_widget)
        self.titles = plot_titles
        self.plots = []
        for index, title in enumerate(plot_titles):
            plot = self.layout_widget.addPlot(row=index, col=0)
            plot.showGrid(x=True, y=True, alpha=0.25)
            plot.setTitle(title, size="9pt")
            plot.addLegend(offset=(-10, 4), labelTextSize="9pt")
            plot.setClipToView(True)
            plot.setDownsampling(auto=True, mode="peak")
            plot.hideButtons()
            if index:
                plot.setXLink(self.plots[0])
            self.plots.append(plot)
        self.channels = []      # [(key, name, color, plot, dashed)]
        self.curves = {}
        self.ring = Ring(0)
        self.window = 10.0
        self.paused = False
        self.origin = None
        self.markers = []
        self.cursor_lines = []
        self.readout = []
        for plot in self.plots:
            line = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen((128, 128, 128, 160), style=Qt.DotLine))
            line.hide()
            plot.addItem(line, ignoreBounds=True)
            self.cursor_lines.append(line)
            text = pg.TextItem(anchor=(0, 0))
            text.hide()
            plot.addItem(text, ignoreBounds=True)
            self.readout.append(text)
        self.layout_widget.scene().sigMouseMoved.connect(self.hover)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.redraw)
        self.timer.start(33)
        theme.bus.changed.connect(lambda _: self.apply_theme())
        self.apply_theme()

    # ---------------- 通道 ----------------
    def set_channels(self, channels):
        """channels: [(key, 名称, 颜色, 图序号或 -1 不显示, 虚线)]。键集合变化时清空缓冲。"""
        keys = [c[0] for c in channels]
        if keys != [c[0] for c in self.channels]:
            self.ring = Ring(len(channels))
            self.origin = None
            self.clear_markers()
        self.channels = list(channels)
        for curve in self.curves.values():
            curve.getViewBox() and curve.getViewBox().removeItem(curve)
        for plot in self.plots:
            plot.legend.clear()
        self.curves = {}
        for column, (key, name, color, plot_index, dashed) in enumerate(channels):
            if not 0 <= plot_index < len(self.plots):
                continue
            pen = pg.mkPen(color, width=1.6, style=Qt.DashLine if dashed else Qt.SolidLine)
            curve = self.plots[plot_index].plot(name=name, pen=pen, connect="finite")
            curve.column = column + 1
            self.curves[key] = curve
        used = {c[3] for c in channels}
        for index, plot in enumerate(self.plots):
            plot.setVisible(index in used)
        self.apply_theme()
        self.redraw(force=True)

    def append(self, times, values):
        """times: [t]，values: [[各通道值]]，单位秒（任意起点）。"""
        if not len(times) or not self.channels:
            return
        if self.origin is None:
            self.origin = times[0]
        rows = np.column_stack((np.asarray(times, dtype=float) - self.origin, np.asarray(values, dtype=float)))
        self.ring.append(rows)

    def clear(self):
        self.ring = Ring(len(self.channels))
        self.origin = None
        self.clear_markers()
        self.redraw(force=True)

    def now(self):
        last = self.ring.last()
        return None if last is None else float(last[0])

    def series(self, key, start=None, end=None):
        """返回 (t, y) numpy 数组，t 与 now() 同一时间基准。"""
        columns = [c[0] for c in self.channels]
        if key not in columns or not self.ring.count:
            return np.empty(0), np.empty(0)
        data = self.ring.view()
        t = data[:, 0]
        lo = 0 if start is None else np.searchsorted(t, start)
        hi = len(t) if end is None else np.searchsorted(t, end, side="right")
        return t[lo:hi], data[lo:hi, columns.index(key) + 1]

    # ---------------- 标记 ----------------
    def add_marker(self, t, text, color=None):
        color = color or theme.current().accent
        for plot in self.plots:
            line = pg.InfiniteLine(pos=t, angle=90, movable=False,
                                   pen=pg.mkPen(color, width=1, style=Qt.DashLine),
                                   label=text if plot is self.plots[0] else None,
                                   labelOpts={"position": 0.96, "color": color, "anchors": [(0, 0), (0, 0)]})
            plot.addItem(line, ignoreBounds=True)
            self.markers.append((plot, line))
        while len(self.markers) > 60 * len(self.plots):
            plot, line = self.markers.pop(0)
            plot.removeItem(line)

    def clear_markers(self):
        for plot, line in self.markers:
            plot.removeItem(line)
        self.markers = []

    # ---------------- 绘制 ----------------
    def set_paused(self, paused):
        self.paused = paused
        for plot in self.plots:
            plot.setMouseEnabled(x=paused, y=True)
        if not paused:
            self.auto_y()

    def auto_y(self):
        for plot in self.plots:
            plot.enableAutoRange(axis="y")

    def redraw(self, force=False):
        if (self.paused and not force) or not self.isVisible() and not force:
            return
        if not self.ring.count:
            for curve in self.curves.values():
                curve.setData([], [])
            return
        data = self.ring.view()
        t = data[:, 0]
        end = t[-1]
        lo = np.searchsorted(t, end - self.window * 1.05)
        part = data[lo:]
        for curve in self.curves.values():
            curve.setData(part[:, 0], part[:, curve.column])
        self.plots[0].setXRange(max(0.0, end - self.window), max(self.window, end), padding=0)

    def hover(self, position):
        hit = None
        for index, plot in enumerate(self.plots):
            if plot.isVisible() and plot.sceneBoundingRect().contains(position):
                hit = index
        for index, (line, text) in enumerate(zip(self.cursor_lines, self.readout)):
            line.setVisible(hit is not None)
            text.setVisible(index == hit)
        if hit is None or not self.ring.count:
            return
        x = self.plots[hit].vb.mapSceneToView(position).x()
        for line in self.cursor_lines:
            line.setPos(x)
        data = self.ring.view()
        i = int(np.clip(np.searchsorted(data[:, 0], x), 0, len(data) - 1))
        lines = [f"t = {data[i, 0]:.3f} s"]
        for key, name, color, plot_index, _ in self.channels:
            if plot_index == hit:
                value = data[i, [c[0] for c in self.channels].index(key) + 1]
                lines.append(f"{name}: {value:.6g}" if math.isfinite(value) else f"{name}: —")
        text = self.readout[hit]
        text.setText("\n".join(lines))
        view = self.plots[hit].vb.viewRange()
        text.setPos(x, view[1][1])

    def apply_theme(self):
        look = theme.current()
        pal = look.pal
        text, grid = QColor(pal.text), QColor(pal.muted)
        font = QFont("Microsoft YaHei UI", 9)
        for plot, label in zip(self.plots, self.readout):
            plot.getViewBox().setBackgroundColor(look.color("card", 0.05))
            for name in ("left", "bottom"):
                axis = plot.getAxis(name)
                axis.setPen(pg.mkPen(grid))
                axis.setTextPen(pg.mkPen(text))
                axis.setTickFont(font)
            plot.setTitle(self.titles[self.plots.index(plot)], size="9pt", color=pal.text)
            label.setColor(text)
            label.fill = pg.mkBrush(QColor(pal.menu))
            if plot.legend is not None:
                plot.legend.setLabelTextColor(text)
                plot.legend.setBrush(pg.mkBrush(look.color("card", 0.2)))

    # ---------------- 导出 ----------------
    def export_csv(self, path):
        data = self.ring.view()
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["时间_s"] + [c[1] for c in self.channels])
            for row in data:
                writer.writerow([f"{row[0]:.4f}"] + ["" if not math.isfinite(v) else f"{v:.7g}" for v in row[1:]])
        return len(data)
