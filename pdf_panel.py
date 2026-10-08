"""
pdf_panel.py - Visor de PDF: la diapositiva se ajusta a la ventana.

Modos: "página" (cabe entera, por defecto), "ancho" (ocupa todo el ancho) y
manual (zoom fijo elegido con los botones, Ctrl + / Ctrl -, Ctrl+rueda).
Ctrl+0 vuelve a ajustar. Se renderiza a la densidad real de la pantalla.
"""

import fitz
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QScrollArea, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QPixmap, QImage, QShortcut, QKeySequence

import theme


class PDFPanel(QWidget):
    """Vista central del PDF con navegación y zoom adaptativo."""

    slide_changed = Signal(int)   # 1-based
    pdf_loaded    = Signal(int)   # total páginas

    MIN_ZOOM, MAX_ZOOM = 0.15, 6.0
    MARGIN = 16                   # aire alrededor de la diapositiva

    def __init__(self):
        super().__init__()
        self.doc = None
        self.current_page = 0
        self.total_pages  = 0
        self._pdf_path    = ""
        self._fit = "page"        # "page" | "width" | "manual"
        self._zoom = 1.0          # zoom efectivo que se está usando
        self._setup_ui()
        self._setup_shortcuts()
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(120)
        self._resize_timer.timeout.connect(self._render)

    # ------------------------------------------------------------------

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Área scroll con la imagen de la diapositiva
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)          # el tamaño lo manda la imagen
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet(f"background:{theme.BG}; border:none;")
        self.scroll.viewport().installEventFilter(self)

        self.slide_label = QLabel("Abre un PDF para comenzar")
        self.slide_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.slide_label.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:15px; background:transparent;")
        self.slide_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.scroll.setWidget(self.slide_label)

        layout.addWidget(self.scroll)

        # Barra de navegación
        nav = QHBoxLayout()
        nav.setContentsMargins(12, 6, 12, 6)

        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedSize(36, 30)
        self.btn_prev.setEnabled(False)
        self.btn_prev.clicked.connect(self.prev_page)
        self.btn_prev.setStyleSheet(self._nav_btn_style())

        self.lbl_page = QLabel("—")
        self.lbl_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_page.setMinimumWidth(100)
        self.lbl_page.setStyleSheet(f"background:transparent; font-size:12px; color:{theme.MUTED}; font-weight:600;")

        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedSize(36, 30)
        self.btn_next.setEnabled(False)
        self.btn_next.clicked.connect(self.next_page)
        self.btn_next.setStyleSheet(self._nav_btn_style())

        self.btn_zoom_out = QPushButton("−")
        self.btn_zoom_out.setFixedSize(30, 30)
        self.btn_zoom_out.setToolTip("Alejar  (Ctrl -)")
        self.btn_zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        self.btn_zoom_out.setStyleSheet(self._nav_btn_style())

        self.lbl_zoom = QLabel("—")
        self.lbl_zoom.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_zoom.setMinimumWidth(48)
        self.lbl_zoom.setStyleSheet(f"background:transparent; font-size:11px; color:{theme.DIM};")

        self.btn_zoom_in = QPushButton("+")
        self.btn_zoom_in.setFixedSize(30, 30)
        self.btn_zoom_in.setToolTip("Acercar  (Ctrl +)")
        self.btn_zoom_in.clicked.connect(lambda: self.zoom_by(1.25))
        self.btn_zoom_in.setStyleSheet(self._nav_btn_style())

        self.btn_fit = QPushButton("Ajustar")
        self.btn_fit.setFixedHeight(30)
        self.btn_fit.setToolTip("Cambia entre página completa y ancho  (Ctrl+0 vuelve a ajustar)")
        self.btn_fit.clicked.connect(self.toggle_fit)
        self.btn_fit.setStyleSheet(self._nav_btn_style())

        nav.addWidget(self.btn_zoom_out)
        nav.addWidget(self.lbl_zoom)
        nav.addWidget(self.btn_zoom_in)
        nav.addWidget(self.btn_fit)
        nav.addStretch()
        nav.addWidget(self.btn_prev)
        nav.addWidget(self.lbl_page)
        nav.addWidget(self.btn_next)
        nav.addStretch()

        nav_widget = QWidget()
        nav_widget.setFixedHeight(44)
        nav_widget.setStyleSheet(f"background:{theme.SURFACE}; border-top:1px solid {theme.BORDER};")
        nav_widget.setLayout(nav)
        layout.addWidget(nav_widget)

    def _setup_shortcuts(self):
        QShortcut(QKeySequence(Qt.Key.Key_Left),  self, self.prev_page)
        QShortcut(QKeySequence(Qt.Key.Key_Right), self, self.next_page)
        QShortcut(QKeySequence("Ctrl++"), self, lambda: self.zoom_by(1.25))
        QShortcut(QKeySequence("Ctrl+="), self, lambda: self.zoom_by(1.25))
        QShortcut(QKeySequence("Ctrl+-"), self, lambda: self.zoom_by(1 / 1.25))
        QShortcut(QKeySequence("Ctrl+0"), self, self.fit_page)

    # ── Zoom y ajuste ────────────────────────────────────────────────────

    def fit_page(self):
        self._fit = "page"
        self._render()

    def fit_width(self):
        self._fit = "width"
        self._render()

    def toggle_fit(self):
        self._fit = "width" if self._fit in ("page", "manual") else "page"
        self._render()

    def zoom_by(self, factor: float):
        if not self.doc:
            return
        self._fit = "manual"
        self._zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, self._zoom * factor))
        self._render()

    def _viewport_size(self) -> tuple[int, int]:
        vp = self.scroll.viewport().size()
        return max(50, vp.width() - self.MARGIN), max(50, vp.height() - self.MARGIN)

    def _zoom_for(self, page) -> float:
        """Zoom que toca según el modo (en puntos PDF → píxeles lógicos)."""
        r = page.rect
        if not r.width or not r.height:
            return 1.0
        vw, vh = self._viewport_size()
        if self._fit == "width":
            return vw / r.width
        if self._fit == "manual":
            return self._zoom
        return min(vw / r.width, vh / r.height)          # página completa

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if obj is self.scroll.viewport():
            if event.type() == QEvent.Type.Resize and self.doc and self._fit != "manual":
                self._resize_timer.start()
            elif event.type() == QEvent.Type.Wheel and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.zoom_by(1.25 if event.angleDelta().y() > 0 else 1 / 1.25)
                return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _nav_btn_style():
        return (
            f"QPushButton{{background:{theme.SURFACE_2};color:{theme.TEXT};border:1px solid {theme.BORDER};"
            f"border-radius:8px;font-size:13px;}}"
            f"QPushButton:hover{{background:{theme.SURFACE_3};}}"
            f"QPushButton:disabled{{color:{theme.DIM};background:{theme.SURFACE};border-color:{theme.SURFACE_2};}}"
        )

    # ------------------------------------------------------------------

    def load_pdf(self, path: str):
        self._pdf_path = path
        try:
            self.doc = fitz.open(path)
            self.total_pages  = len(self.doc)
            self.current_page = 0
            self._fit = "page"                    # cada PDF nuevo entra ajustado
            self._render()
            self.btn_prev.setEnabled(False)
            self.btn_next.setEnabled(self.total_pages > 1)
            self.pdf_loaded.emit(self.total_pages)
        except Exception as e:
            self.slide_label.setText(f"Error abriendo PDF:\n{e}")

    def go_to_page(self, index: int):
        """Navega a una página por índice 0-based sin emitir señal."""
        if self.doc and 0 <= index < self.total_pages:
            self.current_page = index
            self._render()
            self._update_nav()

    # ------------------------------------------------------------------

    def prev_page(self):
        if self.doc and self.current_page > 0:
            self.current_page -= 1
            self._render()
            self._update_nav()
            self.slide_changed.emit(self.current_page + 1)

    def next_page(self):
        if self.doc and self.current_page < self.total_pages - 1:
            self.current_page += 1
            self._render()
            self._update_nav()
            self.slide_changed.emit(self.current_page + 1)

    # ------------------------------------------------------------------

    def _render(self):
        if not self.doc:
            return
        try:
            page = self.doc[self.current_page]
            zoom = self._zoom = self._zoom_for(page)
            dpr = self.devicePixelRatioF() or 1.0            # nitidez en pantallas HiDPI
            mat = fitz.Matrix(zoom * dpr, zoom * dpr)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            img = QImage(pix.samples, pix.width, pix.height,
                         pix.stride, QImage.Format.Format_RGB888).copy()
            px = QPixmap.fromImage(img)
            px.setDevicePixelRatio(dpr)
            self.slide_label.setPixmap(px)
            self.slide_label.resize(px.size() / dpr)
            self.lbl_page.setText(f"{self.current_page + 1}  /  {self.total_pages}")
            modo = {"page": "página", "width": "ancho", "manual": ""}[self._fit]
            self.lbl_zoom.setText(f"{zoom * 100:.0f} %" + (f" · {modo}" if modo else ""))
        except Exception as e:
            self.slide_label.setText(f"Error renderizando:\n{e}")

    def _update_nav(self):
        self.btn_prev.setEnabled(self.current_page > 0)
        self.btn_next.setEnabled(self.current_page < self.total_pages - 1)
