#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${1:-$HOME/softx.corexbd.com}"

if [ ! -d "$APP_ROOT" ]; then
  echo "App root not found: $APP_ROOT"
  exit 1
fi

cd "$APP_ROOT"

echo "Cleaning unnecessary files from: $APP_ROOT"
rm -rf __MACOSX
rm -f .DS_Store
rm -f Inventory.zip
rm -rf .venv

echo "Cleanup complete."
