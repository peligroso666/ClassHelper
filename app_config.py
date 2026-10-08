"""
app_config.py - Configuración local de ClassHelper (config.json).

config.json no se versiona: cada usuario tiene el suyo y se crea solo la primera
vez que la app guarda algo. config.example.json documenta las claves.
"""

import json
import os
import sys

# Cuando corre como .exe (PyInstaller frozen) el config va junto al .exe;
# cuando corre como script Python va junto al .py
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")


def load() -> dict:
    """Devuelve config.json como dict ({} si no existe o está roto)."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_key(key: str, value) -> None:
    """Guarda una sola clave en config.json sin tocar el resto."""
    cfg = load()
    cfg[key] = value
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def is_first_run() -> bool:
    """True hasta que el usuario pasa por la bienvenida."""
    return not load().get("first_run_done", False)
