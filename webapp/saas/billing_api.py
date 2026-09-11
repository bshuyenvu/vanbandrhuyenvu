from __future__ import annotations

import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .api import current_user, org_role, platform_admin
from .catalog import PLANS
from .security import new_id
from .store import audit, connect


def _json(data: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _error(message: str, status: int = 400) -> JSONResponse:
    return _json({"ok": False, "error": message}, status)


def init_billing_schema() -> None:
    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS payment_orders(
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, scope_type TEXT NOT NULL,
            scope_id TEXT NOT NULL, plan_id TEXT NOT NULL, amount_vnd INTEGER NOT NULL,
            included_credits INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            payment_method TEXT NOT NULL DEFAULT 'manual_transfer', provider_reference TEXT,
            note TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, paid_at TEXT
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_payment_user ON payment_orders(user_id,created_at)")
        db.commit()


async def create_order(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    body = await request.json()
    plan_id = str(body.get("plan_id") or "")
    plan = PLANS.get(plan_id)
    if not plan or plan_id in {"free", "organization"}:
        return _error("Gói này không hỗ trợ tự mua")
    scope_type = str(body.get("scope_type") or "user")
    scope_id = str(body.get("scope_id") or user["id"])
    if scope_type == "user" and scope_id != user["id"]:
        return _error("Không thể mua gói cho tài khoản khác", 403)
    if scope_type == "organization":
        if not org_role(user["id"], scope_id) and not platform_admin(user):
            return _error("Bạn không thuộc cơ quan này", 403)
        if plan_id != "team":
            return _error("Tổ chức tự mua hiện chỉ hỗ trợ gói Team")
    if scope_type not in {"user", "organization"}:
        return _error("scope_type không hợp lệ")

    order_id = new_id("ord_")
    amount = int(plan["price_vnd"])
    credits = int(plan["included_credits"])
    with connect() as db:
        db.execute("""INSERT INTO payment_orders(id,user_id,scope_type,scope_id,plan_id,amount_vnd,included_credits,status,payment_method,note)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                   (order_id,user["id"],scope_type,scope_id,plan_id,amount,credits,"pending",body.get("payment_method") or "manual_transfer",body.get("note")))
        db.commit()
    audit(organization_id=scope_id if scope_type=="organization" else None,user_id=user["id"],action="billing.order.create",entity_type="payment_order",entity_id=order_id,after={"plan_id":plan_id,"amount_vnd":amount,"credits":credits})
    instructions = {
        "bank_name": os.getenv("VBHC_PAYMENT_BANK_NAME", ""),
        "account_number": os.getenv("VBHC_PAYMENT_ACCOUNT_NUMBER", ""),
        "account_name": os.getenv("VBHC_PAYMENT_ACCOUNT_NAME", ""),
        "transfer_content": f"HVAI {order_id[-12:].upper()}",
    }
    return _json({"ok": True, "order": {"id":order_id,"plan_id":plan_id,"amount_vnd":amount,"included_credits":credits,"status":"pending"}, "payment_instructions": instructions}, 201)


async def my_orders(request: Request):
    user = current_user(request)
    if not user:
        return _error("Chưa đăng nhập", 401)
    with connect() as db:
        rows = [dict(x) for x in db.execute("SELECT * FROM payment_orders WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user["id"],)).fetchall()]
    return _json({"ok": True, "orders": rows})


async def admin_orders(request: Request):
    user = current_user(request)
    if not user or not platform_admin(user):
        return _error("Chỉ Platform Admin", 403)
    status = request.query_params.get("status")
    with connect() as db:
        if status:
            rows = [dict(x) for x in db.execute("SELECT * FROM payment_orders WHERE status=? ORDER BY created_at DESC LIMIT 300", (status,)).fetchall()]
        else:
            rows = [dict(x) for x in db.execute("SELECT * FROM payment_orders ORDER BY created_at DESC LIMIT 300").fetchall()]
    return _json({"ok": True, "orders": rows})


async def confirm_order(request: Request):
    admin = current_user(request)
    if not admin or not platform_admin(admin):
        return _error("Chỉ Platform Admin được xác nhận thanh toán", 403)
    order_id = request.path_params["order_id"]
    body = await request.json()

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            db.rollback()
            return _error("Không tìm thấy đơn", 404)
        order = dict(row)
        if order["status"] == "paid":
            db.rollback()
            return _json({"ok": True, "order_id": order_id, "status": "paid", "idempotent": True})
        if order["status"] not in {"pending", "review"}:
            db.rollback()
            return _error(f"Không thể xác nhận đơn ở trạng thái {order['status']}", 409)

        # Lock order before changing subscription/wallet. A second confirmation cannot pass this update.
        changed = db.execute("UPDATE payment_orders SET status='processing' WHERE id=? AND status IN ('pending','review')", (order_id,)).rowcount
        if changed != 1:
            db.rollback()
            return _error("Đơn đang được xử lý bởi một phiên khác", 409)

        db.execute("UPDATE subscriptions SET status='replaced' WHERE scope_type=? AND scope_id=? AND status='active'", (order["scope_type"],order["scope_id"]))
        subscription_id = new_id("sub_")
        db.execute("INSERT INTO subscriptions(id,scope_type,scope_id,plan_id,status) VALUES(?,?,?,?,?)", (subscription_id,order["scope_type"],order["scope_id"],order["plan_id"],"active"))

        wallet = db.execute("SELECT * FROM wallets WHERE scope_type=? AND scope_id=?", (order["scope_type"],order["scope_id"])).fetchone()
        if wallet:
            wallet_id = wallet["id"]
            current_balance = int(wallet["balance_credits"] or 0)
        else:
            wallet_id = new_id("wal_")
            current_balance = 0
            db.execute("INSERT INTO wallets(id,scope_type,scope_id,balance_credits) VALUES(?,?,?,0)", (wallet_id,order["scope_type"],order["scope_id"]))

        delta = int(order["included_credits"])
        balance_after = current_balance + delta
        db.execute("UPDATE wallets SET balance_credits=? WHERE id=?", (balance_after,wallet_id))
        db.execute("INSERT INTO credit_ledger(id,wallet_id,delta_credits,balance_after,event_type,reference_type,reference_id,note) VALUES(?,?,?,?,?,?,?,?)",
                   (new_id("led_"),wallet_id,delta,balance_after,"plan_purchase","payment_order",order_id,f"Paid order {order_id} • {order['plan_id']}"))
        db.execute("UPDATE payment_orders SET status='paid',provider_reference=?,paid_at=CURRENT_TIMESTAMP WHERE id=?", (body.get("provider_reference"),order_id))
        db.commit()

    audit(organization_id=order["scope_id"] if order["scope_type"]=="organization" else None,user_id=admin["id"],action="billing.order.paid",entity_type="payment_order",entity_id=order_id,before={"status":order["status"]},after={"status":"paid","plan_id":order["plan_id"],"credited":delta,"balance_after":balance_after})
    return _json({"ok": True, "order_id": order_id, "status": "paid", "subscription_id": subscription_id, "wallet_id": wallet_id, "credited": delta, "balance_after": balance_after})


def routes() -> list[Route]:
    init_billing_schema()
    return [
        Route("/api/v2/billing/orders", create_order, methods=["POST"]),
        Route("/api/v2/billing/orders", my_orders, methods=["GET"]),
        Route("/api/v2/admin/payment-orders", admin_orders, methods=["GET"]),
        Route("/api/v2/admin/payment-orders/{order_id}/confirm", confirm_order, methods=["POST"]),
    ]
