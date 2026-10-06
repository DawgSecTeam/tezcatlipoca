"""Hashing, sealing and atomic byte writes (0600 at creation)."""

import hashlib
import os
import shutil
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_facts(path):
    path = Path(path)
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}


def seal_local_file(src, dest):
    """Copy `src` to `dest` with 0600 at creation and return its facts.

    0600-at-creation rather than chmod-after: these files quote credentials and flags, and the
    repo's own atomic writer documents why that window matters (config_ops.write_text_atomic)."""
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src, dest)
    os.chmod(dest, 0o600)
    facts = file_facts(dest)
    facts["source"] = str(src)
    return facts


def write_bytes_atomic(path, data, mode=0o600):
    """Bytes variant of config_ops.write_text_atomic: mode applied at creation, never torn."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    return path
