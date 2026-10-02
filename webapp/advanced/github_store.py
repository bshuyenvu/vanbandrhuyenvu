from __future__ import annotations

import base64, json, os, re, urllib.error, urllib.parse, urllib.request
from typing import Any

API = "https://api.github.com"

def _slug(value: str) -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-.")
    return value[:80] or "document"

class GitHubStoreError(RuntimeError):
    pass

class GitHubStore:
    def __init__(self, token: str | None = None, repo: str | None = None, branch: str | None = None):
        self.token = token or os.getenv("VBHC_GITHUB_TOKEN", "")
        self.repo = repo or os.getenv("VBHC_GITHUB_DOCS_REPO", os.getenv("VBHC_GITHUB_REPO", "bshuyenvu/vanbandrhuyenvu"))
        self.branch = branch or os.getenv("VBHC_GITHUB_DOCS_BRANCH", "documents")
        if not self.token:
            raise GitHubStoreError("Thiếu VBHC_GITHUB_TOKEN")

    def _request(self, method: str, path: str, data: dict | None = None) -> Any:
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(API + path, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "HuyenVuVanBanAI/3.0", "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read(); return json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            payload = exc.read().decode("utf-8", "replace")
            raise GitHubStoreError(f"GitHub {exc.code}: {payload[:300]}") from exc

    def ensure_branch(self) -> None:
        encoded = urllib.parse.quote(self.branch, safe="")
        try:
            self._request("GET", f"/repos/{self.repo}/git/ref/heads/{encoded}"); return
        except GitHubStoreError as exc:
            if "GitHub 404" not in str(exc): raise
        repo = self._request("GET", f"/repos/{self.repo}")
        base = repo.get("default_branch", "main")
        ref = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{urllib.parse.quote(base, safe='')}")
        self._request("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": ref["object"]["sha"]})

    def get(self, path: str, ref: str | None = None) -> dict:
        ref = ref or self.branch
        p = urllib.parse.quote(path.strip("/"), safe="/")
        return self._request("GET", f"/repos/{self.repo}/contents/{p}?ref={urllib.parse.quote(ref, safe='')}")

    def put(self, path: str, content: bytes | str, message: str, branch: str | None = None) -> dict:
        branch = branch or self.branch
        if branch == self.branch: self.ensure_branch()
        raw = content.encode("utf-8") if isinstance(content, str) else content
        sha = None
        try: sha = self.get(path, branch).get("sha")
        except GitHubStoreError as exc:
            if "GitHub 404" not in str(exc): raise
        payload = {"message": message, "content": base64.b64encode(raw).decode("ascii"), "branch": branch}
        if sha: payload["sha"] = sha
        p = urllib.parse.quote(path.strip("/"), safe="/")
        return self._request("PUT", f"/repos/{self.repo}/contents/{p}", payload)

    def save_document(self, user_id: str, document_id: str, text: str, metadata: dict | None = None) -> dict:
        uid, did = _slug(user_id), _slug(document_id); base = f"documents/{uid}/{did}"
        result = self.put(base + "/document.md", text, f"docs: save {did}")
        if metadata is not None:
            self.put(base + "/metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2), f"docs: metadata {did}")
        return {"repo": self.repo, "branch": self.branch, "path": base + "/document.md", "commit": (result.get("commit") or {}).get("sha")}

    def create_review_branch(self, name: str, base: str = "main") -> str:
        branch = "review/" + _slug(name)
        ref = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{urllib.parse.quote(base, safe='')}")
        try: self._request("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": ref["object"]["sha"]})
        except GitHubStoreError as exc:
            if "422" not in str(exc): raise
        return branch

    def open_pr(self, title: str, head: str, base: str = "main", body: str = "") -> dict:
        return self._request("POST", f"/repos/{self.repo}/pulls", {"title": title, "head": head, "base": base, "body": body})
