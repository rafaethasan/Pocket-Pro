# Pocket Pro Dedicated Database Paths

Pocket Pro runtime currently uses SQLite, not MySQL.

That means:
- there is no SQL `username/password` used by the app runtime
- all data is saved into dedicated `.db` files
- if you later move to PostgreSQL/MySQL, then DB user/password will matter

## Recommended Pocket Pro database paths

Use these exact environment variables:

```env
INVENTORY_DB_PATH=/var/data/pocketpro.db
INVENTORY_BACKUP_DIR=/var/data/backups
SOFTX_ADMIN_DB_PATH=/var/data/pocketpro_admin.db
SOFTX_TENANT_DATA_DIR=/var/data/pocketpro_tenants
```

## File purpose

- `pocketpro.db`
  Main Pocket Pro application data
- `pocketpro_admin.db`
  Admin and account-level data
- `pocketpro_tenants/`
  Tenant-specific database files
- `backups/`
  Generated backup files

## Important note

Do not hardcode hosting or mail passwords into app files.
If you later migrate Pocket Pro to PostgreSQL or MySQL, set the SQL user/password only in hosting environment variables or your database panel.

## Old User Migration

Pocket Pro old user/account নতুন runtime-এ আনতে:

1. old runtime-এ export run করুন:
```bash
python3 app.py --export-pocket-runtime
```

2. generated zip নতুন server-এ নিন

3. নতুন runtime-এ import run করুন:
```bash
python3 app.py --import-pocket-runtime --runtime-package /absolute/path/pocketpro-runtime-YYYYMMDD-HHMMSS.zip
```

এই import main DB + admin DB + tenant DB folder restore করবে, তাই old user-রা same account দিয়ে login করতে পারবে।
