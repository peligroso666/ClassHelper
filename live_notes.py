"""
live_notes.py - «Mis notas» durante la clase.

Lo que el alumno escribe mientras el profe habla. Se guarda en la ficha de la clase
(study["notes"]) en cada autoguardado, y luego la IA lo usa para el resumen, la lección
y el tutor: marca lo que a él le importa y sus dudas.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QLabel
from PySide6.QtCore import Qt, QTimer, Signal

import theme
from widgets import Pill


class LiveNotes(QWidget):
    """Bloc de notas de la clase en curso."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        bar = QWidget()
        bar.setFixedHeight(46)
        bar.setStyleSheet(f"background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER};")
        hb = QHBoxLayout(bar)
        hb.setContentsMargins(14, 6, 12, 6)
        hb.setSpacing(8)
        title = QLabel("Mis notas")
        title.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:13px; font-weight:700;")
        hb.addWidget(title)
        self.pill = Pill("se guarda sola", theme.SURFACE_2, theme.DIM)
        hb.addWidget(self.pill)
        hb.addStretch()
        self.btn_slide = QPushButton("＋ Diapo")
        self.btn_slide.setProperty("flat", True)
        self.btn_slide.setFixedHeight(28)
        self.btn_slide.setToolTip("Escribe la diapositiva y la hora actuales para situar la nota")
        hb.addWidget(self.btn_slide)
        v.addWidget(bar)

        self.text = QTextEdit()
        self.text.setPlaceholderText(
            "Apunta aquí lo tuyo mientras el profe habla:\n"
            "  · dudas que te surjan\n"
            "  · lo que diga que entra en el examen\n"
            "  · cosas que quieras repasar luego\n\n"
            "Se guarda con la clase y la IA lo tiene en cuenta al resumir."
        )
        self.text.setStyleSheet(
            f"QTextEdit{{background:{theme.BG}; border:none; padding:14px; font-size:13px;}}"
        )
        self.text.textChanged.connect(self._on_changed)
        v.addWidget(self.text, 1)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(1200)
        self._timer.timeout.connect(self.changed.emit)

    # ── API ──────────────────────────────────────────────────────────────

    def notes(self) -> str:
        return self.text.toPlainText().strip()

    def set_notes(self, text: str):
        self.text.setPlainText(text or "")

    def clear_notes(self):
        self.text.clear()

    def insert_slide_mark(self, slide: int, hora: str):
        self.text.insertPlainText(f"\n— Diapo {slide} ({hora}): ")
        self.text.setFocus()

    def mark_saved(self):
        self.pill.setText("guardado ✓")
        QTimer.singleShot(1500, lambda: self.pill.setText("se guarda sola"))

    def _on_changed(self):
        self._timer.start()
