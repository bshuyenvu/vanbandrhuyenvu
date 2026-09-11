from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from webapp.saas import api, store, workflow_api
from webapp.saas.security import create_session, hash_password


class ProductionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        self.old_secret = os.environ.get("VBHC_SESSION_SECRET")
        store.DB_PATH = Path(self.tmp.name) / "hardening.db"
        os.environ["VBHC_SESSION_SECRET"] = "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
        store.init_db()

    def tearDown(self):
        store.DB_PATH = self.old_db
        if self.old_secret is None:
            os.environ.pop("VBHC_SESSION_SECRET", None)
        else:
            os.environ["VBHC_SESSION_SECRET"] = self.old_secret
        self.tmp.cleanup()
    def _request(self, token: str) -> Request:
        return Request({
            "type": "http",
            "method": "GET",
            "path": "/api/v2/me",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "query_string": b"",
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "scheme": "http",
        })

    def test_wallet_bootstrap_is_idempotent(self):
        first = store.create_personal_wallet("usr_a", "free")
        second = store.create_personal_wallet("usr_a", "free")
        self.assertEqual(first["id"], second["id"])
        active = store.one("SELECT COUNT(*) n FROM subscriptions WHERE scope_type='user' AND scope_id='usr_a' AND status='active'")
        grants = store.one("SELECT COUNT(*) n FROM credit_ledger WHERE wallet_id=? AND event_type='signup_grant'", (first["id"],))
        self.assertEqual(active["n"], 1)
        self.assertEqual(grants["n"], 1)
    def test_personal_storage_quota_uses_plan(self):
        from webapp.saas.storage import storage_summary
        user_id = "usr_storage_plan"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)", (user_id,"storage@example.com",hash_password("MatKhau123!"),"Storage User","active"))
        store.create_personal_wallet(user_id, "free")
        org = store.ensure_personal_workspace(user_id, "Storage User", "storage@example.com")
        summary = storage_summary(user_id=user_id, organization_id=org["id"])
        self.assertEqual(summary["total_bytes"], 100 * 1024 * 1024)
        self.assertEqual(summary["used_bytes"], 0)

    def test_personal_storage_quota_blocks_overflow(self):
        from webapp.saas.storage import ensure_storage_capacity
        user_id = "usr_storage_limit"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)", (user_id,"limit@example.com",hash_password("MatKhau123!"),"Limit User","active"))
        store.create_personal_wallet(user_id, "free")
        org = store.ensure_personal_workspace(user_id, "Limit User", "limit@example.com")
        store.execute("INSERT INTO document_attachments(id,document_id,organization_id,original_name,stored_name,mime_type,size_bytes,sha256,uploaded_by) VALUES(?,?,?,?,?,?,?,?,?)", ("att_big","doc_big",org["id"],"big.pdf","big.pdf","application/pdf",99*1024*1024,"0"*64,user_id))
        with self.assertRaises(ValueError):
            ensure_storage_capacity(user_id=user_id, organization_id=org["id"], incoming_bytes=2*1024*1024)

    def test_session_version_revokes_old_sessions(self):
        user_id = "usr_secure"
        store.execute(
            "INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,?)",
            (user_id, "secure@example.com", hash_password("MatKhau123!"), "Secure User", "active", 0),
        )
        token = create_session({"uid": user_id, "email": "secure@example.com", "sv": 0})
        self.assertIsNotNone(api.current_user(self._request(token)))
        store.execute("UPDATE users SET session_version=1 WHERE id=?", (user_id,))
        self.assertIsNone(api.current_user(self._request(token)))

    def test_login_guard_blocks_repeated_failures(self):
        email = "guard@example.com"
        ip = "203.0.113.10"
        for _ in range(6):
            api._record_login_failure(email, ip)
        self.assertTrue(api._login_blocked(email, ip))
        api._clear_login_failures(email)
        self.assertFalse(api._login_blocked(email, ip))


    def test_me_provisions_personal_workspace_idempotently(self):
        user_id = "usr_personal_workspace"
        store.execute(
            "INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)",
            (user_id, "personal@example.com", hash_password("MatKhau123!"), "Personal User", "active"),
        )
        token = create_session({"uid": user_id, "email": "personal@example.com", "sv": 0})
        first = json.loads(asyncio.run(api.me(self._request(token))).body)
        second = json.loads(asyncio.run(api.me(self._request(token))).body)
        personal = [m for m in first["memberships"] if m.get("workspace_type") == "personal"]
        self.assertEqual(len(personal), 1)
        self.assertEqual(personal[0]["organization_name"], "Kho cá nhân — Personal User")
        self.assertEqual(first["memberships"], second["memberships"])
        self.assertEqual(store.one("SELECT COUNT(*) n FROM organizations WHERE owner_user_id=? AND workspace_type='personal'", (user_id,))["n"], 1)


    def test_free_monthly_grant_is_idempotent(self):
        wallet = store.create_personal_wallet("usr_free_period", "free")
        store.execute("UPDATE credit_ledger SET created_at='2000-01-01 00:00:00' WHERE wallet_id=? AND event_type='signup_grant'", (wallet["id"],))
        before = store.one("SELECT balance_credits FROM wallets WHERE id=?", (wallet["id"],))["balance_credits"]
        first = store.wallet_for(user_id="usr_free_period")
        second = store.wallet_for(user_id="usr_free_period")
        grants = store.one("SELECT COUNT(*) n FROM credit_ledger WHERE wallet_id=? AND reference_type='free_period'", (wallet["id"],))["n"]
        self.assertEqual(first["balance_credits"], before + 10000)
        self.assertEqual(second["balance_credits"], first["balance_credits"])
        self.assertEqual(grants, 1)

    def test_paid_usage_uses_subscription_period(self):
        from webapp.saas.usage import usage_summary
        wallet = store.create_personal_wallet("usr_paid_period", "free")
        store.set_subscription(scope_type="user", scope_id="usr_paid_period", plan_id="personal")
        sub = store.one("SELECT id FROM subscriptions WHERE scope_type='user' AND scope_id='usr_paid_period' AND status='active'")
        store.execute("UPDATE subscriptions SET starts_at='2026-09-01 00:00:00',ends_at='2099-10-01 00:00:00' WHERE id=?", (sub["id"],))
        for created, tokens in [("2026-08-31 23:59:59", 999), ("2026-09-02 00:00:00", 111)]:
            store.execute("INSERT INTO ai_usage(id,user_id,wallet_id,model_id,task_type,input_tokens,output_tokens,created_at) VALUES(?,?,?,?,?,?,?,?)",
                          (f"use_{tokens}","usr_paid_period",wallet["id"],"gpt-luna","draft",tokens,0,created))
        summary = usage_summary(user_id="usr_paid_period", plan_id="personal", wallet=wallet)
        self.assertEqual(summary["quota"]["requests_used"], 1)
        self.assertEqual(summary["quota"]["tokens_used"], 111)
        self.assertTrue(summary["quota"]["reset_at"].startswith("2099-10-01"))

    def test_team_plan_rejects_personal_workspace_scope(self):
        from webapp.saas import billing_api
        user_id = "usr_team_scope"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)", (user_id,"team@example.com",hash_password("MatKhau123!"),"Team User","active"))
        store.create_personal_wallet(user_id, "free")
        org = store.ensure_personal_workspace(user_id, "Team User", "team@example.com")
        token = create_session({"uid": user_id, "email": "team@example.com", "sv": 0})
        raw = json.dumps({"plan_id":"team","scope_type":"organization","scope_id":org["id"]}).encode()
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type":"http.request","body":b"","more_body":False}
            sent = True
            return {"type":"http.request","body":raw,"more_body":False}
        req = Request({"type":"http","method":"POST","path":"/api/v2/billing/orders","headers":[(b"authorization",f"Bearer {token}".encode()),(b"content-type",b"application/json")],"query_string":b"","client":("127.0.0.1",1234),"server":("test",80),"scheme":"http"}, receive)
        response = asyncio.run(billing_api.create_order(req))
        self.assertEqual(response.status_code, 409)
        data = json.loads(response.body)
        self.assertIn("Kho cá nhân", data["error"])


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        self.old_root = os.environ.get("VBHC_FILE_ROOT")
        self.old_secret = os.environ.get("VBHC_SESSION_SECRET")
        store.DB_PATH = Path(self.tmp.name) / "attachments.db"
        os.environ["VBHC_FILE_ROOT"] = str(Path(self.tmp.name) / "files")
        os.environ["VBHC_SESSION_SECRET"] = "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
        store.init_db()
        self.user_id = "usr_attach"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)", (self.user_id,"attach@example.com",hash_password("MatKhau123!"),"Attach User","active"))
        store.execute("INSERT INTO organizations(id,name,slug,owner_user_id,data_policy) VALUES(?,?,?,?,?)", ("org_attach","Org Attach","org-attach",self.user_id,"internal"))
        store.execute("INSERT INTO memberships(id,organization_id,user_id,role,status) VALUES(?,?,?,?,?)", ("mem_attach","org_attach",self.user_id,"organization_owner","active"))
        store.execute("INSERT INTO documents(id,organization_id,direction,document_type,standard,subject,status,owner_user_id) VALUES(?,?,?,?,?,?,?,?)", ("doc_attach","org_attach","incoming","gov_cong_van","government","Văn bản thử","received",self.user_id))
    def tearDown(self):
        store.DB_PATH = self.old_db
        if self.old_root is None:
            os.environ.pop("VBHC_FILE_ROOT", None)
        else:
            os.environ["VBHC_FILE_ROOT"] = self.old_root
        if self.old_secret is None:
            os.environ.pop("VBHC_SESSION_SECRET", None)
        else:
            os.environ["VBHC_SESSION_SECRET"] = self.old_secret
        self.tmp.cleanup()

    def _post_request(self, body: bytes) -> Request:
        token = create_session({"uid": self.user_id, "email": "attach@example.com", "sv": 0})
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({"type":"http","method":"POST","path":"/x","path_params":{"org_id":"org_attach","doc_id":"doc_attach"},"headers":[(b"authorization",f"Bearer {token}".encode()),(b"content-type",b"application/json")],"query_string":b"","client":("127.0.0.1",1234),"server":("test",80),"scheme":"http"}, receive)
    def test_upload_attachment_stores_private_file_and_hash(self):
        raw = b"%PDF-1.4\n% test attachment\n"
        payload = json.dumps({
            "filename": "van-ban-thu.pdf",
            "mime_type": "application/pdf",
            "file_base64": base64.b64encode(raw).decode(),
        }).encode()
        response = asyncio.run(workflow_api.upload_attachment(self._post_request(payload)))
        data = json.loads(response.body)
        self.assertEqual(response.status_code, 201)
        self.assertTrue(data["ok"])
        row = store.one("SELECT * FROM document_attachments WHERE id=?", (data["attachment"]["id"],))
        self.assertIsNotNone(row)
        target = Path(os.environ["VBHC_FILE_ROOT"]) / "org_attach" / "doc_attach" / row["stored_name"]
        self.assertEqual(target.read_bytes(), raw)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_disguised_attachment_is_rejected(self):
        payload = json.dumps({"filename":"fake.pdf","file_base64":base64.b64encode(b"MZ-not-a-pdf").decode()}).encode()
        response = asyncio.run(workflow_api.upload_attachment(self._post_request(payload)))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(store.one("SELECT COUNT(*) n FROM document_attachments")["n"], 0)


if __name__ == "__main__":
    unittest.main()

class IncomingRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        self.old_secret = os.environ.get("VBHC_SESSION_SECRET")
        store.DB_PATH = Path(self.tmp.name) / "register.db"
        os.environ["VBHC_SESSION_SECRET"] = "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
        store.init_db()
        self.uid = "usr_reg"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)", (self.uid,"reg@example.com",hash_password("MatKhau123!"),"Registry User","active"))
        store.execute("INSERT INTO organizations(id,name,slug,owner_user_id,data_policy) VALUES(?,?,?,?,?)", ("org_reg","Registry Org","registry-org",self.uid,"internal"))
        store.execute("INSERT INTO memberships(id,organization_id,user_id,role,status) VALUES(?,?,?,?,?)", ("mem_reg","org_reg",self.uid,"organization_owner","active"))
    def tearDown(self):
        store.DB_PATH = self.old_db
        if self.old_secret is None: os.environ.pop("VBHC_SESSION_SECRET", None)
        else: os.environ["VBHC_SESSION_SECRET"] = self.old_secret
        self.tmp.cleanup()
    def _req(self, body):
        token=create_session({"uid":self.uid,"email":"reg@example.com","sv":0}); raw=json.dumps(body).encode(); sent=False
        async def receive():
            nonlocal sent
            if sent: return {"type":"http.request","body":b"","more_body":False}
            sent=True; return {"type":"http.request","body":raw,"more_body":False}
        return Request({"type":"http","method":"POST","path":"/x","path_params":{"org_id":"org_reg"},"headers":[(b"authorization",f"Bearer {token}".encode()),(b"content-type",b"application/json")],"query_string":b"","client":("127.0.0.1",1),"server":("test",80),"scheme":"http"},receive)
    def test_incoming_sequence_and_decision_type(self):
        body={"direction":"incoming","document_type":"gov_quyet_dinh","subject":"Quyết định thử","source_number":"12/QĐ-UBND"}
        a=json.loads(asyncio.run(api.create_document(self._req(body))).body); b=json.loads(asyncio.run(api.create_document(self._req(body))).body)
        self.assertEqual((a["intake_number"],b["intake_number"]),(1,2)); self.assertEqual(a["register_year"],b["register_year"])
