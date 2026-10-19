"""The LLM's only three jobs — author a command, arbitrate, write narrative; it is never the decider, chat returns content (never reasoning_content), and a read timeout is terminal (no retry)."""

import json
import os
import re

from .errors import LLMError, LLMUnparseable


class LLM:
    def __init__(self, cfg, poster=None, log=None):
        llm = cfg.get("llm") or {}
        self.cfg = cfg
        self.enabled = bool(llm.get("enabled")) and bool(llm.get("base_url"))
        self.base_url = (llm.get("base_url") or "").rstrip("/")
        self.model = llm.get("model") or ""
        self.timeout = float(llm.get("timeout_sec", 60))
        self.api_key_env = llm.get("api_key_env") or "TLALOC_LLM_API_KEY"
        self.max_tokens = int(llm.get("max_tokens", 1024))
        self._poster = poster
        self.log = log
        self.calls = 0
        self.last_reasoning = None

    def api_key(self):
        return os.environ.get(self.api_key_env) or ""

    def chat(self, messages, *, max_tokens=None, temperature=0.2, json_mode=False,
             timeout=None):
        if not self.enabled:
            raise LLMError("llm disabled (llm.enabled / llm.base_url)")
        body = {"model": self.model or "llm", "messages": list(messages),
                "temperature": temperature,
                "max_tokens": int(max_tokens or self.max_tokens)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        payload = self._post(body, timeout or self.timeout)
        try:
            message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed completion: {str(payload)[:200]}") from exc
        self.last_reasoning = message.get("reasoning_content")
        content = message.get("content") or ""
        if not content and self.last_reasoning:
            # keep the trace for the decision path, but never hand it to prose
            if self.log is not None:
                self.log.emit("llm_reasoning_only", chars=len(self.last_reasoning))
        return content

    def _post(self, body, timeout):
        if self._poster is not None:
            return self._poster(f"{self.base_url}/chat/completions", body, timeout)
        import requests                                     # lazy
        headers = {"Content-Type": "application/json"}
        key = self.api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        response = requests.post(f"{self.base_url}/chat/completions", json=body,
                                 headers=headers, timeout=timeout)
        if response.status_code != 200:
            raise LLMError(f"llm HTTP {response.status_code}: {response.text[:200]}")
        return response.json()

    def choose(self, options, context=None, *, max_tokens=256):
        """The returned id is not checked against `options` here; decide.py matches it or falls back to deterministic."""
        listing = "\n".join(
            f"- id={o['id']} effect={o['effect']} reason={o.get('reason', '')} "
            f"params={json.dumps(o.get('params', {}), separators=(',', ':'))}"
            for o in options)
        prompt = (
            "You are the arbiter for an assume-breach CCDC red team. A decision "
            "model was not confident enough to choose. Choose EXACTLY ONE id from "
            "the list below — never invent one — or answer null if none is worth "
            "doing this cycle.\n\n"
            f"Context: {json.dumps(context or {}, default=str)[:600]}\n\n"
            f"Candidates:\n{listing}\n\n"
            'Reply with JSON only: {"id": "<id or null>", "why": "<one line>"}')
        text = self.chat([{"role": "user", "content": prompt}],
                         max_tokens=max_tokens, json_mode=True)
        data = extract_json(text)
        if not isinstance(data, dict):
            raise LLMUnparseable(f"arbiter reply was not JSON: {text[:120]!r}")
        cid = data.get("id")
        if not cid or str(cid).lower() == "null":
            return None
        return {"id": str(cid), "why": str(data.get("why") or "")}

    def author_command(self, goal, facts, os_name="linux", *, max_tokens=512):
        """The gate lints the command before it runs — this method does not decide what is safe."""
        shell = ("PowerShell (Windows, no double quotes: multi-line scripts are "
                 "encoded by the transport)" if os_name == "windows" else "bash")
        prompt = (
            "You are the command author for an assume-breach CCDC red team on a "
            "training range you already own. Write ONE " + shell + " command that "
            "makes progress on the goal, plus ONE command that verifies the "
            "outcome. Keep each under 180 characters, one line, no destructive "
            "patterns (no recursive deletes, no disk/partition/registry-hive "
            "operations, no reboots).\n\n"
            f"Goal: {goal}\n"
            f"Known facts: {json.dumps(facts or {}, default=str)[:800]}\n\n"
            'Reply with JSON only: {"command": "...", "verify": "...", "why": "..."}')
        text = self.chat([{"role": "user", "content": prompt}], max_tokens=max_tokens,
                         json_mode=True)
        data = extract_json(text)
        if not isinstance(data, dict) or not data.get("command"):
            raise LLMUnparseable(f"command reply was not usable: {text[:120]!r}")
        return {"command": str(data["command"]).strip(),
                "verify": str(data.get("verify") or "").strip() or None,
                "why": str(data.get("why") or "")[:200]}

    def narrative(self, summary, *, max_tokens=900):
        prompt = (
            "Write a short after-action narrative for this CCDC red-team run. "
            "Plain prose, past tense, no headings, no bullet lists, at most six "
            "sentences, no invented facts — use only the data below.\n\n"
            f"Data: {json.dumps(summary, default=str)[:3000]}")
        return self.chat([{"role": "user", "content": prompt}],
                         max_tokens=max_tokens, temperature=0.3).strip()


def extract_json(text):
    """The first balanced JSON object/array in `text`, else None."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        while start != -1:
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                char = text[i]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == opener:
                    depth += 1
                elif char == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except ValueError:
                            break
            start = text.find(opener, start + 1)
    return None
