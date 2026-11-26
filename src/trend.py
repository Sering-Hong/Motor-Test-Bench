from __future__ import annotations

import bisect
from collections import deque
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt5.QtWidgets import QWidget

import theme


class Trend(QWidget):
    def __init__(self, title, unit, curve, target=False, target_name="目标"):
        super().__init__()
        self.title = title
        self.unit = unit
        self.curve = curve  # 主题曲线色序号
        self.has_target = target
        self.target_name = target_name
        theme.bus.changed.connect(lambda _: self.update())
        self.points = deque(maxlen=6000)
        self.window = 10
        self.setMinimumHeight(142)
        self.setMouseTracking(True)
        self.cursor = None
        self.zoom = 1.0

    def set_zoom(self, scale):
        self.zoom = scale
        self.setMinimumHeight(round(142 * scale))
        self.update()

    def clear(self):
        self.points.clear()
        self.update()

    def add(self, t, value, target=None):
        self.points.append((t, value, target))
        self.update()

    def mouseMoveEvent(self, event):
        self.cursor = event.localPos() / self.zoom
        self.update()

    def leaveEvent(self, event):
        self.cursor = None
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = painter.font()
        font.setPixelSize(max(1, round(painter.fontInfo().pixelSize() / self.zoom)))
        painter.setFont(font)
        painter.scale(self.zoom, self.zoom)
        width, height = self.width() / self.zoom, self.height() / self.zoom
        look = theme.current()
        pal = look.pal
        accent = QColor(pal.curves[self.curve % len(pal.curves)])
        painter.setPen(QPen(QColor(pal.border)))
        painter.setBrush(look.color("card"))
        painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 12, 12)
        title_font = painter.font()
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QColor(pal.text))
        painter.drawText(QRectF(16, 9, width - 32, 22), Qt.AlignLeft | Qt.AlignVCenter, self.title)
        title_font.setBold(False)
        painter.setFont(title_font)
        painter.setPen(accent)
        last = self.points[-1][1] if self.points else None
        value_text = f"{last:.2f} {self.unit}" if last is not None else f"—  {self.unit}"
        painter.drawText(QRectF(16, 9, width - 32, 22), Qt.AlignRight | Qt.AlignVCenter, value_text)

        plot = QRectF(61, 42, max(5, width - 80), max(10, height - 66))
        end = max(self.window, self.points[-1][0]) if self.points else self.window
        start = end - self.window
        points = [row for row in self.points if row[0] >= start]
        values = [value for row in points for value in row[1:] if value is not None and math.isfinite(value)]
        low, high = (min(values), max(values)) if values else (0.0, 1.0)
        spread = max(high - low, abs(high) * 0.02, 0.05)
        center = (low + high) / 2
        low, high = center - spread * 0.6, center + spread * 0.6
        grid = QColor(pal.grid)
        muted = QColor(pal.muted)
        painter.setFont(painter.font())
        for index in range(4):
            y = plot.top() + plot.height() * index / 3
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(muted)
            label = f"{high - (high - low) * index / 3:.2f}" if values else "–"
            painter.drawText(QRectF(5, y - 9, 49, 18), Qt.AlignRight | Qt.AlignVCenter, label)
        for index in range(5):
            ratio = index / 4
            x = plot.left() + plot.width() * ratio
            painter.setPen(QPen(grid, 1))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(muted)
            painter.drawText(QRectF(x - 20, plot.bottom() + 3, 40, 18), Qt.AlignCenter,
                             f"{start + self.window * ratio:.1f}".rstrip("0").rstrip(".") + "s")
        if not values:
            painter.setPen(muted)
            painter.drawText(plot, Qt.AlignCenter, "等待电机反馈")
            return

        def location(t, value):
            return QPointF(
                plot.left() + (t - start) / self.window * plot.width(),
                plot.bottom() - (value - low) / (high - low) * plot.height(),
            )

        painter.save()
        painter.setBrush(Qt.NoBrush)
        painter.setClipRect(plot.adjusted(0, -1, 0, 1))
        for column in (2, 1) if self.has_target else (1,):
            path = QPainterPath()
            continue_line = False
            for row in points:
                value = row[column]
                if value is None or not math.isfinite(value):
                    continue_line = False
                    continue
                p = location(row[0], value)
                if continue_line:
                    path.lineTo(p)
                else:
                    path.moveTo(p)
                    path.lineTo(p + QPointF(0.6, 0))
                continue_line = True
            pen = QPen(accent if column == 1 else muted, 2 if column == 1 else 1.4)
            if column == 2:
                pen.setStyle(Qt.DashLine)
            painter.setPen(pen)
            painter.drawPath(path)
        painter.restore()
        if self.cursor and plot.contains(self.cursor):
            t = start + (self.cursor.x() - plot.left()) / plot.width() * self.window
            index = min(len(points) - 1, bisect.bisect_left([row[0] for row in points], t))
            t, value, target = points[index]
            if value is not None and math.isfinite(value):
                p = location(t, value)
                painter.setPen(QPen(muted, 1, Qt.DotLine))
                painter.drawLine(QPointF(p.x(), plot.top()), QPointF(p.x(), plot.bottom()))
                painter.setBrush(accent)
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(p, 3, 3)
                painter.setPen(QColor(pal.text))
                text = f"{t:.2f}s  ·  {value:.2f}"
                if target is not None and math.isfinite(target):
                    text += f"  ·  {self.target_name} {target:.2f}"
                x = min(max(p.x() + 7, plot.left()), plot.right() - 230)
                painter.drawText(QRectF(x, plot.top() + 2, 230, 20), Qt.AlignLeft, text)
