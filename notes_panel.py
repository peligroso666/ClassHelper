"""
notes_panel.py - Panel de transcripción en tarjetas + resumen IA + guardado.

Cada fragmento es una tarjeta (FragmentCard) que se puede actualizar en sitio:
el texto crudo de Whisper aparece al instante y, cuando la IA termina,
la tarjeta se sustituye por el texto pulido sin reescribir todo el panel.

Al guardar genera un .txt organizado:
  1. Cabecera (fecha, duración, IA usada)
  2. Resumen IA (si hay IA activa)
  3. Transcripción mejorada por diapositiva
  4. Transcripción original Whisper (solo si la IA la modificó)
  + Audio .wav completo de la clase en la misma carpeta

to_session() convierte los fragmentos en una Session de session_store para la
biblioteca; el botón 🎓 Estudiar (study_requested) la abre en modo estudio.
"""

import os
import re
import html
import shutil
import struct
import datetime
from dataclasses import dataclass
from collections import defaultdict

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QScrollArea, QFrame,
    QPushButton, QLabel, QFileDialog, QMessageBox, QDialog,
    QApplication, QLineEdit, QSizePolicy,
)
from PySide6.QtGui import QFont
from PySide6.QtCore import Qt, QThread, Signal, QTimer

import theme
from widgets import Pill
import session_store


PLACEHOLDER_TEXT = (
    "La transcripción aparecerá aquí por diapositiva.\n\n"
    "1 · Abre el PDF de la clase\n"
    "2 · Ve a la diapositiva inicial\n"
    "3 · Pulsa  Iniciar clase\n\n"
    "Ctrl+M marca un momento importante."
)


# ── Modelo de datos ───────────────────────────────────────────────────────

@dataclass
class Entry:
    id: int
    slide: int
    ts: str
    raw: str
    enhanced: str = ""            # vacío = todavía no ha llegado la IA
    kind: str = "text"            # "text" | "marker"
    pending_ai: bool = False

    @property
    def best(self) -> str:
        return self.enhanced or self.raw


# ── Worker de resumen (no bloquea la UI) ─────────────────────────────────

class SummaryWorker(QThread):
    done   = Signal(str)
    failed = Signal(str)

    def __init__(self, enhancer, full_text: str, parent=None):
        super().__init__(parent)
        self.enhancer, self.full_text = enhancer, full_text

    def run(self):
        try:
            ok, summary = self.enhancer.generate_summary(self.full_text)
            (self.done if ok else self.failed).emit(summary)
        except Exception as e:
            self.failed.emit(str(e))


# ── Diálogo de resumen ────────────────────────────────────────────────────

class SummaryDialog(QDialog):
    def __init__(self, summary_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Resumen de la clase")
        self.setMinimumSize(720, 540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)

        title = QLabel("📋  Resumen de la clase")
        title.setStyleSheet(f"background:transparent; font-size:16px; font-weight:700; color:{theme.TEXT};")
        sub = QLabel("Generado por IA a partir de la transcripción completa.")
        sub.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        layout.addWidget(title)
        layout.addWidget(sub)

        self.text_edit = QTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(QFont("Segoe UI", 10))
        self.text_edit.setPlainText(summary_text)
        self.text_edit.setStyleSheet(
            f"QTextEdit{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
            f"border-radius:10px; padding:12px;}}"
        )
        layout.addWidget(self.text_edit)

        btns = QHBoxLayout()
        btn_copy = QPushButton("Copiar al portapapeles")
        btn_copy.clicked.connect(lambda: QApplication.clipboard().setText(summary_text))
        btn_close = QPushButton("Cerrar")
        btn_close.setProperty("accent", True)
        btn_close.clicked.connect(self.accept)
        btns.addWidget(btn_copy)
        btns.addStretch()
        btns.addWidget(btn_close)
        layout.addLayout(btns)


# ── Tarjeta de fragmento ──────────────────────────────────────────────────

class FragmentCard(QFrame):
    def __init__(self, entry: Entry, parent=None):
        super().__init__(parent)
        self.entry = entry
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.lbl_ts = QLabel(entry.ts)
        self.lbl_ts.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:10px; font-family:Consolas,monospace;"
        )
        self.badge = Pill("")
        self.badge.hide()
        head.addWidget(self.lbl_ts)
        head.addWidget(self.badge)
        head.addStretch()
        lay.addLayout(head)

        self.lbl_text = QLabel()
        self.lbl_text.setWordWrap(True)
        self.lbl_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lbl_text.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:13px; line-height:1.5;")
        lay.addWidget(self.lbl_text)

        self._highlight = ""
        self.refresh()

    def refresh(self):
        e = self.entry
        if e.kind == "marker":
            self.setStyleSheet(
                f"FragmentCard{{background:{theme.WARN_BG}; border:1px solid #6b4d14;"
                f"border-left:4px solid {theme.WARN}; border-radius:10px;}}"
            )
            self.lbl_text.setStyleSheet(f"background:transparent; color:#ffd98a; font-size:13px; font-weight:600;")
            self.badge.setText("📌 MARCA")
            self.badge.set_colors("#5a3d0a", theme.WARN)
            self.badge.show()
        else:
            self.setStyleSheet(
                f"FragmentCard{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER};"
                f"border-radius:10px;}}"
                f"FragmentCard:hover{{border-color:#3a4152;}}"
            )
            if e.pending_ai:
                self.badge.setText("✦ puliendo…")
                self.badge.set_colors("#2a2140", "#8f74c9")
                self.badge.show()
            elif e.enhanced and e.enhanced != e.raw:
                self.badge.setText("✦ IA")
                self.badge.set_colors("#2f1f55", theme.AI_H)
                self.badge.show()
            else:
                self.badge.hide()
        self._render_text()

    def set_highlight(self, needle: str):
        self._highlight = needle
        self._render_text()

    def _render_text(self):
        text = html.escape(self.entry.best)
        if self._highlight:
            pat = re.compile(re.escape(html.escape(self._highlight)), re.IGNORECASE)
            text = pat.sub(
                lambda m: f'<span style="background:#5a4a00; color:#fff; border-radius:3px;">{m.group(0)}</span>',
                text,
            )
        self.lbl_text.setText(text)

    def matches(self, needle: str) -> bool:
        return needle.lower() in self.entry.best.lower()


class SlideHeader(QLabel):
    def __init__(self, slide: int, parent=None):
        super().__init__(f"Diapositiva {slide}", parent)
        self.setStyleSheet(f"background:transparent; color:{theme.ACCENT}; font-size:11px; font-weight:700; letter-spacing:1px;"
            f"padding:10px 4px 2px 4px; text-transform:uppercase;"
        )


# ── Panel principal ───────────────────────────────────────────────────────

class NotesPanel(QWidget):
    study_requested = Signal()        # botón 🎓 Estudiar (la ventana abre el modo estudio)
    session_saved   = Signal(str)     # ruta del .txt escrito por «Guardar sesión»

    def __init__(self, enhancer=None):
        super().__init__()
        self.enhancer = enhancer
        self.entries: list[Entry] = []
        self._cards: dict[int, FragmentCard] = {}
        self._last_slide: int | None = None
        self._audio_source_path = ""
        self._default_save_path = ""
        self._session_start: datetime.datetime | None = None
        self._local_id = 0            # ids negativos para marcas (no chocan con los del audio)
        self._recording = False       # la ventana lo actualiza: Estudiar solo al terminar
        self._last_summary = ""       # último resumen generado al guardar (va a la biblioteca)
        self._setup_ui()

    # ── UI ───────────────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Cabecera
        header = QWidget()
        header.setObjectName("notesHeader")
        header.setFixedHeight(46)
        header.setStyleSheet(f"QWidget#notesHeader{{background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER};}}")
        hb = QHBoxLayout(header)
        hb.setContentsMargins(14, 6, 12, 6)
        hb.setSpacing(10)

        title = QLabel("Transcripción")
        title.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:13px; font-weight:700;")
        self.lbl_count = Pill("0 fragmentos")
        hb.addWidget(title)
        hb.addWidget(self.lbl_count)
        hb.addStretch()

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Buscar…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedSize(200, 30)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search)
        self.search_input.textChanged.connect(lambda _: self._search_timer.start())
        hb.addWidget(self.search_input)
        layout.addWidget(header)

        # Lista de tarjetas
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setStyleSheet(f"background:{theme.BG};")
        self.vbox = QVBoxLayout(self.inner)
        self.vbox.setContentsMargins(12, 8, 12, 12)
        self.vbox.setSpacing(8)
        self.vbox.addStretch()
        self.scroll.setWidget(self.inner)
        layout.addWidget(self.scroll, 1)

        # Placeholder
        self.placeholder = QLabel(PLACEHOLDER_TEXT)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:12px; line-height:1.6;")
        self.vbox.insertWidget(0, self.placeholder)

        # Barra inferior
        bar = QWidget()
        bar.setObjectName("notesBar")     # acotado: sin selector el fondo pisaba el estilo accent de los botones
        bar.setFixedHeight(52)
        bar.setStyleSheet(f"QWidget#notesBar{{background:{theme.SURFACE}; border-top:1px solid {theme.BORDER};}}")
        bl = QHBoxLayout(bar)
        bl.setContentsMargins(12, 8, 12, 8)
        bl.setSpacing(8)

        self.lbl_audio = QLabel("")
        self.lbl_audio.setStyleSheet(f"background:transparent; color:{theme.SUCCESS}; font-size:11px;")

        self.btn_clear = QPushButton("Limpiar")
        self.btn_clear.setProperty("flat", True)
        self.btn_clear.clicked.connect(self._confirm_clear)

        self.btn_summary = QPushButton("📋  Resumen")
        self.btn_summary.setToolTip("Resumen estructurado de la clase con IA")
        self.btn_summary.clicked.connect(self._on_summary_clicked)

        self.btn_study = QPushButton("🎓  Estudiar")
        self.btn_study.setToolTip("Abrir esta clase en el modo estudio: tarjetas, test y tutor "
                                  "(disponible al terminar la grabación)")
        self.btn_study.setEnabled(False)
        self.btn_study.clicked.connect(self.study_requested.emit)

        self.btn_save = QPushButton("Guardar sesión")
        self.btn_save.setProperty("accent", True)
        self.btn_save.clicked.connect(self.save_notes)

        for b in (self.btn_clear, self.btn_summary, self.btn_study, self.btn_save):
            b.setFixedHeight(34)

        bl.addWidget(self.lbl_audio)
        bl.addStretch()
        bl.addWidget(self.btn_clear)
        bl.addWidget(self.btn_summary)
        bl.addWidget(self.btn_study)
        bl.addWidget(self.btn_save)
        layout.addWidget(bar)

    # ── API pública ──────────────────────────────────────────────────────

    def set_save_path(self, path: str):
        self._default_save_path = path

    def set_audio_path(self, path: str):
        self._audio_source_path = path
        if path and os.path.exists(path):
            mb = os.path.getsize(path) / 1_048_576
            self.lbl_audio.setText(f"🎵  audio {mb:.1f} MB")
        else:
            self.lbl_audio.setText("")

    def set_recording(self, on: bool):
        """La ventana avisa si hay grabación en curso: 🎓 Estudiar solo al terminar."""
        self._recording = on
        self._refresh_study_button()

    def to_session(self, session_id: str, date_iso: str, duration_s: int, pdf_path: str,
                   txt_path: str, wav_path: str, summary: str = "") -> session_store.Session:
        """Session de la biblioteca con los fragmentos actuales (Entry → dict).
        El título es el nombre base del .txt; sin resumen explícito usa el último generado."""
        entries = [{"id": e.id, "slide": e.slide, "ts": e.ts, "raw": e.raw,
                    "enhanced": e.enhanced, "kind": e.kind} for e in self.entries]
        title = os.path.splitext(os.path.basename(txt_path))[0] if txt_path else f"clase_{session_id}"
        s = session_store.session_from_entries(title, date_iso, entries, duration_s,
                                               pdf_path, txt_path, wav_path,
                                               summary or self._last_summary)
        if session_id:
            s.id = session_id
        return s

    def add_transcription(self, chunk_id: int, slide: int, raw: str, ai_pending: bool):
        if self._session_start is None:
            self._session_start = datetime.datetime.now()
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        e = Entry(id=chunk_id, slide=slide, ts=ts, raw=raw, pending_ai=ai_pending)
        self._append(e)

    def set_enhanced(self, chunk_id: int, text: str):
        card = self._cards.get(chunk_id)
        if not card:
            return
        card.entry.enhanced = text
        card.entry.pending_ai = False
        card.refresh()

    def add_marker(self, slide: int, note: str = ""):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        label = note.strip() or "Momento importante — revisar"
        self._local_id -= 1
        e = Entry(id=self._local_id, slide=slide, ts=ts, raw=label, enhanced=label, kind="marker")
        self._append(e)

    def clear_notes(self):
        for c in self._cards.values():
            c.deleteLater()
        while self.vbox.count() > 1:
            item = self.vbox.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        self._cards.clear()
        self.entries.clear()
        self._last_slide = None
        self._session_start = None
        self._last_summary = ""          # el resumen era de la clase anterior
        self._refresh_study_button()
        self.lbl_count.setText("0 fragmentos")
        self.placeholder = QLabel(PLACEHOLDER_TEXT)     # el anterior ya es None tras el primer fragmento
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:12px; line-height:1.6;")
        self.vbox.insertWidget(0, self.placeholder)

    # ── Interno ──────────────────────────────────────────────────────────

    def _append(self, e: Entry):
        if self.placeholder is not None:
            self.placeholder.deleteLater()
            self.placeholder = None
        self.entries.append(e)
        idx = self.vbox.count() - 1          # antes del stretch
        if e.slide != self._last_slide:
            self.vbox.insertWidget(idx, SlideHeader(e.slide))
            idx += 1
            self._last_slide = e.slide
        card = FragmentCard(e)
        self._cards[e.id] = card
        self.vbox.insertWidget(idx, card)
        n = len(self.entries)
        self.lbl_count.setText(f"{n} fragmento{'s' if n != 1 else ''}")
        self._refresh_study_button()
        QTimer.singleShot(30, self._scroll_bottom)

    def _refresh_study_button(self):
        self.btn_study.setEnabled(bool(self.entries) and not self._recording)

    def _scroll_bottom(self):
        sb = self.scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _confirm_clear(self):
        if not self.entries:
            return
        r = QMessageBox.question(self, "Limpiar", "¿Borrar toda la transcripción de pantalla?")
        if r == QMessageBox.StandardButton.Yes:
            self.clear_notes()

    # ── Búsqueda ─────────────────────────────────────────────────────────

    def _apply_search(self):
        needle = self.search_input.text().strip()
        first = None
        for card in self._cards.values():
            card.set_highlight(needle if len(needle) >= 2 else "")
            if needle and first is None and card.matches(needle):
                first = card
        if first:
            self.scroll.ensureWidgetVisible(first, 0, 40)

    # ── Auto-guardado silencioso ─────────────────────────────────────────

    def autosave(self, path: str, audio_source: str = "") -> bool:
        if not self.entries:
            return False
        try:
            self._write_txt(path, summary="", silent=True)
        except Exception:
            return False
        if audio_source and os.path.exists(audio_source):
            try:
                self._copy_wav_fix_header(audio_source, os.path.splitext(path)[0] + ".wav")
            except Exception:
                pass
        return True

    @staticmethod
    def _copy_wav_fix_header(src: str, dst: str):
        """Copia el WAV en curso y repara la cabecera (tamaño 0 mientras graba)."""
        shutil.copy2(src, dst)
        try:
            with open(dst, "r+b") as f:
                raw = f.read()
                n = len(raw)
                f.seek(4)
                f.write(struct.pack("<I", n - 8))
                pos = raw.find(b"data")
                if pos != -1:
                    f.seek(pos + 4)
                    f.write(struct.pack("<I", n - pos - 8))
        except Exception:
            pass

    # ── Guardar sesión ───────────────────────────────────────────────────

    def save_notes(self):
        if not self.entries:
            QMessageBox.information(self, "Sin contenido", "No hay transcripción que guardar.")
            return
        ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default = self._default_save_path or f"clase_{ts_str}.txt"
        path, _ = QFileDialog.getSaveFileName(
            self, "Guardar sesión de clase", default, "Texto (*.txt);;Todos (*)"
        )
        if not path:
            return

        if self.enhancer and self.enhancer.is_active():
            self.btn_save.setText("Generando resumen…")
            self.btn_save.setEnabled(False)
            self._pending_save_path = path
            self._save_worker = SummaryWorker(self.enhancer, self._full_text())
            self._save_worker.done.connect(self._finalize_save)
            self._save_worker.failed.connect(self._save_summary_failed)
            self._save_worker.start()
            return
        self._do_save(path, summary="")

    def _save_summary_failed(self, err: str):
        """El resumen falló: se guarda sin él (nunca el mensaje de error como resumen)."""
        QMessageBox.warning(self, "No se pudo generar el resumen",
                            f"Los apuntes se guardan sin resumen.\n\n{err}")
        self._finalize_save("")

    def _finalize_save(self, summary: str):
        path = self._pending_save_path
        self._pending_save_path = ""
        self.btn_save.setText("Guardar sesión")
        self.btn_save.setEnabled(True)
        self._do_save(path, summary)

    def _do_save(self, path: str, summary: str):
        self._write_txt(path, summary)
        audio_saved = False
        if self._audio_source_path and os.path.exists(self._audio_source_path):
            try:
                self._copy_wav_fix_header(self._audio_source_path, os.path.splitext(path)[0] + ".wav")
                audio_saved = True
            except Exception as e:
                QMessageBox.warning(self, "Audio no guardado",
                                    f"Los apuntes se guardaron pero el audio falló:\n{e}")
        if summary:
            self._last_summary = summary
        self.session_saved.emit(path)          # la ventana actualiza la ficha de la biblioteca
        name = os.path.basename(os.path.splitext(path)[0])
        files = [f"  •  {os.path.basename(path)}"] + ([f"  •  {name}.wav"] if audio_saved else [])
        QMessageBox.information(
            self, "Sesión guardada ✓",
            f"Guardado en:\n{os.path.dirname(path) or '.'}\n\n" + "\n".join(files)
        )

    # ── Resumen ──────────────────────────────────────────────────────────

    def _on_summary_clicked(self):
        if not self.entries:
            QMessageBox.information(self, "Sin contenido", "No hay transcripción todavía.")
            return
        if not self.enhancer or not self.enhancer.is_active():
            QMessageBox.information(
                self, "IA no activa",
                "Activa la IA desde el botón ✦ IA para generar un resumen.\n\n"
                "Recomendado: Google Gemini (gratis, en la nube, sin consumir CPU)."
            )
            return
        self.btn_summary.setText("Generando…")
        self.btn_summary.setEnabled(False)
        self._summary_worker = SummaryWorker(self.enhancer, self._full_text())
        self._summary_worker.done.connect(self._summary_done)
        self._summary_worker.failed.connect(self._summary_failed)
        self._summary_worker.start()

    def _summary_done(self, summary: str):
        self.btn_summary.setText("📋  Resumen")
        self.btn_summary.setEnabled(True)
        SummaryDialog(summary, parent=self).exec()

    def _summary_failed(self, err: str):
        self.btn_summary.setText("📋  Resumen")
        self.btn_summary.setEnabled(True)
        QMessageBox.warning(self, "Error generando resumen", err)

    # ── Exportación ──────────────────────────────────────────────────────

    def _full_text(self) -> str:
        return "\n".join(f"[Diapo {e.slide} - {e.ts}] {e.best}" for e in self.entries)

    def _write_txt(self, path: str, summary: str = "", silent: bool = False):
        by_best: defaultdict[int, list[tuple[str, str]]] = defaultdict(list)
        by_raw:  defaultdict[int, list[tuple[str, str]]] = defaultdict(list)
        for e in self.entries:
            by_best[e.slide].append((e.ts, ("📌 " if e.kind == "marker" else "") + e.best))
            by_raw[e.slide].append((e.ts, e.raw))

        now = datetime.datetime.now()
        ai_info = "Ninguna (solo Whisper)"
        if self.enhancer and self.enhancer.is_active():
            ai_info = {
                "ollama": f"Ollama — {self.enhancer.ollama_model} (local)",
                "gemini": "Google Gemini 2.5 Flash",
                "claude": "Claude (Anthropic)",
                "openai": "OpenAI GPT-4o mini",
            }.get(self.enhancer.provider, self.enhancer.provider)

        dur_str = "—"
        if self._session_start:
            d = now - self._session_start
            h, r = divmod(int(d.total_seconds()), 3600)
            m, s = divmod(r, 60)
            dur_str = f"{h:02d}h {m:02d}m {s:02d}s"

        slides = sorted({e.slide for e in self.entries})
        slide_range = f"{slides[0]} – {slides[-1]}" if len(slides) > 1 else str(slides[0])

        S1 = "═" * 62
        def slide_header(sn: int) -> str:
            prefix = f"  ── Diapositiva {sn} "
            return prefix + "─" * max(0, 62 - len(prefix))

        lines = [
            S1, "           APUNTES DE CLASE  —  ClassHelper", S1,
            f"  Fecha           : {now.strftime('%d/%m/%Y a las %H:%M')}",
            f"  Duración sesión : {dur_str}",
            f"  Diapositivas    : {slide_range}",
            f"  Fragmentos      : {len(self.entries)}",
            f"  IA utilizada    : {ai_info}",
            S1,
        ]
        if summary:
            lines += ["", S1, "  RESUMEN DE LA CLASE  (generado por IA)", S1, "", summary, ""]

        ai_on = bool(self.enhancer and self.enhancer.is_active())
        lines += ["", S1,
                  "  TRANSCRIPCIÓN MEJORADA  (post-procesada por IA)" if ai_on
                  else "  TRANSCRIPCIÓN  (Whisper)", S1]
        for sn in sorted(by_best):
            lines += ["", slide_header(sn)]
            lines += [f"  [{ts}]  {txt}" for ts, txt in by_best[sn]]

        if any(e.enhanced and e.enhanced != e.raw for e in self.entries):
            lines += ["", "", S1, "  TRANSCRIPCIÓN ORIGINAL  (Whisper sin procesar)", S1]
            for sn in sorted(by_raw):
                lines += ["", slide_header(sn)]
                lines += [f"  [{ts}]  {txt}" for ts, txt in by_raw[sn]]

        lines += ["", "", S1, "  Generado con ClassHelper", S1, ""]
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception as e:
            if silent:
                raise
            QMessageBox.critical(self, "Error al guardar", str(e))
