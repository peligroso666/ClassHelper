"""
auto_process.py - La biblioteca se prepara sola cada clase.

Al terminar de grabar, ClassHelper encola la clase y hace por su cuenta, en este orden:

  1. Repaso con turbo  → vuelve a transcribir el .wav con large-v3-turbo (mucho mejor
     que el modelo en directo). A partir de aquí los apuntes vienen del AUDIO.
  2. Resumen           → con ese texto ya bueno, no con el del directo.
  3. Tarjetas y test   → material de estudio listo sin pulsar nada.

Reglas:
  - Una clase cada vez y nunca mientras se está grabando (turbo se come la CPU).
  - Se salta los pasos ya hechos, así que se puede reencolar sin repetir trabajo.
  - Si un paso falla (sin IA, sin cuota, sin audio) se sigue con el siguiente y se
    avisa; nada de dejar la clase a medias sin decir nada.
"""

import os
import datetime

from PySide6.QtCore import QObject, Signal, QTimer

import app_config


# ── Energía (Windows): que el PC no se duerma mientras prepara ──────────
# Medido: una preparación se congeló 4 h porque el portátil se suspendió a mitad.
_ES_CONTINUOUS, _ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001


def _keep_awake(on: bool):
    try:
        import ctypes
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def on_battery() -> bool:
    """True si el portátil va con batería (False si no se puede saber)."""
    try:
        import ctypes

        class _SPS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                        ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                        ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
        st = _SPS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return st.ACLineStatus == 0
    except Exception:
        pass
    return False

import session_store
from session_store import Session
from refine import RefineWorker, REFINE_MODEL, SECONDS_PER_AUDIO_SECOND
from study_ai import StudyAI, AITask


# Pasos y su peso aproximado en la barra de progreso
STEPS = ("turbo", "resumen", "tarjetas", "test")
STEP_LABEL = {
    "turbo":    "Transcribiendo con turbo",
    "resumen":  "Escribiendo el resumen",
    "tarjetas": "Creando tarjetas",
    "test":     "Preparando el test",
}
N_FLASHCARDS = 15
N_QUIZ = 10


def pending_steps(s: Session) -> list[str]:
    """Qué le falta a esta clase (en orden)."""
    st = s.study if isinstance(s.study, dict) else {}
    out = []
    if s.wav_path and os.path.exists(s.wav_path) and not isinstance(st.get("refined"), dict):
        out.append("turbo")
    if not (s.summary or "").strip():
        out.append("resumen")
    if not st.get("flashcards"):
        out.append("tarjetas")
    if not (st.get("quiz") or {}).get("questions"):
        out.append("test")
    return out


def has_text(s: Session) -> bool:
    return any(session_store.best_text(e).strip() for e in (s.entries or [])
               if isinstance(e, dict) and e.get("kind", "text") != "marker")


def estimate_minutes(s: Session) -> int:
    """Minutos aproximados de todo el proceso (lo que manda es turbo)."""
    steps = pending_steps(s)
    secs = 0.0
    if "turbo" in steps:
        try:
            secs += max(0.0, (os.path.getsize(s.wav_path) - 44) / 32000) * SECONDS_PER_AUDIO_SECOND
        except OSError:
            secs += (s.duration_s or 0) * SECONDS_PER_AUDIO_SECOND
    secs += 25 * len([x for x in steps if x != "turbo"])      # cada llamada a la IA
    return max(1, int(secs / 60 + 0.999))


class AutoProcessor(QObject):
    """Cola de clases a preparar. Vive en la ventana principal."""

    progress = Signal(str, str, int)     # session_id, mensaje, % (0-100)
    finished = Signal(str, list)         # session_id, pasos completados
    failed   = Signal(str, str)          # session_id, mensaje
    queue_changed = Signal(int)          # clases en cola (incluida la actual)
    waiting = Signal(str)                # por qué no arranca (grabando, sin cargador)

    def __init__(self, enhancer, is_recording=None, parent=None):
        super().__init__(parent)
        self.enhancer = enhancer
        self.ai = StudyAI(enhancer)
        self.is_recording = is_recording or (lambda: False)
        self._queue: list[str] = []
        self._current: str | None = None
        self._session: Session | None = None
        self._steps: list[str] = []
        self._done: list[str] = []
        self._retried: dict[str, bool] = {}
        self._worker = None
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.setInterval(30_000)              # si se está grabando, reintenta luego
        self._retry.timeout.connect(self._next)

    # ── API ──────────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        return self._current is not None

    def current_id(self) -> str | None:
        return self._current

    def pending_count(self) -> int:
        return len(self._queue) + (1 if self._current else 0)

    def enqueue(self, session_id: str):
        if session_id and session_id not in self._queue and session_id != self._current:
            self._queue.append(session_id)
            self.queue_changed.emit(self.pending_count())
        self._next()

    def enqueue_all_pending(self) -> int:
        """Encola todas las clases de la biblioteca a las que les falta algo."""
        n = 0
        for s in sorted(session_store.list_sessions(), key=lambda s: s.date):
            if pending_steps(s) and s.id != self._current and s.id not in self._queue:
                self._queue.append(s.id)
                n += 1
        if n:
            self.queue_changed.emit(self.pending_count())
        self._next()
        return n

    def cancel(self, session_id: str | None = None):
        """Cancela una clase concreta o todo lo pendiente."""
        if session_id is None:
            self._queue.clear()
            session_id = self._current
            if session_id is None:
                self.queue_changed.emit(0)
                return
        elif session_id in self._queue:
            self._queue.remove(session_id)
            self.queue_changed.emit(self.pending_count())
            return
        if session_id and session_id == self._current:
            w = self._worker
            if isinstance(w, RefineWorker):
                w.cancel()
            self._finish_current(cancelled=True)

    # ── Motor ────────────────────────────────────────────────────────────

    def _next(self):
        if self._current is not None or not self._queue:
            return
        if self.is_recording():
            self.waiting.emit("hay una clase grabándose")
            self._retry.start()                       # hay clase en marcha: más tarde
            return
        if app_config.load().get("prepare_ac_only", False) and on_battery():
            self.waiting.emit("esperando al cargador")
            self._retry.start()
            return
        sid = self._queue.pop(0)
        _keep_awake(True)
        try:
            s = session_store.load_session(session_store.session_path(sid))
        except Exception as e:
            self.failed.emit(sid, f"no se pudo abrir la ficha: {e}")
            QTimer.singleShot(0, self._next)
            return
        self._current, self._session = sid, s
        self._steps = pending_steps(s)
        self._done = []
        self._retried = {}
        if not self._steps:
            self._finish_current()
            return
        self._run_step()

    def _pct(self, extra: float = 0.0) -> int:
        total = max(1, len(self._steps))
        return int(min(99, (len(self._done) + extra) * 100 / total))

    def _run_step(self):
        if self._current is None:
            return
        if not self._steps:
            self._finish_current()
            return
        step = self._steps[0]
        s = self._session
        self.progress.emit(self._current, STEP_LABEL[step] + "…", self._pct())

        if step == "turbo":
            w = RefineWorker(s.id, s.wav_path, s.entries, s.date, parent=self)
            w.progress.connect(self._on_refine_progress)
            w.done.connect(self._on_refine_done)
            w.failed.connect(lambda sid, msg: self._step_failed(msg))
            w.cancelled.connect(lambda sid: self._finish_current(cancelled=True))
            self._worker = w
            w.start()
            return

        if not self.enhancer.is_active():
            self._step_failed("la IA no está activa")
            return
        if not has_text(s):
            self._step_failed("la clase no tiene transcripción")
            return

        texto = self._full_text()
        if step == "resumen":
            self._start_ai(self.enhancer.generate_summary, (texto,), self._on_summary)
        elif step == "tarjetas":
            self._start_ai(self.ai.generate_flashcards, (texto, N_FLASHCARDS), self._on_cards)
        elif step == "test":
            self._start_ai(self.ai.generate_quiz, (texto, N_QUIZ), self._on_quiz)

    def _full_text(self) -> str:
        """Transcripción (ya de turbo si se hizo) más las notas del alumno."""
        s = self._session
        texto = session_store.full_text(s)
        notas = (s.study.get("notes", "") or "").strip()
        if notas:
            texto += ("\n\nNOTAS DEL ALUMNO (lo que él mismo apuntó en clase; tenlas muy en "
                      "cuenta: marcan lo que le importa y sus dudas):\n" + notas)
        return texto

    def _start_ai(self, fn, args, on_done):
        task = AITask(fn, *args, parent=self)
        task.done.connect(on_done)
        task.failed.connect(self._step_failed)
        self._worker = task
        task.start()

    # ── Resultados de cada paso ──────────────────────────────────────────

    def _on_refine_progress(self, sid: str, pct: int, msg: str):
        if sid == self._current:
            self.progress.emit(sid, f"{STEP_LABEL['turbo']}… {pct} %", self._pct(pct / 100))

    def _on_refine_done(self, sid: str, entries: list):
        s = self._session
        if s is None or sid != self._current or not entries:
            self._step_failed("turbo no sacó texto del audio")
            return
        if not isinstance(s.study.get("live_entries"), list):
            s.study["live_entries"] = [dict(e) for e in s.entries]
        s.study["refined"] = {"model": REFINE_MODEL,
                              "date": datetime.datetime.now().isoformat(timespec="seconds")}
        s.entries = [dict(e) for e in entries]
        # El resumen viejo salía del texto en directo: se rehace con el del audio
        s.summary = ""
        if "resumen" not in self._steps:
            self._steps.append("resumen")
        self._step_done()

    def _on_summary(self, result):
        ok, text = result if isinstance(result, tuple) else (True, result)
        text = str(text or "").strip()
        if not ok or not text:
            self._step_failed(text or "la IA no devolvió resumen")
            return
        self._session.summary = text
        self._step_done()

    def _on_cards(self, cards):
        cards = list(cards or [])
        if not cards:
            self._step_failed("la IA no devolvió tarjetas")
            return
        hoy = datetime.date.today().isoformat()
        base = len(self._session.study.get("flashcards", []))
        self._session.study["flashcards"] = [
            {"id": base + i + 1, "q": c.get("q", ""), "a": c.get("a", ""),
             "slide": int(c.get("slide") or 0), "box": 0, "due": hoy, "seen": 0, "correct": 0}
            for i, c in enumerate(cards)
        ]
        self._step_done()

    def _on_quiz(self, questions):
        questions = list(questions or [])
        if not questions:
            self._step_failed("la IA no devolvió preguntas")
            return
        quiz = self._session.study.setdefault("quiz", {"questions": [], "attempts": []})
        quiz["questions"] = [dict(q, id=i + 1) for i, q in enumerate(questions)]
        quiz.setdefault("attempts", [])
        self._step_done()

    # ── Avance de la cola ────────────────────────────────────────────────

    def _step_done(self):
        step = self._steps.pop(0) if self._steps else ""
        if step:
            self._done.append(step)
        self._save()
        self.progress.emit(self._current or "", "", self._pct())
        QTimer.singleShot(0, self._run_step)

    def _step_failed(self, msg):
        step = self._steps[0] if self._steps else ""
        # La IA a veces devuelve una respuesta rota de forma puntual (medido: las tarjetas
        # fallaron una vez y al repetir salieron bien). Un reintento antes de rendirse.
        if step and step != "turbo" and not self._retried.get(step):
            self._retried[step] = True
            self.progress.emit(self._current or "", f"{STEP_LABEL[step]}: reintentando…", self._pct())
            QTimer.singleShot(3000, self._run_step)
            return
        if self._steps:
            self._steps.pop(0)
        self.failed.emit(self._current or "", f"{STEP_LABEL.get(step, step)}: {msg}")
        self._save()
        QTimer.singleShot(0, self._run_step)

    def _save(self):
        s = self._session
        if s is None:
            return
        try:
            path = session_store.session_path(s.id)
            if os.path.exists(path):
                # La clase puede haber cambiado mientras tanto (notas, tarjetas repasadas)
                fresh = session_store.load_session(path)
                fresh.entries = s.entries
                fresh.summary = s.summary
                for k in ("refined", "live_entries", "flashcards", "quiz"):
                    if k in s.study:
                        fresh.study[k] = s.study[k]
                s = self._session = fresh
            session_store.save_session(s)
        except Exception as e:
            self.failed.emit(self._current or "", f"no se pudo guardar: {e}")

    def _finish_current(self, cancelled: bool = False):
        sid, done = self._current or "", list(self._done)
        self._current = self._session = self._worker = None
        self._steps, self._done = [], []
        if sid and not cancelled:
            self.finished.emit(sid, done)
        self.queue_changed.emit(self.pending_count())
        if not self._queue:
            _keep_awake(False)                    # cola vacía: el PC ya puede dormir
        QTimer.singleShot(0, self._next)
