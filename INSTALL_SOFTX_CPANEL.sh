#!/usr/bin/env bash
set -euo pipefail

# You can override these values by prefixing env vars before running:
# CPANEL_USER=unboxing APP_ROOT=/home/unboxing/pocketpro.corexbd.com bash INSTALL_SOFTX_CPANEL.sh
CPANEL_USER="${CPANEL_USER:-unboxing}"
APP_ROOT="${APP_ROOT:-/home/${CPANEL_USER}/pocketpro.corexbd.com}"
DATA_ROOT="${DATA_ROOT:-/home/${CPANEL_USER}/pocketpro_data}"
VENV_ACTIVATE="${VENV_ACTIVATE:-/home/${CPANEL_USER}/virtualenv/pocketpro.corexbd.com/3.11/bin/activate}"
VENV_PYTHON="${VENV_PYTHON:-${VENV_ACTIVATE%/activate}/python}"

echo "== Soft X cPanel installer =="
echo "CPANEL_USER:   ${CPANEL_USER}"
echo "APP_ROOT:      ${APP_ROOT}"
echo "DATA_ROOT:     ${DATA_ROOT}"
echo "VENV_ACTIVATE: ${VENV_ACTIVATE}"
echo "VENV_PYTHON:   ${VENV_PYTHON}"
echo

if [ ! -d "${APP_ROOT}" ]; then
  echo "ERROR: APP_ROOT not found: ${APP_ROOT}"
  exit 1
fi

cd "${APP_ROOT}"

echo "1) Fixing .htaccess Passenger block..."
if [ -f ".htaccess" ]; then
  TMP_HTACCESS="$(mktemp)"
  awk '
    BEGIN { skip=0 }
    /# DO NOT REMOVE\. CLOUDLINUX PASSENGER CONFIGURATION BEGIN/ { skip=1; next }
    /# DO NOT REMOVE\. CLOUDLINUX PASSENGER CONFIGURATION END/ {
      if (skip==1) { skip=0; next }
    }
    { if (skip==0) print $0 }
  ' ".htaccess" > "${TMP_HTACCESS}"

  {
    echo
    echo "# DO NOT REMOVE. CLOUDLINUX PASSENGER CONFIGURATION BEGIN"
    echo "PassengerAppRoot \"${APP_ROOT}\""
    echo "PassengerBaseURI \"/\""
    echo "PassengerPython \"${VENV_PYTHON}\""
    echo "# DO NOT REMOVE. CLOUDLINUX PASSENGER CONFIGURATION END"
  } >> "${TMP_HTACCESS}"
  mv "${TMP_HTACCESS}" ".htaccess"
fi

echo "2) Cleaning unnecessary files..."
rm -rf __MACOSX
rm -f .DS_Store
rm -f Inventory.zip
rm -f stderr.log
rm -rf .venv
rm -rf cgi-bin public tmp

echo "3) Verifying required files..."
for f in app.py passenger_wsgi.py requirements.txt; do
  if [ ! -f "${f}" ]; then
    echo "ERROR: Missing ${APP_ROOT}/${f}"
    exit 1
  fi
done

for d in templates static; do
  if [ ! -d "${d}" ]; then
    echo "ERROR: Missing ${APP_ROOT}/${d}/"
    exit 1
  fi
done

echo "4) Creating persistent data folders..."
mkdir -p "${DATA_ROOT}/backups"
mkdir -p "${DATA_ROOT}/pocketpro_tenants"

echo "5) Activating cPanel Python virtualenv..."
if [ ! -f "${VENV_ACTIVATE}" ]; then
  echo "ERROR: Virtualenv activate file not found:"
  echo "       ${VENV_ACTIVATE}"
  echo "Check Python App manager root/python version, then run again."
  exit 1
fi

# shellcheck disable=SC1090
source "${VENV_ACTIVATE}"

echo "6) Installing dependencies..."
pip install -r requirements.txt

echo "7) Initializing database on persistent path..."
INVENTORY_DB_PATH="${DATA_ROOT}/pocketpro.db" \
INVENTORY_BACKUP_DIR="${DATA_ROOT}/backups" \
SOFTX_ADMIN_DB_PATH="${DATA_ROOT}/pocketpro_admin.db" \
SOFTX_TENANT_DATA_DIR="${DATA_ROOT}/pocketpro_tenants" \
python - <<'PY'
import app
app.init_admin_db()
app.init_db()
print("DB init done")
PY

echo
echo "Install complete."
echo "Now set these Environment Variables in Python App Manager:"
echo "  INVENTORY_DB_PATH=${DATA_ROOT}/pocketpro.db"
echo "  INVENTORY_BACKUP_DIR=${DATA_ROOT}/backups"
echo "  SOFTX_ADMIN_DB_PATH=${DATA_ROOT}/pocketpro_admin.db"
echo "  SOFTX_TENANT_DATA_DIR=${DATA_ROOT}/pocketpro_tenants"
echo
echo "Then click RESTART in Python App Manager."
