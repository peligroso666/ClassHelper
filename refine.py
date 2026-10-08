"""
refine.py - Repaso con turbo de una clase ya grabada.

En directo se transcribe con Whisper small porque, sin GPU, es el único modelo que va en
tiempo real. Con el audio ya guardado no hay prisa: large-v3-turbo lo vuelve a transcribir
entero y mucho mejor (en una clase real corrigió frases que small había convertido en
palabras sin sentido).

RefineWorker trocea el .wav igual que la grabación en directo (audio_thread.find_cut),
transcribe cada trozo con los mismos parámetros (audio_thread.transcribe_audio) y asigna
a cada fragmento la diapositiva que había abierta, cruzando su posición en el audio con la
hora de los fragmentos en directo. Las marcas 📌 se conservan.
"""

import os
import json
import wave
import datetime

import numpy as np
from PySide6.QtCore import QThread, Signal

import app_config
from audio_thread import find_cut, transcribe_segments, load_whisper, _dedupe


REFINE_MODEL = "large-v3-turbo"
REFINE_BEAM = 3


def refine_threads() -> int:
    """
    Núcleos para turbo. Por defecto la MITAD de los del equipo: usando casi todos
    (14 de 16) el equipo se volvía inusable mientras preparaba una clase.
    Se puede ajustar con "refine_threads" en config.json.
    """
    total = os.cpu_count() or 8
    try:
        n = int(app_config.load().get("refine_threads") or 0)
        if n > 0:
            return max(1, min(total, n))
    except Exception:
        pass
    return max(2, total // 2)
# Segundos de CPU por segundo de audio, medido en un i7-1360P con trozos de 2 min y 8 hilos
# (para la estimación que ve el usuario; con GPU acaba bastante antes)
SECONDS_PER_AUDIO_SECOND = 0.5
PIECE_MIN_S, PIECE_MAX_S = 100, 120     # trozo que se le pasa a Whisper
ENTRY_S = 25                            # duración de cada fragmento de los apuntes
SR = 16000


# ── Punto de guardado: si el portátil se apaga a mitad, turbo sigue donde iba ──
def _ckpt_path(wav_path: str) -> str:
    return wav_path + ".turbo.json"


def refine_progress(wav_path: str) -> float:
    """Fracción (0-1) del audio que turbo ya tiene hecha y guardada."""
    try:
        with open(_ckpt_path(wav_path), encoding="utf-8") as f:
            d = json.load(f)
        total = max(1, (os.path.getsize(wav_path) - 44) // 2)
        return min(1.0, max(0.0, d.get("pos", 0) / total))
    except Exception:
        return 0.0


def read_wav_16k(path: str) -> np.ndarray:
    """Lee el .wav de ClassHelper (16 kHz, mono, 16 bits). Tolera cabeceras sin cerrar."""
    with wave.open(path, "rb") as w:
        rate, channels, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
    if rate != SR or channels != 1 or width != 2:
        raise RuntimeError(f"Formato de audio no soportado ({rate} Hz, {channels} canales, {width * 8} bits)")
    with open(path, "rb") as f:
        raw = f.read()
    i = raw.find(b"data")
    if i < 0:
        raise RuntimeError("El archivo de audio no tiene datos")
    size = int.from_bytes(raw[i + 4:i + 8], "little")
    body = raw[i + 8:]
    if 0 < size <= len(body):
        body = body[:size]                    # cabecera correcta
    body = body[: len(body) // 2 * 2]         # cabecera a 0 (grabación sin cerrar): todo lo que hay
    return np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768


def _clock_seconds(ts: str) -> int | None:
    try:
        h, m, s = (int(x) for x in str(ts).split(":")[:3])
        return h * 3600 + m * 60 + s
    except Exception:
        return None


def _clock_text(seconds: float) -> str:
    s = int(seconds) % 86400
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def slide_timeline(entries: list, date_iso: str) -> tuple[int, list[tuple[float, int]]]:
    """
    (segundo de reloj en que empezó la clase, [(segundo del audio, diapositiva)]).
    Cada fragmento en directo se emitía al acabar de transcribirse su trozo, con la
    diapositiva que había abierta al cortar ese trozo; su hora sirve de marca temporal.
    """
    try:
        start = datetime.datetime.fromisoformat(date_iso)
        start_s = start.hour * 3600 + start.minute * 60 + start.second
    except Exception:
        first = next((_clock_seconds(e.get("ts")) for e in entries if _clock_seconds(e.get("ts")) is not None), 0)
        start_s = first or 0
    points = []
    for e in entries:
        if not isinstance(e, dict) or e.get("kind", "text") == "marker":
            continue
        c = _clock_seconds(e.get("ts"))
        if c is None:
            continue
        off = c - start_s
        if off < 0:
            off += 86400                      # clase que cruza la medianoche
        points.append((float(off), int(e.get("slide") or 1)))
    points.sort()
    return start_s, points


def slide_at(t: float, points: list[tuple[float, int]], default: int = 1) -> int:
    """Diapositiva para un trozo que termina en el segundo t del audio."""
    for off, slide in points:
        if off >= t:
            return slide
    return points[-1][1] if points else default


class RefineWorker(QThread):
    """Re-transcribe con turbo el audio de una sesión. No toca el disco: devuelve los fragmentos."""

    progress  = Signal(str, int, str)       # session_id, %, mensaje
    done      = Signal(str, object)         # session_id, lista de entries
    failed    = Signal(str, str)            # session_id, mensaje
    cancelled = Signal(str)                 # session_id

    def __init__(self, session_id: str, wav_path: str, entries: list, date_iso: str, parent=None):
        super().__init__(parent)
        self.session_id = session_id
        self.wav_path = wav_path
        self.entries = [dict(e) for e in (entries or []) if isinstance(e, dict)]
        self.date_iso = date_iso
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        sid = self.session_id
        try:
            if not self.wav_path or not os.path.exists(self.wav_path):
                raise RuntimeError("No se encuentra el audio de esta clase")
            audio = read_wav_16k(self.wav_path)
            if len(audio) < SR * 3:
                raise RuntimeError("El audio de esta clase está vacío")

            self.progress.emit(sid, 0, f"Cargando turbo ({refine_threads()} núcleos, "
                                       "la primera vez se descarga ~1,6 GB)…")
            # Prioridad baja: que el equipo siga respondiendo mientras prepara
            self.setPriority(QThread.Priority.LowestPriority)
            model, _device = load_whisper(REFINE_MODEL, cpu_threads=refine_threads())

            start_s, points = slide_timeline(self.entries, self.date_iso)
            default_slide = points[0][1] if points else 1
            ckpt = _ckpt_path(self.wav_path)
            out: list[dict] = []
            pos, prev = 0, ""
            try:
                with open(ckpt, encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("model") == REFINE_MODEL and 0 < d.get("pos", 0) < len(audio):
                    out, pos, prev = list(d["out"]), int(d["pos"]), d.get("prev", "")
            except Exception:
                pass
            buf = audio[pos:]
            total = len(audio)
            while len(buf) >= SR:                 # menos de 1 s al final no merece la pena
                if self._cancel:
                    self.cancelled.emit(sid)
                    return
                # Trozos de ~2 min: Whisper rellena cada ventana hasta 30 s, así que con
                # trozos de 25 s gastaba el doble (medido: 0,76× frente a 0,37× t. real)
                cut = find_cut(buf, min_s=PIECE_MIN_S, max_s=PIECE_MAX_S) or len(buf)
                piece, buf = buf[:cut], buf[cut:]
                base = pos / SR
                pos += cut
                segs = transcribe_segments(model, piece, prev, beam=REFINE_BEAM)
                # Fragmentos de ~25 s, cada uno con la diapositiva de su momento
                grupo, ini = [], None
                for k, (a, b, t) in enumerate(segs):
                    ini = a if ini is None else ini
                    grupo.append(t)
                    if b - ini >= ENTRY_S or k == len(segs) - 1:
                        text = _dedupe(" ".join(grupo))
                        grupo, ini = [], None
                        if not text:
                            continue
                        prev = (prev + " " + text)[-400:]
                        end_s = base + b
                        out.append({
                            "id": len(out) + 1,
                            "slide": slide_at(end_s, points, default_slide),
                            "ts": _clock_text(start_s + end_s),
                            "raw": text,
                            "enhanced": "",
                            "kind": "text",
                        })
                try:                                      # guardar por dónde va
                    with open(ckpt + ".tmp", "w", encoding="utf-8") as f:
                        json.dump({"model": REFINE_MODEL, "pos": pos, "prev": prev, "out": out}, f)
                    os.replace(ckpt + ".tmp", ckpt)
                except OSError:
                    pass
                pct = min(99, int(pos * 100 / total))
                self.progress.emit(sid, pct, f"Repasando con turbo… {pct} %")

            # Las marcas 📌 se mantienen en su momento de la clase
            markers = [dict(e) for e in self.entries if e.get("kind") == "marker"]
            merged = out + markers
            merged.sort(key=lambda e: ((_clock_seconds(e.get("ts")) or 0) - start_s) % 86400)
            self.progress.emit(sid, 100, "Repaso terminado")
            self.done.emit(sid, merged)
            try:
                os.remove(ckpt)
            except OSError:
                pass
        except Exception as e:
            self.failed.emit(sid, str(e) or e.__class__.__name__)
