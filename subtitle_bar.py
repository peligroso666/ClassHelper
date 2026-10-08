"""
subtitle_bar.py - Subtítulos en directo bajo la diapositiva.

La transcripción en vivo (Whisper small) sirve para seguir el hilo, no para estudiar:
se muestra como subtítulo, grande y de paso, con la frase anterior en gris. Los apuntes
buenos salen después del audio con turbo (ver auto_process.py).
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton
from PySide6.QtCore import Qt, QTimer

import theme
from widgets import Pill


class SubtitleBar(QWidget):
    """Dos líneas: lo último que se ha oído y, encima, lo anterior en gris."""

    MAX_CHARS = 240                      # lo que cabe sin que el texto se haga diminuto

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("subtitleBar")
        self.setStyleSheet(
            f"QWidget#subtitleBar{{background:{theme.SURFACE}; border-top:1px solid {theme.BORDER};}}"
        )
        self.setFixedHeight(96)

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 8, 18, 10)
        v.setSpacing(4)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.pill = Pill("SUBTÍTULOS EN DIRECTO", theme.SURFACE_2, theme.DIM)
        self.pill.setToolTip(
            "Orientativo: es la transcripción rápida para seguir la clase.\n"
            "Los apuntes buenos se generan del audio al terminar."
        )
        top.addWidget(self.pill)
        self.lbl_state = QLabel("")
        self.lbl_state.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:10px;")
        top.addWidget(self.lbl_state)
        top.addStretch()
        self.btn_hide = QPushButton("Ocultar")
        self.btn_hide.setProperty("flat", True)
        self.btn_hide.setFixedHeight(22)
        self.btn_hide.setToolTip("Oculta los subtítulos (la clase se sigue grabando igual)")
        self.btn_hide.clicked.connect(self.hide)
        top.addWidget(self.btn_hide)
        v.addLayout(top)

        self.lbl_prev = QLabel("")
        self.lbl_prev.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:12px;")
        self.lbl_prev.setWordWrap(False)
        v.addWidget(self.lbl_prev)

        self.lbl_now = QLabel("Esperando a que empiece la clase…")
        self.lbl_now.setStyleSheet(
            f"background:transparent; color:{theme.TEXT}; font-size:15px; font-weight:600;"
        )
        self.lbl_now.setWordWrap(True)
        v.addWidget(self.lbl_now, 1)

        self._last_id: int | None = None
        self._blink = QTimer(self)
        self._blink.setSingleShot(True)
        self._blink.setInterval(1200)
        self._blink.timeout.connect(lambda: self.lbl_state.setText(""))

    # ── API ──────────────────────────────────────────────────────────────

    def start(self):
        self.lbl_prev.setText("")
        self.lbl_now.setText("Escuchando…")
        self.lbl_state.setText("")
        self._last_id = None
        self.show()

    def stop(self):
        self.lbl_state.setText("clase terminada")
        self.lbl_now.setText("Los apuntes buenos se están generando del audio…")
        self.lbl_prev.setText("")

    def add(self, chunk_id: int, text: str):
        """Texto nuevo de Whisper (el de la barra pasa a ser el anterior)."""
        text = _short(text, self.MAX_CHARS)
        if not text:
            return
        self.lbl_prev.setText(_short(self.lbl_now.text(), 120)
                              if self._last_id is not None else "")
        self.lbl_now.setText(text)
        self._last_id = chunk_id

    def update_enhanced(self, chunk_id: int, text: str):
        """La IA ha pulido ese trozo: si sigue en pantalla, se sustituye."""
        if chunk_id == self._last_id and text.strip():
            self.lbl_now.setText(_short(text, self.MAX_CHARS))
            self.lbl_state.setText("✦ corregido por IA")
            self._blink.start()


def _short(text: str, limit: int) -> str:
    """Se queda con el final, que es lo que se acaba de decir."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    corte = text[-limit:]
    esp = corte.find(" ")
    return "…" + (corte[esp + 1:] if esp > 0 else corte)
