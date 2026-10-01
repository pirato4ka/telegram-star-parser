#!/usr/bin/env bash
# Сборка stars_parser (Linux/macOS) — для проверки перед сборкой .exe на Windows.
set -euo pipefail

python3 -m pip install -r requirements.txt
python3 -m pip install pyinstaller
python3 -m PyInstaller stars_parser.spec --noconfirm --clean

echo
echo "Готово: dist/stars_parser/stars_parser (one-dir)."
echo "Для Windows запускайте build_exe.bat — там получится dist/stars_parser.exe"
