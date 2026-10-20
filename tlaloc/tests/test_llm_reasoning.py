"""A reasoning model spends its token budget on reasoning_content before the answer:
empty content with a full trace means the answer was cut off, not refused (live:
cde-2026, every arbiter reply was '' at 256 tokens and every cycle fell back)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from tlaloc.llm import LLM


def _reply(content, reasoning):
    return {"choices": [{"message": {"content": content,
                                     "reasoning_content": reasoning}}]}


def _cfg():
    return {"llm": {"enabled": True, "base_url": "http://llm", "model": "m"}}


def test_an_answer_cut_off_by_reasoning_is_asked_again_with_room():
    calls = []

    def poster(url, body, timeout):
        calls.append(body["max_tokens"])
        if len(calls) == 1:
            return _reply("", "reasoning consumed the whole budget")
        return _reply('{"id": "a"}', "ok")

    llm = LLM(_cfg(), poster)
    assert llm.chat([{"role": "user", "content": "x"}], max_tokens=256) == '{"id": "a"}'
    assert calls == [256, 1024]


def test_the_retry_is_once():
    def poster(url, body, timeout):
        return _reply("", "still reasoning")

    llm = LLM(_cfg(), poster)
    assert llm.chat([{"role": "user", "content": "x"}], max_tokens=64) == ""
