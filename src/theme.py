"""界面主题：浅色（樱花）/ 深色调色板、背景图与透明度，统一生成样式表并持久化。

- 颜色集中在 Palette；style.qss 是 ${名称} 模板，由 Theme.stylesheet() 填值。
- 背景图画在主窗口中央控件上（等比裁切铺满），卡片使用带透明度的面板色。
- 背景图路径、开关、背景不透明度、面板不透明度、遮罩强度、强调色按「浅色 / 深色」分别记忆。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
import os
from pathlib import Path
from string import Template

from PyQt5.QtCore import QObject, QRectF, QSettings, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QImageReader, QPainter, QPixmap
from PyQt5.QtWidgets import QWidget

DEFAULT_ACCENT = "#e0709a"  # 樱花粉
DEFAULT_BG = os.path.join(os.path.expanduser("~"), "Pictures",
                          "Wallpaper Alchemy - 樱花初音未来粉色动漫壁纸 4K.jpg")
ACCENTS = ("#e0709a", "#d9467b", "#9c6ade", "#3f8fd8", "#13a08f", "#e08a2e")


@dataclass(frozen=True)
class Palette:
    dark: bool
    window: str
    header: str
    header_text: str
    header_muted: str
    card: str
    nav: str
    tune: str
    field: str
    menu: str
    button: str
    button_hover: str
    button_pressed: str
    border: str
    border_strong: str
    text: str
    section: str
    muted: str
    disabled_text: str
    disabled_bg: str
    overlay: str
    grid: str
    idle_bg: str
    idle_text: str
    ok_bg: str
    ok_text: str
    busy_bg: str
    busy_text: str
    fault_bg: str
    fault_text: str
    note_bg: str
    note_text: str
    danger: str
    dirty_bg: str
    curves: tuple


LIGHT = Palette(
    dark=False, window="#fbf4f7", header="#fff7fa", header_text="#3a2a33", header_muted="#8a7180",
    card="#ffffff", nav="#fdf6f9", tune="#fbf1f5", field="#ffffff", menu="#ffffff",
    button="#ffffff", button_hover="#fbe3ec", button_pressed="#f4cddc",
    border="#ecd2dd", border_strong="#c995ab", text="#3a2a33", section="#2f1f29", muted="#7d6672",
    disabled_text="#a8979f", disabled_bg="#f5eff2", overlay="#ffffff", grid="#ead9e1",
    idle_bg="#f3e9ee", idle_text="#6d5664", ok_bg="#dcf4e6", ok_text="#1d6b3f",
    busy_bg="#fff0d6", busy_text="#87540b", fault_bg="#fde4e8", fault_text="#a2243c",
    note_bg="#fdf0f5", note_text="#7a3f58", danger="#cf3b55", dirty_bg="#fff3c4",
    curves=("#d9467b", "#7b5cc4", "#1a8f84"),
)

DARK = Palette(
    dark=True, window="#17141a", header="#211c23", header_text="#f7eef3", header_muted="#b9a7b2",
    card="#221d24", nav="#272027", tune="#2a2229", field="#1a161b", menu="#262027",
    button="#30282f", button_hover="#45383f", button_pressed="#241e23",
    border="#4b3f49", border_strong="#7a6574", text="#f4eef2", section="#fff6fa", muted="#b7a9b2",
    disabled_text="#7c7178", disabled_bg="#2a2429", overlay="#120d12", grid="#3e343c",
    idle_bg="#3a3038", idle_text="#d9c9d2", ok_bg="#1f4b33", ok_text="#9be2b5",
    busy_bg="#4d3b18", busy_text="#f3cf8a", fault_bg="#5a2230", fault_text="#ffb3c1",
    note_bg="#3a2a33", note_text="#f3c6d8", danger="#e0485f", dirty_bg="#6b5517",
    curves=("#ff8fb8", "#b9a2ff", "#5fd3c4"),
)


def rgba(color, alpha):
    c = QColor(color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{max(0, min(255, round(alpha * 255)))})"


def mix(a, b, t):
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * t),
                  round(ca.green() + (cb.green() - ca.green()) * t),
                  round(ca.blue() + (cb.blue() - ca.blue()) * t)).name()


@dataclass
class ThemeVariant:
    """某个明暗模式下的外观参数，分别记忆。"""
    bg_enabled: bool = True
    bg_opacity: int = 100      # 背景图不透明度 %
    panel_opacity: int = 70    # 卡片不透明度 %，越低越透出背景
    overlay: int = 10          # 遮罩强度 %（浅色提亮 / 深色压暗）
    accent: str = DEFAULT_ACCENT


@dataclass
class Appearance:
    mode: str = "light"
    bg_path: str = DEFAULT_BG
    variants: dict = field(default_factory=lambda: {
        "light": ThemeVariant(panel_opacity=70, overlay=10),
        "dark": ThemeVariant(panel_opacity=80, overlay=55),
    })

    @property
    def dark(self):
        return self.mode == "dark"

    def variant(self, dark=None):
        return self.variants["dark" if (self.dark if dark is None else dark) else "light"]

    @classmethod
    def load(cls, settings: QSettings):
        a = cls()
        mode = str(settings.value("appearance/mode", a.mode))
        a.mode = mode if mode in ("light", "dark") else "light"
        a.bg_path = str(settings.value("appearance/bgPath", a.bg_path) or "")
        for key, variant in a.variants.items():
            values = {}
            for f in fields(ThemeVariant):
                raw = settings.value(f"appearance/{key}/{f.name}")
                if raw is None:
                    continue
                try:
                    if f.type in (bool, "bool"):
                        values[f.name] = raw in (True, "true", "1", 1)
                    elif f.type in (int, "int"):
                        values[f.name] = max(0, min(100, int(raw)))
                    elif QColor(str(raw)).isValid():
                        values[f.name] = str(raw)
                except (TypeError, ValueError):
                    pass
            a.variants[key] = replace(variant, **values)
        return a

    def save(self, settings: QSettings):
        settings.setValue("appearance/mode", self.mode)
        settings.setValue("appearance/bgPath", self.bg_path)
        for key, variant in self.variants.items():
            for name, value in asdict(variant).items():
                settings.setValue(f"appearance/{key}/{name}", value)
        settings.sync()

    def copy(self):
        return Appearance(self.mode, self.bg_path, {k: replace(v) for k, v in self.variants.items()})


@dataclass(frozen=True)
class Theme:
    pal: Palette
    accent: str
    panel_alpha: float
    has_image: bool

    @property
    def accent_on(self):
        return "#ffffff" if QColor(self.accent).lightness() < 175 else "#2a1a22"

    def alpha(self, extra=0.0):
        return max(0.0, min(1.0, self.panel_alpha + extra)) if self.has_image else 1.0

    def color(self, name, extra=0.0):
        """带当前透明度的 QColor，供自绘控件使用。"""
        c = QColor(getattr(self.pal, name))
        c.setAlphaF(self.alpha(extra))
        return c

    def stylesheet(self, template):
        p, a, acc = self.pal, self.alpha, self.accent
        translucent = lambda color, extra=0.0: rgba(color, a(extra))  # noqa: E731
        values = dict(
            window=p.window if not self.has_image else "transparent",
            header=translucent(p.header, 0.08), header_text=p.header_text, header_muted=p.header_muted,
            chip=translucent(p.button, 0.1), card=translucent(p.card), nav=translucent(p.nav, 0.05),
            tune=translucent(p.tune, 0.05), field=translucent(p.field, 0.15),
            field_alt=translucent(mix(p.field, acc, 0.05), 0.15), table_head=translucent(mix(p.nav, acc, 0.08), 0.15),
            menu=p.menu, button=translucent(p.button, 0.15), button_hover=p.button_hover,
            button_pressed=p.button_pressed, border=rgba(p.border, 0.9), border_strong=p.border_strong,
            text=p.text, section=p.section, muted=p.muted, disabled_text=p.disabled_text,
            disabled_bg=translucent(p.disabled_bg, 0.1), idle_bg=p.idle_bg, idle_text=p.idle_text,
            ok_bg=p.ok_bg, ok_text=p.ok_text, busy_bg=p.busy_bg, busy_text=p.busy_text,
            fault_bg=p.fault_bg, fault_text=p.fault_text, note_bg=translucent(p.note_bg, 0.1),
            note_text=p.note_text, danger=p.danger, danger_hover=mix(p.danger, "#000000", 0.15),
            dirty_bg=p.dirty_bg, scroll=rgba(p.border_strong, 0.7),
            accent=acc, accent_soft=rgba(acc, 0.28 if p.dark else 0.2), accent_on=self.accent_on,
            accent_hover=mix(acc, "#ffffff" if p.dark else "#000000", 0.12),
            accent_text=mix(acc, p.text, 0.25 if p.dark else 0.35),
        )
        return Template(template).substitute(values)


def build_theme(appearance: Appearance, has_image: bool):
    v = appearance.variant()
    accent = v.accent if QColor(v.accent).isValid() else DEFAULT_ACCENT
    return Theme(DARK if appearance.dark else LIGHT, accent, v.panel_opacity / 100, has_image)


_current = Theme(LIGHT, DEFAULT_ACCENT, 1.0, False)


def current():
    return _current


def set_current(theme):
    global _current
    _current = theme
    bus.changed.emit(theme)


class _Bus(QObject):
    changed = pyqtSignal(object)


bus = _Bus()


class BackgroundWidget(QWidget):
    """画背景图的容器：等比裁切铺满，缓存缩放结果，再叠加遮罩。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._src = None
        self._path = None
        self._cache = None
        self._cache_size = None
        self._opacity = 1.0
        self._overlay = QColor(0, 0, 0, 0)
        self._base = QColor("#ffffff")
        self.error = ""

    def set_image(self, path):
        """加载背景图；失败返回 False 并记录 error。路径相同不重复加载。"""
        path = path or ""
        if path == self._path:
            return self._src is not None
        self._path, self._src, self._cache, self.error = path, None, None, ""
        if path:
            if not Path(path).is_file():
                self.error = "文件不存在"
            else:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                size = reader.size()
                screen = self.screen().availableGeometry().size() if self.screen() else None
                if size.isValid() and screen is not None:
                    dpr = self.devicePixelRatioF()
                    scale = max(screen.width() * dpr / size.width(), screen.height() * dpr / size.height())
                    if scale < 1.0:  # 4K 原图按屏幕尺寸读取，省内存
                        reader.setScaledSize(size * scale)
                image = reader.read()
                if image.isNull():
                    self.error = reader.errorString() or "无法读取图片"
                else:
                    self._src = QPixmap.fromImage(image)
        self.update()
        return self._src is not None

    @property
    def has_image(self):
        return self._src is not None

    def set_look(self, base, opacity, overlay, strength):
        self._base = QColor(base)
        self._opacity = max(0.0, min(1.0, opacity))
        self._overlay = QColor(overlay)
        self._overlay.setAlphaF(max(0.0, min(1.0, strength)))
        self.update()

    def _scaled(self):
        if self._src is None:
            return None
        dpr = self.devicePixelRatioF()
        size = (max(1, round(self.width() * dpr)), max(1, round(self.height() * dpr)))
        if self._cache is None or self._cache_size != size:
            pixmap = self._src.scaled(size[0], size[1], Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            x = max(0, (pixmap.width() - size[0]) // 2)
            y = max(0, (pixmap.height() - size[1]) // 3)
            pixmap = pixmap.copy(x, y, size[0], size[1])
            pixmap.setDevicePixelRatio(dpr)
            self._cache, self._cache_size = pixmap, size
        return self._cache

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = event.rect()
        painter.fillRect(rect, self._base)
        pixmap = self._scaled()
        if pixmap is not None:
            painter.setOpacity(self._opacity)
            dpr = pixmap.devicePixelRatio()
            painter.drawPixmap(QRectF(rect), pixmap,
                               QRectF(rect.x() * dpr, rect.y() * dpr, rect.width() * dpr, rect.height() * dpr))
            painter.setOpacity(1.0)
            if self._overlay.alpha():
                painter.fillRect(rect, self._overlay)
        painter.end()
