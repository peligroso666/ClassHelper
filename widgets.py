"""
widgets.py - Widgets pequeños reutilizables: vúmetro, punto de estado, separadores.
"""

from PySide6.QtWidgets import QWidget, QFrame, QLabel, QHBoxLayout
from PySide6.QtCore import Qt, QTimer, QRectF
from PySide6.QtGui import QPainter, QColor, QLinearGradient

import theme


class LevelMeter(QWidget):
    """Vúmetro horizontal de barras: muestra el nivel del micrófono en vivo."""

    BARS = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.BARS * 5 + 4, 22)
        self._level = 0.0        # objetivo (0..1)
        self._shown = 0.0        # valor suavizado que se pinta
        self._peak  = 0.0
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._animate)
        self.setToolTip("Nivel del micrófono")

    def set_active(self, on: bool):
        self._active = on
        if on:
            self._timer.start()
        else:
            self._timer.stop()
            self._level = self._shown = self._peak = 0.0
            self.update()

    def set_level(self, v: float):
        self._level = max(0.0, min(1.0, v))

    def _animate(self):
        # subida rápida, bajada suave
        if self._level > self._shown:
            self._shown = self._level
        else:
            self._shown = max(self._level, self._shown - 0.06)
        self._peak = max(self._shown, self._peak - 0.012)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        bar_w, gap = 3, 2
        lit  = int(round(self._shown * self.BARS))
        peak = int(round(self._peak * self.BARS))
        for i in range(self.BARS):
            x = 2 + i * (bar_w + gap)
            frac = i / self.BARS
            if frac < 0.6:
                col = QColor(theme.SUCCESS)
            elif frac < 0.85:
                col = QColor(theme.WARN)
            else:
                col = QColor(theme.RECORD)
            if i < lit or (i == peak - 1 and peak > 0):
                col.setAlpha(255 if i < lit else 160)
            else:
                col = QColor(theme.SURFACE_3) if self._active else QColor(theme.SURFACE_2)
            # barras más altas hacia la derecha para darle forma
            bh = 6 + int(12 * (0.35 + 0.65 * frac))
            p.setBrush(col)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(QRectF(x, (h - bh) / 2, bar_w, bh), 1.5, 1.5)
        p.end()


class StatusDot(QLabel):
    """Punto de color con pulso para indicar estado (idle / grabando / procesando)."""

    def __init__(self, parent=None):
        super().__init__("●", parent)
        self.setFixedWidth(18)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._color = theme.DIM
        self._on = True
        self._timer = QTimer(self)
        self._timer.setInterval(650)
        self._timer.timeout.connect(self._blink)
        self._apply()

    def set_state(self, state: str):
        """state: 'idle' | 'live' | 'busy'"""
        if state == "live":
            self._color = theme.RECORD
            self._timer.start()
        elif state == "busy":
            self._color = theme.WARN
            self._timer.start()
        else:
            self._color = theme.DIM
            self._timer.stop()
            self._on = True
        self._apply()

    def _blink(self):
        self._on = not self._on
        self._apply()

    def _apply(self):
        c = QColor(self._color)
        if not self._on:
            c.setAlpha(70)
        self.setStyleSheet(f"background:transparent; color: rgba({c.red()},{c.green()},{c.blue()},{c.alpha()}); font-size: 16px;")


class Pill(QLabel):
    """Etiqueta pequeña redondeada (badge)."""

    def __init__(self, text: str = "", bg: str = theme.SURFACE_2, fg: str = theme.MUTED, parent=None):
        super().__init__(text, parent)
        self.set_colors(bg, fg)

    def set_colors(self, bg: str, fg: str):
        self.setStyleSheet(
            f"background:{bg}; color:{fg}; border-radius:9px; padding:2px 8px;"
            f"font-size:10px; font-weight:700;"
        )


def vsep(height: int = 26) -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    f.setFixedSize(1, height)
    f.setStyleSheet(f"background:{theme.BORDER}; border:none;")
    return f


def hsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.HLine)
    f.setFixedHeight(1)
    f.setStyleSheet(f"background:{theme.BORDER}; border:none;")
    return f
