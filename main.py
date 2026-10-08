"""
main.py - Punto de entrada de ClassHelper.
"""

import os
import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
import theme
from window import MainWindow


def selftest(wav_path: str, out_path: str, model: str = "small"):
    """
    ClassHelper.exe --selftest audio.wav salida.txt [modelo]
    Transcribe un WAV con la misma función que usa la app y escribe el texto.
    Sirve para comprobar que el .exe compilado lleva todo (VAD, ctranslate2…).
    """
    import time
    import wave
    import numpy as np
    from audio_thread import AudioWorker, load_whisper

    t0 = time.time()
    try:
        with wave.open(wav_path) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        audio = audio.astype(np.float32) / 32767
        worker = AudioWorker(model_name=model)
        worker.model, device = load_whisper(model)
        text = worker._transcribe(audio[: 16000 * 29])
        result = f"OK {time.time() - t0:.1f}s ({device})\n{text}"
    except Exception as e:
        import traceback
        result = "FAIL\n" + traceback.format_exc()
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result)


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        selftest(sys.argv[2], sys.argv[3], *(sys.argv[4:5] or []))
        os._exit(0)

    app = QApplication(sys.argv)
    app.setApplicationName("ClassHelper")
    app.setApplicationDisplayName("ClassHelper")
    # Fusion respeta la hoja de estilos igual en todos los Windows;
    # aplicarla a nivel de app llega también a diálogos y mensajes.
    app.setStyle("Fusion")
    app.setStyleSheet(theme.APP_QSS)
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    icono = os.path.join(base, "assets", "icon.png")
    if os.path.exists(icono):
        from PySide6.QtGui import QIcon
        app.setWindowIcon(QIcon(icono))

    window = MainWindow()
    window.show()
    code = app.exec()
    # Si el modelo Whisper aún se estaba cargando/descargando al cerrar, su hilo sigue
    # vivo y Qt abortaría al destruirlo. La sesión ya se guardó en closeEvent → salir ya.
    os._exit(code)


if __name__ == "__main__":
    main()
