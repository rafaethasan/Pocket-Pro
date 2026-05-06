#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path


def normalize_username(value: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "", value.strip().lower().replace(" ", "_"))
    return clean or "shop"


def looks_bad_path(raw_path: str) -> bool:
    normalized = (raw_path or "").strip().replace("\\", "/")
    if not normalized:
        return True
    if normalized.startswith("/Users/"):
        return True
    if normalized.upper().startswith("C:/"):
        return True
    if "/Documents/Inventory" in normalized:
        return True
    if not normalized.endswith(".db"):
        return True
    return False


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    admin_db_path = Path(os.getenv("SOFTX_ADMIN_DB_PATH", "").strip() or (base_dir / "softx_admin.db"))
    tenant_dir = Path(os.getenv("SOFTX_TENANT_DATA_DIR", "").strip() or (base_dir / "tenants"))
    tenant_dir.mkdir(parents=True, exist_ok=True)

    if not admin_db_path.exists():
        print(f"Admin DB not found: {admin_db_path}")
        return

    fixed = 0
    with sqlite3.connect(admin_db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, username, shop_name, db_path FROM tenant_accounts").fetchall()
        for row in rows:
            current_path = str(row["db_path"] or "")
            username = normalize_username(str(row["username"] or ""))
            if not username:
                username = normalize_username(str(row["shop_name"] or "shop"))
            target = tenant_dir / f"{username}.db"
            if looks_bad_path(current_path) or current_path.strip() != str(target):
                conn.execute(
                    "UPDATE tenant_accounts SET db_path = ? WHERE id = ?",
                    (str(target), int(row["id"])),
                )
                fixed += 1
        conn.commit()

    print(f"Done. Updated tenant path rows: {fixed}")
    print(f"Admin DB: {admin_db_path}")
    print(f"Tenant dir: {tenant_dir}")


if __name__ == "__main__":
    main()
