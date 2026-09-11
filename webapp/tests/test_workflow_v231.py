import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from starlette.requests import Request

from webapp.saas import api, store, work_api, workflow_api
from webapp.saas.security import create_session, hash_password


class WorkflowV231Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = store.DB_PATH
        self.old_secret = os.environ.get("VBHC_SESSION_SECRET")
        store.DB_PATH = Path(self.tmp.name) / "workflow.db"
        os.environ["VBHC_SESSION_SECRET"] = "workflow-test-secret-0123456789-abcdefgh"
        store.init_db()
        workflow_api.init_workflow_schema()
        self.org = "org_flow"
        self.owner = "usr_owner"
        self.member = "usr_member"
        self.other = "usr_other"
        for uid,email,name in ((self.owner,"owner@example.com","Chủ sở hữu"),(self.member,"member@example.com","Chuyên viên"),(self.other,"other@example.com","Người khác")):
            store.execute("INSERT INTO users(id,email,password_hash,full_name,status,session_version) VALUES(?,?,?,?,?,0)",
                          (uid,email,hash_password("MatKhau123!"),name,"active"))
        store.execute("INSERT INTO organizations(id,name,slug,owner_user_id,status,data_policy,workspace_type) VALUES(?,?,?,?,?,?,?)",
                      (self.org,"Cơ quan thử nghiệm","co-quan-flow",self.owner,"active","internal","organization"))
        store.execute("INSERT INTO departments(id,organization_id,name,code,type,status) VALUES(?,?,?,?,?,?)",
                      ("dep_a",self.org,"Phòng Tổng hợp","TH","department","active"))
        for mid,uid,role in (("mem_o",self.owner,"organization_owner"),("mem_m",self.member,"member"),("mem_x",self.other,"member")):
            store.execute("INSERT INTO memberships(id,organization_id,user_id,department_id,role,status) VALUES(?,?,?,?,?,?)",
                          (mid,self.org,uid,"dep_a",role,"active"))
        self.member_token = create_session({"uid":self.member,"email":"member@example.com","sv":0})
        self.owner_token = create_session({"uid":self.owner,"email":"owner@example.com","sv":0})

    def tearDown(self):
        store.DB_PATH = self.old_db
        if self.old_secret is None:
            os.environ.pop("VBHC_SESSION_SECRET",None)
        else:
            os.environ["VBHC_SESSION_SECRET"] = self.old_secret
        self.tmp.cleanup()

    def request(self, token, body=None, doc_id=None):
        raw = b"" if body is None else json.dumps(body).encode()
        sent = False
        async def receive():
            nonlocal sent
            if sent:
                return {"type":"http.request","body":b"","more_body":False}
            sent = True
            return {"type":"http.request","body":raw,"more_body":False}
        params = {"org_id":self.org}
        if doc_id:
            params["doc_id"] = doc_id
        return Request({"type":"http","method":"POST" if body is not None else "GET","path":"/api/v2/test",
                        "path_params":params,"headers":[(b"authorization",f"Bearer {token}".encode()),(b"content-type",b"application/json")],
                        "query_string":b"","client":("127.0.0.1",1234),"server":("test",80),"scheme":"http"},receive)

    def add_doc(self, doc_id, status="processing", owner=None, due=None, assignee=None):
        store.execute("""INSERT INTO documents(id,organization_id,department_id,direction,document_type,standard,subject,status,priority,confidentiality,owner_user_id,deadline,metadata)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (doc_id,self.org,"dep_a","incoming","gov_cong_van","government",f"Hồ sơ {doc_id}",status,"normal","internal",owner or self.owner,due,"{}"))
        if assignee:
            store.execute("""INSERT INTO document_assignments(id,document_id,organization_id,department_id,assignee_user_id,status,due_at,assigned_by)
                VALUES(?,?,?,?,?,?,?,?)""",
                ("asg_"+doc_id,doc_id,self.org,"dep_a",assignee,"assigned",due,self.owner))

    def test_deadline_states_use_vietnam_time(self):
        now = datetime.now(work_api.VN_TZ)
        self.assertEqual(work_api._deadline_state(now-timedelta(hours=2),"processing",now)[0],"qua_han")
        self.assertEqual(work_api._deadline_state(now+timedelta(hours=12),"processing",now)[0],"sap_han_24h")
        self.assertEqual(work_api._deadline_state(now+timedelta(hours=36),"processing",now)[0],"sap_han_48h")
        self.assertEqual(work_api._deadline_state(now+timedelta(hours=72),"processing",now)[0],"binh_thuong")

    def test_member_only_sees_assigned_work(self):
        now = datetime.now(work_api.VN_TZ)
        self.add_doc("doc_mine",due=(now-timedelta(hours=1)).isoformat(),assignee=self.member)
        self.add_doc("doc_other",due=(now+timedelta(hours=8)).isoformat(),assignee=self.other)
        response = asyncio.run(work_api.work_queue(self.request(self.member_token)))
        data = json.loads(response.body)
        self.assertEqual(response.status_code,200)
        self.assertEqual([x["id"] for x in data["items"]],["doc_mine"])
        self.assertEqual(data["alerts"]["qua_han"],1)

    def test_document_list_hides_unassigned_items_from_member(self):
        self.add_doc("doc_mine_list",assignee=self.member)
        self.add_doc("doc_other_list",assignee=self.other)
        response = asyncio.run(api.list_documents(self.request(self.member_token)))
        data = json.loads(response.body)
        self.assertEqual(response.status_code,200)
        self.assertEqual([x["id"] for x in data["documents"]],["doc_mine_list"])

    def test_member_cannot_transition_unassigned_document(self):
        self.add_doc("doc_other",status="processing",assignee=self.other)
        response = asyncio.run(workflow_api.transition_document(
            self.request(self.member_token,{"status":"submitted","note":"thử"},"doc_other")))
        data = json.loads(response.body)
        self.assertEqual(response.status_code,403)
        self.assertIn("không được giao",data["error"].lower())
        self.assertEqual(store.one("SELECT status FROM documents WHERE id='doc_other'")["status"],"processing")

    def test_owner_sees_review_and_deadline_counts(self):
        now = datetime.now(work_api.VN_TZ)
        self.add_doc("doc_review",status="submitted",due=(now+timedelta(hours=20)).isoformat(),assignee=self.member)
        self.add_doc("doc_issue",status="approved",due=(now+timedelta(hours=70)).isoformat(),assignee=self.member)
        response = asyncio.run(work_api.work_queue(self.request(self.owner_token)))
        data = json.loads(response.body)
        self.assertEqual(response.status_code,200)
        self.assertEqual(data["counts"]["cho_duyet"],1)
        self.assertEqual(data["counts"]["cho_phat_hanh"],1)
        self.assertEqual(data["alerts"]["sap_han_24h"],1)


if __name__ == "__main__":
    unittest.main()
