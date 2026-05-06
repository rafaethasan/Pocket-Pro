#!/usr/bin/env python3
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path


def main() -> None:
    base_dir = Path(__file__).resolve().parent
    admin_db_path = Path(os.getenv("SOFTX_ADMIN_DB_PATH", "").strip() or (base_dir / "softx_admin.db"))

    if not admin_db_path.exists():
        print(f"Admin DB not found: {admin_db_path}")
        return

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(admin_db_path) as conn:
        # login_attempts table may not exist in very old builds; fail safely.
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_attempts'"
        ).fetchone()
        if not exists:
            print("login_attempts table not found. Nothing to unlock.")
            return

        conn.execute(
            """
            UPDATE login_attempts
            SET fail_count = 0,
                blocked_until = NULL,
                updated_at = ?
            """,
            (now_text,),
        )
        conn.commit()

    print("Done. All login locks cleared.")
    print(f"Admin DB: {admin_db_path}")


if __name__ == "__main__":
    main()
