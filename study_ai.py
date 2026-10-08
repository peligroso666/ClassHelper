"""
study_ai.py - Capa de IA del modo estudio: tarjetas, test, tutor y lección guiada.

Reutiliza el proveedor activo de AIEnhancer (_call_with_system) y devuelve
estructuras ya validadas y normalizadas. Todas las funciones de StudyAI son
bloqueantes: ejecútalas dentro de un AITask (QThread) para no congelar la UI.

    task = AITask(study.generate_flashcards, transcript, 15, parent=self)
    task.done.connect(self._on_cards)       # recibe list[dict]
    task.failed.connect(self._on_error)     # recibe str en español
    task.start()

Formato del transcript esperado: una línea por fragmento con prefijo
"[Diapo n - HH:MM:SS] texto" (lo produce session_store.full_text).
"""

import json
import re

from PySide6.QtCore import QThread, Signal


# ── Prompts de sistema ────────────────────────────────────────────────────

_CONTEXTO = (
    "Eres profesor ayudante de un grado de Ingeniería de Telecomunicaciones / "
    "Electrónica (líneas de transmisión, filtros, amplificadores, modulaciones, "
    "señales y sistemas, electrónica analógica y digital). Trabajas sobre la "
    "transcripción automática de UNA clase concreta: cada línea empieza por "
    "\"[Diapo n - hh:mm:ss]\" indicando la diapositiva en la que se dijo.\n"
)

FLASHCARDS_SYSTEM = _CONTEXTO + """
Tu tarea: crear tarjetas de memoria (flashcards) para que el alumno repase esa clase.

REGLAS:
1. Básate SOLO en lo que se dijo en clase. No inventes datos, fórmulas ni valores que no aparezcan en la transcripción.
2. Mezcla tipos de tarjeta: definiciones, fórmulas y relaciones matemáticas, preguntas de "¿por qué?" (razonamiento) y relaciones entre conceptos.
3. "q": pregunta concreta y autocontenida (nunca "¿qué se dijo?"). "a": respuesta de 1 a 3 frases, precisa, con las palabras del profesor siempre que se pueda.
4. "slide": número de la diapositiva de la que sale el contenido, tomado del prefijo "[Diapo n - hh:mm:ss]". Si no se puede saber, 0.
5. Ignora avisos organizativos (fechas, entregas) salvo que tengan contenido técnico.
6. Todo en español, tuteando al alumno. Sin repetir preguntas.

FORMATO DE SALIDA (OBLIGATORIO):
Devuelve ÚNICAMENTE un array JSON válido. Sin markdown, sin ```json, sin texto antes ni después.
[{"q": "pregunta", "a": "respuesta", "slide": 3}, ...]"""

QUIZ_SYSTEM = _CONTEXTO + """
Tu tarea: redactar un test de opción múltiple sobre esa clase para detectar lagunas de conocimiento.

REGLAS:
1. Cada pregunta se basa SOLO en lo dicho en clase; no inventes contenido.
2. "options": exactamente 4 opciones, cortas y del mismo estilo. Solo UNA correcta. Las otras tres deben ser errores plausibles de un alumno (unidad equivocada, concepto vecino, relación invertida, valor cambiado). Prohibido "todas las anteriores" o "ninguna de las anteriores".
3. "answer": índice de la opción correcta empezando en 0 (0, 1, 2 o 3). Reparte la posición de la correcta entre las preguntas.
4. "explanation": 1-2 frases que expliquen por qué es correcta y por qué falla el error más habitual.
5. "slide": diapositiva de la que sale, tomada del prefijo "[Diapo n - hh:mm:ss]"; 0 si no se sabe.
6. Cubre distintos conceptos de la clase; mezcla preguntas de definición, de cálculo/relación y de razonamiento. Español, tuteando.

FORMATO DE SALIDA (OBLIGATORIO):
Devuelve ÚNICAMENTE un array JSON válido. Sin markdown, sin ```json, sin texto antes ni después.
[{"q": "pregunta", "options": ["A", "B", "C", "D"], "answer": 2, "explanation": "por qué", "slide": 3}, ...]"""

TUTOR_SYSTEM = _CONTEXTO + """
Tu tarea: ser un tutor paciente y cercano que ayuda al alumno a entender esa clase.

REGLAS:
1. Responde basándote SOLO en lo que dijo el profesor en la transcripción. Cita la diapositiva cuando ayude, por ejemplo "(diapo 3)".
2. Si lo que pregunta el alumno NO aparece en la clase, dilo claramente al principio ("Esto no se trató en la clase") y después responde con tu conocimiento general en un bloque que empiece por "Fuera de la clase:".
3. Explica paso a paso, con ejemplos sencillos y sin rodeos. Si el alumno se equivoca, corrígele con amabilidad.
4. Tutea al alumno y responde en español.
5. TEXTO PLANO: sin markdown. Nada de **negritas**, ni #, ni tablas, ni ```. Puedes usar saltos de línea y viñetas con "•".
6. Sé concreto: de 1 a 4 párrafos cortos. Si la pregunta es de sí/no, responde primero y justifica después."""

GAPS_SYSTEM = _CONTEXTO + """
Tu tarea: el alumno ha hecho un test sobre esa clase y ha fallado algunas preguntas. Detecta sus lagunas de conocimiento y dile qué repasar.

FORMATO DE SALIDA: de 3 a 6 líneas, cada una con este formato exacto:
• Concepto — qué repasar y por qué (diapo n)

REGLAS: agrupa las preguntas falladas por concepto (no repitas el mismo concepto); si hay pocos fallos, desglosa el concepto en los subpuntos que conviene repasar. Indica en qué diapositiva se explicó y por qué ese error importa. Básate en la transcripción. Texto plano, sin markdown, sin títulos ni texto antes o después de la lista. Español, tuteando al alumno."""

LESSON_SYSTEM = _CONTEXTO + """
Tu tarea: escribir una lección guiada de esa clase para que el alumno la estudie de forma autónoma.

ESTRUCTURA: de 4 a 7 secciones, en el orden de la clase. Cada sección empieza con una línea de título con este formato exacto:
━━ TÍTULO DE LA SECCIÓN ━━
Debajo, 1 a 3 párrafos que expliquen el concepto con las palabras y el enfoque del profesor, y después un ejemplo concreto (numérico o de circuito si la clase lo permite) introducido con "Ejemplo:". La última sección es "━━ PARA RECORDAR ━━" con 3-6 viñetas "•".

REGLAS: no inventes contenido que no esté en la clase; si añades un dato general para aclarar, márcalo como "(fuera de la clase)". Texto plano, sin markdown (**, #, ```). Español, tuteando al alumno."""


# ── Utilidades de parseo ─────────────────────────────────────────────────

_SLIDE_RE          = re.compile(r"\[Diapo\s+(\d+)\s*-")
_FENCE_RE          = re.compile(r"```[a-zA-Z]*[ \t]*\n?|```")
_TRAILING_COMMA_RE = re.compile(r",\s*([\]}])")
# Valor de texto sin comilla de apertura: "a": Se sitúan…",  →  "a": "Se sitúan…",
_UNQUOTED_VALUE_RE = re.compile(
    r'("(?:q|a|question|answer|pregunta|respuesta|explanation|explicacion|explicación)"\s*:\s*+)'   # *+ posesivo: sin retroceso no mete comillas de más
    r'(?!["\[{]|-?\d|true\b|false\b|null\b)'
)
_OPTION_PREFIX_RE  = re.compile(r"^[A-Da-d][\)\.\:]\s+")
_MD_HEADING_RE     = re.compile(r"^\s{0,3}#{1,6}\s+")
_MD_BULLET_RE      = re.compile(r"^(\s*)[-*]\s+")

_ERR_JSON = "La IA devolvió una respuesta con un formato inesperado. Inténtalo de nuevo."


def _extract_json(text: str):
    """Saca el JSON de la respuesta aunque venga con fences o texto alrededor."""
    if not text or not text.strip():
        raise RuntimeError(_ERR_JSON)
    s = _FENCE_RE.sub("", text).strip()

    candidates = []
    starts = [i for i in (s.find("["), s.find("{")) if i != -1]
    if starts:
        start = min(starts)
        close = "]" if s[start] == "[" else "}"
        end = s.rfind(close)
        if end > start:
            candidates.append(s[start:end + 1])
    for o, c in (("[", "]"), ("{", "}")):
        i, j = s.find(o), s.rfind(c)
        if i != -1 and j > i and s[i:j + 1] not in candidates:
            candidates.append(s[i:j + 1])
    candidates.append(s)
    longest = max(s.splitlines(), key=len, default="").strip()
    if longest and longest not in candidates:
        candidates.append(longest)

    for frag in candidates:
        fixed = _UNQUOTED_VALUE_RE.sub(r'\1"', frag)
        for attempt in (frag, _TRAILING_COMMA_RE.sub(r"\1", frag),
                        fixed, _TRAILING_COMMA_RE.sub(r"\1", fixed)):
            try:
                return json.loads(attempt, strict=False)
            except Exception:
                continue
    raise RuntimeError(_ERR_JSON)


def _as_list(data) -> list:
    """Acepta un array o un objeto envoltorio tipo {"flashcards": [...]}."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return v
        return [data]
    return []


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _norm_slide(v) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        m = re.search(r"\d+", _s(v))
        n = int(m.group()) if m else 0
    return n if n >= 1 else 0


def _norm_flashcard(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    q = _s(item.get("q") or item.get("question") or item.get("pregunta"))
    a = _s(item.get("a") or item.get("answer") or item.get("respuesta"))
    if not q or not a:
        return None
    return {"q": q, "a": a, "slide": _norm_slide(item.get("slide"))}


def _norm_answer(ans, options: list[str]) -> int | None:
    if isinstance(ans, bool):
        return None
    if isinstance(ans, float) and ans.is_integer():
        ans = int(ans)
    if isinstance(ans, int):
        return ans if 0 <= ans < len(options) else None
    s = _s(ans)
    if s.isdigit():
        i = int(s)
        return i if 0 <= i < len(options) else None
    if len(s) == 1 and s.upper() in "ABCD":
        i = "ABCD".index(s.upper())
        return i if i < len(options) else None
    for i, o in enumerate(options):
        if o.lower() == s.lower():
            return i
    return None


def _norm_question(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    q = _s(item.get("q") or item.get("question") or item.get("pregunta"))
    raw_opts = item.get("options") or item.get("opciones") or []
    if not q or not isinstance(raw_opts, list):
        return None
    options = [_OPTION_PREFIX_RE.sub("", _s(o)) for o in raw_opts]
    options = [o for o in options if o]
    if len(options) < 4:
        return None
    answer = _norm_answer(item.get("answer", item.get("respuesta")), options)
    if answer is None:
        return None
    if len(options) > 4:
        if answer >= 4:
            return None
        options = options[:4]
    return {
        "q": q,
        "options": options,
        "answer": answer,
        "explanation": _s(item.get("explanation") or item.get("explicacion") or item.get("explicación")),
        "slide": _norm_slide(item.get("slide")),
    }


def _plain(text: str) -> str:
    """Quita el markdown que algunos modelos cuelan aunque se les pida texto plano."""
    text = text.replace("```", "")
    lines = []
    for line in text.split("\n"):
        line = _MD_HEADING_RE.sub("", line)
        line = _MD_BULLET_RE.sub(r"\1• ", line)
        lines.append(line)
    return "\n".join(lines).replace("**", "").replace("__", "").strip()


def _friendly_error(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    low = msg.lower()
    if "429" in msg or "quota" in low or "resource_exhausted" in low or "rate limit" in low:
        return "Límite de peticiones de la IA alcanzado (429). Espera un minuto y vuelve a intentarlo."
    if "401" in msg or "api key" in low or "api_key" in low or "unauthenticated" in low:
        return "La clave de la IA no es válida. Revísala en el botón ✦ IA."
    if "connect" in low or "timeout" in low or "timed out" in low or "unavailable" in low:
        return "No se pudo conectar con la IA. Comprueba tu conexión e inténtalo de nuevo."
    return f"Error de la IA: {msg[:300]}"


def _slides_in(transcript: str) -> str:
    nums = sorted({int(n) for n in _SLIDE_RE.findall(transcript)})
    return ", ".join(str(n) for n in nums) if nums else "ninguna (usa slide 0)"


def _block(transcript: str) -> str:
    return f'Transcripción de la clase:\n"""\n{transcript}\n"""'


# ── Clase principal ───────────────────────────────────────────────────────

class StudyAI:
    """Genera material de estudio a partir de la transcripción usando el LLM activo."""

    MIN_ITEMS = 3

    def __init__(self, enhancer):
        self.enhancer = enhancer

    # ── Interno ──────────────────────────────────────────────────────────

    def _call(self, system: str, user: str, json_mode: bool = False) -> str:
        if not self.enhancer or not self.enhancer.is_active():
            raise RuntimeError("Activa la IA (botón ✦ IA) para usar el modo estudio")
        try:
            try:
                out = self.enhancer._call_with_system(system, user, json_mode=json_mode)
            except TypeError:                 # proveedor sin modo JSON
                out = self.enhancer._call_with_system(system, user)
        except Exception as e:
            raise RuntimeError(_friendly_error(e)) from e
        if not out or not out.strip():
            raise RuntimeError("La IA devolvió una respuesta vacía")
        return out.strip()

    @staticmethod
    def _check_transcript(transcript: str) -> str:
        t = (transcript or "").strip()
        if not t:
            raise RuntimeError("La transcripción está vacía: no hay nada que estudiar")
        return t

    # ── API pública ──────────────────────────────────────────────────────

    def generate_flashcards(self, transcript: str, n: int = 15) -> list[dict]:
        """Devuelve hasta n dicts {q, a, slide}."""
        transcript = self._check_transcript(transcript)
        n = max(self.MIN_ITEMS, int(n))
        user = (
            f"Genera exactamente {n} tarjetas de memoria.\n"
            f"Diapositivas presentes en la transcripción: {_slides_in(transcript)}\n\n"
            f"{_block(transcript)}\n\nDevuelve SOLO el array JSON."
        )
        data = _extract_json(self._call(FLASHCARDS_SYSTEM, user, json_mode=True))
        cards, seen = [], set()
        for item in _as_list(data):
            c = _norm_flashcard(item)
            if c and c["q"].lower() not in seen:
                seen.add(c["q"].lower())
                cards.append(c)
        if len(cards) < self.MIN_ITEMS:
            raise RuntimeError("La IA no generó suficientes tarjetas válidas. Inténtalo de nuevo.")
        return cards[:n]

    def generate_quiz(self, transcript: str, n: int = 10) -> list[dict]:
        """Devuelve hasta n dicts {q, options(4), answer, explanation, slide}."""
        transcript = self._check_transcript(transcript)
        n = max(self.MIN_ITEMS, int(n))
        user = (
            f"Redacta exactamente {n} preguntas de opción múltiple.\n"
            f"Diapositivas presentes en la transcripción: {_slides_in(transcript)}\n\n"
            f"{_block(transcript)}\n\nDevuelve SOLO el array JSON."
        )
        data = _extract_json(self._call(QUIZ_SYSTEM, user, json_mode=True))
        questions, seen = [], set()
        for item in _as_list(data):
            q = _norm_question(item)
            if q and q["q"].lower() not in seen:
                seen.add(q["q"].lower())
                questions.append(q)
        if len(questions) < self.MIN_ITEMS:
            raise RuntimeError("La IA no generó suficientes preguntas válidas. Inténtalo de nuevo.")
        return questions[:n]

    def tutor_answer(self, transcript: str, history: list[dict], question: str) -> str:
        """Responde a la pregunta del alumno usando la clase y los últimos 8 turnos."""
        transcript = self._check_transcript(transcript)
        question = _s(question)
        if not question:
            raise RuntimeError("Escribe una pregunta para el tutor")
        turns = []
        for m in (history or [])[-8:]:
            text = _s(m.get("text")) if isinstance(m, dict) else ""
            if not text:
                continue
            who = "Tutor" if m.get("role") == "assistant" else "Alumno"
            turns.append(f"{who}: {text}")
        convo = ("Conversación previa:\n" + "\n".join(turns) + "\n\n") if turns else ""
        user = f"{_block(transcript)}\n\n{convo}Pregunta del alumno: {question}"
        return _plain(self._call(TUTOR_SYSTEM, user))

    def knowledge_gaps(self, transcript: str, failed_questions: list[dict]) -> str:
        """Texto corto: qué conceptos repasar a partir de las preguntas falladas."""
        transcript = self._check_transcript(transcript)
        failed = [f for f in (failed_questions or []) if isinstance(f, dict) and _s(f.get("q"))]
        if not failed:
            return "No fallaste ninguna pregunta: no hay lagunas que repasar."
        lines = []
        for f in failed:
            slide = _norm_slide(f.get("slide"))
            where = f" (diapo {slide})" if slide else ""
            ans = _s(f.get("answer"))
            expl = _s(f.get("explanation"))
            lines.append(f"- {_s(f.get('q'))}{where}"
                         + (f" — Respuesta correcta: {ans}" if ans else "")
                         + (f" — Explicación: {expl}" if expl else ""))
        user = (
            f"{_block(transcript)}\n\n"
            f"Preguntas que el alumno ha fallado ({len(failed)}):\n" + "\n".join(lines) +
            "\n\nDevuelve SOLO la lista de lagunas con el formato indicado."
        )
        return _plain(self._call(GAPS_SYSTEM, user))

    def guided_lesson(self, transcript: str) -> str:
        """Lección guiada estructurada en secciones ━━ TÍTULO ━━."""
        transcript = self._check_transcript(transcript)
        user = (
            f"Diapositivas presentes en la transcripción: {_slides_in(transcript)}\n\n"
            f"{_block(transcript)}\n\nEscribe la lección guiada."
        )
        text = _plain(self._call(LESSON_SYSTEM, user))
        # Algunos modelos anteponen "Aquí tienes…": nos quedamos desde el primer título
        i = text.find("━━")
        return text[i:].strip() if i > 0 else text


# ── Hilo genérico para no bloquear la UI ─────────────────────────────────

class AITask(QThread):
    """Ejecuta fn(*args) en un hilo. done(resultado) si va bien, failed(mensaje) si no."""

    done   = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self._fn, self._args = fn, args

    def run(self):
        try:
            self.done.emit(self._fn(*self._args))
        except Exception as e:
            self.failed.emit(str(e) or e.__class__.__name__)
