@echo off
chcp 65001 >nul
REM ── Сборка stars_parser.exe (Windows) ──────────────────────────────────────
REM Запускать из каталога проекта:  build_exe.bat

echo [1/3] Установка зависимостей...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller
if errorlevel 1 goto :error

echo [2/3] Сборка...
python -m PyInstaller stars_parser.spec --noconfirm --clean
if errorlevel 1 goto :error

echo [3/3] Готово.
echo.
echo Исполняемый файл: dist\stars_parser.exe
echo Положите conf.ini РЯДОМ с dist\stars_parser.exe и запустите его.
echo Сессия (anon.session), parser.log и папка output появятся там же.
goto :eof

:error
echo.
echo Сборка завершилась с ошибкой.
exit /b 1
