"""
thumbnail_strip.py - Panel lateral con miniaturas de todas las diapositivas.
Las miniaturas se cargan en segundo plano para no bloquear la UI.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel, QFrame
from PySide6.QtCore import Qt, Signal, QThread
from PySide6.QtGui import QPixmap, QImage, QCursor

import theme


class _Card(QFrame):
    """Tarjeta de miniatura clickeable."""

    clicked = Signal(int)

    def __init__(self, index: int):
        super().__init__()
        self.index = index
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setFixedWidth(96)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 3)
        layout.setSpacing(3)

        self.img = QLabel()
        self.img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img.setFixedSize(84, 60)
        self.img.setStyleSheet(f"background:{theme.SURFACE_3}; border-radius:3px;")

        self.num = QLabel(str(index + 1))
        self.num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.num.setStyleSheet(f"color:{theme.DIM}; font-size:10px; background:transparent;")

        layout.addWidget(self.img)
        layout.addWidget(self.num)

        self._sel = False
        self._refresh()

    # ------------------------------------------------------------------

    def set_selected(self, v: bool):
        self._sel = v
        self._refresh()
        self.num.setStyleSheet(
            f"color:{theme.TEXT}; font-size:10px; font-weight:700; background:transparent;"
            if v else
            f"color:{theme.DIM}; font-size:10px; background:transparent;"
        )

    def set_pixmap(self, px: QPixmap):
        scaled = px.scaled(84, 60, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self.img.setPixmap(scaled)

    def _refresh(self):
        if self._sel:
            self.setStyleSheet(
                f"QFrame{{background:#1a2a4a;border:2px solid {theme.ACCENT};border-radius:8px;}}"
            )
        else:
            self.setStyleSheet(
                f"QFrame{{background:{theme.SURFACE_2};border:2px solid transparent;border-radius:8px;}}"
                f"QFrame:hover{{background:{theme.SURFACE_3};border-color:{theme.BORDER};}}"
            )

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.index)
        super().mousePressEvent(e)


class _Loader(QThread):
    """Renderiza miniaturas de todas las páginas a baja resolución."""

    ready = Signal(int, QPixmap)

    def __init__(self, path: str, n: int):
        super().__init__()
        self.path = path
        self.n = n
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import fitz
        doc = fitz.open(self.path)
        mat = fitz.Matrix(0.2, 0.2)
        for i in range(self.n):
            if self._stop:
                break
            pix = doc[i].get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format.Format_RGB888)
            self.ready.emit(i, QPixmap.fromImage(img))
        doc.close()


class ThumbnailStrip(QWidget):
    """
    Barra lateral izquierda con miniaturas de todas las diapositivas.
    Emite slide_selected(index) al hacer click en una miniatura.
    """

    slide_selected = Signal(int)  # 0-based

    def __init__(self):
        super().__init__()
        self.setFixedWidth(112)
        self.setStyleSheet(f"background:{theme.SURFACE};")
        self._cards: list[_Card] = []
        self._current = 0
        self._loader: _Loader | None = None
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        header = QLabel("DIAPOS")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setFixedHeight(28)
        header.setStyleSheet(
            f"color:{theme.MUTED}; font-size:10px; font-weight:700; "
            f"background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER}; letter-spacing:1px;"
        )
        root.addWidget(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(f"border:none; background:{theme.SURFACE};")

        self.inner = QWidget()
        self.inner.setStyleSheet(f"background:{theme.SURFACE};")
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(6, 8, 6, 8)
        self.vbox.setSpacing(6)
        self.vbox.addStretch()

        self.scroll.setWidget(self.inner)
        root.addWidget(self.scroll)

    # ------------------------------------------------------------------

    def load(self, path: str, n_pages: int):
        if self._loader:
            self._loader.stop()
            self._loader.wait(2000)

        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self._current = 0

        for i in range(n_pages):
            card = _Card(i)
            card.clicked.connect(self.slide_selected.emit)
            self.vbox.insertWidget(i, card)
            self._cards.append(card)

        if self._cards:
            self._cards[0].set_selected(True)

        self._loader = _Loader(path, n_pages)
        self._loader.ready.connect(self._on_ready)
        self._loader.start()

    def set_page(self, index: int):
        if 0 <= index < len(self._cards):
            self._cards[self._current].set_selected(False)
            self._current = index
            self._cards[index].set_selected(True)
            self.scroll.ensureWidgetVisible(self._cards[index])

    def _on_ready(self, i: int, px: QPixmap):
        if i < len(self._cards):
            self._cards[i].set_pixmap(px)
