"""
theme.py - Paleta y hoja de estilos global de ClassHelper.

Un único sitio para los colores: todos los módulos importan de aquí.
"""

# ── Paleta ────────────────────────────────────────────────────────────────
BG        = "#0f1117"   # fondo de la app
SURFACE   = "#161a23"   # barras, paneles
SURFACE_2 = "#1e2330"   # tarjetas, inputs
SURFACE_3 = "#262c3b"   # hover
BORDER    = "#2b3140"
TEXT      = "#e8eaf0"
MUTED     = "#8a90a3"
DIM       = "#5a6072"

ACCENT    = "#4f8cff"   # azul principal
ACCENT_H  = "#6a9dff"
RECORD    = "#ff4d5e"   # rojo grabación
RECORD_H  = "#ff6b79"
SUCCESS   = "#35c48d"   # verde
AI        = "#a06bff"   # morado IA
AI_H      = "#b284ff"
WARN      = "#ffb648"   # ámbar marcas
WARN_BG   = "#3a2a10"
SUCCESS_BG = "#12352a"  # fondo de badges verdes (dominio alto, acierto)
RECORD_BG  = "#4a1c24"  # fondo de badges rojos (fallo)
SUBJECT_BG = "#22304d"  # fondo de pastillas de asignatura / clase

FONT = '"Segoe UI Variable", "Segoe UI", sans-serif'


# ── Hoja de estilos global ────────────────────────────────────────────────
APP_QSS = f"""
* {{ font-family: {FONT}; font-size: 12px; }}

QMainWindow, QWidget {{ background: {BG}; color: {TEXT}; }}
QDialog {{ background: {BG}; }}

QLabel {{ background: transparent; border: none; }}

QPushButton {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 14px; font-weight: 600;
}}
QPushButton:hover    {{ background: {SURFACE_3}; border-color: #3a4152; }}
QPushButton:pressed  {{ background: {SURFACE}; }}
QPushButton:disabled {{ color: {DIM}; background: {SURFACE}; border-color: {SURFACE_2}; }}

QPushButton[accent="true"] {{
    background: {ACCENT}; color: white; border: none;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_H}; }}
QPushButton[accent="true"]:disabled {{ background: #2a3a5c; color: #6b7a99; }}

QPushButton[flat="true"] {{
    background: transparent; border: none; color: {MUTED};
}}
QPushButton[flat="true"]:hover {{ background: {SURFACE_2}; color: {TEXT}; }}

QComboBox {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px;
    padding: 5px 30px 5px 10px; min-height: 22px;
}}
QComboBox:hover {{ border-color: #3a4152; }}
QComboBox:disabled {{ color: {DIM}; }}
QComboBox::drop-down {{ border: none; width: 26px; }}
QComboBox::down-arrow {{
    image: none; width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid {MUTED}; margin-right: 10px;
}}
QComboBox QAbstractItemView {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px;
    selection-background-color: {ACCENT}; selection-color: white;
    padding: 4px; outline: none;
}}

QLineEdit {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 10px; selection-background-color: {ACCENT};
}}
QLineEdit:focus {{ border-color: {ACCENT}; }}

QTextEdit {{
    background: {BG}; color: {TEXT}; border: none;
    selection-background-color: {ACCENT};
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #333a4c; border-radius: 4px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #454d63; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: #333a4c; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:hover {{ background: {ACCENT}; }}

QStatusBar {{
    background: {SURFACE}; color: {MUTED};
    border-top: 1px solid {BORDER}; padding: 0 10px; font-size: 11px;
}}
QStatusBar::item {{ border: none; }}

QToolTip {{
    background: {SURFACE_2}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 6px 8px;
}}

QMessageBox {{ background: {SURFACE}; }}
QMessageBox QLabel {{ color: {TEXT}; font-size: 12px; }}
QMessageBox QPushButton {{ min-width: 80px; }}
"""

# Botón de grabación (dos estados)
BTN_RECORD_IDLE = f"""
QPushButton {{
    background: {SUCCESS}; color: #06281a; border: none; border-radius: 10px;
    font-size: 13px; font-weight: 700; padding: 0 22px; min-width: 150px;
}}
QPushButton:hover {{ background: #4bd6a0; }}
QPushButton:disabled {{ background: #1c3a2e; color: #3f6b58; }}
"""
BTN_RECORD_LIVE = f"""
QPushButton {{
    background: {RECORD}; color: white; border: none; border-radius: 10px;
    font-size: 13px; font-weight: 700; padding: 0 22px; min-width: 150px;
}}
QPushButton:hover {{ background: {RECORD_H}; }}
"""

BTN_AI_ON = f"""
QPushButton {{
    background: {AI}; color: white; border: none; border-radius: 8px;
    font-weight: 700; padding: 0 12px;
}}
QPushButton:hover {{ background: {AI_H}; }}
"""
BTN_AI_OFF = f"""
QPushButton {{
    background: {SURFACE_2}; color: {MUTED}; border: 1px dashed #3a4152;
    border-radius: 8px; padding: 0 12px;
}}
QPushButton:hover {{ color: {TEXT}; border-color: {AI}; }}
"""
