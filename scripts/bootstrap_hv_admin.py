#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webapp.saas.security import hash_password, new_id  # noqa: E402
from webapp.saas.store import connect, create_personal_wallet, init_db  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap Platform Admin — Huyền Vũ Văn Bản AI")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="Platform Admin")
    args = parser.parse_args()
    email = args.email.strip().lower()
    password = getpass.getpass("Mật khẩu admin (>=8 ký tự): ")
    confirm = getpass.getpass("Nhập lại mật khẩu: ")
    if password != confirm:
        raise SystemExit("Mật khẩu xác nhận không khớp")

    init_db()
    hashed = hash_password(password)
    with connect() as db:
        row = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if row:
            user_id = row["id"]
            db.execute("UPDATE users SET password_hash=?,full_name=?,status='active' WHERE id=?", (hashed,args.name,user_id))
        else:
            user_id = new_id("usr_")
            db.execute("INSERT INTO users(id,email,password_hash,full_name,status) VALUES(?,?,?,?,?)", (user_id,email,hashed,args.name,"active"))
        db.commit()
    create_personal_wallet(user_id, "professional")
    print("OK — tài khoản admin đã active:", email)
    print("Thêm email này vào /etc/vbhc-web.env:")
    print(f"VBHC_PLATFORM_ADMIN_EMAILS={email}")


if __name__ == "__main__":
    main()
