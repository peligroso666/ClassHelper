@echo off
title Publicar ClassHelper en GitHub
echo.
echo  Se va a abrir el navegador para iniciar sesion en GitHub (solo la primera vez).
echo  Despues se crea el repositorio ClassHelper, se sube y se monta tu perfil.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0publicar.ps1"
echo.
pause
