"""
study_panel.py - Modo estudio de una clase grabada (inspirado en Astra AI).

Cinco pestañas sobre una Session de la biblioteca:
  📖 Apuntes  - transcripción en tarjetas por diapositiva, con buscador
  🧭 Lección  - lección guiada generada por IA (se guarda en study["lesson"])
  🃏 Tarjetas - tarjetas de memoria con repetición espaciada (cajas Leitner)
  📝 Test     - test tipo quiz con explicación y lagunas de conocimiento
  💬 Tutor    - chat con la IA sobre el contenido de la clase

Toda llamada a la IA corre en un AITask (QThread) y la sesión se guarda con
session_store.save_session tras cada cambio de estado (generar algo, responder
una tarjeta, terminar un test, enviar un mensaje). El dominio se recalcula con
session_store.compute_mastery cada vez que se guarda.
"""

import os
import re
import html
import random
import datetime

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame, QScrollArea,
    QTabWidget, QLineEdit, QTextEdit, QStackedWidget, QProgressBar, QSizePolicy,
    QMessageBox, QApplication,
)
from PySide6.QtCore import Qt, Signal, QTimer, QRectF
from PySide6.QtGui import QFontMetrics, QPainter, QPen, QColor, QShortcut, QKeySequence

import theme
from widgets import Pill, hsep, vsep
import session_store
from session_store import Session
from study_ai import StudyAI, AITask
from refine import RefineWorker, REFINE_MODEL, SECONDS_PER_AUDIO_SECOND


COL_W  = 720      # ancho máximo de la columna central de cada pestaña
CARD_W = 520      # ancho de la tarjeta de memoria

_DIAS  = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
_LETRAS = "ABCD"
_SIN_TEXTO = "Esta clase no tiene transcripción, así que no hay nada con lo que generar material de estudio."


# ── Utilidades ────────────────────────────────────────────────────────────

def _hoy() -> str:
    return datetime.date.today().isoformat()


def _fecha_larga(iso: str) -> str:
    try:
        d = datetime.datetime.fromisoformat(iso)
    except Exception:
        return iso or "—"
    return f"{_DIAS[d.weekday()]} {d.day} de {_MESES[d.month - 1]} de {d.year} · {d:%H:%M}"


def _fecha_corta(iso: str) -> str:
    try:
        d = datetime.date.fromisoformat(iso[:10])
    except Exception:
        return iso
    return f"{d.day} de {_MESES[d.month - 1]}"


def _duracion(seg: int) -> str:
    """Mismo formato que library_panel.fmt_duration ("" si no hay duración)."""
    seg = int(seg or 0)
    if seg <= 0:
        return ""
    h, m = divmod(seg // 60, 60)
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min" if m else f"{seg} s"


def _mastery_colors(v: int) -> tuple[str, str]:
    """(fondo, texto) del badge de dominio; mismos umbrales que library_panel."""
    if v >= 80:
        return theme.SUCCESS_BG, theme.SUCCESS
    if v >= 50:
        return theme.WARN_BG, theme.WARN
    return theme.SURFACE_3, theme.DIM


def _score_colors(v: int) -> tuple[str, str]:
    """(fondo, texto) para resultados de test: rojo cuando va mal."""
    if v >= 70:
        return theme.SUCCESS_BG, theme.SUCCESS
    if v >= 40:
        return "#5a3d0a", theme.WARN
    return theme.RECORD_BG, theme.RECORD


def _box_colors(box: int) -> tuple[str, str]:
    """(fondo, texto) de la caja Leitner: neutro al empezar, verde al dominarla."""
    if box >= 4:
        return theme.SUCCESS_BG, theme.SUCCESS
    if box >= 2:
        return theme.WARN_BG, theme.WARN
    return theme.SURFACE_3, theme.MUTED


def _column(inner: QWidget, max_w: int = COL_W) -> QWidget:
    """Centra `inner` en una columna de ancho máximo `max_w`."""
    wrap = QWidget()
    h = QHBoxLayout(wrap)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(0)
    inner.setMaximumWidth(max_w)
    inner.setSizePolicy(QSizePolicy.Policy.Expanding, inner.sizePolicy().verticalPolicy())
    h.addStretch()
    h.addWidget(inner, 1)
    h.addStretch()
    return wrap


def _scroll(inner: QWidget) -> QScrollArea:
    s = QScrollArea()
    s.setWidgetResizable(True)
    s.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    s.setWidget(inner)
    return s


def _lbl(text: str, style: str, wrap: bool = False, align=None) -> QLabel:
    l = QLabel(text)
    l.setStyleSheet("background:transparent; " + style)
    l.setWordWrap(wrap)
    if align is not None:
        l.setAlignment(align)
    return l


def _card_frame(selector: str, extra: str = "") -> str:
    return (f"{selector}{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
            f"border-radius:10px; {extra}}}")


def _card(name: str, extra: str = "") -> QFrame:
    """QFrame con estilo de tarjeta. El selector por objectName evita que el borde
    alcance a los QLabel hijos (QLabel hereda de QFrame)."""
    f = QFrame()
    f.setObjectName(name)
    f.setStyleSheet(_card_frame(f"QFrame#{name}", extra))
    return f


def _clear_layout(lay, keep_last: int = 0, start: int = 0):
    """Quita y destruye los widgets de `lay` desde `start`, dejando `keep_last` al final."""
    while lay.count() > start + keep_last:
        it = lay.takeAt(start)
        w = it.widget() if it else None
        if w is not None:
            w.hide()                      # que no siga pintándose hasta el deleteLater
            w.deleteLater()


def _progress_bar() -> QProgressBar:
    p = QProgressBar()
    p.setTextVisible(False)
    p.setFixedHeight(6)
    p.setStyleSheet(
        f"QProgressBar{{background:{theme.SURFACE_2}; border:none; border-radius:3px;}}"
        f"QProgressBar::chunk{{background:{theme.ACCENT}; border-radius:3px;}}"
    )
    return p


# ── Widgets pequeños ──────────────────────────────────────────────────────

class _ElideLabel(QLabel):
    """QLabel de una línea que recorta con "…" en vez de empujar el layout."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)

    def setText(self, text: str):
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, _):
        p = QPainter(self)
        r = self.contentsRect()
        p.setPen(QColor(self.palette().color(self.foregroundRole())))
        p.drawText(r, int(self.alignment()) | Qt.TextFlag.TextSingleLine,
                   self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, r.width()))
        p.end()


class _MasteryRing(QWidget):
    """Anillo de progreso con un QLabel grande "72 %" en el centro."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(72, 72)
        self._value = 0
        self.setToolTip("Dominio de la clase: tarjetas aprendidas y resultados de los tests")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        self.lbl = _lbl("0 %", f"color:{theme.TEXT}; font-size:16px; font-weight:800;",
                        align=Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.lbl)

    def set_value(self, v: int):
        self._value = max(0, min(100, int(v)))
        self.lbl.setText(f"{self._value} %")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(5, 5, self.width() - 10, self.height() - 10)
        pen = QPen(QColor(theme.SURFACE_3), 6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(r, 0, 360 * 16)
        if self._value > 0:
            fg = _mastery_colors(self._value)[1]
            pen.setColor(QColor(theme.MUTED if fg == theme.DIM else fg))
            p.setPen(pen)
            p.drawArc(r, 90 * 16, -int(360 * 16 * self._value / 100))
        p.end()


class _EmptyState(QWidget):
    """Estado vacío centrado: icono, título, texto y un botón principal."""

    def __init__(self, icon: str, title: str, text: str, button_text: str, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 40, 24, 40)
        v.setSpacing(10)
        self._text = text
        v.addStretch()
        v.addWidget(_lbl(icon, "font-size:40px;", align=Qt.AlignmentFlag.AlignCenter))
        v.addWidget(_lbl(title, f"color:{theme.TEXT}; font-size:16px; font-weight:700;",
                         align=Qt.AlignmentFlag.AlignCenter))
        self.lbl_text = _lbl(text, f"color:{theme.MUTED}; font-size:12px; line-height:1.5;",
                             wrap=True, align=Qt.AlignmentFlag.AlignCenter)
        v.addWidget(_column(self.lbl_text, 460))
        v.addSpacing(8)
        self.button = QPushButton(button_text)
        self.button.setProperty("accent", True)
        self.button.setFixedHeight(38)
        self.button.setMinimumWidth(220)
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.secondary = QPushButton("")
        self.secondary.setProperty("flat", True)
        self.secondary.setFixedHeight(34)
        self.secondary.hide()
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch()
        btns.addWidget(self.button)
        btns.addWidget(self.secondary)
        btns.addStretch()
        v.addLayout(btns)
        v.addStretch()

    def set_secondary(self, text: str):
        self.secondary.setText(text)
        self.secondary.setVisible(bool(text))

    def set_available(self, ok: bool):
        """Sin transcripción no hay nada que generar: botones apagados y aviso."""
        self.lbl_text.setText(self._text if ok else _SIN_TEXTO)
        self.button.setEnabled(ok)
        self.secondary.setEnabled(ok)


class _Bubble(QFrame):
    """Burbuja de chat: usuario a la derecha (azul), tutor a la izquierda."""

    MAX_W = int(COL_W * 0.8)
    PAD = 14

    def __init__(self, role: str, text: str, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(self.PAD, 9, self.PAD, 10)
        v.setSpacing(0)
        self.lbl = QLabel()
        self.lbl.setWordWrap(True)
        self.lbl.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        f = self.lbl.font()
        f.setPixelSize(13)
        f.setItalic(role == "thinking")
        self.lbl.setFont(f)
        if role == "user":
            self.setStyleSheet(f"_Bubble{{background:{theme.ACCENT}; border:none; border-radius:14px;"
                               f"border-bottom-right-radius:4px;}}")
            self.lbl.setStyleSheet("background:transparent; color:white;")
        else:
            self.setStyleSheet(f"_Bubble{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
                               f"border-radius:14px; border-bottom-left-radius:4px;}}")
            self.lbl.setStyleSheet(f"background:transparent; color:{theme.DIM if role == 'thinking' else theme.TEXT};")
        self.lbl.setText(text)
        v.addWidget(self.lbl)
        # Ancho según el texto (QLabel con wordWrap pide un sizeHint muy estrecho)
        fm = QFontMetrics(f)
        longest = max((fm.horizontalAdvance(line) for line in text.split("\n")), default=0)
        self.setFixedWidth(min(longest + 2 * self.PAD + 4, self.MAX_W))


class _OptionButton(QPushButton):
    """Opción de test: botón ancho con letra + texto alineado a la izquierda y con
    salto de línea (un QLabel dentro del botón, que QPushButton no sabe envolver)."""

    def __init__(self, letter: str, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # Vertical "Minimum": con "Fixed" Qt toparía la altura al sizeHint del botón
        # (texto vacío) e ignoraría el alto-por-ancho de la etiqueta interior.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(12)
        self.lbl_letter = QLabel(letter)
        self.lbl_letter.setFixedSize(26, 26)
        self.lbl_letter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for l in (self.lbl_letter, self.lbl_text):
            l.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        h.addWidget(self.lbl_letter, 0, Qt.AlignmentFlag.AlignTop)
        h.addWidget(self.lbl_text, 1)
        self.set_state("idle")

    def set_text(self, text: str):
        self.lbl_text.setText(text)
        self.updateGeometry()

    def set_state(self, state: str):
        """state: idle | correct | wrong | dim"""
        if state == "correct":
            bg, bd, fg, lbg, lfg = theme.SUCCESS_BG, theme.SUCCESS, theme.TEXT, theme.SUCCESS, "#06281a"
        elif state == "wrong":
            bg, bd, fg, lbg, lfg = theme.RECORD_BG, theme.RECORD, theme.TEXT, theme.RECORD, "white"
        elif state == "dim":
            bg, bd, fg, lbg, lfg = theme.SURFACE, theme.SURFACE_2, theme.MUTED, theme.SURFACE_2, theme.DIM
        else:
            bg, bd, fg, lbg, lfg = theme.SURFACE_2, theme.BORDER, theme.TEXT, theme.SURFACE_3, theme.MUTED
        hover = (f"QPushButton:hover{{border-color:{theme.ACCENT}; background:{theme.SURFACE_3};}}"
                 f"QPushButton:focus{{border-color:{theme.ACCENT};}}" if state == "idle" else "")
        self.setStyleSheet(
            f"QPushButton{{background:{bg}; border:1px solid {bd}; border-radius:10px; padding:0; text-align:left;}}"
            f"QPushButton:disabled{{background:{bg}; border:1px solid {bd};}}" + hover)
        self.lbl_letter.setStyleSheet(f"background:{lbg}; color:{lfg}; border-radius:13px; font-size:11px; font-weight:700;")
        self.lbl_text.setStyleSheet(f"background:transparent; color:{fg}; font-size:14px;")


class _NoteCard(QFrame):
    """Tarjeta de fragmento de solo lectura (versión mínima de FragmentCard)."""

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(_lbl(str(entry.get("ts", "")),
                            f"color:{theme.DIM}; font-size:10px; font-family:Consolas,monospace;"))
        self.badge = Pill("")
        self.badge.hide()
        head.addWidget(self.badge)
        head.addStretch()
        lay.addLayout(head)

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.lbl_text)

        if entry.get("kind") == "marker":
            self.setStyleSheet(
                f"_NoteCard{{background:{theme.WARN_BG}; border:1px solid #6b4d14;"
                f"border-left:4px solid {theme.WARN}; border-radius:10px;}}"
            )
            self.lbl_text.setStyleSheet("background:transparent; color:#ffd98a; font-size:13px; font-weight:600;")
            self.badge.setText("📌 MARCA")
            self.badge.set_colors("#5a3d0a", theme.WARN)
            self.badge.show()
        else:
            self.setStyleSheet(_card_frame("_NoteCard") + "_NoteCard:hover{border-color:#3a4152;}")
            self.lbl_text.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:13px; line-height:1.5;")
            if entry.get("enhanced") and entry.get("enhanced") != entry.get("raw"):
                self.badge.setText("✦ IA")
                self.badge.set_colors("#2f1f55", theme.AI_H)
                self.badge.show()
        self._highlight = ""
        self._render()

    @property
    def text(self) -> str:
        return session_store.best_text(self.entry)

    def set_highlight(self, needle: str):
        self._highlight = needle
        self._render()

    def matches(self, needle: str) -> bool:
        return needle.lower() in self.text.lower()

    def _render(self):
        t = html.escape(self.text)
        if self._highlight:
            pat = re.compile(re.escape(html.escape(self._highlight)), re.IGNORECASE)
            t = pat.sub(lambda m: f'<span style="background:#5a4a00; color:#fff; border-radius:3px;">{m.group(0)}</span>', t)
        self.lbl_text.setText(t)


# ── Pestaña 1: Apuntes ────────────────────────────────────────────────────

class _NotesTab(QWidget):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._cards: list[_NoteCard] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 14, 16, 8)
        cv.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.pill_count = Pill("0 fragmentos")
        self.pill_slides = Pill("")
        top.addWidget(self.pill_count)
        top.addWidget(self.pill_slides)
        top.addStretch()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar en los apuntes…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedSize(240, 32)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self._apply_search)
        self.search.textChanged.connect(lambda _: self._timer.start())
        top.addWidget(self.search)
        cv.addLayout(top)
        v.addWidget(_column(col))

        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(16, 4, 16, 16)
        self.vbox.setSpacing(8)
        self.vbox.addStretch()
        self.scroll = _scroll(_column(self.inner))
        v.addWidget(self.scroll, 1)

    def load(self):
        _clear_layout(self.vbox, keep_last=1)
        self._cards.clear()
        entries = self.panel.session.entries or []
        last = None
        idx = 0
        for e in entries:
            slide = e.get("slide", 0)
            if slide != last:
                h = _lbl(f"Diapositiva {slide}",
                         f"color:{theme.ACCENT}; font-size:11px; font-weight:700; letter-spacing:1px;"
                         f"padding:10px 4px 2px 4px; text-transform:uppercase;")
                self.vbox.insertWidget(idx, h)
                idx += 1
                last = slide
            c = _NoteCard(e)
            self._cards.append(c)
            self.vbox.insertWidget(idx, c)
            idx += 1
        if not entries:
            self.vbox.insertWidget(0, _lbl("Esta clase no tiene transcripción.",
                                           f"color:{theme.DIM}; font-size:12px; padding:40px;",
                                           align=Qt.AlignmentFlag.AlignCenter))
        n = len(entries)
        self.pill_count.setText(f"{n} fragmento{'s' if n != 1 else ''}")
        slides = sorted({e.get("slide", 0) for e in entries})
        if slides:
            self.pill_slides.setText(f"Diapos {slides[0]} – {slides[-1]}" if len(slides) > 1
                                     else f"Diapo {slides[0]}")
        self.pill_slides.setVisible(bool(slides))
        self.search.clear()

    def _apply_search(self):
        needle = self.search.text().strip()
        first = None
        for c in self._cards:
            c.set_highlight(needle if len(needle) >= 2 else "")
            if needle and first is None and c.matches(needle):
                first = c
        if first:
            self.scroll.ensureWidgetVisible(first, 0, 40)


# ── Pestaña 2: Lección guiada ─────────────────────────────────────────────

class _LessonTab(QWidget):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        v.addWidget(self.stack)

        self.empty = _EmptyState(
            "🧭", "Lección guiada",
            "La IA ordena la transcripción en una lección con introducción, conceptos "
            "explicados paso a paso, ejemplos y un cierre con lo esencial.",
            "Generar lección guiada")
        self.empty.button.clicked.connect(lambda: self._generate(self.empty.button))
        self.stack.addWidget(self.empty)

        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 14, 16, 16)
        cv.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        tt = QVBoxLayout()
        tt.setSpacing(2)
        tt.addWidget(_lbl("Lección guiada", f"color:{theme.TEXT}; font-size:14px; font-weight:700;"))
        tt.addWidget(_lbl("Generada por IA a partir de la transcripción de la clase.",
                          f"color:{theme.MUTED}; font-size:11px;"))
        top.addLayout(tt)
        top.addStretch()
        self.btn_copy = QPushButton("Copiar")
        self.btn_copy.setProperty("flat", True)
        self.btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.text.toPlainText()))
        self.btn_regen = QPushButton("Regenerar")
        self.btn_regen.setProperty("flat", True)
        self.btn_regen.clicked.connect(lambda: self._generate(self.btn_regen))
        for b in (self.btn_copy, self.btn_regen):
            b.setFixedHeight(32)
            top.addWidget(b)
        cv.addLayout(top)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet(
            f"QTextEdit{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
            f"border-radius:10px; padding:14px; font-size:13px;}}"
        )
        cv.addWidget(self.text, 1)
        self.stack.addWidget(_column(col))

    def load(self):
        lesson = self.panel.session.study.get("lesson", "") or ""
        self.text.setPlainText(lesson)
        self.stack.setCurrentIndex(1 if lesson.strip() else 0)

    def _generate(self, btn: QPushButton):
        self.panel.run_task(self.panel.ai.guided_lesson, (self.panel.full_text(),), btn, self._done)

    def _done(self, result):
        text = str(result or "").strip()
        if not text:
            QMessageBox.warning(self, "Lección vacía", "La IA no ha devuelto ninguna lección. Inténtalo de nuevo.")
            return
        self.panel.session.study["lesson"] = text
        self.panel.save()
        self.load()


# ── Pestaña 3: Tarjetas de memoria ────────────────────────────────────────

class _FlashcardsTab(QWidget):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._queue: list[dict] = []
        self._idx = 0
        self._ok = 0
        self._flipped = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        v.addWidget(self.stack)

        # 0 · sin tarjetas
        self.empty = _EmptyState(
            "🃏", "Tarjetas de memoria",
            "La IA extrae preguntas y respuestas de la clase. Cada tarjeta sube de caja cuando "
            "la aciertas y vuelve a salir justo cuando estás a punto de olvidarla.",
            "Generar 15 tarjetas con IA")
        self.empty.button.clicked.connect(lambda: self._generate(15, self.empty.button))
        self.stack.addWidget(self.empty)

        # 1 · nada pendiente hoy
        self.nodue = _EmptyState("✅", "No tienes tarjetas pendientes hoy", "", "Repasar todas igualmente")
        self.nodue.button.clicked.connect(lambda: self._start(all_cards=True))
        self.nodue.set_secondary("Generar 10 más")
        self.nodue.secondary.clicked.connect(lambda: self._generate(10, self.nodue.secondary))
        self.stack.addWidget(self.nodue)

        # 2 · repaso
        self.stack.addWidget(self._build_review())

        # 3 · fin del repaso
        self.done = _EmptyState("🎉", "Repaso terminado", "", "Volver a empezar")
        self.done.button.clicked.connect(lambda: self._start(all_cards=False))
        self.done.set_secondary("Generar 10 más")
        self.done.secondary.clicked.connect(lambda: self._generate(10, self.done.secondary))
        self.stack.addWidget(self.done)

        # Atajos: solo activos mientras esta pestaña está visible
        self._shortcuts = []
        for key, fn in ((Qt.Key.Key_Space, self._flip), (Qt.Key.Key_1, lambda: self._answer(False)),
                        (Qt.Key.Key_2, lambda: self._answer(True))):
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WindowShortcut)
            sc.activated.connect(fn)
            sc.setEnabled(False)
            self._shortcuts.append(sc)

    def _build_review(self) -> QWidget:
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.lbl_progress = _lbl("0 / 0", f"color:{theme.MUTED}; font-size:12px; font-weight:600;")
        self.pill_box = Pill("Caja 1")
        top.addWidget(self.lbl_progress)
        top.addWidget(self.pill_box)
        top.addStretch()
        self.btn_more = QPushButton("Generar más")
        self.btn_more.setProperty("flat", True)
        self.btn_more.setFixedHeight(32)
        self.btn_more.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_more.clicked.connect(lambda: self._generate(10, self.btn_more))
        top.addWidget(self.btn_more)
        cv.addLayout(top)
        self.bar = _progress_bar()
        cv.addWidget(self.bar)
        cv.addStretch(1)

        # Tarjeta grande centrada (520 px, se encoge si la ventana es estrecha)
        self.card = _card("flashcard", "border-radius:14px;")
        self.card.setMaximumWidth(CARD_W)
        self.card.setMinimumHeight(220)
        self.card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        cl = QVBoxLayout(self.card)
        cl.setContentsMargins(28, 22, 28, 22)
        cl.setSpacing(10)
        cl.addWidget(_lbl("PREGUNTA", f"color:{theme.ACCENT}; font-size:10px; font-weight:700; letter-spacing:1px;",
                          align=Qt.AlignmentFlag.AlignCenter))
        self.lbl_q = _lbl("", f"color:{theme.TEXT}; font-size:14px; font-weight:600; line-height:1.4;",
                          wrap=True, align=Qt.AlignmentFlag.AlignCenter)
        self.lbl_q.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addStretch(1)
        cl.addWidget(self.lbl_q)
        cl.addStretch(1)
        self.answer_box = QWidget()
        self.answer_box.setStyleSheet("background:transparent;")
        al = QVBoxLayout(self.answer_box)
        al.setContentsMargins(0, 0, 0, 0)
        al.setSpacing(10)
        al.addWidget(hsep())
        al.addWidget(_lbl("RESPUESTA", f"color:{theme.SUCCESS}; font-size:10px; font-weight:700; letter-spacing:1px;",
                          align=Qt.AlignmentFlag.AlignCenter))
        self.lbl_a = _lbl("", f"color:{theme.TEXT}; font-size:14px; line-height:1.4;",
                          wrap=True, align=Qt.AlignmentFlag.AlignCenter)
        self.lbl_a.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        al.addWidget(self.lbl_a)
        cl.addWidget(self.answer_box)
        crow = QHBoxLayout()
        crow.addStretch()
        crow.addWidget(self.card, 1)
        crow.addStretch()
        cv.addLayout(crow)

        # Botones
        btns = QHBoxLayout()
        btns.setSpacing(10)
        btns.addStretch()
        self.btn_flip = QPushButton("Ver respuesta")
        self.btn_flip.setProperty("accent", True)
        self.btn_flip.clicked.connect(self._flip)
        self.btn_no = QPushButton("✕  No la sabía")
        self.btn_no.setProperty("flat", True)
        self.btn_no.setStyleSheet(
            f"QPushButton{{color:{theme.RECORD}; border:1px solid {theme.BORDER}; font-size:13px;}}"
            f"QPushButton:hover{{background:{theme.RECORD_BG}; border-color:{theme.RECORD}; color:{theme.RECORD_H};}}"
        )
        self.btn_no.clicked.connect(lambda: self._answer(False))
        self.btn_yes = QPushButton("✓  La sabía")
        self.btn_yes.setStyleSheet(
            f"QPushButton{{background:{theme.SUCCESS}; color:#06281a; border:none; font-size:13px; font-weight:700;}}"
            f"QPushButton:hover{{background:#4bd6a0;}}"
        )
        self.btn_yes.clicked.connect(lambda: self._answer(True))
        for b in (self.btn_flip, self.btn_no, self.btn_yes):
            b.setFixedHeight(40)
            b.setMinimumWidth(170)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            btns.addWidget(b)
        btns.addStretch()
        cv.addLayout(btns)
        cv.addWidget(_lbl("Espacio · ver respuesta      1 · no la sabía      2 · la sabía",
                          f"color:{theme.DIM}; font-size:11px;", align=Qt.AlignmentFlag.AlignCenter))
        cv.addStretch(2)
        return _scroll(_column(col))

    # ── estado ──
    def showEvent(self, e):
        super().showEvent(e)
        for sc in self._shortcuts:
            sc.setEnabled(True)

    def hideEvent(self, e):
        super().hideEvent(e)
        for sc in self._shortcuts:
            sc.setEnabled(False)

    def _cards(self) -> list[dict]:
        return self.panel.session.study.setdefault("flashcards", [])

    def load(self):
        if not self._cards():
            self.stack.setCurrentIndex(0)
            return
        self._start(all_cards=False)

    def _start(self, all_cards: bool):
        cards = self._cards()
        if all_cards:
            queue = sorted((c for c in cards if isinstance(c, dict)), key=lambda c: int(c.get("box", 0) or 0))
        else:
            queue = session_store.due_flashcards(self.panel.session, datetime.date.today())
        if not queue:
            nxt = min((str(c.get("due", "")) for c in cards if c.get("due")), default="")
            n = len(cards)
            self.nodue.lbl_text.setText(
                f"Tienes {n} tarjeta{'s' if n != 1 else ''} y {'todas están' if n != 1 else 'está'} al día."
                + (f" La próxima vence el {_fecha_corta(nxt)}." if nxt else ""))
            self.stack.setCurrentIndex(1)
            return
        self._queue, self._idx, self._ok = list(queue), 0, 0
        self.stack.setCurrentIndex(2)
        self._show_card()

    def _show_card(self):
        c = self._queue[self._idx]
        n = len(self._queue)
        self.lbl_progress.setText(f"{self._idx + 1} / {n}")
        self.bar.setMaximum(n)
        self.bar.setValue(self._idx)
        box = int(c.get("box", 0) or 0)
        self.pill_box.setText(f"Caja {box + 1} de 5")
        self.pill_box.set_colors(*_box_colors(box))
        self.lbl_q.setText(str(c.get("q", "")))
        self.lbl_a.setText(str(c.get("a", "")))
        self._flipped = False
        self.answer_box.hide()
        self.btn_flip.show()
        self.btn_no.hide()
        self.btn_yes.hide()

    def _flip(self):
        if self.stack.currentIndex() != 2 or self._flipped:
            return
        self._flipped = True
        self.answer_box.show()
        self.btn_flip.hide()
        self.btn_no.show()
        self.btn_yes.show()

    def _answer(self, known: bool):
        if self.stack.currentIndex() != 2 or not self._flipped:
            return
        c = self._queue[self._idx]
        session_store.schedule_flashcard(c, known, datetime.date.today())
        if known:
            self._ok += 1
        self.panel.save()
        self._idx += 1
        if self._idx < len(self._queue):
            self._show_card()
            return
        n = len(self._queue)
        self.done.lbl_text.setText(
            f"Has repasado {n} tarjeta{'s' if n != 1 else ''} · {self._ok} acertada{'s' if self._ok != 1 else ''}")
        self.stack.setCurrentIndex(3)

    # ── generación ──
    def _generate(self, n: int, btn: QPushButton):
        self.panel.run_task(self.panel.ai.generate_flashcards, (self.panel.full_text(), n), btn, self._generated)

    def _generated(self, result):
        cards = self._cards()
        next_id = max((int(c.get("id", 0) or 0) for c in cards), default=0) + 1
        added = 0
        for item in (result or []):
            if not isinstance(item, dict) or not str(item.get("q", "")).strip():
                continue
            cards.append({
                "id": next_id, "q": str(item.get("q", "")).strip(), "a": str(item.get("a", "")).strip(),
                "slide": int(item.get("slide", 0) or 0), "box": 0, "due": _hoy(), "seen": 0, "correct": 0,
            })
            next_id += 1
            added += 1
        if not added:
            QMessageBox.warning(self, "Sin tarjetas", "La IA no ha devuelto tarjetas válidas. Inténtalo de nuevo.")
            return
        self.panel.save()
        self._start(all_cards=False)


# ── Pestaña 4: Test ───────────────────────────────────────────────────────

class _QuizTab(QWidget):
    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._run: list[dict] = []
        self._idx = 0
        self._score = 0
        self._failed: list[dict] = []
        self._answered = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        v.addWidget(self.stack)

        self.empty = _EmptyState(
            "📝", "Test de la clase",
            "Diez preguntas tipo test sobre lo explicado. Al terminar verás qué has fallado "
            "y la IA te dirá qué deberías repasar.",
            "Generar test de 10 preguntas")
        self.empty.button.clicked.connect(lambda: self._generate(self.empty.button))
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self._build_question())
        self.stack.addWidget(self._build_result())

    def _build_question(self) -> QWidget:
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 16, 16, 16)
        cv.setSpacing(12)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.lbl_progress = _lbl("Pregunta 1 de 10", f"color:{theme.MUTED}; font-size:12px; font-weight:600;")
        self.pill_slide = Pill("Diapo 1")
        self.lbl_best = _lbl("", f"color:{theme.DIM}; font-size:11px;")
        top.addWidget(self.lbl_progress)
        top.addWidget(self.pill_slide)
        top.addStretch()
        top.addWidget(self.lbl_best)
        cv.addLayout(top)
        self.bar = _progress_bar()
        cv.addWidget(self.bar)

        qcard = _card("quizQuestion", "border-radius:12px;")
        ql = QVBoxLayout(qcard)
        ql.setContentsMargins(22, 18, 22, 18)
        self.lbl_q = _lbl("", f"color:{theme.TEXT}; font-size:14px; font-weight:600; line-height:1.4;", wrap=True)
        self.lbl_q.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ql.addWidget(self.lbl_q)
        cv.addWidget(qcard)

        self.options: list[_OptionButton] = []
        for i, letter in enumerate(_LETRAS):
            o = _OptionButton(letter)
            o.clicked.connect(lambda _=False, i=i: self._choose(i))
            self.options.append(o)
            cv.addWidget(o)

        self.expl = _card("quizExplanation", f"border-left:4px solid {theme.ACCENT};")
        el = QVBoxLayout(self.expl)
        el.setContentsMargins(18, 12, 18, 14)
        el.setSpacing(6)
        self.lbl_verdict = _lbl("", "font-size:12px; font-weight:700;")
        self.lbl_expl = _lbl("", f"color:{theme.TEXT}; font-size:14px; line-height:1.4;", wrap=True)
        self.lbl_expl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        el.addWidget(self.lbl_verdict)
        el.addWidget(self.lbl_expl)
        self.expl.hide()
        cv.addWidget(self.expl)

        row = QHBoxLayout()
        row.addStretch()
        self.btn_next = QPushButton("Siguiente  ›")
        self.btn_next.setProperty("accent", True)
        self.btn_next.setFixedHeight(38)
        self.btn_next.setMinimumWidth(150)
        self.btn_next.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_next.clicked.connect(self._next)
        self.btn_next.hide()
        row.addWidget(self.btn_next)
        cv.addLayout(row)
        cv.addStretch()
        return _scroll(_column(col))

    def _build_result(self) -> QWidget:
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 28, 16, 16)
        cv.setSpacing(12)
        self.lbl_score = _lbl("0 / 0", "font-size:44px; font-weight:800;", align=Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self.lbl_score)
        prow = QHBoxLayout()
        prow.addStretch()
        self.pill_score = Pill("")
        prow.addWidget(self.pill_score)
        prow.addStretch()
        cv.addLayout(prow)
        self.lbl_result = _lbl("", f"color:{theme.MUTED}; font-size:13px;", align=Qt.AlignmentFlag.AlignCenter)
        cv.addWidget(self.lbl_result)

        self.failed_box = QWidget()
        self.failed_layout = QVBoxLayout(self.failed_box)
        self.failed_layout.setContentsMargins(0, 8, 0, 0)
        self.failed_layout.setSpacing(8)
        cv.addWidget(self.failed_box)

        grow = QHBoxLayout()
        grow.addStretch()
        self.btn_gaps = QPushButton("💡  Ver lagunas de conocimiento")
        self.btn_gaps.setProperty("accent", True)
        self.btn_gaps.setFixedHeight(38)
        self.btn_gaps.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_gaps.clicked.connect(self._gaps)
        grow.addWidget(self.btn_gaps)
        grow.addStretch()
        cv.addLayout(grow)

        self.gaps = QFrame()
        self.gaps.setObjectName("quizGaps")
        self.gaps.setStyleSheet(
            f"QFrame#quizGaps{{background:{theme.WARN_BG}; border:1px solid #6b4d14;"
            f"border-left:4px solid {theme.WARN}; border-radius:10px;}}")
        gl = QVBoxLayout(self.gaps)
        gl.setContentsMargins(18, 12, 18, 14)
        gl.setSpacing(6)
        gl.addWidget(_lbl("💡  QUÉ REPASAR", f"color:{theme.WARN}; font-size:10px; font-weight:700; letter-spacing:1px;"))
        self.lbl_gaps = _lbl("", "color:#ffd98a; font-size:13px; line-height:1.5;", wrap=True)
        self.lbl_gaps.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        gl.addWidget(self.lbl_gaps)
        self.gaps.hide()
        cv.addWidget(self.gaps)

        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addStretch()
        self.btn_repeat = QPushButton("↻  Repetir test")
        self.btn_repeat.clicked.connect(lambda: self._start(shuffle=True))
        self.btn_new = QPushButton("Generar test nuevo")
        self.btn_new.setProperty("flat", True)
        self.btn_new.clicked.connect(lambda: self._generate(self.btn_new))
        for b in (self.btn_repeat, self.btn_new):
            b.setFixedHeight(36)
            brow.addWidget(b)
        brow.addStretch()
        cv.addSpacing(6)
        cv.addLayout(brow)
        cv.addStretch()
        return _scroll(_column(col))

    # ── estado ──
    def _quiz(self) -> dict:
        q = self.panel.session.study.setdefault("quiz", {})
        q.setdefault("questions", [])
        q.setdefault("attempts", [])
        return q

    def load(self):
        self.gaps.hide()
        if not self._quiz()["questions"]:
            self.stack.setCurrentIndex(0)
            return
        self._start(shuffle=False)

    def _start(self, shuffle: bool):
        qs = [q for q in self._quiz()["questions"] if isinstance(q, dict) and q.get("options")]
        if not qs:
            self.stack.setCurrentIndex(0)
            return
        if shuffle:
            random.shuffle(qs)
        run = []
        for q in qs:
            opts = [str(o) for o in q.get("options", [])][:4]
            ans = int(q.get("answer", 0) or 0)
            ans = ans if 0 <= ans < len(opts) else 0
            order = list(range(len(opts)))
            if shuffle:
                random.shuffle(order)
            run.append({"id": q.get("id"), "q": str(q.get("q", "")), "options": [opts[i] for i in order],
                        "answer": order.index(ans), "explanation": str(q.get("explanation", "") or ""),
                        "slide": int(q.get("slide", 0) or 0)})
        self._run, self._idx, self._score, self._failed = run, 0, 0, []
        attempts = [a for a in self._quiz()["attempts"] if isinstance(a, dict) and int(a.get("total", 0) or 0) > 0]
        if attempts:
            best = max(attempts, key=lambda a: int(a.get("score", 0) or 0) / int(a["total"]))
            self.lbl_best.setText(f"Mejor resultado: {int(best.get('score', 0) or 0)} / {int(best['total'])}")
        else:
            self.lbl_best.setText("")
        self.gaps.hide()
        self.stack.setCurrentIndex(1)
        self._show_question()

    def _show_question(self):
        q = self._run[self._idx]
        n = len(self._run)
        self._answered = False
        self.lbl_progress.setText(f"Pregunta {self._idx + 1} de {n}")
        self.pill_slide.setText(f"Diapo {q['slide']}")
        self.pill_slide.setVisible(q["slide"] > 0)
        self.bar.setMaximum(n)
        self.bar.setValue(self._idx)
        self.lbl_q.setText(q["q"])
        for i, o in enumerate(self.options):
            if i < len(q["options"]):
                o.set_text(q["options"][i])
                o.set_state("idle")
                o.setEnabled(True)
                o.show()
            else:
                o.hide()
        self.expl.hide()
        self.btn_next.hide()

    def _choose(self, i: int):
        if self._answered:
            return
        self._answered = True
        q = self._run[self._idx]
        ok = i == q["answer"]
        for j, o in enumerate(self.options):
            o.setEnabled(False)
            if j == q["answer"]:
                o.set_state("correct")
            elif j == i:
                o.set_state("wrong")
            else:
                o.set_state("dim")
        if ok:
            self._score += 1
            self.lbl_verdict.setText("✓  Correcto")
            self.lbl_verdict.setStyleSheet(f"background:transparent; color:{theme.SUCCESS}; font-size:12px; font-weight:700;")
        else:
            self._failed.append(q)
            self.lbl_verdict.setText(f"✕  Incorrecto · la respuesta correcta era la {_LETRAS[q['answer']]}")
            self.lbl_verdict.setStyleSheet(f"background:transparent; color:{theme.RECORD}; font-size:12px; font-weight:700;")
        self.lbl_expl.setText(q["explanation"] or "Sin explicación para esta pregunta.")
        self.expl.show()
        self.btn_next.setText("Ver resultado  ›" if self._idx + 1 >= len(self._run) else "Siguiente  ›")
        self.btn_next.show()

    def _next(self):
        if not self._answered:
            return
        self._idx += 1
        if self._idx < len(self._run):
            self._show_question()
            return
        self._finish()

    def _finish(self):
        n = len(self._run)
        pct = int(round(100 * self._score / n)) if n else 0
        bg, fg = _score_colors(pct)
        self.lbl_score.setText(f"{self._score} / {n}")
        self.lbl_score.setStyleSheet(f"background:transparent; color:{fg}; font-size:44px; font-weight:800;")
        self.pill_score.setText(f"{pct} % de aciertos")
        self.pill_score.set_colors(bg, fg)
        k = len(self._failed)
        self.lbl_result.setText("¡Todo correcto! Dominas esta clase." if k == 0
                                else f"Has fallado {k} pregunta{'s' if k != 1 else ''}. Repásalas antes de seguir:")
        _clear_layout(self.failed_layout)
        for q in self._failed:
            f = _card("quizFailed", f"border-left:4px solid {theme.RECORD};")
            fl = QVBoxLayout(f)
            fl.setContentsMargins(16, 10, 16, 10)
            fl.setSpacing(4)
            fl.addWidget(_lbl(q["q"], f"color:{theme.TEXT}; font-size:13px; font-weight:600;", wrap=True))
            fl.addWidget(_lbl(f"Correcta: {q['options'][q['answer']]}",
                              f"color:{theme.SUCCESS}; font-size:12px;", wrap=True))
            self.failed_layout.addWidget(f)
        self.btn_gaps.setVisible(k > 0)
        self.gaps.hide()

        self._quiz()["attempts"].append({
            "date": datetime.datetime.now().isoformat(timespec="seconds"),
            "score": self._score, "total": n,
            "failed_ids": [q["id"] for q in self._failed if q.get("id") is not None],
        })
        self.panel.save()
        self.stack.setCurrentIndex(2)

    # ── IA ──
    def _gaps(self):
        failed = [{"q": q["q"], "answer": q["options"][q["answer"]], "explanation": q["explanation"],
                   "slide": q["slide"]} for q in self._failed]
        self.panel.run_task(self.panel.ai.knowledge_gaps, (self.panel.full_text(), failed),
                            self.btn_gaps, self._gaps_done)

    def _gaps_done(self, result):
        self.lbl_gaps.setText(str(result or "").strip() or "La IA no ha encontrado lagunas concretas.")
        self.gaps.show()
        self.btn_gaps.hide()

    def _generate(self, btn: QPushButton):
        quiz = self._quiz()
        if quiz["questions"] and quiz["attempts"]:
            r = QMessageBox.question(
                self, "Generar test nuevo",
                "El test nuevo sustituye a las preguntas actuales y se borra el historial "
                "de resultados del test anterior.\n\n¿Quieres continuar?")
            if r != QMessageBox.StandardButton.Yes:
                return
        self.panel.run_task(self.panel.ai.generate_quiz, (self.panel.full_text(), 10), btn, self._generated)

    def _generated(self, result):
        quiz = self._quiz()
        next_id = max((int(q.get("id", 0) or 0) for q in quiz["questions"]), default=0) + 1
        new = []
        for item in (result or []):
            if not isinstance(item, dict):
                continue
            opts = [str(o) for o in (item.get("options") or [])]
            if not str(item.get("q", "")).strip() or len(opts) < 2:
                continue
            new.append({"id": next_id, "q": str(item["q"]).strip(), "options": opts[:4],
                        "answer": int(item.get("answer", 0) or 0), "explanation": str(item.get("explanation", "") or ""),
                        "slide": int(item.get("slide", 0) or 0)})
            next_id += 1
        if not new:
            QMessageBox.warning(self, "Sin preguntas", "La IA no ha devuelto preguntas válidas. Inténtalo de nuevo.")
            return
        quiz["questions"] = new
        quiz["attempts"] = []          # los resultados eran de otro test
        self.panel.save()
        self._start(shuffle=False)


# ── Pestaña 5: Tutor ──────────────────────────────────────────────────────

class _MyNotesTab(QWidget):
    """Notas propias del alumno. Se guardan solas y la IA las tiene en cuenta."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._loading = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(16, 14, 16, 16)
        cv.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(8)
        tt = QVBoxLayout()
        tt.setSpacing(2)
        tt.addWidget(_lbl("Mis notas", f"color:{theme.TEXT}; font-size:14px; font-weight:700;"))
        tt.addWidget(_lbl("Lo que escribas aquí se guarda solo y la IA lo tiene en cuenta "
                          "al resumir, explicar y responder.",
                          f"color:{theme.MUTED}; font-size:11px;"))
        top.addLayout(tt)
        top.addStretch()
        self.pill_saved = Pill("Guardado", theme.SURFACE_2, theme.DIM)
        self.pill_saved.hide()
        top.addWidget(self.pill_saved)
        self.btn_slide = QPushButton("＋ Diapositiva actual")
        self.btn_slide.setProperty("flat", True)
        self.btn_slide.setFixedHeight(32)
        self.btn_slide.setToolTip("Inserta una línea con la diapositiva y la hora para situar la nota")
        self.btn_slide.clicked.connect(self._insert_mark)
        top.addWidget(self.btn_slide)
        cv.addLayout(top)

        self.text = QTextEdit()
        self.text.setPlaceholderText(
            "Escribe aquí tus comentarios de la clase: dudas, lo que no entendiste, "
            "lo que el profe dijo que entra en el examen…\n\n"
            "Se guarda solo mientras escribes."
        )
        self.text.setStyleSheet(
            f"QTextEdit{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
            f"border-radius:10px; padding:14px; font-size:13px;}}"
            f"QTextEdit:focus{{border-color:{theme.ACCENT};}}"
        )
        self.text.textChanged.connect(self._on_changed)
        cv.addWidget(self.text, 1)
        v.addWidget(_column(col))

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(900)          # guarda al dejar de escribir
        self._save_timer.timeout.connect(self._save)

    def load(self):
        self._loading = True
        self.text.setPlainText(self.panel.session.study.get("notes", "") or "")
        self._loading = False
        self.pill_saved.hide()

    def _on_changed(self):
        if not self._loading:
            self._save_timer.start()

    def _insert_mark(self):
        s = self.panel.session
        entries = [e for e in (s.entries or []) if e.get("kind", "text") != "marker"]
        slide = entries[-1].get("slide", 1) if entries else 1
        self.text.insertPlainText(f"\n— Diapositiva {slide}: ")
        self.text.setFocus()

    def _save(self):
        if self.panel.session is None:
            return
        self.panel.session.study["notes"] = self.text.toPlainText()
        self.panel.save()
        self.pill_saved.setText("Guardado ✓")
        self.pill_saved.show()
        QTimer.singleShot(1500, self.pill_saved.hide)

    def flush(self):
        """Guarda ya (al salir de la clase o cerrar la app)."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save()


class _TutorTab(QWidget):
    SUGERENCIAS = [
        "Explícame lo más importante de la clase",
        "¿Qué fórmulas se vieron?",
        "Hazme una pregunta para comprobar que lo entendí",
    ]

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self.panel = panel
        self._thinking: QWidget | None = None
        self._pending: dict | None = None     # mensaje del alumno a la espera de respuesta
        self._pending_sid = ""                # id de la sesión en la que se hizo
        self._busy = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 14, 16, 6)
        tt = QVBoxLayout()
        tt.setSpacing(2)
        tt.addWidget(_lbl("Tutor IA", f"color:{theme.TEXT}; font-size:14px; font-weight:700;"))
        tt.addWidget(_lbl("Responde usando solo lo que se dijo en esta clase.", f"color:{theme.MUTED}; font-size:11px;"))
        hl.addLayout(tt)
        hl.addStretch()
        self.btn_clear = QPushButton("Limpiar chat")
        self.btn_clear.setProperty("flat", True)
        self.btn_clear.setFixedHeight(32)
        self.btn_clear.clicked.connect(self._clear)
        hl.addWidget(self.btn_clear)
        v.addWidget(_column(head))

        self.inner = QWidget()
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(16, 8, 16, 12)
        self.vbox.setSpacing(10)
        self.vbox.addStretch()
        self.scroll = _scroll(_column(self.inner))
        v.addWidget(self.scroll, 1)

        # Sugerencias iniciales
        self.sugg = QWidget()
        sl = QVBoxLayout(self.sugg)
        sl.setContentsMargins(0, 30, 0, 10)
        sl.setSpacing(8)
        sl.addWidget(_lbl("💬", "font-size:34px;", align=Qt.AlignmentFlag.AlignCenter))
        sl.addWidget(_lbl("Pregunta lo que quieras sobre la clase", f"color:{theme.TEXT}; font-size:15px; font-weight:700;",
                          align=Qt.AlignmentFlag.AlignCenter))
        sl.addWidget(_lbl("Por ejemplo:", f"color:{theme.MUTED}; font-size:12px;", align=Qt.AlignmentFlag.AlignCenter))
        self._sugg_buttons: list[QPushButton] = []
        for s in self.SUGERENCIAS:
            b = QPushButton(s)
            self._sugg_buttons.append(b)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton{{background:{theme.SURFACE_2}; color:{theme.TEXT}; border:1px solid {theme.BORDER};"
                f"border-radius:17px; padding:8px 18px; font-weight:500; font-size:12px;}}"
                f"QPushButton:hover{{border-color:{theme.ACCENT}; color:{theme.ACCENT_H}; background:{theme.SURFACE_3};}}"
            )
            b.clicked.connect(lambda _=False, s=s: self._send(s))
            row = QHBoxLayout()
            row.addStretch()
            row.addWidget(b)
            row.addStretch()
            sl.addLayout(row)
        self.vbox.insertWidget(0, self.sugg)

        bottom = QWidget()
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(16, 8, 16, 16)
        bl.setSpacing(8)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Escribe tu pregunta sobre la clase…")
        self.input.setFixedHeight(38)
        self.input.returnPressed.connect(lambda: self._send(self.input.text()))
        self.btn_send = QPushButton("Enviar")
        self.btn_send.setProperty("accent", True)
        self.btn_send.setFixedSize(96, 38)
        self.btn_send.clicked.connect(lambda: self._send(self.input.text()))
        bl.addWidget(self.input, 1)
        bl.addWidget(self.btn_send)
        v.addWidget(_column(bottom))

    def _history(self) -> list[dict]:
        return self.panel.session.study.setdefault("chat", [])

    def load(self):
        self._remove_thinking()
        pending, self._pending = self._pending, None
        hist = self._history()
        # Pregunta sin respuesta guardada por versiones anteriores: vuelve al input
        if hist and isinstance(hist[-1], dict) and hist[-1].get("role") == "user":
            orphan = hist.pop()
            self.panel.save()
            if not self.input.text().strip():
                self.input.setText(str(orphan.get("text", "")))
        # Pregunta que se quedó sin respuesta al recargar esta misma clase
        if pending is not None and self._pending_sid == self.panel.session.id \
                and not self.input.text().strip():
            self.input.setText(pending["text"])
        _clear_layout(self.vbox, keep_last=1, start=1)     # deja sugerencias + stretch
        for m in hist:
            if isinstance(m, dict) and str(m.get("text", "")).strip():
                self._add_bubble(m.get("role", "assistant"), str(m.get("text", "")))
        has_text = self.panel.has_text()
        for b in self._sugg_buttons:
            b.setEnabled(has_text)
        self.input.setPlaceholderText("Escribe tu pregunta sobre la clase…" if has_text
                                      else "Esta clase no tiene transcripción")
        self._set_busy(False)
        self.sugg.setVisible(not hist)
        QTimer.singleShot(30, self._scroll_bottom)

    def _add_bubble(self, role: str, text: str) -> QWidget:
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        b = _Bubble(role, text)
        if role == "user":
            rl.addStretch(1)
            rl.addWidget(b)
        else:
            rl.addWidget(b)
            rl.addStretch(1)
        self.vbox.insertWidget(self.vbox.count() - 1, row)
        QTimer.singleShot(30, self._scroll_bottom)
        return row

    def _scroll_bottom(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_busy(self, busy: bool):
        self._busy = busy
        ok = not busy and self.panel.has_text()
        self.input.setEnabled(ok)
        self.btn_send.setEnabled(ok)
        self.btn_send.setText("Pensando…" if busy else "Enviar")

    def _remove_thinking(self):
        if self._thinking is not None:
            self.vbox.removeWidget(self._thinking)
            self._thinking.hide()
            self._thinking.deleteLater()
            self._thinking = None

    def _send(self, text: str):
        text = (text or "").strip()
        if not text or self._busy:
            return
        if not self.panel.enhancer.is_active():
            self.panel.warn_no_ai()
            return
        self.input.clear()
        self.sugg.hide()
        history = [dict(m) for m in self._history()]
        # No se guarda hasta que llega la respuesta: si el alumno sale de la clase
        # mientras el tutor piensa, no queda una pregunta huérfana en la ficha.
        self._pending = {"role": "user", "text": text}
        self._pending_sid = self.panel.session.id
        self._add_bubble("user", text)
        self._thinking = self._add_bubble("thinking", "Pensando…")
        self._set_busy(True)
        ok = self.panel.run_task(self.panel.ai.tutor_answer, (self.panel.full_text(), history, text),
                                 None, self._answered, self._failed)
        if not ok:
            self._failed("")

    def _answered(self, result):
        self._remove_thinking()
        pending, self._pending = self._pending, None
        answer = str(result or "").strip() or "No he podido responder a eso con lo que hay en la clase."
        hist = self._history()
        if pending is not None:
            hist.append(pending)
        hist.append({"role": "assistant", "text": answer})
        self.panel.save()
        self._add_bubble("assistant", answer)
        self._set_busy(False)
        self.input.setFocus()

    def _failed(self, _msg: str):
        """La IA no respondió: la pregunta (que aún no está en el historial) vuelve al input."""
        self._remove_thinking()
        if self._pending is not None:
            self.input.setText(self._pending["text"])
        self._pending = None
        self._set_busy(False)
        self.load()

    def _clear(self):
        if not self._history():
            return
        r = QMessageBox.question(self, "Limpiar chat", "¿Borrar toda la conversación con el tutor?")
        if r != QMessageBox.StandardButton.Yes:
            return
        self._history().clear()
        self.panel.save()
        self.load()


# ── Panel principal ───────────────────────────────────────────────────────

TABS_QSS = f"""
QTabWidget::pane {{ border: none; border-top: 1px solid {theme.BORDER}; background: {theme.BG}; }}
QTabWidget::tab-bar {{ left: 16px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: {theme.SURFACE_2}; color: {theme.MUTED};
    border: 1px solid {theme.BORDER}; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    padding: 8px 18px; margin-right: 4px; margin-top: 6px; font-weight: 600;
}}
QTabBar::tab:hover {{ background: {theme.SURFACE_3}; color: {theme.TEXT}; }}
QTabBar::tab:selected {{
    background: {theme.SURFACE_2}; color: {theme.TEXT};
    border-bottom: 2px solid {theme.ACCENT}; margin-top: 2px; padding-bottom: 8px;
}}
"""


class StudyPanel(QWidget):
    """Modo estudio de una sesión de la biblioteca."""

    back = Signal()

    def __init__(self, enhancer, parent=None):
        super().__init__(parent)
        self.enhancer = enhancer
        self.ai = StudyAI(enhancer)
        self.session: Session | None = None
        self._tasks: list[AITask] = []
        self._gen = 0                     # invalida resultados de tareas de sesiones anteriores
        self._refine: RefineWorker | None = None
        self._refine_pct = 0
        self.is_recording = lambda: False  # la ventana lo sustituye (turbo no debe competir con el directo)
        self._setup_ui()

    # ── UI ──
    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QWidget()
        header.setObjectName("studyHeader")
        header.setFixedHeight(84)
        header.setStyleSheet(f"QWidget#studyHeader{{background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER};}}")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(12, 8, 20, 8)
        hb.setSpacing(12)

        self.btn_back = QPushButton("‹  Biblioteca")
        self.btn_back.setProperty("flat", True)
        self.btn_back.setFixedHeight(34)
        self.btn_back.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_back.clicked.connect(self.back.emit)
        hb.addWidget(self.btn_back)
        hb.addWidget(vsep(36))

        tt = QVBoxLayout()
        tt.setSpacing(3)
        trow = QHBoxLayout()
        trow.setSpacing(8)
        self.lbl_title = _ElideLabel("")
        self.lbl_title.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:16px; font-weight:700;")
        self.pill_subject = Pill("", theme.SUBJECT_BG, theme.ACCENT_H)
        self.pill_subject.hide()
        trow.addWidget(self.lbl_title, 1)
        trow.addWidget(self.pill_subject)
        tt.addLayout(trow)
        self.lbl_sub = _lbl("", f"color:{theme.MUTED}; font-size:11px;")
        tt.addWidget(self.lbl_sub)
        self.lbl_ai = _lbl("IA no activa · actívala con el botón ✦ IA de la barra superior",
                           f"color:{theme.WARN}; font-size:11px;")
        self.lbl_ai.hide()
        tt.addWidget(self.lbl_ai)
        hb.addLayout(tt, 1)

        self.btn_refine = QPushButton("✨  Repasar con turbo")
        self.btn_refine.setFixedHeight(34)
        self.btn_refine.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_refine.setToolTip(
            "Vuelve a transcribir el audio de la clase con un modelo mucho más preciso.\n"
            "Tarda más que la clase, pero puedes seguir usando la app mientras tanto."
        )
        self.btn_refine.clicked.connect(self._on_refine_clicked)
        self.btn_refine.hide()
        hb.addWidget(self.btn_refine)

        # Indicador de dominio: anillo con el % grande + Pill "Dominio" + detalle
        self.ring = _MasteryRing()
        self.lbl_mastery = self.ring.lbl
        hb.addWidget(self.ring)
        mcol = QVBoxLayout()
        mcol.setSpacing(4)
        mcol.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self.pill_mastery = Pill("Dominio")
        self.lbl_mastery_hint = _lbl("", f"color:{theme.DIM}; font-size:11px;")
        mcol.addWidget(self.pill_mastery, 0, Qt.AlignmentFlag.AlignLeft)
        mcol.addWidget(self.lbl_mastery_hint)
        hb.addLayout(mcol)
        root.addWidget(header)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(TABS_QSS)
        self.tab_notes = _NotesTab(self)
        self.tab_lesson = _LessonTab(self)
        self.tab_cards = _FlashcardsTab(self)
        self.tab_quiz = _QuizTab(self)
        self.tab_notes_mine = _MyNotesTab(self)
        self.tab_tutor = _TutorTab(self)
        for w, name in ((self.tab_notes, "📖  Apuntes"), (self.tab_lesson, "🧭  Lección"),
                        (self.tab_cards, "🃏  Tarjetas"), (self.tab_quiz, "📝  Test"),
                        (self.tab_notes_mine, "✍️  Mis notas"), (self.tab_tutor, "💬  Tutor")):
            self.tabs.addTab(w, name)
        self.tabs.currentChanged.connect(self._on_tab)
        root.addWidget(self.tabs, 1)

    def _on_tab(self, i: int):
        if self.tabs.widget(i) is self.tab_tutor:
            self.tab_tutor.input.setFocus()

    # ── API pública ──
    def load(self, session: Session):
        if self.session is not None and hasattr(self, "tab_notes_mine"):
            self.tab_notes_mine.flush()        # no perder lo que estaba escribiendo
        self._gen += 1
        self.session = session
        st = session.study
        if not isinstance(st, dict):
            st = session.study = {}
        st.setdefault("flashcards", [])
        st.setdefault("quiz", {}).setdefault("questions", [])
        st["quiz"].setdefault("attempts", [])
        st.setdefault("chat", [])
        st.setdefault("mastery", 0)
        try:
            session_store.compute_mastery(session)
        except Exception:
            pass
        self._refresh_header()
        for t in (self.tab_notes, self.tab_lesson, self.tab_cards, self.tab_quiz,
                  self.tab_notes_mine, self.tab_tutor):
            t.load()
        self._apply_availability()
        self.tabs.setCurrentIndex(0)

    def refresh_ai_state(self):
        """La ventana lo llama al cambiar la configuración de la IA."""
        self._refresh_header()

    def sync_class_data(self, other: Session):
        """Copia a la sesión abierta los datos de la clase (fragmentos, duración, título,
        rutas, resumen) de `other` si es la misma ficha. El estado de estudio no se toca."""
        s = self.session
        if s is None or other is None or other is s or other.id != s.id:
            return
        changed = other.entries != s.entries
        for name in ("title", "date", "duration_s", "pdf_path", "txt_path", "wav_path", "summary"):
            setattr(s, name, getattr(other, name))
        s.entries = [dict(e) for e in other.entries]
        if changed:
            self.tab_notes.load()
            self._apply_availability()
        self._refresh_header()

    # ── Helpers para las pestañas ──
    def full_text(self) -> str:
        """Transcripción de la clase + lo que el alumno haya anotado."""
        if not self.session:
            return ""
        texto = session_store.full_text(self.session)
        notas = (self.session.study.get("notes", "") or "").strip()
        if notas:
            texto += ("\n\nNOTAS DEL ALUMNO (lo que él mismo apuntó en clase; tenlas muy en "
                      "cuenta: marcan lo que le importa y sus dudas):\n" + notas)
        return texto

    def has_text(self) -> bool:
        """Hay transcripción con la que generar material (las marcas no cuentan)."""
        if self.session is None:
            return False
        return any(session_store.best_text(e).strip() for e in (self.session.entries or [])
                   if isinstance(e, dict) and e.get("kind", "text") != "marker")

    def _apply_availability(self):
        ok = self.has_text()
        for es in (self.tab_lesson.empty, self.tab_cards.empty, self.tab_quiz.empty):
            es.set_available(ok)
        for b in (self.tab_lesson.btn_regen, self.tab_cards.btn_more, self.tab_cards.nodue.secondary,
                  self.tab_cards.done.secondary, self.tab_quiz.btn_new):
            b.setEnabled(ok)

    def save(self):
        """Recalcula el dominio, guarda la sesión y refresca la cabecera.
        Si la ficha ya está en disco se relee y solo se vuelca el estado de estudio:
        así no se pisan los fragmentos que el autoguardado de una clase en curso
        acaba de escribir (y la sesión abierta recibe esos fragmentos)."""
        if self.session is None:
            return
        s = self.session
        try:
            s.study["mastery"] = int(session_store.compute_mastery(s))
        except Exception:
            pass
        try:
            path = session_store.session_path(s.id)
            fresh = None
            if os.path.exists(path):
                try:
                    fresh = session_store.load_session(path)
                except Exception:
                    fresh = None                  # corrupto: se reescribe con lo de memoria
            if fresh is not None:
                fresh.study = s.study
                fresh.subject = s.subject
                session_store.save_session(fresh)
                self.sync_class_data(fresh)
            else:
                session_store.save_session(s)
        except Exception as e:
            QMessageBox.warning(self, "No se pudo guardar", f"No se pudo guardar la sesión:\n{e}")
        self._refresh_header()

    def warn_no_ai(self):
        QMessageBox.information(
            self, "IA no activa",
            "Activa la IA con el botón ✦ IA de la barra superior para usar el modo estudio.\n\n"
            "Recomendado: Google Gemini (gratis, en la nube, sin consumir CPU).")

    def run_task(self, fn, args: tuple, button: QPushButton | None, on_done, on_fail=None,
                 busy_text: str = "Generando…") -> bool:
        """Ejecuta fn(*args) en un AITask. Deshabilita `button` mientras corre."""
        if not self.enhancer.is_active():
            self.warn_no_ai()
            return False
        original = button.text() if button is not None else ""
        if button is not None:
            button.setEnabled(False)
            button.setText(busy_text)
        gen = self._gen
        task = AITask(fn, *args, parent=self)

        def restore():
            if button is not None:
                button.setText(original)
                button.setEnabled(self.has_text())

        def cleanup():
            # solo cuando el hilo ha terminado de verdad (done/failed llegan antes)
            if task in self._tasks:
                self._tasks.remove(task)
            task.deleteLater()

        def done(result):
            restore()
            if gen == self._gen:
                on_done(result)

        def failed(msg):
            restore()
            if gen != self._gen:
                return
            if on_fail is not None:
                on_fail(msg)
            QMessageBox.warning(self, "Error de IA", str(msg) or "La IA no ha respondido. Inténtalo de nuevo.")

        task.done.connect(done)
        task.failed.connect(failed)
        task.finished.connect(cleanup)
        self._tasks.append(task)
        task.start()
        return True

    def _refresh_header(self):
        s = self.session
        if s is None:
            return
        self.lbl_title.setText(s.title or s.id)
        self.pill_subject.setText(s.subject or "")
        self.pill_subject.setVisible(bool(s.subject))
        n = len(s.entries or [])
        parts = [_fecha_larga(s.date)]
        if _duracion(s.duration_s):
            parts.append(_duracion(s.duration_s))
        parts.append(f"{n} fragmento{'s' if n != 1 else ''}")
        self.lbl_sub.setText("  ·  ".join(parts))
        self.lbl_ai.setVisible(not self.enhancer.is_active())
        m = int(s.study.get("mastery", 0) or 0)
        self.ring.set_value(m)
        self.pill_mastery.set_colors(*_mastery_colors(m))
        self.pill_mastery.setToolTip(f"Dominio {m} %: tarjetas aprendidas y mejor resultado del test")
        cards = s.study.get("flashcards", [])
        learned = sum(1 for c in cards if int(c.get("box", 0) or 0) >= 3)
        attempts = s.study.get("quiz", {}).get("attempts", [])
        bits = []
        if cards:
            bits.append(f"{learned}/{len(cards)} tarjetas aprendidas")
        if attempts:
            bits.append(f"{len(attempts)} test{'s' if len(attempts) != 1 else ''}")
        self.lbl_mastery_hint.setText(" · ".join(bits) or "Sin actividad")
        self._refresh_refine_button()

    # ── Repaso con turbo ──
    def _wav_seconds(self) -> float:
        s = self.session
        try:
            return max(0.0, (os.path.getsize(s.wav_path) - 44) / 32000) if s and s.wav_path else 0.0
        except OSError:
            return 0.0

    def _refresh_refine_button(self):
        s = self.session
        b = self.btn_refine
        running = self._refine is not None and self._refine.isRunning()
        if s is None or not (running or (s.wav_path and os.path.exists(s.wav_path))):
            b.hide()
            return
        b.show()
        if running:
            if self._refine.session_id == s.id:
                b.setText(f"✨  Repasando… {self._refine_pct} %")
                b.setToolTip("Turbo está re-transcribiendo esta clase. Pulsa para cancelar.")
            else:
                b.setText("✨  Repasando otra clase…")
                b.setToolTip("Turbo está ocupado con otra clase. Pulsa para cancelar ese repaso.")
            b.setEnabled(True)
        elif isinstance(s.study.get("refined"), dict):
            b.setText("✓  Repasada con turbo")
            b.setToolTip("Estos apuntes ya son la versión de turbo. Los fragmentos originales en directo "
                         "están guardados en la ficha.")
            b.setEnabled(False)
        else:
            b.setText("✨  Repasar con turbo")
            b.setToolTip("Vuelve a transcribir el audio de la clase con un modelo mucho más preciso.\n"
                         "Tarda más que la clase, pero puedes seguir usando la app mientras tanto.")
            b.setEnabled(True)

    def _on_refine_clicked(self):
        s = self.session
        if s is None:
            return
        if self._refine is not None and self._refine.isRunning():
            r = QMessageBox.question(self, "Cancelar repaso",
                                     "¿Cancelar el repaso con turbo? Los apuntes se quedan como estaban.")
            if r == QMessageBox.StandardButton.Yes:
                self._refine.cancel()
                self.btn_refine.setText("✨  Cancelando…")
                self.btn_refine.setEnabled(False)
            return
        if self.is_recording():
            QMessageBox.information(
                self, "Hay una clase grabándose",
                "Termina la clase antes de repasar con turbo: los dos a la vez se pelearían por "
                "la CPU y la transcripción en directo se quedaría atrás.")
            return
        secs = self._wav_seconds() or float(s.duration_s or 0)
        mins = max(1, int(secs * SECONDS_PER_AUDIO_SECOND / 60 + 0.999))
        r = QMessageBox.question(
            self, "Repasar con turbo",
            "Turbo vuelve a transcribir todo el audio de esta clase con un modelo mucho más "
            f"preciso que el de la grabación en directo.\n\nSin GPU tardará unos {mins} min y el equipo irá "
            "más cargado; puedes seguir usando la app mientras tanto.\n\nLos apuntes se sustituyen "
            "por la versión nueva (los originales quedan guardados). Tus tarjetas, test y chat no se tocan.")
        if r != QMessageBox.StandardButton.Yes:
            return
        w = RefineWorker(s.id, s.wav_path, s.entries, s.date, parent=self)
        w.progress.connect(self._on_refine_progress)
        w.done.connect(self._on_refine_done)
        w.failed.connect(self._on_refine_failed)
        w.cancelled.connect(self._on_refine_cancelled)
        w.finished.connect(self._on_refine_finished)
        self._refine = w
        self._refine_pct = 0
        w.start()
        self._refresh_refine_button()

    def _on_refine_progress(self, sid: str, pct: int, msg: str):
        self._refine_pct = pct
        if self.session is not None and self.session.id == sid:
            self.btn_refine.setText(f"✨  Repasando… {pct} %")

    def _on_refine_done(self, sid: str, entries: list):
        if not entries:
            QMessageBox.warning(self, "Repaso con turbo", "Turbo no ha sacado texto del audio de esta clase.")
            return
        stamp = {"model": REFINE_MODEL, "date": datetime.datetime.now().isoformat(timespec="seconds")}
        try:
            path = session_store.session_path(sid)
            fresh = session_store.load_session(path) if os.path.exists(path) else None
            if fresh is None and self.session is not None and self.session.id == sid:
                fresh = self.session
            if fresh is None:
                raise RuntimeError("la ficha de la clase ya no existe")
            if not isinstance(fresh.study.get("live_entries"), list):
                fresh.study["live_entries"] = [dict(e) for e in fresh.entries]
            fresh.study["refined"] = stamp
            fresh.entries = [dict(e) for e in entries]
            session_store.save_session(fresh)
        except Exception as e:
            QMessageBox.warning(self, "No se pudo guardar", f"El repaso terminó pero no se pudo guardar:\n{e}")
            return
        if self.session is not None and self.session.id == sid:
            # save() vuelca self.session.study encima de la ficha: tiene que llevar las marcas del repaso
            self.session.study["live_entries"] = fresh.study["live_entries"]
            self.session.study["refined"] = stamp
            self.sync_class_data(fresh)
        n = sum(1 for e in entries if e.get("kind", "text") != "marker")
        QMessageBox.information(
            self, "Apuntes repasados ✓",
            f"«{fresh.title or sid}» ya tiene la transcripción de turbo ({n} fragmentos).\n\n"
            "Si generaste tarjetas o test con la versión anterior, puedes generarlos de nuevo "
            "para aprovechar el texto mejor.")

    def _on_refine_failed(self, sid: str, msg: str):
        QMessageBox.warning(self, "Repaso con turbo", f"No se pudo repasar la clase:\n{msg}")

    def _on_refine_cancelled(self, sid: str):
        pass

    def _on_refine_finished(self):
        w = self._refine
        self._refine = None
        if w is not None:
            w.deleteLater()
        self._refresh_refine_button()
