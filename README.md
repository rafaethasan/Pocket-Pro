# Soft X - Wholesale Mobile Inventory SaaS

Mobile/Electronics/Grocery/Clothing + Pocket Money multi-module inventory, sales, profit এবং finance tracking এর জন্য Flask + SQLite SaaS app।

## Features
- Product In (IMEI, purchase price, wholesale price, retail price)
- Duplicate IMEI block
- Camera photo auto-fill (Brand/Model/Variant/Color OCR assist)
- Bulk same-model intake (multiple IMEI rows / paste list)
- Supplier management + supplier-wise purchase ledger
- Customer/shop management + day/month/year/lifetime ledger
- Customer detail page থেকে quick bulk sale (IMEI list)
- Wholesale/Retail sale entry
- প্রতি ফোন profit auto calculation
- Brand/category/customer-wise profit রিপোর্ট
- Daily, monthly, yearly, lifetime profit summary
- Return flow: IMEI দিয়ে wholesale cancel + stock/warehouse restock
- IMEI lookup: full phone profile + sale/return history
- Mobile camera দিয়ে IMEI barcode scan (browser supported হলে)
- CSV export + print-friendly reports
- Backup center + daily auto backup
- Optional Google Drive backup sync
- Super Admin Panel: shop account create/manage
- Shop-wise login system (username/password)
- Tenant-wise আলাদা database (প্রতি client data isolate)
- Single shop + multi-branch ready foundation (`branches` table included)
- English + Bangla UI switch (`/set-language/en` / `/set-language/bn`)
- Business module setup per tenant (Mobile Wholesale, Electronics, Grocery, Clothing, Pocket Money)
- Tracking code mode auto by business: IMEI / Serial Number / Item Code
- Tenant admin `Shop Settings` page (`/shop-settings`) for module + language update
- Login brute-force protection (attempt limit + temporary block)
- Super admin optional OTP (`SOFTX_SUPERADMIN_OTP`)
- Central security audit logs (`/admin/audit`)
- Subscription automation: due/expired reminder generation
- Payment gateway webhook collector API (`/api/billing/webhook`)

## Multi-Tenant Login (Admin + Shop)
- Super Admin Login URL: `/admin/login`
- Shop Login URL: `/login`
- Super Admin panel থেকে নতুন shop account তৈরি করুন
- প্রতিটি shop account আলাদা DB file পায় (`tenants/<username>.db`)
- Shop Login এ 3 field: `Shop ID + User ID + Password`
- Tenant side role system: `ADMIN`, `MANAGER`, `CASHIER`
- Tenant admin page: `/team-users` (manager/cashier user create + role update + password reset)
- Monthly billing supported: fee, paid-until, collect bill, auto service expiry block

### Default Super Admin Credential
- Username: `admin`
- Password: `921514@d2K`

Production এ অবশ্যই env দিয়ে change করুন:
- `SOFTX_SUPERADMIN_USER`
- `SOFTX_SUPERADMIN_PASS`
- `SOFTX_SUPERADMIN_OTP` (optional, set করলে OTP field required হবে)
- Optional admin db path: `SOFTX_ADMIN_DB_PATH`
- Optional tenant db dir: `SOFTX_TENANT_DATA_DIR`
- `SOFTX_MAX_LOGIN_ATTEMPTS` (default 5)
- `SOFTX_LOGIN_BLOCK_MINUTES` (default 15)
- `SOFTX_SESSION_SECURE` (`1` for HTTPS secure cookie)
- `SOFTX_BILLING_WEBHOOK_SECRET` (gateway collection secret)
- `SOFTX_BILLING_NOTIFY_WEBHOOK` (optional reminder webhook URL)

## Monthly Billing Flow
1. Super Admin panel এ account create করার সময় `Monthly Fee` এবং `Initial Billing Months` দিন।
2. System auto `Paid Until` set করবে।
3. প্রতি মাসে bill collect করতে Shop Accounts table থেকে `Collect Bill` ব্যবহার করুন।
4. বিল মেয়াদ শেষ হলে shop owner login blocked হবে, bill collect করলেই service resume হবে।
5. Admin dashboard থেকে monthly/yearly/lifetime collection, payer list, due/expired account list দেখতে পারবেন।
6. `Run Automation` ব্যবহার করে due/expired reminder তৈরি করতে পারবেন।

## Payment Webhook (Gateway Integration)
Server-to-server POST endpoint:
- `/api/billing/webhook`

Sample JSON:
```json
{
  "secret": "YOUR_SOFTX_BILLING_WEBHOOK_SECRET",
  "shop_username": "rahim_telecom",
  "months": 1,
  "amount": 500,
  "tx_ref": "BKASH-INV-2001",
  "gateway": "BKASH",
  "note": "Monthly payment"
}
```

Response এ নতুন `paid_until` পাওয়া যাবে এবং bill auto-collect হয়ে যাবে।

## Tenant Role Access
- `ADMIN`: Full access (inventory, reports, backup, team user management)
- `MANAGER`: Operations + reports (product/sale/return/customer/report/lookup)
- `CASHIER`: Fast sales only (dashboard, sales, tracking-code lookup)

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

তারপর ব্রাউজারে খুলুন:
`http://127.0.0.1:5000`

## Offline Desktop Build (.exe / .dmg)
- Guide: `DESKTOP_OFFLINE_A_TO_Z_BN.md`
- Launcher: `desktop/softx_desktop.py`
- Windows build script: `desktop/build_windows_exe.bat`
- macOS build script: `desktop/build_mac_dmg.sh`

Desktop launcher `SOFTX_OFFLINE_MODE=1` সেট করে local DB path auto configure করে।

## Offline Android APK (Full Source)
- Use this single package: `softx_android_offline_full_source.zip`
- Android project folder: `android_softx_offline/`
- Build guide (Bangla): `android_softx_offline/APK_BUILD_A_TO_Z_BN.md`
- Features in mobile app source:
  - Super admin (`admin` / `921514@d2K`)
  - Shop create + monthly billing tracking
  - Role login (`ADMIN`, `MANAGER`, `CASHIER`)
  - Offline inventory/sales/return workflow
  - Daily/monthly/yearly/lifetime profit cards
  - Bangla/English toggle
  - Barcode scan button (camera)

## Data
- SQLite file: `inventory.db`
- Admin account DB: `softx_admin.db`
- Tenant DB folder: `tenants/`
- প্রথম রানেই database table auto-create বা migrate হবে।
- Backup folder: `backups/`

## Pocket Pro Dedicated SQLite Setup
Pocket Pro-কে আলাদা runtime data file-এ রাখতে recommended names:

- Main DB: `pocketpro.db`
- Admin DB: `pocketpro_admin.db`
- Tenant DB folder: `pocketpro_tenants/`
- Backup folder: `backups/`

Recommended env:

- `INVENTORY_DB_PATH=/var/data/pocketpro.db`
- `INVENTORY_BACKUP_DIR=/var/data/backups`
- `SOFTX_ADMIN_DB_PATH=/var/data/pocketpro_admin.db`
- `SOFTX_TENANT_DATA_DIR=/var/data/pocketpro_tenants`

## Pocket Pro Old User Migration
যদি `app.corexbd.com` থেকে old Pocket Pro user/account নতুন `pocketpro.corexbd.com` runtime-এ আনতে চান, এখন full runtime export/import command আছে।

Old runtime থেকে export:
```bash
python3 app.py --export-pocket-runtime
```

এতে একটা zip হবে:
- main DB
- admin DB
- সব tenant DB files

New Pocket Pro runtime-এ import:
```bash
python3 app.py --import-pocket-runtime --runtime-package /absolute/path/pocketpro-runtime-YYYYMMDD-HHMMSS.zip
```

Import-এর আগে system automatic safety backup নেওয়ার চেষ্টা করবে।
এই migration করলে old user login, tenant mapping, আর account data নতুন runtime-এ carry হবে।

## Quick Demo (Mobile সহ)
1. একই Wi-Fi তে laptop + mobile রাখুন
2. চালান:
```bash
python3 app.py --host 0.0.0.0 --port 5000
```
3. laptop IP বের করে mobile থেকে খুলুন: `http://YOUR_LAN_IP:5000`

## Deploy (Render)
1. এই project GitHub এ push করুন
2. Render এ নতুন `Web Service` তৈরি করুন (GitHub repo connect)
   - or use Render `Blueprint` with `render.yaml`
3. Build command:
```bash
pip install -r requirements.txt
```
4. Start command:
```bash
gunicorn app:app
```
5. SQLite persist রাখতে Render disk mount path ব্যবহার করুন এবং env set করুন:
   - `INVENTORY_DB_PATH=/var/data/pocketpro.db`
   - `INVENTORY_BACKUP_DIR=/var/data/backups`
   - `SOFTX_ADMIN_DB_PATH=/var/data/pocketpro_admin.db`
   - `SOFTX_TENANT_DATA_DIR=/var/data/pocketpro_tenants`
6. Repo already includes:
   - `render.yaml`
   - `runtime.txt`

## Backup Commands
Manual backup:
```bash
python3 app.py --backup
```

Backup + Google Drive sync (optional config required):
```bash
python3 app.py --backup --sync-google
```

## Optional Google Drive Config
1. `pip install google-api-python-client google-auth`
2. Env সেট করুন:
   - `GOOGLE_SERVICE_ACCOUNT_FILE=/absolute/path/service-account.json`
   - `GOOGLE_DRIVE_FOLDER_ID=<drive_folder_id>`
3. Auto daily backup এ Google sync চাইলে:
   - `AUTO_GOOGLE_BACKUP=1`

## Soft X Enterprise Blueprint (New)
For scaling to 100+ modules and large multi-tenant load, see:
- `docs/SOFTX_ENTERPRISE_V1_BLUEPRINT.md`
- `docs/SOFTX_DB_AND_TENANCY.md`
- `docs/SOFTX_SECURITY_BASELINE.md`
- `docs/SOFTX_SRE_RUNBOOK.md`
- `docs/SOFTX_DEPLOYMENT_PROFILES.md`
- `docs/SOFTX_MODULE_DEV_GUIDE.md`
- `docs/SOFTX_PHASE1_POSTGRES_REDIS_QUEUE.md`
- `docs/SOFTX_TALLY_ODOO_GAP_ANALYSIS.md`
- `docs/SOFTX_ENTERPRISE_MODULES_PHASE1_IMPLEMENTED.md`
- `docs/SOFTX_MONEY_MODULE_STRUCTURE.md`

## Phase-1 Infra Commands (New)
Redis queue worker:
```bash
python app.py --worker
```

PostgreSQL main migration:
```bash
python app.py --migrate-postgres --postgres-schema softx
```

Sync all active tenants to PostgreSQL:
```bash
python app.py --sync-tenants-postgres
```

Apply index hardening on all tenant DBs:
```bash
python app.py --harden-tenant-indexes
```

Module scaffold and validation:
- `platform/README.md`
- `platform/schemas/module_manifest.schema.json`
- `platform/modules/example_inventory/module.json`
- `platform/modules/example_sales/module.json`
- `platform/tools/validate_manifest.py`
