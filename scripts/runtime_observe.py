#!/usr/bin/env python3
"""运行时观测：把 ``scheduler.log`` 的 JSON 事件流聚合成可复核的指标。

这个脚本只做一件事 —— **把日志行变成数字**。它不产生事件、不碰数据库、不读
任何凭据、不参与交易判断，也**不做性能结论**：结论必须由报告读者对着样本量、
口径和原始字段名自己下。

数据来源（两种，都通过仓库里已有的 ``scripts/oracle_ctl.py`` 连接参数）：

* ``--source remote``（默认）：从 Oracle 服务器拉 ``/opt/astock-codex/reports/scheduler.log``
  的最近 ``--lines`` 行；
  - ``--pull sftp``：复用 ``oracle_ctl.connect`` 的 paramiko 连接 + 同一份
    ``.env.oracle``（含 HTTP CONNECT 代理），按块从文件尾部倒读，二进制安全；
  - ``--pull exec``：走 ``python scripts/oracle_ctl.py exec -- 'tail -n N ...'``，
    输出经 UTF-8 传递；
  - ``--pull auto``（默认）：走 ``exec``。实测同一批 2000 行 ``exec`` ≈ 21 s、
    ``sftp`` ≈ 206 s（块倒读每块一次 RTT）；``sftp`` 留给"要二进制原样"的场合。
* ``--source local``：直接读本机文件（离线复算、留证复现）。

拉下来的原始字节先落到本地临时文件（``--keep-raw`` 可保留），再从临时文件解析，
因此"拉取"和"解析"可以分开复核。

容忍坏输入（生产日志实测存在三类）：

1. **GBK/UTF-8 混排**：中文 reason 会显示成乱码。本脚本只按 **JSON 字段名** 归类，
   不匹配中文文本；解码统一 ``utf-8/replace``，乱码只污染值、不影响结构。
2. **GBK 尾字节撞上反斜杠**：GBK 双字节字符的尾字节可以是 ``0x5C``（``\\``），
   解码后可能形成 ``\\x`` 这类非法 JSON 转义 → 首次解析失败；脚本会在有界窗口内
   做一次转义修复后重试。
3. **截断行 / 非 JSON 行**：逐条计数为 ``unparsed``，绝不抛异常、绝不中断聚合。

用法::

    .venv\\Scripts\\python.exe scripts/runtime_observe.py --lines 2000
    .venv\\Scripts\\python.exe scripts/runtime_observe.py --lines 20000 \\
        --since 2026-11-03T09:34 --until 2026-11-03T13:10 \\
        --json reports/runtime_observe_base.json
    .venv\\Scripts\\python.exe scripts/runtime_observe.py --source local \\
        --log reports/scheduler.log --since 2026-11-03T09:34

口径与原始字段名（报告里必须照抄这一节）：

* **slot** 位置的唯一事实来源是 ``timing.slot``（``paper_slot_timing`` 事件与 runner
  结果行都在 ``timing`` 里）；``paper_host_lock`` / ``paper_slot_start`` /
  ``paper_slot_exit`` 是主机 shell 发的，slot 在 **顶层** ``slot``。两种形状分别取值，
  互不回落。
* **一次尝试（attempt）** = 一个 ``timing.attempt_id``。同一个 attempt 在日志里有
  两条记录：``{"event":"paper_slot_timing","status":…,"timing":{"scope":"finished"}}``
  与 runner 结果行（``"timing":{"scope":"runner_result"}``，顶层多出 ``error`` /
  ``retry_reason`` / ``risk`` / ``risk_snapshot_refresh`` 等字段）。聚合时按
  ``attempt_id`` 合并，**不重复计数**。
* **实际完成的风控扫描**：``timing.risk_scans[]`` 里 ``status == "completed"`` 的
  身份 ``(cycle_id, asof_date, scan_minute)`` 去重计数。外层 slot ``status`` 写
  ``completed`` **不等于**又扫了一次（``already_scanned`` 是耐久去重结果）。
* **跨入口重复扫描身份**：同一 ``(cycle_id, asof_date, scan_minute)`` 出现在
  **不同 slot** 的 ``risk_scans`` 里。``completed`` 同时出现在两个 slot 属于契约
  异常（耐久认领应保证独占），单独标出。
* **风控开始间隔**：``risk_scans[].started_at``（不是 slot 的 ``timing.started_at``）
  按时间排序、**不同 scan_minute 相邻**两条的差值。
* **阶段秒数**：``timing.stages[name].seconds`` 是**该 attempt 内该阶段累计**秒数
  （含子阶段），**不能相加得到总耗时**；``calls`` / ``errors`` 是累计计数。
* **runtime_commit**：``timing.runtime_commit``（来自容器环境 ``ASTOCK_GIT_COMMIT``），
  旧日志缺失时为 ``unknown``，**不当成某个版本**。
* **risk_snapshot_refresh**：runner 结果行顶层字段 ``risk_snapshot_refresh`` 的字符串
  值（``scheduled`` / ``already_running`` / ``completed`` / ``failed`` / ``skipped`` …）。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
DEFAULT_REMOTE_LOG = "/opt/astock-codex/reports/scheduler.log"

#: 事件名常量（与 backend/paper_dispatch_metrics.py 共用一份事实）。
SLOT_TIMING_EVENT = "paper_slot_timing"
SLOT_START_EVENT = "paper_slot_start"
SLOT_EXIT_EVENT = "paper_slot_exit"
HOST_LOCK_EVENT = "paper_host_lock"

#: 外层任务完成/空转/失败三分类（与 paper_dispatch_metrics 一致）。
COMPLETED_STATUSES = frozenset({"completed", "ok"})
IDLE_STATUSES = frozenset({"already_done", "skipped", "up_to_date", "already_scanned"})
FAILED_STATUSES = frozenset({"failed", "error", "blocked"})

#: 报告里要点名统计的阶段（另有 ``quote.*`` 前缀族单独展开）。
NAMED_STAGES = ("risk_pass", "http_get_attempt", "source_health", "position_quotes")
QUOTE_PREFIX = "quote."

#: 判定"风控仍在别的入口跑"的状态值（``paper_risk_scan_state`` 域）。
RISK_RUNNING_STATES = frozenset({"running", "in_progress", "claimed"})

LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


# ───────────────────────────────────────────────────────────── 时间与统计 ──
def parse_time(value):
    """解析日志里的时间；无时区的按 +08:00 解释（日志由 CST 主机产生）。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment


def record_time(record):
    """一条记录的权威时刻：``timing.started_at`` → 顶层 ``at`` → 顶层 ``started_at``。"""
    timing = record.get("timing")
    if isinstance(timing, dict):
        moment = parse_time(timing.get("started_at"))
        if moment is not None:
            return moment
    moment = parse_time(record.get("started_at"))
    if moment is not None:
        return moment
    return parse_time(record.get("at"))


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _counter(value):
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, math.ceil(len(sorted_values) * fraction) - 1))
    return sorted_values[index]


def describe(values):
    """样本量 / p50 / p95 / max / min；空样本全为 None，不冒充 0。"""
    clean = sorted(value for value in values if value is not None)
    if not clean:
        return {"n": 0, "p50": None, "p95": None, "max": None, "min": None}
    return {
        "n": len(clean),
        "p50": round(statistics.median(clean), 3),
        "p95": round(_percentile(clean, 0.95), 3),
        "max": round(clean[-1], 3),
        "min": round(clean[0], 3),
    }


# ────────────────────────────────────────────────────────────── 日志解析 ──
#: JSON 字符串里合法的转义引导字符。
_VALID_ESCAPE_LEADS = frozenset('"\\/bfnrtu')
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _repair_escapes(text):
    """修 GBK 尾字节造成的非法 JSON 转义（``\\x`` → ``\\\\x``）。

    只动 ``\\`` 后跟 **非** JSON 转义引导字符的位置；合法转义（含 ``\\uXXXX``）
    保持原样。修复是有损的，目的只是"结构能读出来"，值仍可能是乱码。
    """
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "\\" and index + 1 < length:
            nxt = text[index + 1]
            if nxt in _VALID_ESCAPE_LEADS:
                if nxt == "u":
                    hexpart = text[index + 2:index + 6]
                    if len(hexpart) == 4 and all(c in _HEX_DIGITS for c in hexpart):
                        out.append(text[index:index + 6])
                        index += 6
                        continue
                    out.append("\\\\")
                    index += 1
                    continue
                out.append(char)
                out.append(nxt)
                index += 2
                continue
            out.append("\\\\")
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


REPAIR_WINDOW = 4 * 1024 * 1024


def iter_records(text, stats=None):
    """从文本里流式抠出连续 JSON 对象（兼容单行紧凑与多行缩进两种写法）。

    ``json.JSONDecoder.raw_decode`` 不关心换行，因此"一个 ``jq`` 美化过的多行
    对象"和"一行一个紧凑对象"走同一条路径。解析失败的分支只计数并跳过到下一个
    行首的 ``{``，永不抛异常。
    """
    decoder = json.JSONDecoder()
    stats = stats if stats is not None else {}
    stats.setdefault("repairs", 0)
    index = 0
    length = len(text)
    while index < length:
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            window = text[start:start + REPAIR_WINDOW]
            try:
                value, rel = decoder.raw_decode(_repair_escapes(window), 0)
                end = start + rel
                stats["repairs"] += 1
            except ValueError:
                stats["unparsed"] = stats.get("unparsed", 0) + 1
                newline = text.find("\n", start)
                index = (newline + 1) if newline >= 0 else length
                continue
        if isinstance(value, dict):
            yield value
        index = max(end, start + 1)


# ──────────────────────────────────────────────────────────────── 拉取层 ──
def _import_oracle_ctl():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import oracle_ctl  # noqa: PLC0415 - 延迟导入，local 模式不需要 paramiko

    return oracle_ctl


def pull_sftp(conf, remote_log, lines, max_bytes=512 * 1024 * 1024, chunk=2 * 1024 * 1024):
    """从远端文件尾部倒读 ``lines`` 行，二进制安全（不经 shell、不做编码转换）。

    倒读按块累加换行计数，够 ``lines`` 行就停；``max_bytes`` 是防止单次拉取失控的
    硬上限（配置过大时优先保证"能跑完"，并在 ``source`` 里如实记录实际字节数）。
    """
    oracle_ctl = _import_oracle_ctl()
    with oracle_ctl.connect(conf) as client:
        sftp = client.open_sftp()
        try:
            size = sftp.stat(remote_log).st_size
            budget = min(size, max_bytes)
            with sftp.open(remote_log, "rb") as handle:
                position = size
                blocks = []
                newlines = 0
                while position > 0 and newlines <= lines and budget > 0:
                    step = min(chunk, position, budget)
                    position -= step
                    budget -= step
                    handle.seek(position)
                    block = handle.read(step)
                    blocks.append(block)
                    newlines += block.count(b"\n")
        finally:
            sftp.close()
    data = b"".join(reversed(blocks))
    parts = data.split(b"\n")
    if parts and parts[-1] == b"":
        parts.pop()
    return b"\n".join(parts[-lines:])


def pull_exec(remote_log, lines, timeout=600):
    """走既有运维客户端 ``oracle_ctl.py exec -- 'tail -n N LOG'`` 取原始行。"""
    cmd = f"tail -n {int(lines)} {remote_log}"
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "oracle_ctl.py"), "exec", "--", cmd],
        cwd=str(REPO), env=env, capture_output=True, timeout=timeout,
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise RuntimeError(f"oracle_ctl exec 失败 rc={proc.returncode}: {tail}")
    return proc.stdout


def fetch(args):
    """返回 (raw_bytes, source_meta)。原始字节先落临时文件，再读回解析。"""
    if args.source == "local":
        path = Path(args.log).expanduser()
        if not path.is_absolute():
            path = REPO / path
        if not path.is_file():
            raise SystemExit(f"日志文件不存在：{path}")
        data = path.read_bytes()
        if args.lines and data.count(b"\n") > args.lines:
            parts = data.split(b"\n")
            if parts and parts[-1] == b"":
                parts.pop()
            data = b"\n".join(parts[-args.lines:])
        return data, {"kind": "local", "path": str(path), "bytes": len(data)}

    oracle_ctl = _import_oracle_ctl()
    conf = oracle_ctl.load_conf()
    if not conf.get("ORACLE_HOST"):
        raise SystemExit("缺少 ORACLE_HOST：写仓库根 .env.oracle 或设环境变量")
    method = args.pull
    if method == "auto":
        # 默认走 exec：实测同样的 2000 行，exec 约 21 s，sftp 分块倒读约 206 s
        # （每块一次 RTT，经 HTTP CONNECT 代理时尤其明显）。sftp 只在显式
        # ``--pull sftp`` 时使用：它是二进制安全的（不经 shell、不做编码转换），
        # 但要求登录用户对日志有读权限，且慢。
        method = "exec"
    remote = args.remote_log
    note = None
    if method == "sftp":
        try:
            data = pull_sftp(conf, remote, args.lines)
        except (PermissionError, IOError, OSError) as exc:
            # 生产上 scheduler.log 归 root，ubuntu 用户 sftp 读不到；exec 走的是
            # ``sudo -E bash -lc``，所以这里退到 exec 而不是直接失败。
            note = f"sftp 读取失败（{type(exc).__name__}），已回退 exec: {exc}"
            data = pull_exec(remote, args.lines)
            method = "exec"
    else:
        data = pull_exec(remote, args.lines)
    meta = {
        "kind": "remote", "method": method, "host": conf.get("ORACLE_HOST"),
        "remote_log": remote, "requested_lines": args.lines, "bytes": len(data),
    }
    if note:
        meta["fallback_note"] = note
    return data, meta


def materialize(data, keep_path=None):
    """把原始字节写进本地临时文件并从文件读回文本（GBK 乱码靠 replace 兜住）。"""
    if keep_path:
        path = Path(keep_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path.read_text(encoding="utf-8", errors="replace"), str(path)
    handle = tempfile.NamedTemporaryFile(prefix="runtime_observe_", suffix=".log", delete=False)
    path = Path(handle.name)
    try:
        handle.write(data)
        handle.close()
        text = path.read_text(encoding="utf-8", errors="replace")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return text, None


# ──────────────────────────────────────────────────────── 事件 → 尝试聚合 ──
def _timing_block(record):
    timing = record.get("timing")
    return timing if isinstance(timing, dict) else None


def _text(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _scalar_text(value):
    """把 ``cycle_id`` 这类可能写成整数/字符串的身份字段统一成字符串。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(int(value)) if value.is_integer() else str(value)
    return _text(value)


def merge_attempt(attempts, record, moment):
    """按 ``attempt_id`` 合并同一尝试的记录；缺字段才填，绝不覆盖已有证据。"""
    timing = _timing_block(record)
    if timing is None:
        return None
    attempt_id = _text(timing.get("attempt_id"))
    if attempt_id is None:
        return None
    slot = _text(timing.get("slot")) or _text(record.get("slot")) or "unknown"
    entry = attempts.get(attempt_id)
    if entry is None:
        entry = {
            "attempt_id": attempt_id, "slot": slot, "started_at": moment,
            "runtime_commit": _text(timing.get("runtime_commit")) or "unknown",
            "status": None, "scope_seen": [], "elapsed_seconds": None,
            "stages": {}, "risk_scans": [], "result": {}, "tick_id": None,
            "host_lock_wait": None,
        }
        attempts[attempt_id] = entry
    if slot != "unknown" and entry["slot"] == "unknown":
        entry["slot"] = slot
    if entry["started_at"] is None and moment is not None:
        entry["started_at"] = moment
    scope = _text(timing.get("scope"))
    if scope and scope not in entry["scope_seen"]:
        entry["scope_seen"].append(scope)
    commit = _text(timing.get("runtime_commit"))
    if commit and entry["runtime_commit"] == "unknown":
        entry["runtime_commit"] = commit
    elapsed = _finite(timing.get("elapsed_seconds"))
    if elapsed is not None and entry["elapsed_seconds"] is None:
        entry["elapsed_seconds"] = elapsed
    stages = timing.get("stages")
    if isinstance(stages, dict) and len(stages) >= len(entry["stages"]):
        entry["stages"] = stages
    scans = timing.get("risk_scans")
    if isinstance(scans, list) and len(scans) >= len(entry["risk_scans"]):
        entry["risk_scans"] = [scan for scan in scans if isinstance(scan, dict)]
    tick = _text(timing.get("scheduler_tick_id"))
    if tick and entry["tick_id"] is None:
        entry["tick_id"] = tick
    wait = _counter(timing.get("host_paper_lock_wait_seconds"))
    if wait is not None and entry["host_lock_wait"] is None:
        entry["host_lock_wait"] = wait
    # 结束事件自带状态；runner 结果行的顶层状态是同一事实的第二次落盘。
    if record.get("event") == SLOT_TIMING_EVENT:
        status = _text(record.get("status"))
        if status and entry["status"] is None:
            entry["status"] = status
    status_field = _text(record.get("status"))
    if status_field and entry["status"] is None:
        entry["status"] = status_field
    # runner 结果行的顶层字段（error / retry_reason / risk / risk_snapshot_refresh …）
    for key, value in record.items():
        if key in ("timing", "event"):
            continue
        entry["result"].setdefault(key, value)
    return entry


def status_bucket(status):
    if status is None:
        return "unknown"
    lowered = status.strip().lower()
    if lowered in COMPLETED_STATUSES:
        return "completed"
    if lowered in IDLE_STATUSES:
        return "idle"
    if lowered in FAILED_STATUSES:
        return "failed"
    if lowered == "in_progress":
        return "in_progress"
    return "unknown"


def _risk_state(entry):
    """从 JSON 字段判"风控在别处跑"：顶层 ``risk_scan_state`` 优先，其次嵌套 ``risk``。"""
    result = entry["result"]
    state = _text(result.get("risk_scan_state"))
    if state:
        return state
    nested = result.get("risk")
    if isinstance(nested, dict):
        state = _text(nested.get("risk_scan_state"))
        if state:
            return state
        state = _text(nested.get("status"))
        if state:
            return state
    return None


def classify_failure(entry):
    """失败归类**只看 JSON 字段值**（error / retry_reason / risk_scan_state / status）。

    中文 ``reason`` 在传输链路上会变成乱码，因此不参与匹配。
    """
    result = entry["result"]
    error = _text(result.get("error"))
    retry = _text(result.get("retry_reason"))
    state = _risk_state(entry)
    status = (entry["status"] or "").strip().lower()

    if error and error.startswith("paper lease lost"):
        return "lease_lost"
    if state and state.lower() in RISK_RUNNING_STATES:
        return "risk_running"
    if retry and retry.lower() in RISK_RUNNING_STATES:
        return "risk_running"
    if retry and retry.lower() == "risk_in_progress":
        return "risk_in_progress"
    if error == "slot_work_incomplete":
        return "slot_work_incomplete"
    if error:
        lowered = error.lower()
        if "timeout" in lowered or "timed out" in lowered:
            return "timeout"
        return f"other:{error}"
    if status == "blocked":
        return "blocked"
    if status == "in_progress":
        return "in_progress"
    if status == "failed":
        return "failed_without_error_field"
    return f"status:{status or 'unknown'}"


def scan_identity(scan):
    """扫描身份 ``(cycle_id, asof_date, scan_minute)``，三者缺一即不成立。

    ``cycle_id`` 在日志里是**整数**（如 ``15``），``_text`` 只接受字符串，所以这里
    单独用 ``_scalar_text`` 转成字符串 —— 否则所有周期都会被压成同一个 key，
    "不同 cycle 同一分钟"的重复会被静默合并。
    """
    minute = _text(scan.get("scan_minute"))
    if minute is None:
        return None
    return (
        _scalar_text(scan.get("cycle_id")),
        _text(scan.get("asof_date")),
        minute,
    )


# ────────────────────────────────────────────────────────────────── 聚合 ──
def summarize(records, *, since=None, until=None, source_meta=None, parse_stats=None,
              label=None):
    parse_stats = parse_stats or {}
    records = list(records)

    slots = {}
    commits = {}
    attempts = {}
    risk_snapshot = {}
    host_events = []
    window_records = 0
    unparsed_hint = parse_stats.get("unparsed", 0)
    repaired = parse_stats.get("repairs", 0)
    times = []

    def _slot_bucket(slot):
        return slots.setdefault(slot, {
            "paper_slot_start": 0, "paper_slot_exit": 0, "paper_slot_exit_nonzero": 0,
            "paper_slot_timing": 0, "paper_host_lock_not_acquired": 0,
            "paper_host_lock_yielded_to_full_scan": 0,
            "paper_host_lock_other": 0,
            "timing_status": {}, "exit_codes": {}, "host_lock_wait_seconds": [],
            "timing_host_lock_wait_seconds": [],
        })

    for record in records:
        moment = record_time(record)
        if moment is not None:
            times.append(moment)
        if not in_window(moment, since, until):
            continue
        window_records += 1

        commit_hint = None
        timing = _timing_block(record)
        if timing is not None:
            commit_hint = _text(timing.get("runtime_commit"))
        if commit_hint is None:
            commit_hint = _text(record.get("runtime_commit"))

        kind = _text(record.get("event"))

        if kind == SLOT_TIMING_EVENT:
            slot = "unknown"
            if timing is not None:
                slot = _text(timing.get("slot")) or "unknown"
            bucket = _slot_bucket(slot)
            bucket["paper_slot_timing"] += 1
            status = _text(record.get("status")) or "unknown"
            bucket["timing_status"][status] = bucket["timing_status"].get(status, 0) + 1
            if timing is not None:
                wait = _counter(timing.get("host_paper_lock_wait_seconds"))
                if wait is not None:
                    bucket["timing_host_lock_wait_seconds"].append(wait)
        elif kind == SLOT_START_EVENT:
            slot = _text(record.get("slot")) or "unknown"
            bucket = _slot_bucket(slot)
            bucket["paper_slot_start"] += 1
            wait = _counter(record.get("wait_seconds"))
            if wait is not None:
                bucket["host_lock_wait_seconds"].append(wait)
            host_events.append(("start", slot, moment, _text(record.get("tick_id"))))
        elif kind == SLOT_EXIT_EVENT:
            slot = _text(record.get("slot")) or "unknown"
            bucket = _slot_bucket(slot)
            bucket["paper_slot_exit"] += 1
            code = record.get("exit_code")
            bucket["exit_codes"][str(code)] = bucket["exit_codes"].get(str(code), 0) + 1
            if isinstance(code, int) and code != 0:
                bucket["paper_slot_exit_nonzero"] += 1
            host_events.append(("exit", slot, moment, _text(record.get("tick_id"))))
        elif kind == HOST_LOCK_EVENT:
            slot = _text(record.get("slot")) or "unknown"
            bucket = _slot_bucket(slot)
            status = (_text(record.get("status")) or "unknown")
            if status == "not_acquired":
                bucket["paper_host_lock_not_acquired"] += 1
            elif status == "yielded_to_full_scan":
                bucket["paper_host_lock_yielded_to_full_scan"] += 1
            else:
                bucket["paper_host_lock_other"] += 1
            wait = _counter(record.get("wait_seconds"))
            if wait is not None:
                bucket["host_lock_wait_seconds"].append(wait)

        # runner 结果行没有 ``event``，但带 ``timing`` + 顶层 error/risk_snapshot_refresh
        entry = merge_attempt(attempts, record, moment)
        if entry is not None and commit_hint:
            commits[commit_hint] = commits.get(commit_hint, 0) + 1
        refresh = _text(record.get("risk_snapshot_refresh"))
        if refresh:
            key = (entry["attempt_id"] if entry else None, refresh)
            risk_snapshot[key] = risk_snapshot.get(key, 0) + 1

    # ── runtime_commit 分布 ──────────────────────────────────────────────
    commit_summary = {"by_attempt": {}, "by_record": dict(sorted(commits.items())),
                      "attempts": len(attempts), "windows": {}, "transitions": []}
    windows = {}
    for entry in attempts.values():
        commit = entry["runtime_commit"]
        commit_summary["by_attempt"][commit] = commit_summary["by_attempt"].get(commit, 0) + 1
        window = windows.setdefault(commit, {"attempts": 0, "first": None, "last": None})
        window["attempts"] += 1
        moment = entry["started_at"]
        if moment is not None:
            if window["first"] is None or moment < window["first"]:
                window["first"] = moment
            if window["last"] is None or moment > window["last"]:
                window["last"] = moment
    commit_summary["windows"] = {
        commit: {
            "attempts": data["attempts"],
            "first_seen": data["first"].isoformat() if data["first"] else None,
            "last_seen": data["last"].isoformat() if data["last"] else None,
        }
        for commit, data in sorted(windows.items(),
                                   key=lambda item: (item[1]["first"] or dt.datetime.max.replace(
                                       tzinfo=LOCAL_TZ)))
    }
    # 版本切换点：按 started_at 排序后 commit 发生变化的位置 —— 这就是"部署后
    # 样本"的分界；没有切换就等于本窗口内没有新版本上线。
    ordered_attempts = sorted(
        (entry for entry in attempts.values() if entry["started_at"] is not None),
        key=lambda entry: entry["started_at"],
    )
    previous_commit = None
    for entry in ordered_attempts:
        if previous_commit is not None and entry["runtime_commit"] != previous_commit:
            commit_summary["transitions"].append({
                "at": entry["started_at"].isoformat(),
                "from": previous_commit, "to": entry["runtime_commit"],
                "slot": entry["slot"],
            })
        previous_commit = entry["runtime_commit"]
    commit_summary["latest_commit"] = (
        ordered_attempts[-1]["runtime_commit"] if ordered_attempts else None
    )
    commit_summary["newest_commit_at"] = (
        ordered_attempts[-1]["started_at"].isoformat() if ordered_attempts else None
    )

    # ── risk_scans：实际完成次数 + 跨入口重复身份 ────────────────────────
    by_status = {}
    by_slot_status = {}
    completed_identities = {}
    identity_slots = {}
    identity_statuses = {}
    completed_started = []
    identity_incomplete = 0
    scans_total = 0
    for entry in attempts.values():
        slot = entry["slot"]
        for scan in entry["risk_scans"]:
            status = _text(scan.get("status"))
            if status is None:
                continue
            scans_total += 1
            by_status[status] = by_status.get(status, 0) + 1
            by_slot_status.setdefault(slot, {})
            by_slot_status[slot][status] = by_slot_status[slot].get(status, 0) + 1
            ident = scan_identity(scan)
            if ident is None:
                identity_incomplete += 1
                continue
            identity_slots.setdefault(ident, set()).add(slot)
            identity_statuses.setdefault(ident, set()).add(status)
            if status.lower() == "completed":
                completed_identities[ident] = completed_identities.get(ident, 0) + 1
                moment = parse_time(scan.get("started_at"))
                if moment is not None:
                    completed_started.append((moment, ident))

    duplicates = []
    completed_in_multiple_slots = []
    for ident, slot_set in identity_slots.items():
        if len(slot_set) > 1:
            item = {
                "cycle_id": ident[0], "asof_date": ident[1], "scan_minute": ident[2],
                "slots": sorted(slot_set),
                "statuses": sorted(identity_statuses.get(ident, set())),
            }
            duplicates.append(item)
            if "completed" in identity_statuses.get(ident, set()):
                owners = [slot for slot in slot_set
                          if "completed" in _identity_status_by_slot(
                              attempts, ident, slot)]
                if len(owners) > 1:
                    completed_in_multiple_slots.append(dict(item, completed_slots=sorted(owners)))
    duplicates.sort(key=lambda item: (item["asof_date"] or "", item["scan_minute"] or ""))

    # ── 风控开始间隔：不同 scan_minute 相邻 started_at 差值 ───────────────
    ordered = sorted(completed_started, key=lambda pair: pair[0])
    intervals = []
    cross_session = []
    previous = None
    for moment, ident in ordered:
        if previous is not None and ident != previous[1]:
            gap = (moment - previous[0]).total_seconds()
            if gap >= 0:
                item = {"seconds": round(gap, 3), "from": previous[0].isoformat(),
                        "to": moment.isoformat(),
                        "from_minute": previous[1][2], "to_minute": ident[2]}
                # 只有"同一天、同一个交易窗口（相邻不超过 30 分钟）"的一对才计入
                # 节拍统计；跨午休（11:24→13:00）与跨日单独列出，避免每天凭空
                # 出现一次 96 分钟"违规"。
                same_day = previous[0].date() == moment.date()
                if same_day and gap <= 30 * 60:
                    intervals.append(gap)
                else:
                    cross_session.append(item)
        previous = (moment, ident)

    # ── 每 slot 尝试级成功/失败 + 失败归类 ───────────────────────────────
    slot_outcomes = {}
    for entry in attempts.values():
        slot = entry["slot"]
        bucket = slot_outcomes.setdefault(slot, {
            "attempts": 0, "by_status": {}, "buckets": {}, "failure_reasons": {},
            "elapsed_seconds": [], "host_lock_wait_seconds": [],
        })
        bucket["attempts"] += 1
        status = entry["status"] or "unknown"
        bucket["by_status"][status] = bucket["by_status"].get(status, 0) + 1
        group = status_bucket(entry["status"])
        bucket["buckets"][group] = bucket["buckets"].get(group, 0) + 1
        if entry["elapsed_seconds"] is not None:
            bucket["elapsed_seconds"].append(entry["elapsed_seconds"])
        if entry["host_lock_wait"] is not None:
            bucket["host_lock_wait_seconds"].append(entry["host_lock_wait"])
        if group in ("failed", "in_progress"):
            reason = classify_failure(entry)
            bucket["failure_reasons"][reason] = bucket["failure_reasons"].get(reason, 0) + 1

    # ── 阶段秒数分布 ─────────────────────────────────────────────────────
    stage_rows = {}
    for entry in attempts.values():
        for name, stage in entry["stages"].items():
            if not isinstance(stage, dict):
                continue
            row = stage_rows.setdefault(str(name), {
                "n": 0, "seconds": [], "calls": [], "errors": [], "attempts_with_errors": 0,
            })
            seconds = _counter(stage.get("seconds"))
            calls = _counter(stage.get("calls"))
            errors = _counter(stage.get("errors"))
            if seconds is not None:
                row["seconds"].append(seconds)
                row["n"] += 1
            if calls is not None:
                row["calls"].append(calls)
            if errors is not None:
                row["errors"].append(errors)
                if errors > 0:
                    row["attempts_with_errors"] += 1

    def _stage_render(name):
        row = stage_rows[name]
        return {
            "n": row["n"],
            "seconds": describe(row["seconds"]),
            "calls_total": int(sum(row["calls"])) if row["calls"] else None,
            "errors_total": int(sum(row["errors"])) if row["errors"] else None,
            "attempts_with_errors": row["attempts_with_errors"],
        }

    named = {name: _stage_render(name) for name in NAMED_STAGES if name in stage_rows}
    quote_stages = {name: _stage_render(name) for name in sorted(stage_rows)
                    if name.startswith(QUOTE_PREFIX)}
    other_stages = {name: _stage_render(name) for name in sorted(stage_rows)
                    if name not in NAMED_STAGES and not name.startswith(QUOTE_PREFIX)}

    refresh_dist = {}
    for (_attempt, refresh), count in risk_snapshot.items():
        refresh_dist[refresh] = refresh_dist.get(refresh, 0) + count

    return {
        "version": 1,
        "label": label,
        "generated_at": dt.datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
        "source": source_meta or {},
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
            "records_in_window": window_records,
            "records_total": len(records),
            "observed_first": min(times).isoformat() if times else None,
            "observed_last": max(times).isoformat() if times else None,
        },
        "parse": {
            "records": len(records), "unparsed": unparsed_hint, "repairs": repaired,
            "attempts": len(attempts), "host_events": len(host_events),
        },
        "runtime_commit": commit_summary,
        "slots": {
            slot: {
                "paper_slot_start": data["paper_slot_start"],
                "paper_slot_exit": data["paper_slot_exit"],
                "paper_slot_exit_nonzero": data["paper_slot_exit_nonzero"],
                "exit_codes": dict(sorted(data["exit_codes"].items())),
                "paper_slot_timing": data["paper_slot_timing"],
                "timing_status": dict(sorted(data["timing_status"].items())),
                "paper_host_lock_not_acquired": data["paper_host_lock_not_acquired"],
                "paper_host_lock_yielded_to_full_scan":
                    data["paper_host_lock_yielded_to_full_scan"],
                "paper_host_lock_other": data["paper_host_lock_other"],
                "host_lock_wait_seconds": describe(data["host_lock_wait_seconds"]),
                "timing_host_lock_wait_seconds":
                    describe(data["timing_host_lock_wait_seconds"]),
            }
            for slot, data in sorted(slots.items())
        },
        "risk_scans": {
            "markers_total": scans_total,
            "by_status": dict(sorted(by_status.items())),
            "by_slot_status": {slot: dict(sorted(value.items()))
                               for slot, value in sorted(by_slot_status.items())},
            "completed_unique_identities": len(completed_identities),
            "completed_markers": sum(completed_identities.values()),
            "identity_incomplete": identity_incomplete,
            "identities_seen": len(identity_slots),
            "cross_slot_duplicate_identities": duplicates[:50],
            "cross_slot_duplicate_count": len(duplicates),
            "completed_in_multiple_slots": completed_in_multiple_slots[:20],
        },
        "risk_scan_intervals": {
            "adjacent_pairs": len(intervals),
            "seconds": describe(intervals),
            "excluded_cross_session": cross_session[:20],
            "excluded_cross_session_count": len(cross_session),
        },
        "slot_outcomes": {
            slot: {
                "attempts": data["attempts"],
                "by_status": dict(sorted(data["by_status"].items())),
                "buckets": dict(sorted(data["buckets"].items())),
                "failure_reasons": dict(sorted(data["failure_reasons"].items())),
                "elapsed_seconds": describe(data["elapsed_seconds"]),
                "host_lock_wait_seconds": describe(data["host_lock_wait_seconds"]),
            }
            for slot, data in sorted(slot_outcomes.items())
        },
        "stages": {"named": named, "quote": quote_stages, "other": other_stages},
        "risk_snapshot_refresh": dict(sorted(refresh_dist.items())),
        "notes": [
            "阶段秒数为 attempt 内累计值（含子阶段），不能相加得到总耗时。",
            "外层 slot status=completed 不代表又扫了一次风控；以 risk_scans 标记为准。",
            "风控开始间隔只统计同日同交易窗口内的相邻对，跨午休/跨日单独列出。",
            "runtime_commit=unknown 的样本不能归入任何版本。",
            "GBK 乱码只影响中文值；本报告的归类字段全部是 ASCII 的 JSON 字段名。",
        ],
    }


def _identity_status_by_slot(attempts, ident, slot):
    found = set()
    for entry in attempts.values():
        if entry["slot"] != slot:
            continue
        for scan in entry["risk_scans"]:
            if scan_identity(scan) != ident:
                continue
            status = _text(scan.get("status"))
            if status:
                found.add(status)
    return found


def in_window(moment, since, until):
    if moment is None:
        # 无时刻的记录（旧日志缺 started_at）在无窗口过滤时仍计入；有窗口时无法归属。
        return since is None and until is None
    if since is not None and moment < since:
        return False
    if until is not None and moment > until:
        return False
    return True


# ─────────────────────────────────────────────────────────────── 渲染层 ──
def _fmt(value, unit=""):
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return f"{text}{unit}"
    return f"{value}{unit}"


def render_markdown(report):
    src = report["source"]
    window = report["window"]
    parse = report["parse"]
    out = []
    out.append("## 1. 样本与来源\n")
    out.append("| 项 | 值 |")
    out.append("| --- | --- |")
    out.append(f"| label | `{report.get('label')}` |")
    out.append(f"| 来源 | {src.get('kind')} / {src.get('method', src.get('path', ''))} |")
    if src.get("host"):
        out.append(f"| 主机 / 远端文件 | `{src.get('host')}` `{src.get('remote_log')}` |")
    out.append(f"| 拉取字节 | {src.get('bytes')} |")
    out.append(f"| 时间窗 | {window.get('since')} → {window.get('until')} |")
    out.append(f"| 观测到的首/末记录时刻 | {window.get('observed_first')} / {window.get('observed_last')} |")
    out.append(f"| 窗口内记录 / 总记录 | {window.get('records_in_window')} / {window.get('records_total')} |")
    out.append(f"| 解析失败行（unparsed） | {parse.get('unparsed')} |")
    out.append(f"| 转义修复行（repairs） | {parse.get('repairs')} |")
    out.append(f"| 尝试数（attempt_id 去重） | {parse.get('attempts')} |")
    out.append("")

    out.append("### runtime_commit 分布\n")
    commits = report["runtime_commit"]["by_attempt"] or {}
    windows = report["runtime_commit"].get("windows") or {}
    out.append("| runtime_commit | 尝试数 | 首次出现 | 末次出现 |")
    out.append("| --- | --- | --- | --- |")
    for commit, count in sorted(commits.items(), key=lambda kv: -kv[1]):
        data = windows.get(commit) or {}
        out.append(f"| `{commit}` | {count} | {data.get('first_seen')} | {data.get('last_seen')} |")
    if not commits:
        out.append("| — | 0 | — | — |")
    out.append("")
    transitions = report["runtime_commit"].get("transitions") or []
    if transitions:
        out.append("版本切换点（`started_at` 序上 commit 发生变化）：")
        for item in transitions:
            out.append(f"- {item['at']}：`{item['from']}` → `{item['to']}`（slot={item['slot']}）")
    else:
        out.append("版本切换点：**无**（本窗口内 runtime_commit 没有变化 → 没有可用的“部署后”样本）。")
    out.append(f"最新样本：`{report['runtime_commit'].get('latest_commit')}` @ "
               f"{report['runtime_commit'].get('newest_commit_at')}")
    out.append("")

    out.append("## 2. 各 slot 事件计数\n")
    out.append("字段：`paper_slot_start` / `paper_slot_exit(+exit_code)` / `paper_slot_timing(+status)` / "
               "`paper_host_lock(status)`。")
    out.append("")
    out.append("| slot | start | exit | exit≠0 | timing | timing 状态 | host_lock not_acquired / yielded / other |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for slot, data in report["slots"].items():
        statuses = ", ".join(f"{k}:{v}" for k, v in data["timing_status"].items()) or "—"
        out.append(
            f"| `{slot}` | {data['paper_slot_start']} | {data['paper_slot_exit']} | "
            f"{data['paper_slot_exit_nonzero']} | {data['paper_slot_timing']} | {statuses} | "
            f"{data['paper_host_lock_not_acquired']} / "
            f"{data['paper_host_lock_yielded_to_full_scan']} / {data['paper_host_lock_other']} |"
        )
    out.append("")

    scans = report["risk_scans"]
    out.append("## 3. 风控扫描（`timing.risk_scans[]`）\n")
    out.append(f"- 标记总数：**{scans['markers_total']}**；"
               f"状态分布：`{json.dumps(scans['by_status'], ensure_ascii=False)}`")
    out.append(f"- **实际完成的不同扫描身份**（`status=completed` 的 "
               f"`(cycle_id, asof_date, scan_minute)` 去重）：**{scans['completed_unique_identities']}**"
               f"（completed 标记 {scans['completed_markers']} 条）")
    out.append(f"- 见过的身份总数：{scans['identities_seen']}；身份字段不全的标记：{scans['identity_incomplete']}")
    out.append(f"- 同身份出现在多个 slot 的次数：**{scans['cross_slot_duplicate_count']}**"
               f"（其中 completed 归属多入口：{len(scans['completed_in_multiple_slots'])}）")
    if scans["by_slot_status"]:
        out.append("")
        out.append("| slot | 标记状态分布 |")
        out.append("| --- | --- |")
        for slot, data in scans["by_slot_status"].items():
            out.append(f"| `{slot}` | `{json.dumps(data, ensure_ascii=False)}` |")
    if scans["cross_slot_duplicate_identities"]:
        out.append("")
        out.append("重复身份样例（最多 10 条）：")
        out.append("")
        out.append("| asof_date | scan_minute | slots | statuses |")
        out.append("| --- | --- | --- | --- |")
        for item in scans["cross_slot_duplicate_identities"][:10]:
            out.append(f"| {item['asof_date']} | {item['scan_minute']} | "
                       f"{', '.join(item['slots'])} | {', '.join(item['statuses'])} |")
    out.append("")

    intervals = report["risk_scan_intervals"]
    out.append("### 风控开始间隔（相邻不同 scan_minute 的 `started_at` 差值）\n")
    stats = intervals["seconds"]
    out.append(f"- 相邻对数：{intervals['adjacent_pairs']}；"
               f"p50 **{_fmt(stats['p50'], ' s')}** / p95 {_fmt(stats['p95'], ' s')} / "
               f"max **{_fmt(stats['max'], ' s')}** / min {_fmt(stats['min'], ' s')}")
    out.append(f"- 被排除的跨时段对（>30 分钟或跨日）：{intervals['excluded_cross_session_count']}")
    if intervals["excluded_cross_session"]:
        for item in intervals["excluded_cross_session"][:5]:
            out.append(f"  - {item['from']} → {item['to']}：{item['seconds']} s")
    out.append("")

    out.append("## 4. slot 尝试级结果与失败归类\n")
    out.append("口径：一个 `attempt_id` 只计一次；失败归类只读 JSON 字段 "
               "`error` / `retry_reason` / `risk_scan_state` / `status`，不匹配中文 reason。")
    out.append("")
    out.append("| slot | attempts | status 分布 | 失败/中断归类 | elapsed p50 / max |")
    out.append("| --- | --- | --- | --- | --- |")
    for slot, data in report["slot_outcomes"].items():
        statuses = ", ".join(f"{k}:{v}" for k, v in data["by_status"].items()) or "—"
        reasons = ", ".join(f"{k}:{v}" for k, v in data["failure_reasons"].items()) or "—"
        elapsed = data["elapsed_seconds"]
        out.append(f"| `{slot}` | {data['attempts']} | {statuses} | {reasons} | "
                   f"{_fmt(elapsed['p50'], ' s')} / {_fmt(elapsed['max'], ' s')} |")
    out.append("")

    out.append("## 5. 阶段秒数分布（attempt 内累计值，不可相加）\n")
    out.append("| 阶段 | n | p50 | max | calls 合计 | errors 合计 | 有错误的 attempt |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for group_name, group in (("named", report["stages"]["named"]),
                              ("quote.*", report["stages"]["quote"])):
        for name, data in group.items():
            out.append(f"| `{name}` | {data['n']} | {_fmt(data['seconds']['p50'], ' s')} | "
                       f"{_fmt(data['seconds']['max'], ' s')} | {_fmt(data['calls_total'])} | "
                       f"{_fmt(data['errors_total'])} | {data['attempts_with_errors']} |")
    out.append("")
    others = sorted(report["stages"]["other"].items(),
                    key=lambda item: -(item[1]["seconds"]["p50"] or 0))[:8]
    if others:
        out.append("其他阶段（按 p50 从大到小取前 8，仅作定位用）：")
        out.append("")
        out.append("| 阶段 | n | p50 | max | errors 合计 |")
        out.append("| --- | --- | --- | --- | --- |")
        for name, data in others:
            out.append(f"| `{name}` | {data['n']} | {_fmt(data['seconds']['p50'], ' s')} | "
                       f"{_fmt(data['seconds']['max'], ' s')} | {_fmt(data['errors_total'])} |")
    out.append("")

    out.append("## 6. risk_snapshot_refresh 状态分布\n")
    out.append(f"`{json.dumps(report['risk_snapshot_refresh'], ensure_ascii=False)}`")
    out.append("")
    return "\n".join(out)


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="scheduler.log 运行时观测聚合")
    parser.add_argument("--source", choices=("remote", "local"), default="remote")
    parser.add_argument("--log", default="reports/scheduler.log",
                        help="--source local 时的本机日志路径")
    parser.add_argument("--remote-log", default=DEFAULT_REMOTE_LOG)
    parser.add_argument("--pull", choices=("auto", "sftp", "exec"), default="auto")
    parser.add_argument("--lines", type=int, default=20000, help="远端最近 N 行（默认 20000）")
    parser.add_argument("--since", help="窗口起点，如 2026-11-03T09:34（无时区按 +08:00）")
    parser.add_argument("--until", help="窗口终点，如 2026-11-03T13:10")
    parser.add_argument("--label", default=None, help="给这份报告起个名字")
    parser.add_argument("--json", dest="json_path", default=None, help="结构化结果输出文件")
    parser.add_argument("--markdown", dest="md_path", default=None, help="人读摘要输出文件")
    parser.add_argument("--keep-raw", default=None, help="保留下载的原始日志到该路径")
    args = parser.parse_args(argv)

    since = parse_time(args.since) if args.since else None
    until = parse_time(args.until) if args.until else None
    if args.since and since is None:
        raise SystemExit(f"--since 解析失败：{args.since}")
    if args.until and until is None:
        raise SystemExit(f"--until 解析失败：{args.until}")

    data, source_meta = fetch(args)
    text, kept = materialize(data, args.keep_raw)
    if kept:
        source_meta["raw_path"] = kept

    parse_stats = {"unparsed": 0, "repairs": 0}
    records = list(iter_records(text, parse_stats))
    report = summarize(records, since=since, until=until, source_meta=source_meta,
                       parse_stats=parse_stats, label=args.label)
    report["parse"]["chars"] = len(text)

    markdown = render_markdown(report)
    if args.md_path:
        path = Path(args.md_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown + "\n", encoding="utf-8")
        print(f"[markdown] {path}")
    print(markdown)
    if args.json_path:
        path = Path(args.json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n[json] {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
