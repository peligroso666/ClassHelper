"""
ai_enhancer.py - Post-procesado con IA con contexto rolling.

Proveedores soportados:
  - Ollama   : LLM local, GRATIS, sin API key (llama3.2, mistral, etc.)
  - Gemini   : Google, GRATIS hasta 15 req/min (necesita key gratuita)
  - Claude   : Anthropic, de pago (necesita key)
  - OpenAI   : GPT-4o mini, de pago (necesita key)

Característica clave:
  enhance_with_context(raw, context) — el LLM recibe el contexto de lo
  transcrito antes para poder completar frases cortadas, conectar ideas
  y entender el flujo de la clase.
"""

import json
import os

from app_config import CONFIG_FILE

# Variables de entorno que sustituyen a config.json (así la key no vive en el repo)
ENV_PROVIDER     = "CLASSHELPER_AI_PROVIDER"
ENV_API_KEY      = "CLASSHELPER_AI_API_KEY"
ENV_OLLAMA_MODEL = "CLASSHELPER_OLLAMA_MODEL"

# Cadenas de modelos Gemini gratuitos. Cada modelo tiene su propia cuota: si uno
# responde 429 se aparta un rato y se usa el siguiente. Medido con una key
# gratuita: con 2.5-flash-lite agotado, 3.5-flash-lite y flash-lite-latest
# seguían respondiendo, y 3.5-flash-lite corrige mucho mejor el vocabulario técnico.
GEMINI_CHUNK_MODELS = [          # un fragmento cada ~25 s: rápidos y baratos
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
]
GEMINI_LONG_MODELS = [           # resumen, tarjetas, test, tutor: pocas llamadas, más calidad
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
]
QUOTA_COOLDOWN_S = 15 * 60       # tiempo que se aparta un modelo sin cuota
MISSING_COOLDOWN_S = 24 * 3600   # modelo que no existe para esta key


class QuotaExhausted(RuntimeError):
    """Todos los modelos de la cadena están sin cuota ahora mismo."""


def _is_quota_error(e) -> bool:
    msg = str(e).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg or "rate limit" in msg


def _is_missing_model(e) -> bool:
    msg = str(e).lower()
    return "404" in msg or "not found" in msg or "is not supported" in msg

# ── Prompt del sistema ────────────────────────────────────────────────────

SYSTEM_PROMPT = """Tu única tarea es PUNTUAR Y LIMPIAR transcripciones automáticas de Whisper de clases universitarias de telecomunicaciones / electrónica en español.

REGLAS ABSOLUTAS — léelas dos veces:

1. Devuelve EXCLUSIVAMENTE el texto mejorado. NADA MÁS.
2. PROHIBIDO añadir "Nota:", "He completado:", "Aquí tienes:", comentarios meta o explicaciones de lo que has hecho.
3. PROHIBIDO inventar palabras o conceptos. Si una palabra está mal pero no puedes adivinarla con certeza, DÉJALA como está.
4. PROHIBIDO reemplazar términos técnicos por traducciones o sinónimos. Si dice "Kuroda" déjalo "Kuroda", no "curoda". Si dice "stub" déjalo "stub". Si dice "Smith" déjalo "Smith".
5. PROHIBIDO añadir contenido que no estaba en el fragmento original — no inventes fórmulas, definiciones, ni explicaciones.
6. PROHIBIDO usar markdown, negritas, cursivas, bullets, listas numeradas.
7. PROHIBIDO repetir el contexto anterior — solo devuelve el fragmento nuevo.

LO QUE SÍ DEBES HACER:
- Añadir puntos, comas, signos de interrogación, mayúsculas.
- Conectar la primera frase del fragmento con el contexto anterior si quedó cortada.
- Corregir errores OBVIOS de Whisper (homófonos, palabras pegadas, ortografía mal).
- Mantener el orden y significado EXACTO del original.

Términos técnicos reconocibles (déjalos tal cual aparecen): amplificador operacional, op-amp, ganancia, ancho de banda, filtro paso bajo/alto/banda, modulación AM/FM, transformada de Fourier, impedancia, decibelios, dB, frecuencia de corte, realimentación, oscilador, BJT, MOSFET, PCB, demodulación, Nyquist, convolución, función de transferencia, diagrama de Smith, línea de transmisión, microcinta, bobina, condensador, identidades de Kuroda, transformación de Richard, stub, lambda, beta L, longitud de onda."""


def _user_prompt(raw_text: str, context: str) -> str:
    if context:
        return (
            f"Contexto anterior (ya procesado, NO lo repitas):\n"
            f'"""\n{context}\n"""\n\n'
            f"Nuevo fragmento de Whisper (puede tener errores y cortes):\n"
            f'"""\n{raw_text}\n"""\n\n'
            "Devuelve solo el fragmento nuevo mejorado y conectado con el contexto."
        )
    else:
        return (
            f"Fragmento de Whisper (primer fragmento de la clase):\n"
            f'"""\n{raw_text}\n"""\n\n'
            "Devuelve el fragmento mejorado con gramática y puntuación correctas."
        )


# ── Clase principal ───────────────────────────────────────────────────────

class AIEnhancer:
    """
    Mejora transcripciones con contexto rolling para conectar fragmentos.
    """

    # La cuota es por key, así que el enfriamiento se comparte entre instancias
    _cooldown: dict = {}         # modelo -> instante hasta el que no se usa
    last_error = ""              # último problema de la IA (vacío si fue bien)
    last_model = ""              # modelo que respondió la última vez

    def __init__(self):
        self.provider   = "none"
        self.api_key    = ""
        self.ollama_model = "llama3.2"
        self._client    = None
        # Contexto rolling: últimos ~800 chars de transcripción procesada
        self._context   = ""
        self._load_config()

    # ── Config ───────────────────────────────────────────────────────────

    def _load_config(self):
        """Lee config.json; las variables de entorno CLASSHELPER_* tienen prioridad."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.provider      = cfg.get("ai_provider", "none")
                self.api_key       = cfg.get("ai_api_key", "")
                self.ollama_model  = cfg.get("ollama_model", "llama3.2")
            except Exception:
                pass
        self.provider     = os.environ.get(ENV_PROVIDER) or self.provider
        self.api_key      = os.environ.get(ENV_API_KEY) or self.api_key
        self.ollama_model = os.environ.get(ENV_OLLAMA_MODEL) or self.ollama_model

    def save_config(self):
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                pass
        cfg["ai_provider"]   = self.provider
        # Una key que viene del entorno no se copia a disco
        if self.api_key != os.environ.get(ENV_API_KEY):
            cfg["ai_api_key"] = self.api_key
        cfg["ollama_model"]  = self.ollama_model
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)

    def configure(self, provider: str, api_key: str, ollama_model: str = "llama3.2"):
        self.provider      = provider
        self.api_key       = api_key
        self.ollama_model  = ollama_model
        self._client       = None
        self._gemini_models = {}
        AIEnhancer._cooldown.clear()     # key nueva: cuotas nuevas
        self.save_config()

    def reset_context(self):
        """Llamar al inicio de una nueva sesión de grabación."""
        self._context = ""

    # ── API pública ──────────────────────────────────────────────────────

    def is_active(self) -> bool:
        if self.provider == "ollama":
            return True                  # Ollama no necesita key
        return self.provider != "none" and bool(self.api_key)

    def enhance(self, raw_text: str) -> str:
        """
        Mejora raw_text usando el contexto de fragmentos anteriores.
        Actualiza el contexto interno para el siguiente fragmento.
        """
        if not self.is_active() or not raw_text.strip():
            self._update_context(raw_text)
            return raw_text

        try:
            result = self._call_provider(raw_text, self._context)
            result = self._strip_meta(result)
            self.last_error = ""
            # Si la IA "mejora" demasiado (texto >2.5x más largo) probablemente alucinó
            if not result or len(result) > max(120, len(raw_text) * 2.5):
                self._update_context(raw_text)
                return raw_text
            self._update_context(result)
            return result
        except Exception as e:
            # El texto de Whisper se queda tal cual; el aviso va a la barra de estado
            self.last_error = ("sin cuota gratuita ahora mismo" if _is_quota_error(e)
                               else str(e)[:120])
            self._update_context(raw_text)
            return raw_text

    @staticmethod
    def _strip_meta(text: str) -> str:
        """
        Elimina líneas de tipo 'Nota:', 'Aquí tienes:', 'He completado:',
        '```', '**', etc. que algunos LLMs cuelan en la respuesta.
        """
        if not text:
            return text
        # Quitar bloques de código markdown
        text = text.replace("```", "")
        # Quitar líneas que empiecen por meta-comentarios
        bad_starts = (
            "nota:", "note:", "aquí tienes", "aqui tienes",
            "he completado", "he conectado", "he corregido",
            "respuesta:", "fragmento mejorado:", "texto mejorado:",
            "explicación:", "explicacion:", "comentario:",
            "**nota", "*nota", "---",
        )
        clean_lines = []
        for line in text.split("\n"):
            low = line.strip().lower()
            if any(low.startswith(b) for b in bad_starts):
                continue
            clean_lines.append(line)
        # Quitar negritas markdown sin perder el texto
        result = "\n".join(clean_lines).strip()
        result = result.replace("**", "")
        # Si empieza/acaba con comillas que envuelven todo, quitarlas
        if len(result) > 2 and result[0] in '"“' and result[-1] in '"”':
            result = result[1:-1].strip()
        return result

    def _update_context(self, text: str):
        """Mantiene ventana deslizante de ~800 chars del texto más reciente."""
        combined = (self._context + " " + text).strip()
        if len(combined) > 800:
            combined = combined[-800:]
            # No cortar a mitad de palabra
            first_space = combined.find(" ")
            if first_space > 0:
                combined = combined[first_space:].strip()
        self._context = combined

    # ── Proveedores ──────────────────────────────────────────────────────

    def _call_provider(self, raw_text: str, context: str) -> str:
        prompt = _user_prompt(raw_text, context)
        if self.provider == "ollama":
            return self._call_ollama(prompt)
        elif self.provider == "gemini":
            return self._call_gemini(prompt)
        elif self.provider == "claude":
            return self._call_claude(prompt)
        elif self.provider == "openai":
            return self._call_openai(prompt)
        return raw_text

    def _call_ollama(self, prompt: str) -> str:
        """Llama al servidor Ollama local (gratis, sin API key)."""
        from openai import OpenAI
        if self._client is None:
            self._client = OpenAI(
                base_url="http://localhost:11434/v1",
                api_key="ollama",          # placeholder, Ollama lo ignora
            )
        resp = self._client.chat.completions.create(
            model=self.ollama_model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()

    def _call_gemini(self, prompt: str) -> str:
        """Fragmento a fragmento con la cadena de modelos flash-lite (gratis, en la nube)."""
        return self._gemini_generate(GEMINI_CHUNK_MODELS, SYSTEM_PROMPT, prompt,
                                     temperature=0.0, max_tokens=2048)

    def _gemini_generate(self, models: list, system: str, prompt: str,
                         temperature: float, max_tokens: int, json_mode: bool = False) -> str:
        """
        Prueba los modelos en orden. Un 429 aparta ese modelo QUOTA_COOLDOWN_S y pasa
        al siguiente; un modelo inexistente se aparta un día. Otros errores se lanzan.
        """
        import time
        import google.generativeai as genai
        cache = getattr(self, "_gemini_models", None)
        if cache is None:
            cache = self._gemini_models = {}
        if not cache:
            genai.configure(api_key=self.api_key)

        now = time.time()
        tried_any = False
        last_exc = None
        for name in models:
            if AIEnhancer._cooldown.get(name, 0) > now:
                continue
            tried_any = True
            key = (name, system)
            if key not in cache:
                cache[key] = genai.GenerativeModel(model_name=name, system_instruction=system)
            try:
                config = {"temperature": temperature, "max_output_tokens": max_tokens}
                if json_mode:
                    # Obliga a Gemini a devolver JSON válido (sin esto, 3.5-flash
                    # se dejaba comillas de apertura y las tarjetas no se podían leer)
                    config["response_mime_type"] = "application/json"
                r = cache[key].generate_content(prompt, generation_config=config)
                text = (r.text or "").strip()     # .text lanza ValueError si no hay texto
            except ValueError as e:               # respuesta vacía / cortada: probar otro
                last_exc = e
                continue
            except Exception as e:
                if _is_quota_error(e):
                    AIEnhancer._cooldown[name] = now + QUOTA_COOLDOWN_S
                    last_exc = e
                    continue
                if _is_missing_model(e):
                    AIEnhancer._cooldown[name] = now + MISSING_COOLDOWN_S
                    last_exc = e
                    continue
                raise
            if text:
                self.last_model = name
                return text
        if not tried_any or (last_exc is not None and _is_quota_error(last_exc)):
            waits = [t - now for t in (AIEnhancer._cooldown.get(m, 0) for m in models) if t > now]
            mins = max(1, int(min(waits) // 60)) if waits else 1
            raise QuotaExhausted(
                f"429 quota: todos los modelos gratuitos de Gemini están sin cuota; "
                f"se reintenta en ~{mins} min"
            )
        raise RuntimeError(f"Gemini no devolvió texto ({last_exc})")

    def _call_claude(self, prompt: str) -> str:
        import anthropic
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self.api_key)
        msg = self._client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()

    def _call_openai(self, prompt: str) -> str:
        from openai import OpenAI
        if self._client is None:
            self._client = OpenAI(api_key=self.api_key)
        resp = self._client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            temperature=0.0,
        )
        return resp.choices[0].message.content.strip()

    # ── Resumen de clase ─────────────────────────────────────────────────

    SUMMARY_SYSTEM = (
        "Eres un asistente especializado en resumir clases universitarias de "
        "ingeniería electrónica y telecomunicaciones.\n\n"
        "Dado el texto completo de una clase, genera un resumen estructurado en "
        "español con estas secciones:\n\n"
        "━━ TEMAS DE LA CLASE ━━\n"
        "• [lista de temas principales tratados]\n\n"
        "━━ CONCEPTOS CLAVE ━━\n"
        "• concepto: breve descripción de una línea\n\n"
        "━━ FÓRMULAS Y RELACIONES ━━\n"
        "• [fórmulas, circuitos o relaciones matemáticas mencionadas; omite si no hay]\n\n"
        "━━ RESUMEN ━━\n"
        "[resumen narrativo de 3-5 párrafos explicando el hilo de la clase]\n\n"
        "Sé conciso pero completo. Responde solo en español."
    )

    def generate_summary(self, full_transcription: str) -> tuple[bool, str]:
        """Genera un resumen estructurado de toda la clase. Devuelve (ok, texto)."""
        if not self.is_active():
            return False, "IA no activa"
        if not full_transcription.strip():
            return False, "Transcripción vacía"
        user = f'Transcripción de la clase:\n"""\n{full_transcription}\n"""'
        try:
            result = self._call_with_system(self.SUMMARY_SYSTEM, user)
            return True, result
        except Exception as e:
            return False, str(e)

    def _call_with_system(self, system: str, user: str, json_mode: bool = False) -> str:
        """Llama al proveedor activo con un system prompt personalizado (para resumen)."""
        if self.provider == "ollama":
            from openai import OpenAI
            c = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
            r = c.chat.completions.create(
                model=self.ollama_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
                temperature=0.3,
            )
            return r.choices[0].message.content.strip()
        elif self.provider == "gemini":
            # Resumen y modo estudio: pocas llamadas, modelos más potentes primero
            return self._gemini_generate(GEMINI_LONG_MODELS, system, user,
                                         temperature=0.3, max_tokens=8192, json_mode=json_mode)
        elif self.provider == "claude":
            import anthropic
            c = anthropic.Anthropic(api_key=self.api_key)
            r = c.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return r.content[0].text.strip()
        elif self.provider == "openai":
            from openai import OpenAI
            c = OpenAI(api_key=self.api_key)
            r = c.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "system", "content": system},
                          {"role": "user",   "content": user}],
                temperature=0.3,
            )
            return r.choices[0].message.content.strip()
        return ""

    # ── Test de conexión ─────────────────────────────────────────────────

    def test_connection(self) -> tuple[bool, str]:
        """Devuelve (ok, mensaje)."""
        test_raw = "el amplificador operacional tiene ganancia infinita en configuracion ideal"
        try:
            result = self._call_provider(test_raw, "")
            if result and result != test_raw:
                return True, result
            return True, "(respuesta igual al original — puede ser normal)"
        except Exception as e:
            return False, str(e)
