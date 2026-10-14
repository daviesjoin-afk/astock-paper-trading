"""Short publication lock and integrity check for the shared factor pair."""
from contextlib import contextmanager
import hashlib
import json
import os
import tempfile
import threading

import pandas as pd

try:
    import fcntl
except ImportError:  # Windows development: serialize threads as elsewhere in the data layer.
    fcntl = None

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


@contextmanager
def file_lock(path, *, blocking=True):
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(os.path.abspath(path), threading.Lock())
    acquired = lock.acquire(blocking=blocking)
    try:
        if not acquired:
            yield False
            return
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a+b") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError:
                    yield False
                    return
            try:
                yield True
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        if acquired:
            lock.release()


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def read(factors_path, meta_path):
    with file_lock(meta_path + ".publish.lock"):
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
        if not isinstance(meta, dict):
            raise ValueError("选股因子元数据格式无效，等待重建")
        expected = meta.get("factors_sha256")
        if expected and _digest(factors_path) != expected:
            raise ValueError("选股因子与元数据不是同一版本，等待重建")
        factors = pd.read_csv(factors_path, dtype={"code": str}, index_col="code")
    return factors, meta


def publish(factors, meta, factors_path, meta_path):
    # Prepare unique files before taking the publication lock.  Readers can
    # still consume the last valid snapshot during the expensive build.
    temps = []
    try:
        for path in (factors_path, meta_path):
            fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp",
                                       dir=os.path.dirname(os.path.abspath(path)))
            os.close(fd)
            temps.append(tmp)
        factors.to_csv(temps[0], encoding="utf-8")
        meta = {**meta, "factors_sha256": _digest(temps[0])}
        with open(temps[1], "w", encoding="utf-8") as handle:
            json.dump(meta, handle, ensure_ascii=False)
        with file_lock(meta_path + ".publish.lock"):
            os.replace(temps[0], factors_path)
            os.replace(temps[1], meta_path)
    finally:
        for path in temps:
            if os.path.exists(path):
                os.unlink(path)
