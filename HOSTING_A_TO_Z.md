# Pocket Pro Hostinger / cPanel Hosting A-to-Z

Use these exact values in Python App manager:

- Python version: `3.11`
- Application root: `pocketpro.corexbd.com`
- Application URL: `pocketpro.corexbd.com` with path `/`
- Startup file: `passenger_wsgi.py`
- Entry point: `application`

Environment variables:

- `INVENTORY_DB_PATH=/home/unboxing/pocketpro_data/pocketpro.db`
- `INVENTORY_BACKUP_DIR=/home/unboxing/pocketpro_data/backups`
- `SOFTX_ADMIN_DB_PATH=/home/unboxing/pocketpro_data/pocketpro_admin.db`
- `SOFTX_TENANT_DATA_DIR=/home/unboxing/pocketpro_data/pocketpro_tenants`

## Terminal Commands

Run inside cPanel terminal:

```bash
mkdir -p /home/unboxing/pocketpro_data/backups
mkdir -p /home/unboxing/pocketpro_data/pocketpro_tenants
cd /home/unboxing/pocketpro.corexbd.com
bash hosting_cleanup.sh /home/unboxing/pocketpro.corexbd.com
pip install -r requirements.txt
```

Then restart the app from Python App manager.

## One-Command Installer (Recommended)

If `INSTALL_SOFTX_CPANEL.sh` exists in app root, run:

```bash
cd /home/unboxing/pocketpro.corexbd.com
bash INSTALL_SOFTX_CPANEL.sh
```

This script will clean unnecessary files, install packages, and initialize DB paths.

## Final File Check

These must exist in `/home/unboxing/pocketpro.corexbd.com`:

- `app.py`
- `passenger_wsgi.py`
- `requirements.txt`
- `.htaccess`
- `templates/` folder
- `static/` folder

Then open:

- `https://pocketpro.corexbd.com/`

If error remains, check `/home/unboxing/pocketpro.corexbd.com/stderr.log`.
