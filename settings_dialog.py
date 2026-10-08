"""
settings_dialog.py - Configuración de IA (Ollama / Gemini / Claude / OpenAI).
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QMessageBox, QStackedWidget, QWidget,
)
from PySide6.QtCore import Qt, QUrl, QThread, Signal
from PySide6.QtGui import QDesktopServices

import theme
from widgets import hsep
from ai_enhancer import AIEnhancer


PROVIDERS = [
    ("none",   "Sin IA  ·  solo Whisper"),
    ("gemini", "Google Gemini  ·  gratis con key  ✓  recomendado"),
    ("ollama", "Ollama  ·  local, gratis"),
    ("claude", "Claude (Anthropic)  ·  de pago"),
    ("openai", "OpenAI GPT-4o mini  ·  de pago"),
]


class _TestWorker(QThread):
    done = Signal(bool, str)

    def __init__(self, provider, key, model, parent=None):
        super().__init__(parent)
        self.args = (provider, key, model)

    def run(self):
        t = AIEnhancer.__new__(AIEnhancer)      # sin tocar config.json
        t.provider, t.api_key, t.ollama_model = self.args
        t._client, t._context = None, ""
        ok, msg = t.test_connection()
        self.done.emit(ok, msg)


class SettingsDialog(QDialog):

    def __init__(self, enhancer: AIEnhancer, parent=None):
        super().__init__(parent)
        self.enhancer = enhancer
        self.setWindowTitle("Mejora con IA")
        self.setFixedSize(580, 480)
        self._setup_ui()
        self._load_current()

    # ── UI ───────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        root.setSpacing(14)

        title = QLabel("✦  Mejora de transcripción con IA")
        title.setStyleSheet(f"background:transparent; font-size:16px; font-weight:700; color:{theme.TEXT};")
        root.addWidget(title)
        desc = QLabel(
            "La IA recibe cada fragmento con el contexto anterior: añade puntuación, "
            "corrige errores de Whisper y respeta los términos técnicos. "
            "Whisper sigue funcionando aunque la IA falle."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        root.addWidget(desc)
        root.addWidget(hsep())

        row = QHBoxLayout()
        lbl = QLabel("Proveedor")
        lbl.setFixedWidth(80)
        lbl.setStyleSheet(f"background:transparent; color:{theme.MUTED};")
        self.combo = QComboBox()
        self.combo.setFixedHeight(36)
        for _, label in PROVIDERS:
            self.combo.addItem(label)
        self.combo.currentIndexChanged.connect(self._on_provider_change)
        row.addWidget(lbl)
        row.addWidget(self.combo)
        root.addLayout(row)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._page_none())
        self.stack.addWidget(self._page_gemini())
        self.stack.addWidget(self._page_ollama())
        self.stack.addWidget(self._page_key("Claude", "console.anthropic.com"))
        self.stack.addWidget(self._page_key("OpenAI", "platform.openai.com"))
        root.addWidget(self.stack, 1)

        root.addWidget(hsep())
        btns = QHBoxLayout()
        self.btn_test = QPushButton("Probar conexión")
        self.btn_test.setFixedHeight(34)
        self.btn_test.clicked.connect(self._test)
        btn_cancel = QPushButton("Cancelar")
        btn_cancel.setProperty("flat", True)
        btn_cancel.setFixedHeight(34)
        btn_cancel.clicked.connect(self.reject)
        btn_save = QPushButton("Guardar")
        btn_save.setProperty("accent", True)
        btn_save.setFixedHeight(34)
        btn_save.clicked.connect(self._save)
        btns.addWidget(self.btn_test)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_save)
        root.addLayout(btns)

    @staticmethod
    def _muted(text: str) -> QLabel:
        l = QLabel(text)
        l.setWordWrap(True)
        l.setStyleSheet(f"background:transparent; color:{theme.MUTED}; font-size:11px;")
        return l

    @staticmethod
    def _key_row(placeholder: str) -> tuple[QHBoxLayout, QLineEdit]:
        row = QHBoxLayout()
        inp = QLineEdit()
        inp.setPlaceholderText(placeholder)
        inp.setEchoMode(QLineEdit.EchoMode.Password)
        inp.setFixedHeight(34)
        btn = QPushButton("Ver")
        btn.setFixedSize(52, 34)
        btn.setCheckable(True)
        btn.toggled.connect(lambda v, i=inp: i.setEchoMode(
            QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password))
        row.addWidget(inp)
        row.addWidget(btn)
        return row, inp

    @staticmethod
    def _link_btn(text: str, url: str) -> QPushButton:
        b = QPushButton(text)
        b.setFixedHeight(32)
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(url)))
        return b

    def _page_none(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.addWidget(self._muted(
            "Whisper transcribirá sin post-procesado. Funciona bien, pero sin puntuación "
            "ni corrección de términos. Puedes activar la IA en cualquier momento, "
            "incluso con la clase en marcha."
        ))
        l.addStretch()
        return w

    def _page_gemini(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        banner = QLabel("✓  Gemini Flash — gratis con cualquier cuenta de Google, sin tarjeta")
        banner.setStyleSheet(
            f"background:#0f2e22; color:{theme.SUCCESS}; font-weight:700; font-size:11px;"
            f"padding:8px 12px; border-radius:8px; border:1px solid #1d4d38;"
        )
        l.addWidget(banner)
        l.addWidget(self._muted(
            "Límite gratuito: 15 peticiones/min y 1.000/día. ClassHelper usa ~3/min. "
            "Si se agota, la app sigue con Whisper y no pierde nada."
        ))
        l.addWidget(self._link_btn("Conseguir key en Google AI Studio  →",
                                   "https://aistudio.google.com/app/apikey"))
        l.addWidget(self._muted("Inicia sesión con Google → «Create API key» → cópiala aquí:"))
        row, self.input_gemini_key = self._key_row("AIzaSy…")
        l.addLayout(row)
        l.addStretch()
        return w

    def _page_ollama(self) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        warn = QLabel(
            "⚠  Ollama comparte la CPU con Whisper y los modelos pequeños (≤3B) inventan "
            "términos técnicos. Solo recomendable con un modelo ≥8B."
        )
        warn.setWordWrap(True)
        warn.setStyleSheet(
            f"background:{theme.WARN_BG}; color:#ffd98a; font-size:11px;"
            f"padding:8px 12px; border-radius:8px; border:1px solid #6b4d14;"
        )
        l.addWidget(warn)
        r = QHBoxLayout()
        r.addWidget(self._link_btn("Instalar Ollama", "https://ollama.com"))
        cmd = QLabel("ollama pull llama3.1:8b")
        cmd.setStyleSheet(
            f"background:{theme.SURFACE_2}; color:{theme.SUCCESS}; font-family:Consolas,monospace;"
            f"padding:6px 10px; border-radius:6px;"
        )
        r.addWidget(cmd)
        r.addStretch()
        l.addLayout(r)
        r2 = QHBoxLayout()
        lbl = QLabel("Modelo")
        lbl.setFixedWidth(60)
        lbl.setStyleSheet(f"background:transparent; color:{theme.MUTED};")
        self.input_ollama_model = QLineEdit()
        self.input_ollama_model.setPlaceholderText("llama3.1:8b")
        self.input_ollama_model.setFixedHeight(34)
        r2.addWidget(lbl)
        r2.addWidget(self.input_ollama_model)
        l.addLayout(r2)
        l.addStretch()
        return w

    def _page_key(self, name: str, url: str) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setSpacing(10)
        l.addWidget(self._muted(f"Necesitas cuenta en {url} y saldo en la API (de pago)."))
        l.addWidget(self._link_btn(f"Ir a {url}", f"https://{url}"))
        row, inp = self._key_row("API key")
        if name == "Claude":
            self.input_claude_key = inp
        else:
            self.input_openai_key = inp
        l.addLayout(row)
        l.addStretch()
        return w

    # ── Lógica ───────────────────────────────────────────────────────────

    def _idx_of(self, provider: str) -> int:
        return next((i for i, (p, _) in enumerate(PROVIDERS) if p == provider), 0)

    def _load_current(self):
        self.combo.setCurrentIndex(self._idx_of(self.enhancer.provider))
        self.input_ollama_model.setText(self.enhancer.ollama_model or "llama3.1:8b")
        key = self.enhancer.api_key
        if self.enhancer.provider == "gemini":
            self.input_gemini_key.setText(key)
        elif self.enhancer.provider == "claude":
            self.input_claude_key.setText(key)
        elif self.enhancer.provider == "openai":
            self.input_openai_key.setText(key)
        self._on_provider_change(self.combo.currentIndex())

    def _on_provider_change(self, idx: int):
        self.stack.setCurrentIndex(idx)
        self.btn_test.setEnabled(idx > 0)

    def _current(self) -> tuple[str, str, str]:
        provider = PROVIDERS[self.combo.currentIndex()][0]
        key = {
            "gemini": lambda: self.input_gemini_key.text(),
            "claude": lambda: self.input_claude_key.text(),
            "openai": lambda: self.input_openai_key.text(),
        }.get(provider, lambda: "")().strip()
        model = self.input_ollama_model.text().strip() or "llama3.1:8b"
        return provider, key, model

    def _test(self):
        self.btn_test.setText("Probando…")
        self.btn_test.setEnabled(False)
        self._tw = _TestWorker(*self._current())
        self._tw.done.connect(self._test_done)
        self._tw.start()

    def _test_done(self, ok: bool, msg: str):
        self.btn_test.setText("Probar conexión")
        self.btn_test.setEnabled(True)
        if ok:
            QMessageBox.information(self, "Conexión OK",
                                    f"La IA responde correctamente.\n\nEjemplo:\n\n{msg}")
        else:
            QMessageBox.critical(self, "Error de conexión",
                                 f"No se pudo conectar:\n\n{msg}\n\n"
                                 "Si usas Ollama, comprueba que está corriendo (ollama serve).")

    def _save(self):
        self.enhancer.configure(*self._current())
        self.accept()
