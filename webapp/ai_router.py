from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class AIError(RuntimeError):
    pass


def _extract_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise AIError("AI trả về nội dung rỗng")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S | re.I)
    candidate = fenced.group(1) if fenced else None
    if not candidate:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidate = text[start : end + 1]
    if candidate:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as exc:
            raise AIError(f"Không đọc được JSON từ AI: {exc}") from exc
    raise AIError("AI không trả về JSON hợp lệ")


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 90) -> dict[str, Any]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise AIError(f"AI HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise AIError(f"Không kết nối được AI provider: {exc.reason}") from exc


@dataclass
class AIResult:
    data: dict[str, Any]
    provider: str
    model: str


class AIRouter:
    """Gemini-first router, OpenAI fallback. Không phụ thuộc SDK riêng."""

    def __init__(self) -> None:
        self.provider = os.getenv("VBHC_AI_PROVIDER", "auto").strip().lower()
        self.gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.openai_key = os.getenv("OPENAI_API_KEY", "").strip()
        self.gemini_model = os.getenv("VBHC_GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        self.openai_model = os.getenv("VBHC_OPENAI_MODEL", "gpt-5.6-luna").strip()

    def status(self) -> dict[str, Any]:
        return {
            "mode": self.provider,
            "gemini": {"configured": bool(self.gemini_key), "model": self.gemini_model},
            "openai": {"configured": bool(self.openai_key), "model": self.openai_model},
        }

    def generate_json(
        self,
        *,
        system: str,
        prompt: str,
        file_b64: str | None = None,
        mime_type: str | None = None,
    ) -> AIResult:
        order: list[str]
        if self.provider == "gemini":
            order = ["gemini"]
        elif self.provider == "openai":
            order = ["openai"]
        else:
            order = ["gemini", "openai"]

        errors: list[str] = []
        for provider in order:
            try:
                if provider == "gemini" and self.gemini_key:
                    return self._gemini(system, prompt, file_b64=file_b64, mime_type=mime_type)
                if provider == "openai" and self.openai_key:
                    return self._openai(system, prompt)
            except AIError as exc:
                errors.append(f"{provider}: {exc}")
        if errors:
            raise AIError(" | ".join(errors))
        raise AIError("Chưa cấu hình GEMINI_API_KEY hoặc OPENAI_API_KEY")

    def _gemini(self, system: str, prompt: str, *, file_b64: str | None, mime_type: str | None) -> AIResult:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.gemini_model}:generateContent"
        )
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if file_b64 and mime_type:
            parts.insert(0, {"inlineData": {"mimeType": mime_type, "data": file_b64}})
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        raw = _post_json(
            url,
            payload,
            {"Content-Type": "application/json", "x-goog-api-key": self.gemini_key},
        )
        try:
            text = raw["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AIError(f"Gemini response không đúng cấu trúc: {raw}") from exc
        return AIResult(_extract_json(text), "gemini", self.gemini_model)

    def _openai(self, system: str, prompt: str) -> AIResult:
        payload = {
            "model": self.openai_model,
            "instructions": system + "\nChỉ trả về một JSON object hợp lệ, không dùng Markdown.",
            "input": prompt,
        }
        raw = _post_json(
            "https://api.openai.com/v1/responses",
            payload,
            {"Content-Type": "application/json", "Authorization": f"Bearer {self.openai_key}"},
        )
        text = ""
        if isinstance(raw.get("output_text"), str):
            text = raw["output_text"]
        if not text:
            chunks: list[str] = []
            for item in raw.get("output", []) or []:
                for part in item.get("content", []) or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
            text = "\n".join(chunks)
        return AIResult(_extract_json(text), "openai", self.openai_model)
