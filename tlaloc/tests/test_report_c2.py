import json
from pathlib import Path

from tlaloc.c2.sliver import FakeSliver
from tlaloc.report import c2_matrix, decision_trace, _render_c2, _render_decisions


def test_c2_matrix_names_each_build_and_its_callback(tmp_path):
    payloads = tmp_path / "red" / "payloads"
    payloads.mkdir(parents=True)
    manifest = {"boxes": {
        "web01-150-mtls": {"name": "web01-150-mtls", "box": "web01-150",
                           "transport": "mtls", "kind": "session", "plant": True,
                           "callback": "mtls://gw:8888"},
        "web01-150-http": {"name": "web01-150-http", "box": "web01-150",
                           "transport": "http", "kind": "session", "plant": True,
                           "callback": "http://gw:8080"},
        "web01-150-dns": {"name": "web01-150-dns", "box": "web01-150",
                          "transport": "dns", "kind": "session", "plant": False,
                          "callback": "dns://x"},
    }}
    (payloads / "manifest.json").write_text(json.dumps(manifest))
    c2 = FakeSliver({"web01-150": {
        "name": "web01-150-mtls", "seen_at": 100, "agent": "mtls",
        "async": False}})
    rows = c2_matrix(c2, str(tmp_path))
    by_name = {row["name"]: row for row in rows}
    assert by_name["web01-150-mtls"]["status"] == "callback"
    assert by_name["web01-150-http"]["status"] == "NO CALLBACK"
    assert by_name["web01-150-dns"]["status"] == "built only"


def test_decision_trace_keeps_the_clef_input_and_confidence(tmp_path):
    (tmp_path / "events.jsonl").write_text("\n".join([
        json.dumps({"kind": "decision", "cycle": 1, "question": "next_effect",
                    "answer": "recon:10.0.0.2", "source": "clef", "top_prob": 0.71,
                    "theta": 0.6, "probabilities": {"recon:10.0.0.2": 0.71},
                    "bundle": {"state": {"t": "2/30", "boxes": []},
                               "questions": {"next_effect": {
                                   "criteria": {"recon:10.0.0.2": "scan the box"}}}}})] ))
    trace = decision_trace(tmp_path)
    assert trace[0]["top_prob"] == 0.71
    lines = []
    _render_decisions(lines, trace)
    assert "next_effect" in "\n".join(lines)
    assert "scan the box" in "\n".join(lines)
