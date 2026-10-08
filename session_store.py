"""
session_store.py - Biblioteca de clases grabadas (persistencia en JSON).

Cada clase es una Session guardada en ~/Documents/ClassHelper/<id>.json con
la transcripción por fragmentos, el resumen y el estado del modo estudio
(tarjetas Leitner, quiz, chat con el tutor y % de dominio).

No depende de Qt: lo usan notes_panel (al guardar la clase), library_panel
(listado semanal) y study_panel (tarjetas, quiz, dominio).

El id es "YYYYMMDD_HHMM" derivado de la fecha de inicio, así que guardar
varias veces la misma clase (autoguardado + guardado final) actualiza el
mismo fichero en vez de duplicarlo.
"""

import os
import json
import glob
import datetime
from dataclasses import dataclass, field, fields, asdict

LIBRARY_DIR = os.path.join(os.path.expanduser("~"), "Documents", "ClassHelper")
FILE_VERSION = 1

# Días hasta el próximo repaso según la caja Leitner (0..4)
LEITNER_DAYS = [1, 2, 4, 7, 15]
MAX_BOX = 4

# Sufijo que ai_enhancer/audio_thread pegan al texto cuando la IA falla
# ("texto  ⚠️ [429 quota…]"): ese enhanced no es texto de la clase.
AI_ERROR_MARK = "⚠️ ["


def _default_study() -> dict:
    return {"flashcards": [], "quiz": {"questions": [], "attempts": []}, "chat": [], "mastery": 0}


# ── Modelo ────────────────────────────────────────────────────────────────

@dataclass
class Session:
    id: str                      # "YYYYMMDD_HHMM"
    title: str                   # nombre base del .txt
    subject: str = ""            # asignatura, editable
    date: str = ""               # ISO "2026-09-14T12:20:00"
    duration_s: int = 0
    pdf_path: str = ""
    txt_path: str = ""
    wav_path: str = ""
    entries: list = field(default_factory=list)   # dicts {id, slide, ts, raw, enhanced, kind}
    summary: str = ""
    study: dict = field(default_factory=_default_study)


# ── Utilidades internas ───────────────────────────────────────────────────

def _ensure_dir():
    os.makedirs(LIBRARY_DIR, exist_ok=True)


def _id_from_date(date_iso: str) -> str:
    try:
        dt = datetime.datetime.fromisoformat(date_iso)
    except (TypeError, ValueError):
        dt = datetime.datetime.now()
    return dt.strftime("%Y%m%d_%H%M")


def _as_date(d) -> datetime.date:
    """Acepta date, datetime o "YYYY-MM-DD"; cualquier otra cosa = hoy."""
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    if isinstance(d, str):
        try:
            return datetime.date.fromisoformat(d[:10])
        except ValueError:
            pass
    return datetime.date.today()


def _normalize_study(study) -> dict:
    """Completa las claves que falten en study (ficheros de versiones viejas)."""
    base = _default_study()
    if not isinstance(study, dict):
        return base
    for k, v in base.items():
        if study.get(k) is None:
            study[k] = v
    if not isinstance(study["quiz"], dict):
        study["quiz"] = {"questions": [], "attempts": []}
    for k in ("questions", "attempts"):
        if not isinstance(study["quiz"].get(k), list):
            study["quiz"][k] = []
    for k in ("flashcards", "chat"):
        if not isinstance(study[k], list):
            study[k] = []
    return study


def _clean_enhanced(text: str) -> str:
    """Descarta el enhanced que solo lleva el error de la IA (se usará raw)."""
    return "" if AI_ERROR_MARK in text else text


def _entry_to_dict(e) -> dict:
    """Acepta dicts o objetos con atributos (p.ej. Entry de notes_panel)."""
    if isinstance(e, dict):
        d = e
    else:
        d = {k: getattr(e, k, None) for k in ("id", "slide", "ts", "raw", "enhanced", "kind")}
    return {
        "id": int(d.get("id") or 0),
        "slide": int(d.get("slide") or 0),
        "ts": str(d.get("ts") or ""),
        "raw": str(d.get("raw") or ""),
        "enhanced": _clean_enhanced(str(d.get("enhanced") or "")),
        "kind": d.get("kind") or "text",
    }


# ── Creación ──────────────────────────────────────────────────────────────

def new_session(title, date_iso, pdf_path="", txt_path="", wav_path="") -> Session:
    return Session(id=_id_from_date(date_iso), title=title, date=date_iso,
                   pdf_path=pdf_path, txt_path=txt_path, wav_path=wav_path)


def session_from_entries(title, date_iso, entries, duration_s, pdf_path, txt_path, wav_path,
                         summary="") -> Session:
    s = new_session(title, date_iso, pdf_path, txt_path, wav_path)
    s.entries = [_entry_to_dict(e) for e in (entries or [])]
    s.duration_s = int(duration_s or 0)
    s.summary = summary or ""
    return s


# ── Persistencia ──────────────────────────────────────────────────────────

def session_path(session_id: str) -> str:
    return os.path.join(LIBRARY_DIR, f"{session_id}.json")


def save_session(s: Session) -> str:
    """Escribe LIBRARY_DIR/<id>.json de forma atómica y devuelve la ruta.
    Recalcula el dominio antes de guardar para que el listado lo lea al día."""
    _ensure_dir()
    s.study = _normalize_study(s.study)
    compute_mastery(s)
    data = {"version": FILE_VERSION, **asdict(s)}
    path = session_path(s.id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    return path


def load_session(path: str) -> Session:
    """Carga un .json; rellena con valores por defecto lo que falte."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Fichero de sesión no válido: {path}")
    kwargs = {fd.name: data[fd.name] for fd in fields(Session)
              if data.get(fd.name) is not None}
    if not kwargs.get("id"):
        kwargs["id"] = os.path.splitext(os.path.basename(path))[0]
    if not kwargs.get("title"):
        kwargs["title"] = kwargs["id"]
    s = Session(**kwargs)
    s.entries = [_entry_to_dict(e) for e in s.entries] if isinstance(s.entries, list) else []
    s.study = _normalize_study(s.study)
    s.duration_s = int(s.duration_s or 0)
    for name in ("subject", "date", "pdf_path", "txt_path", "wav_path", "summary"):
        setattr(s, name, str(getattr(s, name) or ""))
    return s


def list_sessions() -> list:
    """Todas las sesiones de la biblioteca, más recientes primero."""
    if not os.path.isdir(LIBRARY_DIR):
        return []
    out = []
    for p in glob.glob(os.path.join(LIBRARY_DIR, "*.json")):
        try:
            out.append(load_session(p))
        except Exception:
            continue                      # fichero corrupto: se ignora
    out.sort(key=lambda s: s.date, reverse=True)
    return out


def delete_session(session_id: str) -> None:
    """Borra solo el .json de la biblioteca; el PDF, .txt y .wav se conservan."""
    try:
        os.remove(session_path(session_id))
    except FileNotFoundError:
        pass


# ── Texto ─────────────────────────────────────────────────────────────────

def best_text(entry: dict) -> str:
    """enhanced si existe y no es un error de la IA; si no, raw."""
    enhanced = entry.get("enhanced") or ""
    if enhanced and AI_ERROR_MARK not in enhanced:
        return enhanced
    return entry.get("raw") or ""


def full_text(s: Session) -> str:
    """Transcripción completa "[Diapo n - ts] texto" por línea, sin marcas."""
    return "\n".join(
        f"[Diapo {e.get('slide', 0)} - {e.get('ts', '')}] {best_text(e)}"
        for e in s.entries if e.get("kind", "text") != "marker"
    )


# ── Modo estudio ──────────────────────────────────────────────────────────

def compute_mastery(s: Session) -> int:
    """% de dominio 0..100. 60 % tarjetas (media de box/4) + 40 % mejor quiz.
    Si falta uno de los dos, el otro se lleva todo el peso. Escribe study["mastery"]."""
    study = s.study = _normalize_study(s.study)
    cards = [c for c in study["flashcards"] if isinstance(c, dict)]
    attempts = [a for a in study["quiz"]["attempts"]
                if isinstance(a, dict) and int(a.get("total") or 0) > 0]
    if not cards and not attempts:
        study["mastery"] = 0
        return 0

    card_score = quiz_score = None
    if cards:
        boxes = (min(MAX_BOX, max(0, int(c.get("box") or 0))) for c in cards)
        card_score = sum(boxes) / (MAX_BOX * len(cards))
    if attempts:
        quiz_score = max(min(1.0, int(a.get("score") or 0) / int(a["total"])) for a in attempts)

    if card_score is not None and quiz_score is not None:
        value = 0.6 * card_score + 0.4 * quiz_score
    elif card_score is not None:
        value = card_score
    else:
        value = quiz_score
    m = max(0, min(100, int(round(value * 100))))
    study["mastery"] = m
    return m


def schedule_flashcard(card: dict, known: bool, today: datetime.date) -> None:
    """Leitner: acierto sube de caja y aleja el repaso; fallo vuelve a la caja 0 hoy."""
    today = _as_date(today)
    card["seen"] = int(card.get("seen") or 0) + 1
    card["correct"] = int(card.get("correct") or 0)
    if known:
        box = min(MAX_BOX, int(card.get("box") or 0) + 1)
        card["correct"] += 1
        due = today + datetime.timedelta(days=LEITNER_DAYS[box])
    else:
        box = 0
        due = today
    card["box"] = box
    card["due"] = due.isoformat()


def due_flashcards(s: Session, today: datetime.date) -> list:
    """Tarjetas con due <= today (sin fecha = pendiente), cajas bajas primero."""
    today = _as_date(today)
    s.study = _normalize_study(s.study)
    due = []
    for c in s.study["flashcards"]:
        if not isinstance(c, dict):
            continue
        raw_due = c.get("due")
        due_date = today
        if raw_due:
            try:
                due_date = datetime.date.fromisoformat(str(raw_due)[:10])
            except ValueError:
                pass
        if due_date <= today:
            due.append(c)
    due.sort(key=lambda c: int(c.get("box") or 0))
    return due
