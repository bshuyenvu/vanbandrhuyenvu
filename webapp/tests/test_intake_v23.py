import asyncio
import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from starlette.requests import Request

from webapp.saas import intake_api, store, workflow_api
from webapp.saas.security import create_session, hash_password


class IntakeV23Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        self.old_root = os.environ.get("VBHC_FILE_ROOT")
        self.old_mount = os.environ.get("VBHC_REQUIRED_STORAGE_MOUNT")
        self.old_secret = os.environ.get("VBHC_SESSION_SECRET")
        store.DB_PATH = Path(self.tmp.name) / "v23.db"
        os.environ["VBHC_FILE_ROOT"] = str(Path(self.tmp.name) / "files")
        os.environ.pop("VBHC_REQUIRED_STORAGE_MOUNT", None)
        os.environ["VBHC_SESSION_SECRET"] = "test-secret-0123456789-abcdefghijklmnopqrstuvwxyz"
        store.init_db()
        workflow_api.init_workflow_schema()
        self.uid = "usr_v23"
        self.org = "org_v23"
        store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)",
                      (self.uid, "v23@example.com", hash_password("MatKhau123!"), "Văn thư V23", "active"))
        store.execute("INSERT INTO organizations(id,name,slug,owner_user_id,status,data_policy,workspace_type,storage_quota_bytes) VALUES(?,?,?,?,?,?,?,?)",
                      (self.org, "Cơ quan V23", "co-quan-v23", self.uid, "active", "internal", "organization", 50 * 1024 * 1024))
        store.execute("INSERT INTO memberships(id,organization_id,user_id,role,status) VALUES(?,?,?,?,?)",
                      ("mem_v23", self.org, self.uid, "organization_owner", "active"))
        store.execute("INSERT INTO departments(id,organization_id,name,code,type,status) VALUES(?,?,?,?,?,?)",
                      ("dep_khth", self.org, "Phòng Kế hoạch tổng hợp", "KHTH", "department", "active"))
        self.token = create_session({"uid": self.uid, "email": "v23@example.com", "sv": 0})

    def tearDown(self):
        store.DB_PATH = self.old_db
        if self.old_root is None:
            os.environ.pop("VBHC_FILE_ROOT", None)
        else:
            os.environ["VBHC_FILE_ROOT"] = self.old_root
        if self.old_mount is None:
            os.environ.pop("VBHC_REQUIRED_STORAGE_MOUNT", None)
        else:
            os.environ["VBHC_REQUIRED_STORAGE_MOUNT"] = self.old_mount
        if self.old_secret is None:
            os.environ.pop("VBHC_SESSION_SECRET", None)
        else:
            os.environ["VBHC_SESSION_SECRET"] = self.old_secret
        self.tmp.cleanup()

    def request(self, body, path="/api/v2/x"):
        raw = json.dumps(body).encode()
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return Request({
            "type": "http", "method": "POST", "path": path,
            "path_params": {"org_id": self.org},
            "headers": [(b"authorization", f"Bearer {self.token}".encode()), (b"content-type", b"application/json")],
            "query_string": b"", "client": ("127.0.0.1", 1234), "server": ("test", 80), "scheme": "http",
        }, receive)

    def pdf_payload(self, name="van-ban.pdf"):
        raw = b"%PDF-1.4\n% Huyen Vu V23 test\n"
        return raw, {
            "filename": name, "mime_type": "application/pdf",
            "file_base64": base64.b64encode(raw).decode(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }

    def test_commit_is_atomic_and_exact_duplicate_is_blocked(self):
        raw, payload = self.pdf_payload()
        payload.update({
            "document_type": "gov_cong_van", "source_number": "12/CV-TEST",
            "sender": "Sở Y tế", "subject": "Văn bản thử tiếp nhận theo lô",
            "priority": "normal", "confidentiality": "internal",
            "department_id": "dep_khth", "metadata": {"document_date": "11/09/2026"},
        })
        first = asyncio.run(intake_api.commit_intake(self.request(payload)))
        data = json.loads(first.body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(data["intake_number"], 1)
        doc = store.one("SELECT * FROM documents WHERE id=?", (data["document_id"],))
        self.assertEqual(doc["status"], "assigned")
        attachment = store.one("SELECT * FROM document_attachments WHERE document_id=?", (data["document_id"],))
        self.assertEqual(attachment["sha256"], hashlib.sha256(raw).hexdigest())
        target = Path(os.environ["VBHC_FILE_ROOT"]) / self.org / data["document_id"] / attachment["stored_name"]
        self.assertEqual(target.read_bytes(), raw)
        second = asyncio.run(intake_api.commit_intake(self.request(payload)))
        second_data = json.loads(second.body)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second_data["code"], "duplicate_file")
        self.assertEqual(store.one("SELECT COUNT(*) n FROM documents")["n"], 1)

    def test_preflight_flags_metadata_duplicate(self):
        store.execute("""INSERT INTO documents(id,organization_id,direction,document_type,standard,source_number,sender,subject,status,owner_user_id,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("doc_old", self.org, "incoming", "gov_cong_van", "government", "55/CV-SYT",
             "Sở Y tế tỉnh", "Triển khai kế hoạch chuyển đổi số", "received", self.uid,
             json.dumps({"document_date": "10/09/2026"}, ensure_ascii=False)))
        body = {"sha256": "a" * 64, "size_bytes": 1000, "source_number": "55/CV-SYT",
                "sender": "Sở Y tế tỉnh", "subject": "Triển khai kế hoạch chuyển đổi số",
                "document_date": "10/09/2026"}
        response = asyncio.run(intake_api.preflight(self.request(body)))
        data = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(data["duplicates"]["exact"])
        self.assertEqual(data["duplicates"]["possible"][0]["id"], "doc_old")
        self.assertGreaterEqual(data["duplicates"]["possible"][0]["score"], 0.9)

    def test_routing_matches_ai_department_suggestion(self):
        response = asyncio.run(intake_api.suggest_routing(self.request({
            "analysis": {"suggested_department": "Phòng Kế hoạch Tổng hợp",
                         "subject": "Kế hoạch hoạt động năm 2026"}
        })))
        data = json.loads(response.body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data["suggestion"]["department_id"], "dep_khth")
        self.assertGreaterEqual(data["suggestion"]["confidence"], 0.8)


    def test_possible_duplicate_requires_human_confirmation(self):
        store.execute("""INSERT INTO documents(id,organization_id,direction,document_type,standard,source_number,sender,subject,status,owner_user_id,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", ("doc_candidate", self.org, "incoming", "gov_cong_van", "government",
            "77/CV-SYT", "Sở Y tế", "Báo cáo kết quả chuyển đổi số", "received", self.uid,
            json.dumps({"document_date": "11/09/2026"}, ensure_ascii=False)))
        _, payload = self.pdf_payload("candidate.pdf")
        payload.update({"document_type": "gov_cong_van", "source_number": "77/CV-SYT", "sender": "Sở Y tế",
                        "subject": "Báo cáo kết quả chuyển đổi số", "metadata": {"document_date": "11/09/2026"}})
        blocked = asyncio.run(intake_api.commit_intake(self.request(payload)))
        blocked_data = json.loads(blocked.body)
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked_data["code"], "possible_duplicate")
        self.assertEqual(store.one("SELECT COUNT(*) n FROM documents")["n"], 1)
        payload["duplicate_confirmed"] = True
        accepted = asyncio.run(intake_api.commit_intake(self.request(payload)))
        self.assertEqual(accepted.status_code, 201)
        self.assertEqual(store.one("SELECT COUNT(*) n FROM documents")["n"], 2)


if __name__ == "__main__":
    unittest.main()
