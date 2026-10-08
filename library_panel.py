"""
library_panel.py - Biblioteca de clases grabadas.

Vista de todas las sesiones guardadas en session_store:
  1. Cabecera: título, contador, buscador y botón "Nueva clase".
  2. Tira semanal (L..D) con una pastilla por clase y filtro por día.
  3. Lista de tarjetas (SessionCard) con dominio, badges y acciones
     Estudiar / Carpeta / Quitar.

Emite open_session(ruta_json) al pulsar Estudiar (o doble clic en la
tarjeta) y new_class() al pulsar Nueva clase. La ventana principal decide
qué hacer con cada señal.
"""

import os
import datetime

from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QLineEdit,
    QScrollArea, QMessageBox, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QTimer, QUrl, QSize
from PySide6.QtGui import QDesktopServices, QCursor

import theme
from widgets import Pill
import session_store as store
from session_store import Session


# ── Fechas en español (sin depender del locale del sistema) ──────────────

DAY_LETTERS  = ["L", "M", "X", "J", "V", "S", "D"]
DAY_NAMES    = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MONTH_NAMES  = ["ene", "feb", "mar", "abr", "may", "jun",
                "jul", "ago", "sep", "oct", "nov", "dic"]


def session_datetime(s: Session) -> datetime.datetime | None:
    """Fecha de la sesión desde el ISO; si falla, desde el id YYYYMMDD_HHMM."""
    try:
        return datetime.datetime.fromisoformat(s.date)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.datetime.strptime(s.id, "%Y%m%d_%H%M")
    except (TypeError, ValueError):
        return None


def fmt_date(d: datetime.date) -> str:
    return f"{DAY_NAMES[d.weekday()]} {d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


def fmt_duration(seconds: int) -> str:
    seconds = int(seconds or 0)
    if seconds <= 0:
        return ""
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min"
    return f"{s} s"


def fmt_week_range(monday: datetime.date) -> str:
    sunday = monday + datetime.timedelta(days=6)
    if monday.month == sunday.month:
        return f"{monday.day} – {sunday.day} {MONTH_NAMES[monday.month - 1]} {monday.year}"
    if monday.year == sunday.year:
        return (f"{monday.day} {MONTH_NAMES[monday.month - 1]} – "
                f"{sunday.day} {MONTH_NAMES[sunday.month - 1]} {monday.year}")
    return (f"{monday.day} {MONTH_NAMES[monday.month - 1]} {monday.year} – "
            f"{sunday.day} {MONTH_NAMES[sunday.month - 1]} {sunday.year}")


def session_mastery(s: Session) -> int:
    try:
        return int(store.compute_mastery(s))
    except Exception:
        return int((s.study or {}).get("mastery") or 0)


def clear_layout(layout):
    """Vacía un layout ocultando los widgets al instante (deleteLater es diferido)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget() if item else None
        if w:
            w.hide()
            w.deleteLater()


# ── Etiqueta con elipsis (no ensancha el layout con títulos largos) ──────

class ElideLabel(QLabel):
    """QLabel que recorta con «…» cuando no cabe. hpad = padding horizontal del QSS."""

    def __init__(self, text: str = "", hpad: int = 0, parent=None):
        super().__init__(parent)
        self._full = ""
        self._hpad = hpad
        self.setMinimumWidth(36)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.setText(text)

    def setText(self, text: str):
        self._full = text or ""
        self._relayout()

    def text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:
        w = self.fontMetrics().horizontalAdvance(self._full) + self._hpad + 4
        return QSize(w, super().sizeHint().height())

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def _relayout(self):
        avail = max(12, self.width() - self._hpad - 2)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, avail)
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class DayPill(ElideLabel):
    """Pastilla pequeña de la tira semanal: una por clase, título recortado."""

    def __init__(self, text: str, parent=None):
        super().__init__(text, hpad=16, parent=parent)
        self.setStyleSheet(
            f"background:{theme.SUBJECT_BG}; color:{theme.ACCENT_H}; border-radius:8px;"
            f"padding:2px 7px; font-size:10px; font-weight:600;"
        )


# ── Tira semanal ─────────────────────────────────────────────────────────

class DayCell(QFrame):
    clicked = Signal(object)          # datetime.date
    MAX_PILLS = 3

    def __init__(self, day: datetime.date, parent=None):
        super().__init__(parent)
        self.day = day
        self.is_today = False
        self.selected = False
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.setMinimumHeight(96)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 7, 9, 8)
        lay.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(4)
        self.lbl_letter = QLabel(DAY_LETTERS[day.weekday()])
        self.lbl_number = QLabel(str(day.day))
        head.addWidget(self.lbl_letter)
        head.addStretch()
        head.addWidget(self.lbl_number)
        lay.addLayout(head)

        self.pills_box = QVBoxLayout()
        self.pills_box.setSpacing(3)
        lay.addLayout(self.pills_box)
        lay.addStretch()
        self._restyle()

    def set_sessions(self, sessions: list):
        clear_layout(self.pills_box)
        for s in sessions[: self.MAX_PILLS]:
            self.pills_box.addWidget(DayPill(s.title or s.id))
        extra = len(sessions) - self.MAX_PILLS
        if extra > 0:
            more = QLabel(f"+{extra} más")
            more.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:10px; padding-left:4px;")
            self.pills_box.addWidget(more)
        self.setToolTip(f"{fmt_date(self.day)} · {len(sessions)} clase{'s' if len(sessions) != 1 else ''}"
                        if sessions else fmt_date(self.day))

    def set_state(self, today: bool, selected: bool):
        self.is_today, self.selected = today, selected
        self._restyle()

    def _restyle(self):
        if self.selected:
            bg, border = theme.SURFACE_3, theme.ACCENT_H
        elif self.is_today:
            bg, border = theme.SURFACE_2, theme.ACCENT
        else:
            bg, border = theme.SURFACE_2, theme.BORDER
        hover = "" if (self.selected or self.is_today) else f"DayCell:hover{{border-color:{theme.DIM};}}"
        self.setStyleSheet(
            f"DayCell{{background:{bg}; border:1px solid {border}; border-radius:10px;}}{hover}"
        )
        num_color = theme.ACCENT if self.is_today else theme.TEXT
        self.lbl_letter.setStyleSheet(
            f"background:transparent; color:{theme.MUTED}; font-size:10px; font-weight:700; letter-spacing:1px;"
        )
        self.lbl_number.setStyleSheet(
            f"background:transparent; color:{num_color}; font-size:15px; font-weight:700;"
        )

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.day)
        super().mousePressEvent(e)


class WeekStrip(QWidget):
    """Siete columnas (lunes a domingo) con las clases de cada día. Clic = filtrar (toggle)."""

    day_filter_changed = Signal(object)      # datetime.date | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions: list = []
        self._selected: datetime.date | None = None
        self._monday = self._monday_of(datetime.date.today())

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.lbl_range = QLabel()
        self.lbl_range.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:13px; font-weight:700;")
        self.lbl_hint = QLabel()
        self.lbl_hint.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        nav.addWidget(self.lbl_range)
        nav.addWidget(self.lbl_hint)
        nav.addStretch()

        self.btn_prev = QPushButton("‹")
        self.btn_today = QPushButton("Hoy")
        self.btn_next = QPushButton("›")
        for b in (self.btn_prev, self.btn_today, self.btn_next):
            b.setFixedHeight(28)
            b.setProperty("flat", True)
        self.btn_prev.setFixedWidth(32)
        self.btn_next.setFixedWidth(32)
        self.btn_prev.setToolTip("Semana anterior")
        self.btn_next.setToolTip("Semana siguiente")
        self.btn_prev.clicked.connect(lambda: self._shift_week(-1))
        self.btn_next.clicked.connect(lambda: self._shift_week(1))
        self.btn_today.clicked.connect(self.go_today)
        nav.addWidget(self.btn_prev)
        nav.addWidget(self.btn_today)
        nav.addWidget(self.btn_next)
        lay.addLayout(nav)

        self.row = QHBoxLayout()
        self.row.setSpacing(6)
        lay.addLayout(self.row)
        self.cells: list[DayCell] = []
        self._build_cells()

    # ── API ──────────────────────────────────────────────────────────────

    @property
    def selected_day(self) -> datetime.date | None:
        return self._selected

    def set_sessions(self, sessions: list):
        self._sessions = list(sessions)
        self._fill_cells()

    def select_day(self, day: datetime.date | None):
        """Fija el filtro por día (None = sin filtro) y lo notifica."""
        if day is not None and self._monday_of(day) != self._monday:
            self._monday = self._monday_of(day)
            self._build_cells()
        self._selected = day
        self._refresh_states()
        self.day_filter_changed.emit(self._selected)

    def go_today(self):
        self._monday = self._monday_of(datetime.date.today())
        self._build_cells()
        if self._selected is not None:
            self._selected = None
            self.day_filter_changed.emit(None)
        self._refresh_states()

    # ── Interno ──────────────────────────────────────────────────────────

    @staticmethod
    def _monday_of(d: datetime.date) -> datetime.date:
        return d - datetime.timedelta(days=d.weekday())

    def _shift_week(self, n: int):
        self._monday += datetime.timedelta(days=7 * n)
        self._build_cells()
        if self._selected is not None:
            self._selected = None
            self.day_filter_changed.emit(None)
        self._refresh_states()

    def _build_cells(self):
        clear_layout(self.row)
        self.cells = []
        for i in range(7):
            cell = DayCell(self._monday + datetime.timedelta(days=i))
            cell.clicked.connect(self._on_cell_clicked)
            self.row.addWidget(cell, 1)
            self.cells.append(cell)
        self._fill_cells()
        self._refresh_states()

    def _fill_cells(self):
        by_day: dict[datetime.date, list] = {}
        for s in self._sessions:
            dt = session_datetime(s)
            if dt:
                by_day.setdefault(dt.date(), []).append(s)
        for cell in self.cells:
            cell.set_sessions(sorted(by_day.get(cell.day, []), key=lambda s: s.date))

    def _refresh_states(self):
        today = datetime.date.today()
        for cell in self.cells:
            cell.set_state(cell.day == today, cell.day == self._selected)
        self.lbl_range.setText(fmt_week_range(self._monday))
        if self._monday == self._monday_of(today):
            self.lbl_hint.setText("· esta semana")
        else:
            delta = (self._monday - self._monday_of(today)).days // 7
            self.lbl_hint.setText(f"· hace {-delta} semana{'s' if delta != -1 else ''}" if delta < 0
                                  else f"· dentro de {delta} semana{'s' if delta != 1 else ''}")

    def _on_cell_clicked(self, day: datetime.date):
        self._selected = None if self._selected == day else day
        self._refresh_states()
        self.day_filter_changed.emit(self._selected)


# ── Tarjeta de sesión ────────────────────────────────────────────────────

class SessionCard(QFrame):
    study_requested   = Signal(str)     # ruta del json
    delete_requested  = Signal(str)     # id de la sesión
    process_requested = Signal(str)     # id de la sesión a preparar
    pause_requested   = Signal(str)     # id de la sesión a pausar

    def __init__(self, session: Session, parent=None):
        super().__init__(parent)
        self.session = session
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(
            f"SessionCard{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER}; border-radius:10px;}}"
            f"SessionCard:hover{{border-color:{theme.DIM};}}"
        )

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 14, 12)
        lay.setSpacing(12)

        info = QVBoxLayout()
        info.setSpacing(5)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.lbl_title = ElideLabel(session.title or session.id)
        self.lbl_title.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:14px; font-weight:700;")
        head.addWidget(self.lbl_title)
        if session.subject:
            head.addWidget(Pill(session.subject, theme.SUBJECT_BG, theme.ACCENT_H))
        entries = session.entries or []
        texts   = [e for e in entries if e.get("kind", "text") != "marker"]
        markers = [e for e in entries if e.get("kind", "text") == "marker"]
        if any(e.get("enhanced") for e in texts):
            head.addWidget(Pill("✦ IA", "#2f1f55", theme.AI_H))
        if markers:
            head.addWidget(Pill(f"📌 {len(markers)}", "#5a3d0a", theme.WARN))
        head.addStretch()
        info.addLayout(head)

        self.lbl_meta = ElideLabel(self._meta_line(session, texts))
        self.lbl_meta.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        info.addWidget(self.lbl_meta)
        lay.addLayout(info, 1)

        mastery = session_mastery(session)
        if mastery >= 80:
            colors = (theme.SUCCESS_BG, theme.SUCCESS)
        elif mastery >= 50:
            colors = (theme.WARN_BG, theme.WARN)
        else:
            colors = (theme.SURFACE_3, theme.DIM)
        self.pill_mastery = Pill(f"Dominio {mastery} %", *colors)
        self.pill_mastery.setToolTip("Porcentaje de dominio: tarjetas de memoria y mejor resultado del test")
        lay.addWidget(self.pill_mastery, 0, Qt.AlignmentFlag.AlignVCenter)

        # Estado de la preparación automática (turbo, resumen, tarjetas, test)
        self.pill_state = Pill("")
        self.pill_state.hide()
        lay.addWidget(self.pill_state, 0, Qt.AlignmentFlag.AlignVCenter)
        self.btn_process = QPushButton("⚙  Preparar")
        self.btn_process.setToolTip("Rehace los apuntes desde el audio con turbo y genera "
                                    "resumen, tarjetas y test")
        self.btn_process.clicked.connect(lambda: self.process_requested.emit(self.session.id))
        self.btn_process.setFixedHeight(32)
        lay.addWidget(self.btn_process)

        self.btn_study = QPushButton("🎓  Estudiar")
        self.btn_study.setProperty("accent", True)
        self.btn_study.setToolTip("Abrir el modo estudio de esta clase")
        self.btn_study.clicked.connect(self._emit_study)
        self.btn_folder = QPushButton("📂  Carpeta")
        self.btn_folder.setToolTip("Abrir la carpeta con los apuntes y el audio")
        self.btn_folder.clicked.connect(self.open_folder)
        self.btn_delete = QPushButton("🗑️")
        self.btn_delete.setProperty("flat", True)
        self.btn_delete.setFixedWidth(36)
        self.btn_delete.setStyleSheet("padding:0px;")   # el padding global (14 px) recortaba el icono
        self.btn_delete.setToolTip("Quitar de la biblioteca")
        self.btn_delete.clicked.connect(lambda: self.delete_requested.emit(self.session.id))
        for b in (self.btn_study, self.btn_folder, self.btn_delete):
            b.setFixedHeight(32)
            lay.addWidget(b)

    def set_processing(self, estado):
        """estado = (mensaje, %) mientras se prepara; None cuando no."""
        from auto_process import pending_steps
        if estado:
            msg, pct = estado
            self.pill_state.setText(f"⚙ {pct} %")
            self.pill_state.set_colors(theme.SURFACE_3, theme.ACCENT_H)
            self.pill_state.setToolTip(msg or "Preparando la clase")
            self.pill_state.show()
            # Mientras prepara, el botón sirve para PARARLO (antes no había manera)
            self.btn_process.setEnabled(True)
            self.btn_process.setText("⏸  Pausar")
            self.btn_process.setToolTip("Para la preparación y libera la CPU")
            try:
                self.btn_process.clicked.disconnect()
            except TypeError:
                pass
            self.btn_process.clicked.connect(lambda: self.pause_requested.emit(self.session.id))
            self.btn_process.show()
            return
        self.btn_process.setEnabled(True)
        self.btn_process.setText("⚙  Preparar")
        try:
            self.btn_process.clicked.disconnect()
        except TypeError:
            pass
        self.btn_process.clicked.connect(lambda: self.process_requested.emit(self.session.id))
        falta = pending_steps(self.session)
        if not falta:
            self.pill_state.setText("✓ lista")
            self.pill_state.set_colors(theme.SUCCESS_BG, theme.SUCCESS)
            self.pill_state.setToolTip("Apuntes del audio, resumen, tarjetas y test hechos")
            self.pill_state.show()
            self.btn_process.hide()
        else:
            self.pill_state.hide()
            self.btn_process.show()
            self.btn_process.setToolTip("Falta: " + ", ".join(falta))

    @staticmethod
    def _meta_line(s: Session, texts: list) -> str:
        parts = []
        dt = session_datetime(s)
        if dt:
            parts.append(fmt_date(dt.date()))
            parts.append(dt.strftime("%H:%M"))
        dur = fmt_duration(s.duration_s)
        if dur:
            parts.append(dur)
        n = len(texts)
        parts.append(f"{n} fragmento{'s' if n != 1 else ''}")
        slides = sorted({int(e.get("slide") or 0) for e in texts if int(e.get("slide") or 0) > 0})
        if len(slides) > 1:
            parts.append(f"diapos {slides[0]}–{slides[-1]}")
        elif slides:
            parts.append(f"diapo {slides[0]}")
        return "  ·  ".join(parts)

    def matches(self, needle: str) -> bool:
        needle = needle.lower()
        return needle in (self.session.title or "").lower() or needle in (self.session.subject or "").lower()

    def _emit_study(self):
        self.study_requested.emit(store.session_path(self.session.id))

    def open_folder(self):
        folder = ""
        for p in (self.session.txt_path, self.session.wav_path, self.session.pdf_path):
            if p and os.path.isdir(os.path.dirname(p)):
                folder = os.path.dirname(p)
                break
        if not folder:
            folder = store.LIBRARY_DIR
            os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

    def mouseDoubleClickEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._emit_study()
        super().mouseDoubleClickEvent(e)


# ── Panel principal ──────────────────────────────────────────────────────

class LibraryPanel(QWidget):
    open_session = Signal(str)     # ruta del json
    process_requested = Signal(str)  # id de la clase a preparar (turbo + material)
    pause_requested = Signal(str)    # id de la clase cuya preparación se para
    prepare_all_requested = Signal()  # preparar todo lo pendiente
    pause_all_requested = Signal()    # parar toda la cola
    new_class    = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sessions: list[Session] = []
        self._cards: list[SessionCard] = []
        self._processing: dict[str, tuple[str, int]] = {}
        self._day: datetime.date | None = None
        self._setup_ui()

    # ── UI ───────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Cabecera + tira semanal sobre fondo SURFACE
        top = QWidget()
        top.setObjectName("libTop")      # selector acotado: el borde no se hereda en los hijos
        top.setStyleSheet(f"QWidget#libTop{{background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER};}}")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(24, 18, 24, 16)
        tl.setSpacing(14)

        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        title = QLabel("Biblioteca")
        title.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:22px; font-weight:800;")
        self.pill_count = Pill("0 clases")
        title_row.addWidget(title)
        title_row.addWidget(self.pill_count, 0, Qt.AlignmentFlag.AlignVCenter)
        title_row.addStretch()
        sub = QLabel("Todas tus clases, listas para estudiar")
        sub.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:12px;")
        titles.addLayout(title_row)
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Buscar por título o asignatura…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedSize(260, 34)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(150)
        self._search_timer.timeout.connect(self._apply_filters)
        self.search_input.textChanged.connect(lambda _: self._search_timer.start())
        head.addWidget(self.search_input)

        # Preparación (turbo + resumen + material): todo en un menú
        self.btn_prep = QPushButton("⚙  Preparación")
        self.btn_prep.setFixedHeight(36)
        self.btn_prep.setToolTip("Preparar clases pendientes y ajustar cuándo y con cuántos núcleos")
        self.btn_prep.clicked.connect(self._show_prep_menu)
        head.addWidget(self.btn_prep)
        self._queue_count = 0

        self.btn_new = QPushButton("●  Nueva clase")
        self.btn_new.setProperty("accent", True)
        self.btn_new.setFixedHeight(36)
        self.btn_new.setToolTip("Volver a la pantalla de grabación")
        self.btn_new.clicked.connect(self.new_class.emit)
        head.addWidget(self.btn_new)
        tl.addLayout(head)

        self.week = WeekStrip()
        self.week.day_filter_changed.connect(self._on_day_filter)
        tl.addWidget(self.week)
        root.addWidget(top)

        # Lista
        self.lbl_section = QLabel("Todas las clases")
        self.lbl_section.setStyleSheet(
            f"background:transparent; color:{theme.ACCENT}; font-size:11px; font-weight:700; letter-spacing:1px;"
        )
        self.btn_clear_filter = QPushButton("Quitar filtro")
        self.btn_clear_filter.setProperty("flat", True)
        self.btn_clear_filter.setFixedHeight(26)
        self.btn_clear_filter.clicked.connect(self._clear_filters)
        self.btn_clear_filter.hide()
        sec = QHBoxLayout()
        sec.setContentsMargins(24, 14, 24, 0)
        sec.addWidget(self.lbl_section)
        sec.addStretch()
        sec.addWidget(self.btn_clear_filter)
        root.addLayout(sec)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("libInner")     # acotado: sin selector pisaría el estilo de los botones hijos
        self.inner.setStyleSheet(f"QWidget#libInner{{background:{theme.BG};}}")
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(24, 10, 24, 20)
        self.vbox.setSpacing(8)

        # Estado vacío
        self.empty = QWidget()
        el = QVBoxLayout(self.empty)
        el.setContentsMargins(0, 60, 0, 60)
        el.setSpacing(14)
        self.lbl_empty = QLabel("Aún no hay clases grabadas.\nPulsa Nueva clase para empezar.")
        self.lbl_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_empty.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:13px; line-height:1.6;")
        self.btn_empty_new = QPushButton("●  Nueva clase")
        self.btn_empty_new.setProperty("accent", True)
        self.btn_empty_new.setFixedHeight(36)
        self.btn_empty_new.clicked.connect(self.new_class.emit)
        el.addWidget(self.lbl_empty)
        el.addWidget(self.btn_empty_new, 0, Qt.AlignmentFlag.AlignHCenter)
        self.vbox.addWidget(self.empty)
        self.vbox.addStretch()

        self.scroll.setWidget(self.inner)
        root.addWidget(self.scroll, 1)

    # ── API pública ──────────────────────────────────────────────────────

    def set_queue_count(self, n: int):
        self._queue_count = n
        self.btn_prep.setText(f"⚙  Preparando · {n}" if n else "⚙  Preparación")

    def _show_prep_menu(self):
        from PySide6.QtWidgets import QMenu
        from PySide6.QtGui import QAction
        import os
        import app_config
        from auto_process import pending_steps, estimate_minutes

        cfg = app_config.load()
        menu = QMenu(self)
        pend = [s for s in self._sessions if pending_steps(s)]
        mins = sum(estimate_minutes(s) for s in pend)
        a = QAction(f"Preparar todo lo pendiente ({len(pend)} clase{'s' if len(pend) != 1 else ''}"
                    + (f", ~{mins} min)" if pend else ")"), menu)
        a.setEnabled(bool(pend))
        a.triggered.connect(self.prepare_all_requested.emit)
        menu.addAction(a)
        if self._queue_count:
            p = QAction(f"⏸  Pausar todo ({self._queue_count} en cola)", menu)
            p.triggered.connect(self.pause_all_requested.emit)
            menu.addAction(p)
        menu.addSeparator()

        def toggle(key, default, text):
            act = QAction(text, menu, checkable=True)
            act.setChecked(bool(cfg.get(key, default)))
            act.toggled.connect(lambda v, k=key: app_config.save_key(k, v))
            menu.addAction(act)

        toggle("prepare_on_finish", True, "Preparar al terminar cada clase")
        toggle("prepare_ac_only", False, "Solo con el cargador enchufado")

        sub = menu.addMenu("Núcleos para turbo")
        total = os.cpu_count() or 8
        actual = int(cfg.get("refine_threads") or 0) or max(2, total // 2)
        for n, txt in ((max(2, total // 4), "pocos · va fresco, tarda más"),
                       (max(2, total // 2), "la mitad · recomendado"),
                       (max(2, total - 2), "casi todos · rápido, el PC va justo")):
            act = QAction(f"{n}  ·  {txt}", sub, checkable=True)
            act.setChecked(n == actual)
            act.triggered.connect(lambda _=False, v=n: app_config.save_key("refine_threads", v))
            sub.addAction(act)
        menu.exec(self.btn_prep.mapToGlobal(self.btn_prep.rect().bottomLeft()))

    def set_processing(self, session_id: str, msg: str, pct: int):
        """La ventana avisa del progreso: la tarjeta de esa clase lo muestra."""
        if msg:
            self._processing[session_id] = (msg, pct)
        else:
            self._processing.pop(session_id, None)
        for c in self._cards:
            if c.session.id == session_id:
                c.set_processing(self._processing.get(session_id))

    def refresh(self):
        """Relee la biblioteca del disco y reconstruye tira semanal y tarjetas."""
        try:
            self._sessions = store.list_sessions()
        except Exception:
            self._sessions = []
        n = len(self._sessions)
        self.pill_count.setText(f"{n} clase{'s' if n != 1 else ''}")
        self.week.set_sessions(self._sessions)

        for c in self._cards:
            self.vbox.removeWidget(c)
            c.hide()
            c.deleteLater()
        self._cards = []
        for s in self._sessions:
            card = SessionCard(s)
            card.study_requested.connect(self.open_session.emit)
            card.delete_requested.connect(self._confirm_delete)
            card.process_requested.connect(self.process_requested.emit)
            card.pause_requested.connect(self.pause_requested.emit)
            card.set_processing(self._processing.get(s.id))
            self.vbox.insertWidget(self.vbox.count() - 1, card)   # antes del stretch
            self._cards.append(card)
        self._apply_filters()

    # ── Filtros ──────────────────────────────────────────────────────────

    def _on_day_filter(self, day):
        self._day = day
        self._apply_filters()

    def _clear_filters(self):
        self.search_input.clear()
        if self.week.selected_day is not None:
            self.week.select_day(None)
        else:
            self._apply_filters()

    def _apply_filters(self):
        needle = self.search_input.text().strip()
        shown = 0
        for card in self._cards:
            ok = (not needle or card.matches(needle))
            if ok and self._day is not None:
                dt = session_datetime(card.session)
                ok = dt is not None and dt.date() == self._day
            card.setVisible(ok)
            shown += ok

        filtered = bool(needle) or self._day is not None
        self.btn_clear_filter.setVisible(filtered)
        if self._day is not None:
            text = f"Clases del {fmt_date(self._day)}"
        elif needle:
            text = f"Resultados para «{needle}»"
        else:
            text = "Todas las clases"
        if filtered:
            text += f"  ·  {shown}"
        self.lbl_section.setText(text.upper())

        if not self._cards:
            self.lbl_empty.setText("Aún no hay clases grabadas.\nPulsa Nueva clase para empezar.")
            self.btn_empty_new.show()
            self.empty.show()
        elif shown == 0:
            self.lbl_empty.setText("Ninguna clase coincide con el filtro.")
            self.btn_empty_new.hide()
            self.empty.show()
        else:
            self.empty.hide()

    # ── Borrado ──────────────────────────────────────────────────────────

    def _confirm_delete(self, session_id: str):
        s = next((x for x in self._sessions if x.id == session_id), None)
        name = s.title if s else session_id
        r = QMessageBox.question(
            self, "Quitar de la biblioteca",
            f"¿Quitar «{name}» de la biblioteca?\n\n"
            "Se borra solo la ficha de estudio (tarjetas, test y chat).\n"
            "Los apuntes .txt, el PDF y el audio se conservan.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r != QMessageBox.StandardButton.Yes:
            return
        try:
            store.delete_session(session_id)
        except Exception as e:
            QMessageBox.warning(self, "No se pudo quitar", str(e))
        self.refresh()
