"""
audio_thread.py - Motor de audio de ClassHelper.

Pipeline (todo en paralelo, la grabación nunca se detiene):

  micrófono (WASAPI, frecuencia nativa) ──callback──▶ 48 kHz → 16 kHz
                              │
                     buffer + .wav en disco + nivel (VU)
                              │
                     chunker inteligente: corta en SILENCIOS
                     (mín 20 s, máx 29 s) → nunca parte una palabra
                              │
                        cola de audio ──▶ hilo Whisper ──▶ transcription_ready(id, slide, raw)
                                                             │
                                                    hilo IA (Gemini/…) ──▶ enhanced_ready(id, texto)

Calidad:
  - Se graba por WASAPI a la frecuencia nativa del micro. Por MME a 16 kHz Windows
    aplica supresión de ruido que mete silencio digital y se come sílabas de un
    profesor lejano (medido: 12 % de ceros por MME frente a 0,2 % por WASAPI).
  - initial_prompt = frase neutra + cola de lo ya transcrito (continuidad), sin jerga fija.
  - Si Whisper entra en bucle, reintenta con temperatura más alta; además se
    colapsan frases repetidas ("lo que es lo que es lo que es…").
  - Filtro anti-alucinación por segmento (no_speech_prob / avg_logprob / frases típicas).
  - El texto crudo aparece al instante; la IA lo sustituye cuando termina.
"""

import os
import re
import time
import wave
import queue
import tempfile
import threading
import datetime

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QThread, Signal

from ai_enhancer import AIEnhancer


# ── Vocabulario de teleco para Whisper ───────────────────────────────────

# Frase neutra. Medido con una clase real: una lista fija de jerga
# ("Smith, Butterworth, Kuroda…") hacía que Whisper colara esas palabras donde el profe
# no las dijo, y cada 100 caracteres de prompt cuestan ~1 s de CPU por trozo. Tampoco
# ganó pasarle el texto de la diapositiva (se colaba en la transcripción).
VOCAB_PROMPT = "Clase universitaria en español."

# Frases que Whisper inventa cuando hay silencio/ruido (se descartan)
HALLUCINATION_PATTERNS = [
    r"subt[ií]tulos?\s+(realizados|hechos|por)",
    r"amara\.org",
    r"gracias por ver",
    r"suscr[ií]b(e|a)te",
    r"^\s*(música|aplausos|risas)\s*$",
    r"^\s*\[.*\]\s*$",
    r"www\.",
]
_HALLU_RE = re.compile("|".join(HALLUCINATION_PATTERNS), re.IGNORECASE)


def _design_fir(taps: int = 101, fs: int = 48000, fc: int = 7200) -> np.ndarray:
    """Paso bajo windowed-sinc para bajar a 16 kHz sin aliasing (plano hasta 6 kHz)."""
    n = np.arange(taps) - (taps - 1) / 2
    h = np.sinc(2 * fc / fs * n) * np.blackman(taps)
    return (h / h.sum()).astype(np.float32)


# Diseñado para 48 kHz → 16 kHz, el caso normal en Windows.
_FIR = _design_fir()


# ── Carga del modelo: GPU si la hay, si no CPU ───────────────────────────

def cuda_available() -> bool:
    """True si CTranslate2 ve alguna GPU NVIDIA con CUDA."""
    try:
        import ctranslate2
        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def whisper_device() -> str:
    """Dispositivo pedido: CLASSHELPER_WHISPER_DEVICE > config.json > "auto"."""
    import app_config
    dev = os.environ.get("CLASSHELPER_WHISPER_DEVICE") or app_config.load().get("whisper_device", "auto")
    return dev if dev in ("auto", "cpu", "cuda") else "auto"


def load_whisper(model_name: str, cpu_threads: int = 0):
    """
    Carga faster-whisper en GPU (float16) si está disponible y funciona; si no, en CPU (int8).
    Devuelve (modelo, "cuda" | "cpu"). La primera vez descarga el modelo de Hugging Face
    a la caché del usuario; después arranca sin red.
    """
    from faster_whisper import WhisperModel
    dev = whisper_device()
    if dev == "cuda" or (dev == "auto" and cuda_available()):
        try:
            model = WhisperModel(model_name, device="cuda", compute_type="float16")
            # Con CUDA pero sin cuDNN/cuBLAS el fallo salta al transcribir, no al cargar
            list(model.transcribe(np.zeros(16000, np.float32), beam_size=1)[0])
            return model, "cuda"
        except Exception:
            if dev == "cuda":
                raise
    model = WhisperModel(model_name, device="cpu", compute_type="int8",
                         cpu_threads=cpu_threads or max(4, (os.cpu_count() or 8) - 4))
    return model, "cpu"


class AudioWorker(QThread):
    """Hilo principal de audio. Se crea una vez y se reutiliza entre sesiones."""

    # (chunk_id, slide, texto crudo)  → aparece al instante
    transcription_ready = Signal(int, int, str)
    # (chunk_id, texto mejorado)      → llega después, sustituye al crudo
    enhanced_ready      = Signal(int, str)
    level_changed       = Signal(float)          # 0..1 nivel del micro (para el VU)
    status_update       = Signal(str)
    error_occurred      = Signal(str)
    audio_saved         = Signal(str)            # ruta del .wav de sesión
    model_loaded        = Signal(str)

    SAMPLE_RATE = 16000
    # Whisper procesa ventanas de 30 s: un chunk de 15 s cuesta casi lo mismo que
    # uno de 29 s. Por eso buscamos la pausa entre 20 y 29 s (nunca pasar de 30).
    MIN_CHUNK_S = 20      # no transcribir trozos más cortos
    MAX_CHUNK_S = 29      # a partir de aquí corta en el punto más silencioso
    SILENCE_RMS = 0.012   # techo del umbral de silencio (se adapta al ruido del aula)
    SILENCE_MS  = 450     # duración mínima de pausa para cortar ahí

    def __init__(self, model_name: str = "large-v3-turbo", language: str = "es",
                 enhancer: AIEnhancer | None = None, device_index: int | None = None):
        super().__init__()
        self.model_name   = model_name
        self.language     = language
        self.device_index = device_index
        self.enhancer     = enhancer or AIEnhancer()
        self.current_slide = 1
        self.model = None
        self.device = ""                          # "cuda" o "cpu" una vez cargado

        self._active = False
        self._buffer: list[np.ndarray] = []
        self._buffer_len = 0                      # muestras acumuladas
        self._buffer_lock = threading.Lock()
        self._audio_queue: "queue.Queue[tuple[int, int, np.ndarray] | None]" = queue.Queue()
        self._chunk_id = 0
        self._prev_text = ""                      # cola de contexto para Whisper

        self._wav_writer: wave.Wave_write | None = None
        self._wav_lock = threading.Lock()
        self._wav_path = ""

        self._level = 0.0
        self._last_level_emit = 0.0

        # Conversión de la frecuencia nativa del micro a 16 kHz (ver _open_input)
        self._decim = 1
        self._fir_state = np.zeros(len(_FIR) - 1, np.float32)
        self._decim_phase = 0
        self.input_desc = ""                      # qué micro y cómo se abrió (para la UI)

    # ── Control público ──────────────────────────────────────────────────

    def set_slide(self, slide_number: int):
        self.current_slide = slide_number

    @property
    def audio_path(self) -> str:
        return self._wav_path

    def preload(self):
        """Carga el modelo en segundo plano sin grabar (para que Iniciar sea instantáneo)."""
        if self.model is None and not self.isRunning():
            self.start()

    def start_recording(self):
        self._active = True
        with self._buffer_lock:
            self._buffer.clear()
            self._buffer_len = 0
        self._prev_text = ""
        self.enhancer.reset_context()
        self._open_wav_writer()
        if not self.isRunning():
            self.start()

    def stop_recording(self):
        self._active = False

    # ── WAV en tiempo real ───────────────────────────────────────────────

    def _open_wav_writer(self):
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._wav_path = os.path.join(tempfile.gettempdir(), f"classhelper_{ts}.wav")
        try:
            wf = wave.open(self._wav_path, "wb")
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            with self._wav_lock:
                self._wav_writer = wf
        except Exception as e:
            self.status_update.emit(f"Advertencia: no se pudo abrir archivo de audio: {e}")
            self._wav_path = ""

    def _close_wav_writer(self):
        with self._wav_lock:
            if self._wav_writer:
                try:
                    self._wav_writer.close()
                except Exception:
                    pass
                self._wav_writer = None

    # ── Callback del micrófono (hilo de audio del sistema) ───────────────

    def _audio_callback(self, indata, frames, time_info, status):
        chunk = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        if self._decim > 1:
            chunk = self._downsample(chunk)
            if not len(chunk):
                return

        with self._buffer_lock:
            self._buffer.append(chunk)
            self._buffer_len += len(chunk)

        with self._wav_lock:
            if self._wav_writer:
                try:
                    self._wav_writer.writeframes((chunk * 32767).astype(np.int16).tobytes())
                except Exception:
                    pass

        # Nivel para el VU (suavizado, ~20 fps)
        rms = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
        self._level = max(rms, self._level * 0.85)
        now = time.time()
        if now - self._last_level_emit > 0.05:
            self._last_level_emit = now
            self.level_changed.emit(min(1.0, self._level * 6))

    def _downsample(self, x: np.ndarray) -> np.ndarray:
        """Paso bajo FIR + diezmado entero, con estado entre bloques (sin clics)."""
        buf = np.concatenate([self._fir_state, x.astype(np.float32)])
        y = np.convolve(buf, _FIR, mode="valid")
        self._fir_state = buf[-(len(_FIR) - 1):]
        d = self._decim
        out = y[(d - self._decim_phase) % d::d]
        self._decim_phase = (self._decim_phase + len(y)) % d
        return out

    # ── Micrófono ────────────────────────────────────────────────────────

    def _input_candidates(self) -> list[tuple[int | None, int]]:
        """
        (dispositivo, frecuencia) a probar en orden: el micro por WASAPI a su frecuencia
        nativa; si no abre, el mismo a 16 kHz; y por último el del sistema a 16 kHz.
        """
        cands: list[tuple[int | None, int]] = []
        try:
            devs = sd.query_devices()
            apis = sd.query_hostapis()
            wasapi = next((i for i, a in enumerate(apis) if "WASAPI" in a["name"]), None)
            dev = self.device_index
            if dev is None and wasapi is not None:
                default_in = sd.default.device[0]
                if default_in is not None and default_in >= 0:
                    # MME recorta los nombres a 31 caracteres: comparar por prefijo
                    prefix = devs[default_in]["name"][:25]
                    dev = next((i for i, d in enumerate(devs)
                                if d["hostapi"] == wasapi and d["max_input_channels"] > 0
                                and d["name"].startswith(prefix)), None)
                if dev is None:
                    dflt = apis[wasapi].get("default_input_device", -1)
                    dev = dflt if dflt is not None and dflt >= 0 else None
            if dev is not None:
                native = int(devs[dev]["default_samplerate"])
                if native % self.SAMPLE_RATE == 0:
                    cands.append((dev, native))
                cands.append((dev, self.SAMPLE_RATE))
        except Exception:
            pass
        cands.append((None, self.SAMPLE_RATE))
        seen, out = set(), []
        for c in cands:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    def _open_input(self):
        """Abre el micrófono con la mejor opción disponible; lanza el último error si ninguna abre."""
        last_err = None
        for dev, rate in self._input_candidates():
            decim = rate // self.SAMPLE_RATE
            self._decim = decim
            self._fir_state = np.zeros(len(_FIR) - 1, np.float32)
            self._decim_phase = 0
            try:
                stream = sd.InputStream(
                    callback=self._audio_callback, channels=1, samplerate=rate,
                    dtype="float32", blocksize=1024 * decim, device=dev,
                )
            except Exception as e:
                last_err = e
                continue
            try:
                d = sd.query_devices(dev if dev is not None else sd.default.device[0])
                api = sd.query_hostapis()[d["hostapi"]]["name"]
                self.input_desc = f"{d['name']} · {api} · {rate // 1000} kHz"
            except Exception:
                self.input_desc = f"micrófono del sistema · {rate // 1000} kHz"
            return stream
        raise last_err or RuntimeError("no hay micrófono disponible")

    # ── Chunker inteligente ──────────────────────────────────────────────

    def _find_cut(self, audio: np.ndarray) -> int | None:
        return find_cut(audio, self.SAMPLE_RATE, self.MIN_CHUNK_S, self.MAX_CHUNK_S,
                        self.SILENCE_RMS, self.SILENCE_MS)

    def _try_cut_chunk(self, force: bool = False):
        with self._buffer_lock:
            if not self._buffer:
                return
            audio = np.concatenate(self._buffer)
            if force:
                cut = len(audio)
            else:
                cut = self._find_cut(audio)
                if cut is None:
                    return
            piece = audio[:cut]
            rest  = audio[cut:]
            self._buffer = [rest] if len(rest) else []
            self._buffer_len = len(rest)

        if len(piece) < self.SAMPLE_RATE * 2:       # < 2 s: no merece la pena
            return
        self._chunk_id += 1
        self._audio_queue.put((self._chunk_id, self.current_slide, piece))

    # ── Bucle principal ──────────────────────────────────────────────────

    def run(self):
        if self.model is None:
            self.status_update.emit(f"Cargando modelo Whisper «{self.model_name}»…")
            try:
                self.model, self.device = load_whisper(self.model_name)
            except Exception as e:
                self._close_wav_writer()
                self.error_occurred.emit(f"Error cargando Whisper:\n{e}")
                return
            self.model_loaded.emit(self.model_name)
            if not self._active:                 # solo precarga: listo, sin grabar
                self.status_update.emit(f"Modelo «{self.model_name}» listo ({self.device.upper()})")
                return

        try:
            stream = self._open_input()
        except Exception as e:
            self._close_wav_writer()
            self.error_occurred.emit(
                f"No se pudo abrir el micrófono.\n\nDetalle: {e}"
            )
            return

        worker = threading.Thread(target=self._transcribe_loop, daemon=True)
        worker.start()
        self.status_update.emit(f"Escuchando…  ·  🎤 {self.input_desc}")

        with stream:
            while self._active:
                time.sleep(0.25)
                self._try_cut_chunk()
            self._try_cut_chunk(force=True)      # lo que quede al parar

        self._audio_queue.put(None)              # centinela → termina el hilo Whisper
        worker.join()
        self._close_wav_writer()
        self.level_changed.emit(0.0)

        size_mb = (os.path.getsize(self._wav_path) / 1_048_576
                   if self._wav_path and os.path.exists(self._wav_path) else 0)
        self.status_update.emit(f"Grabación detenida · audio guardado ({size_mb:.1f} MB)")
        if self._wav_path:
            self.audio_saved.emit(self._wav_path)

    # ── Hilo de transcripción ────────────────────────────────────────────

    def _transcribe_loop(self):
        while True:
            item = self._audio_queue.get()
            if item is None:
                break
            chunk_id, slide, audio = item
            pending = self._audio_queue.qsize()
            dur = len(audio) / self.SAMPLE_RATE
            self.status_update.emit(
                f"Transcribiendo {dur:.0f} s" + (f"  (+{pending} en cola)" if pending else "")
            )
            # Si vamos con retraso, bajamos precisión de búsqueda para recuperar el ritmo
            raw = self._transcribe(audio, beam=1 if pending >= 2 else 3)
            if not raw:
                self.status_update.emit("Escuchando…")
                continue
            self._prev_text = (self._prev_text + " " + raw)[-400:]
            self.transcription_ready.emit(chunk_id, slide, raw)
            self.status_update.emit("Escuchando…")

            if self.enhancer.is_active():
                threading.Thread(
                    target=self._enhance, args=(chunk_id, raw), daemon=True
                ).start()

    def _enhance(self, chunk_id: int, raw: str):
        try:
            text = self.enhancer.enhance(raw)
        except Exception:
            text = raw                              # nunca pegar errores en los apuntes
        err = getattr(self.enhancer, "last_error", "")
        if err:
            self.status_update.emit(f"⚠ IA: {err} · sigue solo con Whisper")
        self.enhanced_ready.emit(chunk_id, text)

    # ── Whisper ──────────────────────────────────────────────────────────

    def _transcribe(self, audio: np.ndarray, beam: int = 3) -> str:
        try:
            return transcribe_audio(self.model, audio, self._prev_text, beam, self.language)
        except Exception as e:
            self.status_update.emit(f"Error transcribiendo: {e}")
            return ""


def find_cut(audio: np.ndarray, sr: int = 16000, min_s: float = 20, max_s: float = 29,
             silence_ceiling: float = 0.012, silence_ms: int = 450) -> int | None:
    """
    Índice donde cortar `audio`, o None si aún no toca. Busca una pausa (RMS bajo
    durante silence_ms) a partir de min_s; si llega a max_s corta en la ventana más
    silenciosa de los últimos 5 s. Nunca parte una palabra si hay pausa disponible.
    """
    n = len(audio)
    if n < min_s * sr:
        return None
    win = int(sr * 0.05)                           # ventanas de 50 ms
    n_win = n // win
    rms = np.sqrt(np.mean(audio[: n_win * win].reshape(n_win, win) ** 2, axis=1))
    need = max(1, int(silence_ms / 50))
    start_w = int(min_s * sr / win)

    # 1) Primera pausa real después del mínimo. El umbral se adapta al ruido de
    #    fondo del aula (2,5 × percentil 20), sin pasar del techo silence_ceiling.
    thr = float(np.clip(2.5 * np.percentile(rms, 20), 0.003, silence_ceiling))
    quiet = rms < thr
    run = 0
    for i in range(start_w, n_win):
        run = run + 1 if quiet[i] else 0
        if run >= need:
            return (i - need // 2) * win          # mitad de la pausa

    # 2) Forzar corte si es demasiado largo: punto más silencioso de los últimos 5 s
    if n >= max_s * sr:
        tail_w = max(start_w, n_win - int(5 * sr / win))
        idx = tail_w + int(np.argmin(rms[tail_w:n_win]))
        return idx * win
    return None


def transcribe_audio(model, audio: np.ndarray, prev_text: str = "", beam: int = 3,
                     language: str = "es") -> str:
    """Transcribe un trozo con los parámetros de ClassHelper. Lanza si Whisper falla."""
    # Contexto = frase neutra + cola corta de lo anterior (continuidad entre trozos)
    prompt = VOCAB_PROMPT
    if prev_text:
        tail = prev_text[-120:]
        prompt += " " + tail[tail.find(" ") + 1:]     # empezar en palabra entera
    segments, _ = model.transcribe(
        audio.astype(np.float32),
        language=language,
        beam_size=beam,
        # 0.0 primero; si el trozo sale en bucle (compresión alta) o con muy
        # baja confianza, faster-whisper reintenta con temperaturas mayores.
        temperature=[0.0, 0.2, 0.4, 0.6],
        compression_ratio_threshold=2.2,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400, "speech_pad_ms": 200},
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        initial_prompt=prompt,
    )
    parts = []
    for seg in segments:
        t = seg.text.strip()
        if not t:
            continue
        if seg.no_speech_prob > 0.75 and seg.avg_logprob < -0.8:
            continue                              # casi seguro ruido
        if _HALLU_RE.search(t):
            continue
        parts.append(t)
    return _dedupe(" ".join(parts))


_PUNCT = ".,;:¿?¡!«»\"'…"


def _norm_word(w: str) -> str:
    return w.lower().strip(_PUNCT)


def _dedupe(text: str) -> str:
    """
    Colapsa bucles típicos de Whisper: una palabra o frase (hasta 8 palabras)
    repetida 3 o más veces seguidas se deja una sola vez. Dos repeticiones se
    respetan ("sí, sí").
    """
    words = text.split()
    for n in range(8, 0, -1):                     # frases largas primero
        out: list[str] = []
        i = 0
        while i < len(words):
            gram = [_norm_word(w) for w in words[i:i + n]]
            if len(gram) < n:
                out.extend(words[i:])
                break
            reps = 1
            while [_norm_word(w) for w in words[i + reps * n:i + (reps + 1) * n]] == gram:
                reps += 1
            keep = 1 if reps >= 3 else reps
            out.extend(words[i:i + n * keep])
            i += n * reps
        words = out
    return " ".join(words)
