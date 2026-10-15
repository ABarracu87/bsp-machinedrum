#!/bin/sh
# Build the app with PyInstaller (macOS: .app bundle; Windows: run the same command in
# Git Bash, or let the GitHub workflow do it). Output in build/dist/.
set -e
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --windowed --name "BSP Machinedrum" \
  --osx-bundle-identifier io.github.abarracu87.bsp-machinedrum \
  --hidden-import surface --hidden-import panel --hidden-import mdmm_bsp \
  --hidden-import mcp --hidden-import keys \
  --distpath build/dist --workpath build/work --specpath build app.py
