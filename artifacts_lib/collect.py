"""The collector: fetch/seal every planned target, write collection.json."""

import json
import subprocess
from pathlib import Path

from config.config_ops import write_state

from .constants import (ABSENT, COLLECTION_NAME, FAILED, LOST, OK, SCHEMA, SEALED, SKIPPED,
                        STATUSES, UNREACHABLE, Unreachable)
from .env import iso
from .hashing import file_facts, seal_local_file, sha256_file
from .manifest import load_manifest
from .plan import want_label
from .transport import default_transport, fetch


def relative_to_test(test_path, path):
    return str(Path(path).resolve().relative_to(Path(test_path).resolve()))


def _previous_items(collection, target_name, label):
    for target in collection.get("targets", []):
        if target.get("name") == target_name:
            return [item for item in target.get("files", []) if item.get("want") == label]
    return []


def _still_good(test_path, items):
# A re-run re-verifies by hash instead of re-fetching; a hand-edited artifact is re-collected.
    if not items:
        return False
    for item in items:
        if item.get("status") not in (OK, SEALED):
            return False
        path = Path(test_path) / item.get("local", "")
        if not path.exists() or not item.get("sha256"):
            return False
        if sha256_file(path) != item["sha256"]:
            return False
    return True


def _canonical_done(test_path, collection, canonical):
    for derived in collection.get("derived", []):
        if derived.get("path") != canonical:
            continue
        path = Path(test_path) / canonical
        if path.exists() and derived.get("sha256") == sha256_file(path):
            return True
    return False


def collect(test_path, targets, *, transport=None, skip_existing=True, dry_run=False,
            timeout=120, stop_on_unreachable=True):
# A partial collection is recorded as partial, never raised; only wiring bugs fail hard.
    test_path = Path(test_path)
    transport = transport or default_transport()
    unknown = sorted({t.get("route") for t in targets if t.get("route") and not t.get("status")}
                     - set(transport))
    if unknown:
        # No transport is a wiring bug; a per-file `failed` would read as a no-evidence run.
        raise RuntimeError(f"no transport for route(s) {unknown} — refusing to record a "
                           "collection that cannot even be attempted")
    previous = load_collection(test_path)
    manifest = load_manifest(test_path)
    collected = {"schema": SCHEMA, "key": test_path.name, "run_id": manifest.get("run_id"),
                 "collected_at": iso(), "targets": [], "derived": [], "summary": {}}
    counters = {status: 0 for status in STATUSES}

    for target in targets:
        entry = {"name": target.get("name"), "route": target.get("route"),
                 "node": target.get("node"), "vmid": target.get("vmid"),
                 "note": target.get("note"), "files": [], "errors": [], "status": OK,
                 # "Why is RED-TEAM.md missing?" must stay answerable from collection.json alone.
                 "canonical_documents": [w["canonical"] for w in (target.get("want") or [])
                                         if w.get("canonical")]}
        if target.get("status"):
            entry["status"] = target["status"]
            entry["reason"] = target.get("note")
            counters[target["status"]] += 1
            collected["targets"].append(entry)
            continue
        if target.get("route") == "scp-jump" and not (target.get("ssh") or {}).get("host"):
            entry["status"] = UNREACHABLE
            entry["errors"].append("no address recorded for this target")
            counters[UNREACHABLE] += 1
            collected["targets"].append(entry)
            continue

        for want in list(target.get("want", [])) + list(target.get("want_globs", [])):
            label = want_label(want)
            records = []
            already = (_previous_items(previous, entry["name"], label)
                       if skip_existing and not dry_run else [])
            if dry_run:
                records.append({"want": label, "status": SKIPPED, "reason": "dry run"})
            elif _still_good(test_path, already):
                # A "skipped" stub would drop the sha256 provenance the re-run exists to confirm.
                records.extend({**item, "reverified_at": iso()} for item in already)
                counters["reverified"] = counters.get("reverified", 0) + 1
            else:
                try:
                    written = fetch(target, want, test_path, timeout, transport)
                    for path in written:
                        records.append({"want": label, "local": relative_to_test(test_path, path),
                                        "status": OK, **file_facts(path)})
                    if not written:
                        records.append({"want": label, "status": ABSENT,
                                        "reason": "source reachable, nothing matched"})
                except FileNotFoundError as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": ABSENT, "reason": str(e)})
                except (Unreachable, TimeoutError) as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": UNREACHABLE, "reason": str(e)})
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    records.append({"want": label, "canonical": want.get("canonical"),
                                    "status": FAILED, "reason": f"{type(e).__name__}: {e}"})
            if want.get("canonical") and not dry_run:
                records, derived = derive_canonical(test_path, previous, want, records)
                if derived:
                    collected["derived"].append(derived)
            entry["files"].extend(records)
            for record in records:
                counters[record["status"]] = counters.get(record["status"], 0) + 1
            for record in records:
                if record["status"] in LOST:
                    entry["errors"].append(f"{label}: {record.get('reason')}")
            # A dead box must not burn the per-file timeout on every remaining want during teardown.
            if stop_on_unreachable and any(r["status"] == UNREACHABLE for r in records):
                remaining = [w for w in (list(target.get("want", []))
                                         + list(target.get("want_globs", [])))
                             if want_label(w) != label]
                entry["files"].extend({"want": want_label(w), "canonical": w.get("canonical"),
                                       "status": SKIPPED,
                                       "reason": "not attempted: target unreachable"}
                                      for w in remaining)
                counters[SKIPPED] += len(remaining)
                break

        # Worst record wins; target status never comes from matching error strings.
        statuses = {record["status"] for record in entry["files"]}
        if UNREACHABLE in statuses:
            entry["status"] = UNREACHABLE
        elif FAILED in statuses:
            entry["status"] = FAILED
        elif statuses and statuses <= {ABSENT}:
            entry["status"] = ABSENT
        else:
            entry["status"] = OK
        collected["targets"].append(entry)

    collected["summary"] = {**counters,
                            "files": sum(len(t["files"]) for t in collected["targets"])}
    write_state(test_path / COLLECTION_NAME, collected)
    return collected


def derive_canonical(test_path, previous, want, records):
    canonical = want["canonical"]
    if _canonical_done(test_path, previous, canonical):
        for derived in previous.get("derived", []):
            if derived.get("path") == canonical:
                return records, {**derived, "reverified_at": iso()}
        return records, None
    candidates = [Path(test_path) / r["local"] for r in records
                  if r.get("status") == OK and r.get("local")]
    if not candidates:
        return records, None
    # Same-tick files share identical st_mtime on this estate (verified ns-equal on ext4), so
    # max alone can seal an older report; red's report-YYYYMMDD-HHMM.md name orders recency.
    newest = max(candidates, key=lambda p: (p.stat().st_mtime, p.name))
    facts = seal_local_file(newest, Path(test_path) / canonical)
    return records, {"path": canonical, "from": relative_to_test(test_path, newest),
                     "method": "pulled", "sha256": facts["sha256"]}


def load_collection(test_path):
    try:
        data = json.loads((Path(test_path) / COLLECTION_NAME).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
