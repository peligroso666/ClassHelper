@echo off
title ClassHelper - Compilando exe...
echo.
echo  ============================================
echo   ClassHelper - Generando ejecutable (.exe)
echo  ============================================
echo.

cd /d "%~dp0"

echo [1/4] Instalando PyInstaller...
python -m pip install pyinstaller --quiet
if %errorlevel% neq 0 ( echo ERROR instalando PyInstaller & pause & exit /b 1 )

echo [2/4] Compilando... (puede tardar 3-5 minutos)
python -m PyInstaller ClassHelper.spec --noconfirm --clean
if %errorlevel% neq 0 (
    echo.
    echo ERROR durante la compilacion.
    pause
    exit /b 1
)

echo [3/4] Copiando configuracion (key de IA y calidad elegida)...
if exist config.json copy /Y config.json "dist\ClassHelper\config.json" >nul

echo [4/4] Creando acceso directo en el Escritorio...
set "EXE=%~dp0dist\ClassHelper\ClassHelper.exe"
powershell -NoProfile -Command ^
  "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -COM WScript.Shell).CreateShortcut(\"$d\ClassHelper.lnk\"); $s.TargetPath='%EXE%'; $s.WorkingDirectory='%~dp0dist\ClassHelper'; $s.Description='Grabador de clases con transcripcion IA'; $s.Save()"

echo.
echo  ============================================
echo   LISTO!
echo   - Ejecutable:  dist\ClassHelper\ClassHelper.exe
echo   - Acceso directo actualizado en el Escritorio
echo  ============================================
echo.
pause
