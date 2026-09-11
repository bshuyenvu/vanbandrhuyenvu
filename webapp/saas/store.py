from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .catalog import MODEL_DEFAULTS, PLANS, quote_usage
from .security import new_id

DB_PATH = Path(os.getenv("VBHC_DEV_DB", Path(__file__).resolve().parents[2] / "data" / "huyenvu-vbai.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,full_name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',session_version INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS organizations(id TEXT PRIMARY KEY,name TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,owner_user_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',data_policy TEXT NOT NULL DEFAULT 'internal',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS departments(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,parent_id TEXT,name TEXT NOT NULL,code TEXT,type TEXT NOT NULL DEFAULT 'department',status TEXT NOT NULL DEFAULT 'active');
CREATE TABLE IF NOT EXISTS memberships(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,user_id TEXT NOT NULL,department_id TEXT,role TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS wallets(id TEXT PRIMARY KEY,scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,balance_credits INTEGER NOT NULL DEFAULT 0,monthly_budget_credits INTEGER,hard_limit_credits INTEGER,UNIQUE(scope_type,scope_id));
CREATE TABLE IF NOT EXISTS credit_ledger(id TEXT PRIMARY KEY,wallet_id TEXT NOT NULL,delta_credits INTEGER NOT NULL,balance_after INTEGER NOT NULL,event_type TEXT NOT NULL,reference_type TEXT,reference_id TEXT,note TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS subscriptions(id TEXT PRIMARY KEY,scope_type TEXT NOT NULL,scope_id TEXT NOT NULL,plan_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP,starts_at TEXT DEFAULT CURRENT_TIMESTAMP,ends_at TEXT);
CREATE TABLE IF NOT EXISTS documents(id TEXT PRIMARY KEY,organization_id TEXT NOT NULL,department_id TEXT,direction TEXT NOT NULL,document_type TEXT NOT NULL,standard TEXT NOT NULL,register_number TEXT,source_number TEXT,symbol TEXT,sender TEXT,recipient TEXT,subject TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'draft',priority TEXT NOT NULL DEFAULT 'normal',confidentiality TEXT NOT NULL DEFAULT 'internal',deadline TEXT,parent_document_id TEXT,owner_user_id TEXT,intake_number INTEGER,register_year INTEGER,received_at TEXT,metadata TEXT NOT NULL DEFAULT '{}',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS document_versions(id TEXT PRIMARY KEY,document_id TEXT NOT NULL,version INTEGER NOT NULL,content_text TEXT NOT NULL DEFAULT '',created_by TEXT,ai_model_id TEXT,change_note TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(document_id,version));
CREATE TABLE IF NOT EXISTS document_attachments(id TEXT PRIMARY KEY,document_id TEXT NOT NULL,organization_id TEXT NOT NULL,original_name TEXT NOT NULL,stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,uploaded_by TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_doc_attachments_doc ON document_attachments(document_id,created_at);
CREATE TABLE IF NOT EXISTS ai_models(id TEXT PRIMARY KEY,provider TEXT NOT NULL,model_name TEXT NOT NULL,display_name TEXT NOT NULL,tier TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,input_usd_per_million REAL NOT NULL DEFAULT 0,cached_input_usd_per_million REAL NOT NULL DEFAULT 0,output_usd_per_million REAL NOT NULL DEFAULT 0,service_multiplier REAL NOT NULL DEFAULT 1.3,daily_budget_usd REAL,config TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS ai_usage(id TEXT PRIMARY KEY,user_id TEXT,organization_id TEXT,department_id TEXT,wallet_id TEXT,model_id TEXT,task_type TEXT NOT NULL,input_tokens INTEGER NOT NULL DEFAULT 0,cached_tokens INTEGER NOT NULL DEFAULT 0,output_tokens INTEGER NOT NULL DEFAULT 0,provider_cost_usd REAL NOT NULL DEFAULT 0,charged_credits INTEGER NOT NULL DEFAULT 0,request_id TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,organization_id TEXT,user_id TEXT,action TEXT NOT NULL,entity_type TEXT NOT NULL,entity_id TEXT,before_data TEXT,after_data TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS auth_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT,email TEXT,ip TEXT NOT NULL,success INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as db:
        db.executescript(SCHEMA)
        user_cols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
        if "session_version" not in user_cols:
            db.execute("ALTER TABLE users ADD COLUMN session_version INTEGER NOT NULL DEFAULT 0")
        doc_cols = {r[1] for r in db.execute("PRAGMA table_info(documents)").fetchall()}
        for col, ddl in (("intake_number", "INTEGER"), ("register_year", "INTEGER"), ("received_at", "TEXT")):
            if col not in doc_cols:
                db.execute(f"ALTER TABLE documents ADD COLUMN {col} {ddl}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_intake_unique ON documents(organization_id,register_year,intake_number) WHERE direction='incoming' AND intake_number IS NOT NULL")
        sub_cols = {r[1] for r in db.execute("PRAGMA table_info(subscriptions)").fetchall()}
        if "starts_at" not in sub_cols:
            db.execute("ALTER TABLE subscriptions ADD COLUMN starts_at TEXT")
            db.execute("UPDATE subscriptions SET starts_at=created_at WHERE starts_at IS NULL")
        if "ends_at" not in sub_cols:
            db.execute("ALTER TABLE subscriptions ADD COLUMN ends_at TEXT")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auth_attempts_email_time ON auth_attempts(email,created_at)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_auth_attempts_ip_time ON auth_attempts(ip,created_at)")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_credit_reference_unique ON credit_ledger(wallet_id,reference_type,reference_id) WHERE reference_type IS NOT NULL AND reference_id IS NOT NULL")
        db.execute("UPDATE subscriptions SET status='expired' WHERE status='active' AND ends_at IS NOT NULL AND ends_at<=CURRENT_TIMESTAMP")
        db.execute("""UPDATE subscriptions SET status='replaced'
                      WHERE status='active' AND id NOT IN (
                        SELECT id FROM (
                          SELECT s1.id FROM subscriptions s1
                          WHERE s1.status='active'
                            AND s1.rowid=(SELECT s2.rowid FROM subscriptions s2
                                         WHERE s2.scope_type=s1.scope_type AND s2.scope_id=s1.scope_id AND s2.status='active'
                                         ORDER BY COALESCE(s2.starts_at,s2.created_at) DESC,s2.rowid DESC LIMIT 1)
                        )
                      )""")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_active_subscription_scope ON subscriptions(scope_type,scope_id) WHERE status='active'")
        db.execute("DELETE FROM auth_attempts WHERE created_at < datetime('now','-2 days')")
        for model_id, m in MODEL_DEFAULTS.items():
            db.execute("""INSERT OR IGNORE INTO ai_models(id,provider,model_name,display_name,tier,input_usd_per_million,cached_input_usd_per_million,output_usd_per_million,service_multiplier,config) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                       (model_id,m["provider"],m["model_name"],m["display_name"],m["tier"],float(m["input_usd_per_million"]),float(m["cached_input_usd_per_million"]),float(m["output_usd_per_million"]),float(m["service_multiplier"]),"{}"))
        db.commit()


def one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with connect() as db:
        row = db.execute(sql, params).fetchone()
        return dict(row) if row else None


def all_rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with connect() as db:
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def execute(sql: str, params: tuple = ()) -> None:
    with connect() as db:
        db.execute(sql, params)
        db.commit()


def create_personal_wallet(user_id: str, plan_id: str = "free") -> dict[str, Any]:
    plan = PLANS.get(plan_id, PLANS["free"])
    wallet_id = new_id("wal_")
    balance = int(plan["included_credits"])
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user_id,)).fetchone()
        active_sub = db.execute("SELECT id FROM subscriptions WHERE scope_type='user' AND scope_id=? AND status='active' AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP) ORDER BY COALESCE(starts_at,created_at) DESC LIMIT 1", (user_id,)).fetchone()
        if not active_sub:
            db.execute("INSERT INTO subscriptions(id,scope_type,scope_id,plan_id,status) VALUES(?,?,?,?,?)", (new_id("sub_"),"user",user_id,plan_id,"active"))
        if existing:
            db.commit()
            return dict(existing)
        db.execute("INSERT INTO wallets(id,scope_type,scope_id,balance_credits) VALUES(?,?,?,?)", (wallet_id,"user",user_id,balance))
        db.execute("INSERT INTO credit_ledger(id,wallet_id,delta_credits,balance_after,event_type,note) VALUES(?,?,?,?,?,?)", (new_id("led_"),wallet_id,balance,balance,"signup_grant",f"Free plan welcome credits: {balance}"))
        db.commit()
        return dict(db.execute("SELECT * FROM wallets WHERE id=?", (wallet_id,)).fetchone())


def active_plan(scope_type: str, scope_id: str) -> str:
    row = one("SELECT plan_id FROM subscriptions WHERE scope_type=? AND scope_id=? AND status='active' AND (ends_at IS NULL OR ends_at>CURRENT_TIMESTAMP) ORDER BY COALESCE(starts_at,created_at) DESC LIMIT 1", (scope_type,scope_id))
    return str(row["plan_id"]) if row else "free"


def _ensure_free_monthly_grant(user_id: str, wallet: dict[str, Any]) -> dict[str, Any]:
    if active_plan("user", user_id) != "free":
        return wallet
    now = datetime.now(timezone.utc)
    period_key = f"free:{now:%Y-%m}"
    start = f"{now:%Y-%m}-01 00:00:00"
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT id FROM credit_ledger WHERE wallet_id=? AND reference_type='free_period' AND reference_id=?", (wallet["id"],period_key)).fetchone()
        if existing:
            db.commit()
            return dict(db.execute("SELECT * FROM wallets WHERE id=?", (wallet["id"],)).fetchone())
        signup_this_month = db.execute("SELECT id FROM credit_ledger WHERE wallet_id=? AND event_type='signup_grant' AND created_at>=? LIMIT 1", (wallet["id"],start)).fetchone()
        delta = 0 if signup_this_month else int(PLANS["free"]["included_credits"] or 0)
        current = db.execute("SELECT balance_credits FROM wallets WHERE id=?", (wallet["id"],)).fetchone()
        balance = int(current["balance_credits"] if current else 0)
        after = balance + delta
        if delta:
            db.execute("UPDATE wallets SET balance_credits=? WHERE id=?", (after,wallet["id"]))
        db.execute("INSERT INTO credit_ledger(id,wallet_id,delta_credits,balance_after,event_type,reference_type,reference_id,note) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id("led_"),wallet["id"],delta,after,"monthly_free_grant","free_period",period_key,"Free monthly AI Credit" if delta else "Signup grant covers first Free period"))
        db.commit()
        return dict(db.execute("SELECT * FROM wallets WHERE id=?", (wallet["id"],)).fetchone())


def wallet_for(*, user_id: str, organization_id: str | None = None) -> dict[str, Any]:
    if organization_id:
        row = one("SELECT * FROM wallets WHERE scope_type='organization' AND scope_id=?", (organization_id,))
        if row:
            return row
    row = one("SELECT * FROM wallets WHERE scope_type='user' AND scope_id=?", (user_id,))
    wallet = row or create_personal_wallet(user_id, active_plan("user", user_id))
    return _ensure_free_monthly_grant(user_id, wallet)


def ensure_credit(wallet: dict[str, Any], minimum: int = 1) -> None:
    if int(wallet.get("balance_credits") or 0) < minimum:
        raise ValueError("AI Credit đã hết. Vui lòng nâng gói hoặc nạp thêm credit.")


def charge_ai_usage(*, user_id: str, organization_id: str | None, department_id: str | None,
                    provider: str, model_name: str, task_type: str, usage: dict[str, int],
                    request_id: str | None = None) -> dict[str, Any]:
    wallet = wallet_for(user_id=user_id, organization_id=organization_id)
    model = one("SELECT * FROM ai_models WHERE provider=? AND model_name=? AND enabled=1", (provider,model_name))
    if not model:
        model = one("SELECT * FROM ai_models WHERE model_name=? AND enabled=1", (model_name,))
    if not model:
        raise ValueError(f"Model chưa được đăng ký/đang bị tắt: {provider}/{model_name}")
    quote = quote_usage(
        input_tokens=int(usage.get("input_tokens") or 0),
        cached_tokens=int(usage.get("cached_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        model=model,
        usd_vnd=Decimal(os.getenv("VBHC_USD_VND", "27000")),
        reserve_multiplier=Decimal(os.getenv("VBHC_COST_RESERVE", "1.10")),
    )
    credits = int(quote.charged_credits)
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT balance_credits FROM wallets WHERE id=?", (wallet["id"],)).fetchone()
        balance = int(row["balance_credits"] if row else 0)
        if balance < credits:
            raise ValueError(f"Không đủ AI Credit: cần {credits}, hiện có {balance}")
        after = balance - credits
        db.execute("UPDATE wallets SET balance_credits=? WHERE id=?", (after,wallet["id"]))
        usage_id = new_id("use_")
        db.execute("""INSERT INTO ai_usage(id,user_id,organization_id,department_id,wallet_id,model_id,task_type,input_tokens,cached_tokens,output_tokens,provider_cost_usd,charged_credits,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (usage_id,user_id,organization_id,department_id,wallet["id"],model["id"],task_type,int(usage.get("input_tokens") or 0),int(usage.get("cached_tokens") or 0),int(usage.get("output_tokens") or 0),float(quote.provider_cost_usd),credits,request_id))
        db.execute("INSERT INTO credit_ledger(id,wallet_id,delta_credits,balance_after,event_type,reference_type,reference_id,note) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id("led_"),wallet["id"],-credits,after,"ai_usage","ai_usage",usage_id,f"{provider}/{model_name} • {task_type}"))
        db.commit()
    return {"usage_id":usage_id,"charged_credits":credits,"balance_after":after,"provider_cost_usd":float(quote.provider_cost_usd)}


def adjust_wallet(*, wallet_id: str, delta: int, event_type: str, note: str = "") -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT balance_credits FROM wallets WHERE id=?", (wallet_id,)).fetchone()
        if not row:
            raise ValueError("Không tìm thấy ví")
        after = int(row["balance_credits"]) + int(delta)
        if after < 0:
            raise ValueError("Số dư không thể âm")
        db.execute("UPDATE wallets SET balance_credits=? WHERE id=?", (after,wallet_id))
        db.execute("INSERT INTO credit_ledger(id,wallet_id,delta_credits,balance_after,event_type,note) VALUES(?,?,?,?,?,?)", (new_id("led_"),wallet_id,int(delta),after,event_type,note))
        db.commit()
    return {"wallet_id":wallet_id,"balance_after":after}


def set_subscription(*, scope_type: str, scope_id: str, plan_id: str) -> dict[str, Any]:
    if plan_id not in PLANS:
        raise ValueError("Gói không hợp lệ")
    sub_id = new_id("sub_")
    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE subscriptions SET status='replaced' WHERE scope_type=? AND scope_id=? AND status='active'", (scope_type,scope_id))
        ends_expr = None if plan_id in {"free", "organization"} else "+30 days"
        if ends_expr:
            db.execute("INSERT INTO subscriptions(id,scope_type,scope_id,plan_id,status,starts_at,ends_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,datetime('now',?))", (sub_id,scope_type,scope_id,plan_id,"active",ends_expr))
        else:
            db.execute("INSERT INTO subscriptions(id,scope_type,scope_id,plan_id,status,starts_at,ends_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,NULL)", (sub_id,scope_type,scope_id,plan_id,"active"))
        db.commit()
    return {"subscription_id":sub_id,"plan_id":plan_id}


def audit(*, organization_id: str | None, user_id: str | None, action: str, entity_type: str, entity_id: str | None = None, before: Any = None, after: Any = None) -> None:
    execute("INSERT INTO audit_logs(organization_id,user_id,action,entity_type,entity_id,before_data,after_data) VALUES(?,?,?,?,?,?,?)",
            (organization_id,user_id,action,entity_type,entity_id,json.dumps(before,ensure_ascii=False) if before is not None else None,json.dumps(after,ensure_ascii=False) if after is not None else None))
