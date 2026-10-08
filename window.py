"""
window.py - Ventana principal de ClassHelper.

Layout:
  [Toolbar: Logo | 🎙 Clase / 📚 Biblioteca | Abrir PDF | Calidad | ✦ IA | 📌  ……  Diapo | VU | ● timer | ● Iniciar clase]
  [QStackedWidget:  0 = ThumbnailStrip | PDFPanel ‖ NotesPanel   ·   1 = LibraryPanel   ·   2 = StudyPanel]
  [StatusBar]

Modos: Clase (grabar) y Biblioteca (clases guardadas → Estudiar). Cambiar de modo
no detiene la grabación. Cada autoguardado escribe también la ficha JSON de la
biblioteca (session_store) con un id fijo durante toda la clase.
"""

import os
import datetime

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QPushButton, QLabel, QFileDialog, QSplitter, QComboBox, QMessageBox, QApplication,
    QStackedWidget, QButtonGroup, QFrame, QTabWidget,
)
from PySide6.QtCore import Qt, Slot, QTimer
from PySide6.QtGui import QShortcut, QKeySequence, QAction
from PySide6.QtWidgets import QMenu

import theme
from widgets import LevelMeter, StatusDot, vsep
from thumbnail_strip import ThumbnailStrip
from pdf_panel import PDFPanel
from notes_panel import NotesPanel
from subtitle_bar import SubtitleBar
from live_notes import LiveNotes
from audio_thread import AudioWorker, cuda_available
from ai_enhancer import AIEnhancer
import app_config
from settings_dialog import SettingsDialog
import session_store
from library_panel import LibraryPanel
from auto_process import AutoProcessor, pending_steps, estimate_minutes
from study_panel import StudyPanel


# Calidad de transcripción → modelo Whisper.
# Medido en un i7-1360P sin GPU con un chunk de 29 s: small 23 s, turbo 51 s, medium 63 s.
# Sin GPU solo small va en tiempo real; los otros acumulan cola.
QUALITY_LEVELS = [
    ("Tiempo real",  "small",          "Whisper small · va en tiempo real incluso sin GPU · recomendado"),
    ("Turbo",        "large-v3-turbo", "Whisper large-v3-turbo · más preciso · en directo solo con GPU NVIDIA"),
    ("Medium",       "medium",         "Whisper medium · más lento y menos preciso que turbo"),
]


# Páginas del QStackedWidget central
MODE_CLASS, MODE_LIBRARY, MODE_STUDY = 0, 1, 2

# Selector de modo de la toolbar: dos pestañas, la activa en azul
MODE_BTN_QSS = f"""
QPushButton {{
    background: transparent; color: {theme.MUTED}; border: none; border-radius: 7px;
    padding: 0 10px; font-weight: 600;
}}
QPushButton:hover {{ background: {theme.SURFACE_3}; color: {theme.TEXT}; }}
QPushButton:checked {{ background: {theme.ACCENT}; color: white; }}
QPushButton:checked:hover {{ background: {theme.ACCENT_H}; }}
"""


_load_cfg = app_config.load
_save_cfg_key = app_config.save_key


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("ClassHelper")
        self.setMinimumSize(1100, 680)
        self.resize(1440, 860)

        self.is_recording = False
        self._stopping = False
        self.audio_worker: AudioWorker | None = None
        self._elapsed = 0
        self._pdf_path = ""
        self._save_path = ""
        self._class_id = ""            # id de la ficha en la biblioteca (fijo durante la clase)
        self._class_date_iso = ""
        self.enhancer = AIEnhancer()

        self._session_timer = QTimer(self)
        self._session_timer.setInterval(1000)
        self._session_timer.timeout.connect(self._tick)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(10 * 60 * 1000)
        self._autosave_timer.timeout.connect(self._autosave)

        # La biblioteca se prepara sola al acabar cada clase (turbo, resumen, material)
        self.auto = AutoProcessor(self.enhancer, is_recording=lambda: self.is_recording, parent=self)
        self.auto.progress.connect(self._on_auto_progress)
        self.auto.finished.connect(self._on_auto_finished)
        self.auto.failed.connect(self._on_auto_failed)

        self._setup_ui()
        self.setStyleSheet(theme.APP_QSS)
        self._refresh_ai_button()

        QShortcut(QKeySequence("Ctrl+M"), self, self._mark_moment)
        QShortcut(QKeySequence("Ctrl+O"), self, self._open_pdf)
        QShortcut(QKeySequence("Ctrl+R"), self, self._toggle_recording)
        QShortcut(QKeySequence("Ctrl+F"), self, self._focus_search)
        QShortcut(QKeySequence("Ctrl+1"), self, lambda: self._set_mode(MODE_CLASS))
        QShortcut(QKeySequence("Ctrl+2"), self, lambda: self._set_mode(MODE_LIBRARY))

        self.statusBar().showMessage("  Abre un PDF para empezar  ·  Ctrl+O")
        # Precarga del modelo Whisper nada más abrir (así Iniciar clase es instantáneo)
        QTimer.singleShot(800, lambda: self._ensure_worker().preload())
        if app_config.is_first_run():
            QTimer.singleShot(400, self._welcome)

    # ── Construcción ─────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)
        main.addWidget(self._build_toolbar())

        content = QWidget()
        row = QHBoxLayout(content)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.thumb_strip = ThumbnailStrip()
        row.addWidget(self.thumb_strip)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(2)
        self.pdf_panel = PDFPanel()
        self.notes_panel = NotesPanel(self.enhancer)
        self.live_notes = LiveNotes()

        # La transcripción en directo es solo orientativa: la protagonista es la
        # diapositiva, y a la derecha manda el bloc del alumno.
        self.side_tabs = QTabWidget()
        self.side_tabs.setDocumentMode(True)
        self.side_tabs.addTab(self.live_notes, "✍️  Mis notas")
        self.side_tabs.addTab(self.notes_panel, "📝  Transcripción")
        self.side_tabs.setTabToolTip(1, "Transcripción rápida en directo: orientativa. "
                                        "Los apuntes buenos salen del audio al terminar.")
        self.splitter.addWidget(self.pdf_panel)
        self.splitter.addWidget(self.side_tabs)
        self.splitter.setStretchFactor(0, 5)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([980, 380])
        row.addWidget(self.splitter)

        # Página de clase = contenido + subtítulos abajo
        class_page = QWidget()
        cp = QVBoxLayout(class_page)
        cp.setContentsMargins(0, 0, 0, 0)
        cp.setSpacing(0)
        cp.addWidget(content, 1)
        self.subtitles = SubtitleBar()
        self.subtitles.hide()
        cp.addWidget(self.subtitles)

        self.stack = QStackedWidget()
        self.stack.addWidget(class_page)              # 0 · Clase
        self.library = LibraryPanel()
        self.stack.addWidget(self.library)            # 1 · Biblioteca
        self.study = StudyPanel(self.enhancer)
        self.study.is_recording = lambda: self.is_recording
        self.stack.addWidget(self.study)              # 2 · Estudiar
        main.addWidget(self.stack, 1)

        self.pdf_panel.slide_changed.connect(self._on_slide_changed)
        self.pdf_panel.pdf_loaded.connect(self._on_pdf_loaded)
        self.thumb_strip.slide_selected.connect(self._on_thumb_selected)
        self.library.open_session.connect(self._open_library_session)
        self.library.new_class.connect(lambda: self._set_mode(MODE_CLASS))
        self.library.process_requested.connect(self._process_session)
        self.library.pause_requested.connect(self._pause_processing)
        self.library.prepare_all_requested.connect(self._prepare_all)
        self.library.pause_all_requested.connect(self._pause_all)
        self.auto.queue_changed.connect(self.library.set_queue_count)
        self.auto.waiting.connect(lambda why: self.statusBar().showMessage(
            f"  ⏳ Preparación en espera: {why}", 8000))
        self.study.back.connect(lambda: self._set_mode(MODE_LIBRARY))
        self.notes_panel.study_requested.connect(self._study_current_class)
        self.live_notes.changed.connect(self._save_live_notes)
        self.live_notes.btn_slide.clicked.connect(self._insert_slide_note)
        self.notes_panel.session_saved.connect(self._on_session_saved)

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(60)
        bar.setStyleSheet(f"background:{theme.SURFACE}; border-bottom:1px solid {theme.BORDER};")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 8, 16, 8)
        row.setSpacing(8)

        logo = QLabel("Class<span style='color:%s'>Helper</span>" % theme.ACCENT)
        logo.setTextFormat(Qt.TextFormat.RichText)
        logo.setStyleSheet(f"background:transparent; color:{theme.TEXT}; font-size:17px; font-weight:800; letter-spacing:0.5px;")
        row.addWidget(logo)
        row.addWidget(vsep())
        row.addWidget(self._build_mode_selector())
        sep_mode = vsep()
        row.addWidget(sep_mode)

        self.btn_open = QPushButton("📄  Abrir PDF")
        self.btn_open.setFixedHeight(36)
        self.btn_open.setToolTip("Abrir el PDF de la clase  (Ctrl+O)")
        self.btn_open.clicked.connect(self._open_pdf)
        row.addWidget(self.btn_open)

        lbl_q = QLabel("Calidad")
        lbl_q.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        row.addWidget(lbl_q)
        self.combo_quality = QComboBox()
        self.combo_quality.setFixedHeight(36)
        for label, _model, tip in QUALITY_LEVELS:
            self.combo_quality.addItem(label)
        saved_model = _load_cfg().get("whisper_model", "small")
        idx = next((i for i, (_, m, _) in enumerate(QUALITY_LEVELS) if m == saved_model), 0)
        self.combo_quality.setCurrentIndex(idx)
        self.combo_quality.setToolTip("\n".join(f"{l}: {t}" for l, _, t in QUALITY_LEVELS))
        self.combo_quality.currentIndexChanged.connect(self._on_quality_changed)
        row.addWidget(self.combo_quality)
        sep_q = vsep()
        row.addWidget(sep_q)

        self.btn_ai = QPushButton("✦ IA")
        self.btn_ai.setFixedSize(96, 36)
        self.btn_ai.clicked.connect(self._open_settings)
        row.addWidget(self.btn_ai)

        self.btn_mark = QPushButton("📌")
        self.btn_mark.setFixedSize(40, 36)
        self.btn_mark.setToolTip("Marcar momento importante  (Ctrl+M)")
        self.btn_mark.setEnabled(False)
        self.btn_mark.clicked.connect(self._mark_moment)
        row.addWidget(self.btn_mark)

        row.addStretch()

        self.lbl_slide = QLabel("—")
        self.lbl_slide.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:12px; font-weight:600; min-width:64px;")
        self.lbl_slide.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.lbl_slide)
        sep_slide = vsep()
        row.addWidget(sep_slide)

        self.btn_mic = QPushButton("🎤")
        self.btn_mic.setFixedSize(40, 36)
        self.btn_mic.setProperty("flat", True)
        self.btn_mic.setToolTip("Elegir micrófono")
        self.btn_mic.clicked.connect(self._show_mic_menu)
        row.addWidget(self.btn_mic)

        self.meter = LevelMeter()
        row.addWidget(self.meter)

        self.dot = StatusDot()
        row.addWidget(self.dot)

        self.lbl_timer = QLabel("00:00:00")
        self.lbl_timer.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:14px; font-weight:600; "
            f"font-family:Consolas,'Cascadia Mono',monospace; min-width:80px;"
        )
        row.addWidget(self.lbl_timer)
        sep_timer = vsep()
        row.addWidget(sep_timer)

        self.btn_record = QPushButton("●  Iniciar clase")
        self.btn_record.setFixedHeight(38)
        self.btn_record.setEnabled(False)
        self.btn_record.setStyleSheet(theme.BTN_RECORD_IDLE)
        self.btn_record.setToolTip("Iniciar / detener la grabación  (Ctrl+R)")
        self.btn_record.clicked.connect(self._toggle_recording)
        row.addWidget(self.btn_record)

        # Controles que solo tienen sentido en modo Clase. El punto y el timer no
        # están: se quedan visibles en Biblioteca/Estudiar mientras se graba.
        # ✦ IA tampoco: es global y el modo estudio la necesita.
        self._class_controls = [
            sep_mode, self.btn_open, lbl_q, self.combo_quality, self.btn_mark,
            self.lbl_slide, sep_slide, self.btn_mic, self.meter, sep_timer, self.btn_record,
        ]
        return bar

    def _build_mode_selector(self) -> QWidget:
        """Pestañas 🎙 Clase / 📚 Biblioteca (QButtonGroup exclusivo)."""
        box = QFrame()
        box.setObjectName("modeBox")
        box.setStyleSheet(
            f"QFrame#modeBox{{background:{theme.SURFACE_2}; border:1px solid {theme.BORDER}; border-radius:9px;}}"
        )
        lay = QHBoxLayout(box)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.mode_group = QButtonGroup(self)
        self.mode_group.setExclusive(True)
        self.btn_mode_class = QPushButton("🎙 Clase")
        self.btn_mode_class.setToolTip("Grabar una clase  (Ctrl+1)")
        self.btn_mode_lib = QPushButton("📚 Biblioteca")
        self.btn_mode_lib.setToolTip("Clases grabadas y modo estudio  (Ctrl+2)")
        for i, b in enumerate((self.btn_mode_class, self.btn_mode_lib)):
            b.setCheckable(True)
            b.setFixedHeight(30)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(MODE_BTN_QSS)
            self.mode_group.addButton(b, i)
            lay.addWidget(b)
        self.btn_mode_class.setChecked(True)
        self.btn_mode_class.clicked.connect(lambda: self._set_mode(MODE_CLASS))
        self.btn_mode_lib.clicked.connect(lambda: self._set_mode(MODE_LIBRARY))
        return box

    # ── Modos: Clase / Biblioteca / Estudiar ─────────────────────────────

    def _set_mode(self, mode: int):
        """Cambia la página central. No toca la grabación: si hay clase en curso sigue."""
        if mode == MODE_LIBRARY:
            self.library.refresh()
        self.stack.setCurrentIndex(mode)
        (self.btn_mode_class if mode == MODE_CLASS else self.btn_mode_lib).setChecked(True)
        self._update_toolbar()
        if mode == MODE_LIBRARY and not self.is_recording:
            self.statusBar().showMessage(
                "  📚 Biblioteca · doble clic en una clase para estudiarla · Ctrl+1 vuelve a la clase")
        elif mode == MODE_STUDY and not self.is_recording:
            self.statusBar().showMessage(
                "  🎓 Modo estudio · ‹ Biblioteca para volver · Ctrl+1 vuelve a la clase")

    def _update_toolbar(self):
        """Fuera del modo Clase se ocultan los controles de grabación; el punto y el
        timer se quedan como recordatorio si hay una clase grabándose."""
        in_class = self.stack.currentIndex() == MODE_CLASS
        for w in self._class_controls:
            w.setVisible(in_class)
        self.dot.setVisible(in_class or self.is_recording)
        self.lbl_timer.setVisible(in_class or self.is_recording)

    def _focus_search(self):
        if self.stack.currentIndex() == MODE_LIBRARY and hasattr(self.library, "search_input"):
            self.library.search_input.setFocus()
        elif self.stack.currentIndex() == MODE_STUDY:
            self.study.tabs.setCurrentWidget(self.study.tab_notes)
            self.study.tab_notes.search.setFocus()
        else:
            self.notes_panel.search_input.setFocus()

    @Slot(str)
    def _open_library_session(self, path: str):
        try:
            session = session_store.load_session(path)
        except Exception as e:
            QMessageBox.warning(self, "No se pudo abrir", f"No se pudo abrir la clase:\n{e}")
            return
        self.study.load(session)
        self._set_mode(MODE_STUDY)

    def _study_current_class(self):
        """Botón 🎓 Estudiar del panel de notas: guarda la clase y la abre en modo estudio."""
        if self.is_recording:
            return
        if not self.notes_panel.entries:
            QMessageBox.information(self, "Sin contenido", "No hay transcripción que estudiar.")
            return
        session = self._save_library_session()
        if session is None:
            return                              # el error ya está en la barra de estado
        self.study.load(session)
        self._set_mode(MODE_STUDY)

    @Slot(str)
    def _on_session_saved(self, txt_path: str):
        """«Guardar sesión» del panel de notas: la ficha apunta al nuevo .txt (y lleva el resumen).
        Autoguardado y guardado final pasan a escribir ese mismo fichero."""
        self._save_path = txt_path
        self.notes_panel.set_save_path(txt_path)
        self._save_library_session(txt_path=txt_path)

    # ── Micrófono ─────────────────────────────────────────────────────────

    @staticmethod
    def _input_devices() -> list[tuple[int, str]]:
        """(índice, nombre) de los micrófonos. Solo WASAPI para no repetir cada micro 4 veces."""
        import sounddevice as sd
        try:
            apis = sd.query_hostapis()
            wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
            devs = [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                    if d["max_input_channels"] > 0 and (wasapi is None or d["hostapi"] == wasapi)]
            return devs or [(i, d["name"]) for i, d in enumerate(sd.query_devices())
                            if d["max_input_channels"] > 0]
        except Exception:
            return []

    def _mic_index(self) -> int | None:
        """Índice del micro guardado en config (por nombre), o None = el del sistema."""
        wanted = _load_cfg().get("mic_device", "")
        if not wanted:
            return None
        for idx, name in self._input_devices():
            if name == wanted:
                return idx
        return None

    def _show_mic_menu(self):
        menu = QMenu(self)
        current = _load_cfg().get("mic_device", "")

        def pick(name: str):
            _save_cfg_key("mic_device", name)
            self.btn_mic.setToolTip(f"Micrófono: {name or 'el del sistema'}")
            self.statusBar().showMessage(
                f"  🎤 {name or 'Micrófono del sistema'}"
                + ("  ·  se aplica al iniciar la próxima clase" if self.is_recording else ""), 5000)

        act = QAction("Micrófono del sistema (por defecto)", menu, checkable=True)
        act.setChecked(current == "")
        act.triggered.connect(lambda: pick(""))
        menu.addAction(act)
        menu.addSeparator()
        for _idx, name in self._input_devices():
            a = QAction(name, menu, checkable=True)
            a.setChecked(name == current)
            a.triggered.connect(lambda _=False, n=name: pick(n))
            menu.addAction(a)
        menu.exec(self.btn_mic.mapToGlobal(self.btn_mic.rect().bottomLeft()))

    # ── IA ────────────────────────────────────────────────────────────────

    def _open_settings(self):
        SettingsDialog(self.enhancer, parent=self).exec()
        self._refresh_ai_button()
        self.study.refresh_ai_state()

    def _welcome(self):
        """Primera vez que se abre la app: explica lo básico y ofrece configurar la IA."""
        app_config.save_key("first_run_done", True)
        if self.enhancer.is_active():              # ya viene configurada (p. ej. por variable de entorno)
            return
        gpu = "Se ha detectado una GPU NVIDIA: Whisper la usará." if cuda_available() \
            else "No hay GPU NVIDIA: Whisper irá en CPU (el modo «Tiempo real» va sobrado)."
        box = QMessageBox(self)
        box.setWindowTitle("Bienvenido a ClassHelper")
        box.setText(
            "<b>ClassHelper ya funciona tal cual.</b><br><br>"
            "La transcripción es 100 % local con Whisper. La primera vez descarga el modelo "
            "(«small», ≈ 500 MB) y después funciona sin internet.<br><br>"
            f"{gpu}<br><br>"
            "Opcional: activa una IA (Gemini gratis, u Ollama en local) para puntuar los apuntes "
            "y usar el modo estudio: resumen, tarjetas, test y tutor. Puedes hacerlo cuando "
            "quieras desde el botón <b>✦ IA</b>."
        )
        btn_ai = box.addButton("Configurar IA ahora", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Empezar sin IA", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is btn_ai:
            self._open_settings()

    def _refresh_ai_button(self):
        if self.enhancer.is_active():
            name = {"ollama": "Ollama", "gemini": "Gemini", "claude": "Claude",
                    "openai": "GPT"}.get(self.enhancer.provider, "IA")
            self.btn_ai.setText(f"✦ {name}")
            self.btn_ai.setStyleSheet(theme.BTN_AI_ON)
            self.btn_ai.setToolTip(f"IA activa: {name}. Click para cambiar.")
        else:
            self.btn_ai.setText("✦ IA")
            self.btn_ai.setStyleSheet(theme.BTN_AI_OFF)
            self.btn_ai.setToolTip("Sin IA — solo Whisper. Click para activar.")

    # ── Marcar momento ────────────────────────────────────────────────────

    def _mark_moment(self):
        if not self.is_recording:
            return
        slide = self.pdf_panel.current_page + 1
        self.notes_panel.add_marker(slide)
        self.statusBar().showMessage(
            f"  📌 Marca añadida · diapo {slide} · {datetime.datetime.now():%H:%M:%S}", 4000
        )

    # ── PDF ───────────────────────────────────────────────────────────────

    def _open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Abrir PDF de clase", "", "PDF (*.pdf)")
        if not path:
            return
        self._pdf_path = path
        self.pdf_panel.load_pdf(path)
        self.btn_record.setEnabled(True)
        self.setWindowTitle(f"ClassHelper — {os.path.basename(path)}")
        if self.stack.currentIndex() != MODE_CLASS:
            self._set_mode(MODE_CLASS)

    @Slot(int)
    def _on_pdf_loaded(self, total: int):
        if self._pdf_path:
            self.thumb_strip.load(self._pdf_path, total)
        self.statusBar().showMessage(
            f"  {os.path.basename(self._pdf_path)} · {total} diapositivas · "
            "ve a la diapo inicial y pulsa Iniciar clase"
        )
        self.lbl_slide.setText("Diapo 1")

    @Slot(int)
    def _on_slide_changed(self, slide_num: int):
        self.lbl_slide.setText(f"Diapo {slide_num}")
        self.thumb_strip.set_page(slide_num - 1)
        if self.audio_worker:
            self.audio_worker.set_slide(slide_num)

    @Slot(int)
    def _on_thumb_selected(self, index: int):
        self.pdf_panel.go_to_page(index)
        self._on_slide_changed(index + 1)

    # ── Grabación ─────────────────────────────────────────────────────────

    def _toggle_recording(self):
        if not self.btn_record.isEnabled():
            return
        if self.is_recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self):
        if self.notes_panel.entries:
            r = QMessageBox.question(
                self, "Nueva clase",
                "¿Empezar una clase nueva?\n\nSe limpia de pantalla la transcripción anterior "
                "(ya está guardada en la biblioteca).")
            if r != QMessageBox.StandardButton.Yes:
                return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
        default = os.path.join(os.path.expanduser("~\\Documents"), f"clase_{ts}.txt")
        save_path, _ = QFileDialog.getSaveFileName(
            self, "¿Dónde guardar la sesión? (se auto-guarda cada 10 min)",
            default, "Texto (*.txt);;Todos (*)"
        )
        if not save_path:
            return
        if self.notes_panel.entries:
            self._save_library_session()          # por si no llegó el guardado final
            self.notes_panel.clear_notes()
        self._save_path = save_path
        self.notes_panel.set_save_path(save_path)
        self._new_class_id()
        self.live_notes.clear_notes()
        self.notes_panel.set_recording(True)

        worker = self._ensure_worker()
        if worker.isRunning() and not worker.model:
            worker.wait(60000)                 # precarga aún en curso: esperar a que acabe
        worker.set_slide(self.pdf_panel.current_page + 1)
        worker.device_index = self._mic_index()
        worker.start_recording()

        self.is_recording = True
        self.btn_record.setText("■  Detener clase")
        self.btn_record.setStyleSheet(theme.BTN_RECORD_LIVE)
        self.combo_quality.setEnabled(False)
        self.btn_mark.setEnabled(True)
        self.meter.set_active(True)
        self.dot.set_state("busy" if self.audio_worker.model is None else "live")

        self.subtitles.start()
        self.side_tabs.setCurrentIndex(0)             # el bloc de notas, delante
        self._elapsed = 0
        self._session_timer.start()
        self._autosave_timer.start()
        self.lbl_timer.setStyleSheet(f"background:transparent; color:{theme.RECORD}; font-size:14px; font-weight:700; "
            f"font-family:Consolas,'Cascadia Mono',monospace; min-width:80px;"
        )
        if self.stack.currentIndex() != MODE_CLASS:      # Ctrl+R desde la biblioteca
            self._set_mode(MODE_CLASS)

    def _ensure_worker(self) -> AudioWorker:
        """Devuelve el AudioWorker para la calidad elegida, creándolo si hace falta."""
        model_name = QUALITY_LEVELS[self.combo_quality.currentIndex()][1]
        if self.audio_worker is not None and self.audio_worker.model_name == model_name:
            return self.audio_worker
        old = self.audio_worker
        if old and old.isRunning():
            old.stop_recording()
            old.wait(5000)
        w = AudioWorker(model_name=model_name, language="es", enhancer=self.enhancer)
        w.transcription_ready.connect(self._on_transcription)
        w.enhanced_ready.connect(self._on_enhanced)
        w.level_changed.connect(self.meter.set_level)
        w.status_update.connect(self._on_status)
        w.error_occurred.connect(self._on_error)
        w.audio_saved.connect(self.notes_panel.set_audio_path)
        w.model_loaded.connect(self._on_model_loaded)
        w.finished.connect(self._on_worker_finished)
        self.audio_worker = w
        return w

    def _on_quality_changed(self, idx: int):
        _save_cfg_key("whisper_model", QUALITY_LEVELS[idx][1])
        if idx > 0 and not cuda_available():
            self.statusBar().showMessage(
                "  ⚠ Sin GPU este modelo va más lento que la clase: la transcripción "
                "irá con retraso creciente. Para clase en directo usa «Tiempo real».", 12000)
        if not self.is_recording:
            self._ensure_worker().preload()

    def _on_model_loaded(self, name: str):
        if self.is_recording:
            self.dot.set_state("live")

    def _stop_recording(self):
        """Parada asíncrona: deja que Whisper acabe el último chunk sin congelar la UI."""
        if not self.is_recording or self._stopping:
            return
        self._stopping = True
        self.btn_record.setEnabled(False)
        self.btn_record.setText("…  Terminando")
        self.statusBar().showMessage("  Terminando de transcribir lo último…")
        self._session_timer.stop()
        self._autosave_timer.stop()
        self.meter.set_active(False)
        self.dot.set_state("busy")
        if self.audio_worker and self.audio_worker.isRunning():
            self.audio_worker.stop_recording()      # → finished → _on_worker_finished
        else:
            self._on_worker_finished()

    def _on_worker_finished(self):
        if not self._stopping:
            return                                   # fin de una precarga, no de una clase
        self._stopping = False
        self.is_recording = False
        self.btn_record.setEnabled(True)
        self.btn_record.setText("●  Iniciar clase")
        self.btn_record.setStyleSheet(theme.BTN_RECORD_IDLE)
        self.combo_quality.setEnabled(True)
        self.btn_mark.setEnabled(False)
        self.dot.set_state("idle")
        self.lbl_timer.setStyleSheet(f"background:transparent; color:{theme.DIM}; font-size:14px; font-weight:600; "
            f"font-family:Consolas,'Cascadia Mono',monospace; min-width:80px;"
        )
        self.notes_panel.set_recording(False)
        self.subtitles.stop()
        self._update_toolbar()
        self._autosave(final=True)
        if self._class_id and app_config.load().get("prepare_on_finish", True):
            self._process_session(self._class_id, avisar=True)

    # ── Auto-guardado ────────────────────────────────────────────────────

    def _autosave(self, final: bool = False):
        if not self._save_path:
            return
        audio_src = self.audio_worker.audio_path if self.audio_worker else ""
        if self.notes_panel.autosave(self._save_path, audio_src):
            tag = "Guardado final" if final else "Auto-guardado"
            msg = f"  ✓ {tag} {datetime.datetime.now():%H:%M} · {os.path.basename(self._save_path)}"
            # La ficha de la biblioteca se escribe en cada autoguardado: si la app
            # se cae a mitad de clase, lo transcrito hasta entonces ya está ahí.
            if self._save_library_session() is not None and final:
                msg = (f"  ✓ Clase guardada en la biblioteca y en {os.path.basename(self._save_path)}"
                       " · Ctrl+2 para estudiarla")
            self.statusBar().showMessage(msg)

    def _new_class_id(self):
        """Id de ficha fijo para toda la clase: se crea al iniciar y cada guardado
        (autoguardado, final, Guardar sesión, Estudiar) sobrescribe el mismo JSON."""
        self._class_date_iso = datetime.datetime.now().isoformat(timespec="seconds")
        self._class_id = session_store.new_session("", self._class_date_iso).id

    def _save_library_session(self, txt_path: str | None = None, summary: str = ""):
        """Guarda la clase actual en la biblioteca. Devuelve la Session o None
        (sin fragmentos o error; el error se muestra en la barra de estado)."""
        if not self.notes_panel.entries:
            return None
        if not self._class_id:
            self._new_class_id()
        txt = self._save_path if txt_path is None else txt_path
        wav = os.path.splitext(txt)[0] + ".wav" if txt else ""
        if wav and not os.path.exists(wav):
            wav = ""
        try:
            s = self.notes_panel.to_session(self._class_id, self._class_date_iso, self._elapsed,
                                            self._pdf_path, txt, wav, summary)
            # Si la ficha ya existe (autoguardado anterior o modo estudio) se
            # conservan tarjetas, test, chat, asignatura y resumen.
            try:
                old = session_store.load_session(session_store.session_path(s.id))
            except Exception:
                old = None
            if old is not None:
                s.study = old.study
                s.subject = old.subject
                s.summary = s.summary or old.summary
            notas = self.live_notes.notes()
            if notas:
                s.study["notes"] = notas
            session_store.save_session(s)
            # Si esta clase está abierta en el modo estudio, que vea los fragmentos nuevos
            self.study.sync_class_data(s)
            return s
        except Exception as e:
            self.statusBar().showMessage(f"  ⚠ No se pudo guardar la clase en la biblioteca: {e}", 10000)
            return None

    # ── La biblioteca se prepara sola ────────────────────────────────────

    def _process_session(self, session_id: str, avisar: bool = False):
        """Encola una clase para prepararla (turbo, resumen, tarjetas, test)."""
        if not session_id:
            return
        try:
            s = session_store.load_session(session_store.session_path(session_id))
        except Exception as e:
            self.statusBar().showMessage(f"  ⚠ No se pudo abrir la clase: {e}", 8000)
            return
        pasos = pending_steps(s)
        if not pasos:
            self.statusBar().showMessage("  ✓ Esta clase ya está preparada", 6000)
            return
        self.auto.enqueue(session_id)
        if avisar:
            mins = estimate_minutes(s)
            self.statusBar().showMessage(
                f"  ⚙ Preparando los apuntes desde el audio (turbo, resumen y material) · "
                f"unos {mins} min · puedes seguir usando la app", 12000)

    def _prepare_all(self):
        n = self.auto.enqueue_all_pending()
        self.statusBar().showMessage(
            f"  ⚙ {n} clase{'s' if n != 1 else ''} en cola · el PC no se dormirá hasta terminar"
            if n else "  ✓ No hay nada pendiente", 10000)

    def _pause_all(self):
        self.auto.cancel(None)
        self.library.refresh()
        self.statusBar().showMessage("  ⏸ Preparación parada · CPU libre", 8000)

    def _pause_processing(self, session_id: str):
        """El usuario para la preparación desde la biblioteca."""
        self.auto.cancel(session_id)
        self.library.set_processing(session_id, "", 0)
        self.library.refresh()
        self.statusBar().showMessage(
            "  ⏸ Preparación parada · la CPU queda libre · puedes retomarla con «⚙ Preparar»", 10000)

    def _on_auto_progress(self, session_id: str, msg: str, pct: int):
        if msg:
            self.statusBar().showMessage(f"  ⚙ {msg}  ({pct} %)")
        self.library.set_processing(session_id, msg, pct)

    def _on_auto_finished(self, session_id: str, pasos: list):
        self.library.set_processing(session_id, "", 0)
        self.library.refresh()
        try:
            s = session_store.load_session(session_store.session_path(session_id))
            self.study.sync_class_data(s)
        except Exception:
            pass
        if pasos:
            nombres = {"turbo": "apuntes del audio", "resumen": "resumen",
                       "tarjetas": "tarjetas", "test": "test"}
            hecho = ", ".join(nombres.get(p, p) for p in pasos)
            self.statusBar().showMessage(f"  ✓ Clase lista: {hecho} · Ctrl+2 para estudiarla", 15000)

    def _on_auto_failed(self, session_id: str, msg: str):
        self.statusBar().showMessage(f"  ⚠ {msg}", 12000)
        self.library.set_processing(session_id, "", 0)

    # ── Timers y slots ────────────────────────────────────────────────────

    def _tick(self):
        self._elapsed += 1
        h, r = divmod(self._elapsed, 3600)
        m, s = divmod(r, 60)
        self.lbl_timer.setText(f"{h:02d}:{m:02d}:{s:02d}")

    def _on_enhanced(self, chunk_id: int, text: str):
        self.notes_panel.set_enhanced(chunk_id, text)
        self.subtitles.update_enhanced(chunk_id, text)

    def _save_live_notes(self):
        """Guarda el bloc del alumno en la ficha de la clase."""
        notas = self.live_notes.notes()
        if not notas or not self._class_id:
            return
        try:
            path = session_store.session_path(self._class_id)
            if not os.path.exists(path):
                return                      # aún no hay ficha: se guardará en el autoguardado
            s = session_store.load_session(path)
            if s.study.get("notes", "") != notas:
                s.study["notes"] = notas
                session_store.save_session(s)
                self.study.sync_class_data(s)
            self.live_notes.mark_saved()
        except Exception as e:
            self.statusBar().showMessage(f"  ⚠ No se pudieron guardar tus notas: {e}", 6000)

    def _insert_slide_note(self):
        slide = self.pdf_panel.current_page + 1
        self.live_notes.insert_slide_mark(slide, datetime.datetime.now().strftime("%H:%M"))

    @Slot(int, int, str)
    def _on_transcription(self, chunk_id: int, slide: int, raw: str):
        self.subtitles.add(chunk_id, raw)
        self.notes_panel.add_transcription(chunk_id, slide, raw, self.enhancer.is_active())

    @Slot(str)
    def _on_status(self, msg: str):
        self.statusBar().showMessage(f"  {msg}")
        if self.is_recording:
            self.dot.set_state("busy" if msg.startswith(("Transcribiendo", "Cargando")) else "live")

    @Slot(str)
    def _on_error(self, error: str):
        QMessageBox.critical(self, "Error", error)
        if self.is_recording:
            self._stop_recording()

    def closeEvent(self, event):
        if self.is_recording and self.audio_worker:
            # Cierre síncrono: esperar al último chunk y guardar antes de salir
            self.audio_worker.stop_recording()
            self.audio_worker.wait(60000)
            QApplication.processEvents()
            self._stopping = True
            self._on_worker_finished()
        elif self.audio_worker and self.audio_worker.isRunning():
            self.audio_worker.wait(3000)
        loader = getattr(self.thumb_strip, "_loader", None)
        if loader and loader.isRunning():
            loader.stop()
            loader.wait(2000)
        event.accept()
