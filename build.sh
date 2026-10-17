#!/bin/sh
# Build the app with PyInstaller (macOS: .app bundle; Windows: run the same command in
# Git Bash, or let the GitHub workflow do it). Output in build/dist/.
set -e
python -m pip install -r requirements.txt pyinstaller
# macOS: sign with a fixed local identity when the keychain has one, so the Accessibility
# permission (needed for p-locks) survives rebuilds; otherwise PyInstaller signs ad hoc.
SIGN=""
ID="${CODESIGN_IDENTITY:-BSP Machinedrum Local Signing}"
if command -v security >/dev/null; then
  HASH=$(security find-identity -p codesigning | awk -v id="$ID" 'index($0, id) {print $2; exit}')
  [ -n "$HASH" ] && SIGN="--codesign-identity=$HASH"    # the hash: no spaces to split
fi
python -m PyInstaller --noconfirm --windowed --name "BSP Machinedrum" $SIGN \
  --osx-bundle-identifier io.github.abarracu87.bsp-machinedrum \
  --hidden-import surface --hidden-import panel --hidden-import mdmm_bsp \
  --hidden-import mcp --hidden-import keys \
  --distpath build/dist --workpath build/work --specpath build app.py
# one signer for everything inside: a framework left with its old signature is refused at
# launch ("different Team IDs", 2026-10-17)
if [ -n "$SIGN" ]; then
  codesign --force --deep --sign "$HASH" "build/dist/BSP Machinedrum.app"
fi
