# -*- coding: utf-8 -*-
"""modlens 视觉桥接模块：让纯文本模型（如 MiMo v2.5 Pro）获得图像理解能力。

工作原理：
1. 接收图片路径（本地文件或URL）
2. 调用 modlens CLI 将图片转为结构化 JSON（OCR + 布局 + 语义）
3. 将结构化结果格式化为纯文本，注入到 LLM 的 prompt 中

依赖：需要 Node.js 环境，modlens 会通过 npx 自动下载。
"""
from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# modlens CLI 命令模板
MODLENS_CMD = "npx"
MODLENS_PACKAGE = "@liustack/modlens"
MODLENS_TIMEOUT = int(os.getenv("MODLENS_TIMEOUT_SECONDS", "60"))
MODLENS_MAX_BYTES = int(os.getenv("MODLENS_MAX_BYTES", str(20 * 1024 * 1024)))
MODLENS_MAX_OUTPUT_BYTES = int(os.getenv("MODLENS_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024)))
MODLENS_MAX_CONCURRENCY = max(1, int(os.getenv("MODLENS_MAX_CONCURRENCY", "3")))
MODLENS_MAX_PROMPT_CHARS = max(256, int(os.getenv("MODLENS_MAX_PROMPT_CHARS", "4000")))
_MODLENS_SEMAPHORE = threading.BoundedSemaphore(MODLENS_MAX_CONCURRENCY)
# Image analysis is an optional tool, so its default input root is deliberately
# narrow.  Deployments may add upload directories with a platform-separated
# value (comma is accepted too), e.g. MODLENS_ALLOWED_DIRS=/srv/app/uploads,/srv/app/data_cache/modlens_uploads.
_DEFAULT_ALLOWED_DIR = Path(__file__).resolve().parent / "data_cache" / "modlens_uploads"

# 支持的图片格式
SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif", ".svg"}


def _allowed_dirs() -> list[Path]:
    raw = os.getenv("MODLENS_ALLOWED_DIRS", "")
    values = [item.strip() for item in raw.replace(",", os.pathsep).split(os.pathsep) if item.strip()]
    if not values:
        values = [str(_DEFAULT_ALLOWED_DIR)]
    result = []
    for value in values:
        try:
            result.append(Path(value).expanduser().resolve())
        except (OSError, RuntimeError):
            continue
    return result


def _under_allowed_root(candidate: Path) -> bool:
    return any(candidate == root or root in candidate.parents for root in _allowed_dirs())


def _validate_remote_url(value: str) -> tuple[str | None, str | None]:
    """Allow only explicitly configured HTTPS hosts and public IPs."""
    if os.getenv("MODLENS_ALLOW_REMOTE_URLS", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None, "远程图片URL默认禁用"
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
            return None, "仅允许无凭据的HTTPS图片URL"
        allowed = {item.strip().lower().rstrip(".") for item in os.getenv("MODLENS_ALLOWED_HOSTS", "").split(",") if item.strip()}
        if not allowed or not any(host == item or (item.startswith("*.") and host.endswith(item[1:])) for item in allowed):
            return None, "图片URL主机不在白名单"
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                return None, "图片URL解析到受保护地址"
        return value, None
    except (ValueError, OSError, socket.gaierror):
        return None, "图片URL无法安全校验"


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that pins the already validated DNS address."""

    def __init__(self, host, pinned_ip, **kwargs):
        super().__init__(host, **kwargs)
        self._pinned_ip = pinned_ip
        self._server_hostname = host

    def connect(self):  # pragma: no cover - exercised by network integration
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self._server_hostname)


def _download_remote_image(url: str) -> tuple[str | None, str | None]:
    """Download an allow-listed HTTPS image through a pinned socket.

    The previous check resolved DNS and then handed the hostname to npx, so a
    DNS answer change between validation and fetch could reach a private host.
    Redirects are deliberately not followed; every request uses the validated
    address and Host/SNI name.
    """
    checked, error = _validate_remote_url(url)
    if error or not checked:
        return None, error
    parsed = urlparse(checked)
    host = str(parsed.hostname or "").lower().rstrip(".")
    try:
        addresses = sorted({item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)})
    except (OSError, socket.gaierror):
        return None, "图片URL无法安全解析"
    if not addresses:
        return None, "图片URL没有可用地址"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
                continue
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn = _PinnedHTTPSConnection(host, address, port=443, timeout=min(15, MODLENS_TIMEOUT))
            conn.request("GET", path, headers={"Host": host, "Accept": "image/*"})
            response = conn.getresponse()
            if response.status != 200:
                conn.close()
                return None, f"图片URL返回非成功状态: {response.status}"
            length = response.getheader("Content-Length")
            if length and int(length) > MODLENS_MAX_BYTES:
                conn.close()
                return None, f"远程图片超过{MODLENS_MAX_BYTES}字节限制"
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                suffix = ".jpg"
            allowed_root = _allowed_dirs()[0]
            allowed_root.mkdir(parents=True, exist_ok=True)
            tmp = tempfile.NamedTemporaryFile(dir=allowed_root, suffix=suffix, delete=False)
            total = 0
            try:
                with tmp:
                    while True:
                        chunk = response.read(min(1024 * 1024, MODLENS_MAX_BYTES - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MODLENS_MAX_BYTES:
                            raise ValueError(f"远程图片超过{MODLENS_MAX_BYTES}字节限制")
                        tmp.write(chunk)
                    tmp.flush()
                    os.fsync(tmp.fileno())
                conn.close()
                return tmp.name, None
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                conn.close()
                raise
        except (OSError, ValueError, socket.timeout, http.client.HTTPException) as exc:
            return None, f"远程图片下载失败: {type(exc).__name__}"
    return None, "图片URL解析到受保护地址"


def _validate_image_reference(value: str, *, allow_remote: bool = True) -> tuple[str | None, str | None]:
    if not value or "\x00" in str(value) or any(ord(ch) < 32 for ch in str(value)):
        return None, "图片引用无效"
    value = str(value).strip()
    if value.lower().startswith(("http://", "https://")):
        return _validate_remote_url(value) if allow_remote else (None, "远程图片URL不允许")
    raw = Path(value)
    # Relative references are resolved only below the configured upload roots;
    # traversal and arbitrary absolute paths are never accepted.
    if not raw.is_absolute() and ".." in raw.parts:
        return None, "图片路径不允许目录穿越"
    try:
        candidates = [raw] if raw.is_absolute() else [root / raw for root in _allowed_dirs()]
        candidate = next(
            item.resolve(strict=False) for item in candidates
            if _under_allowed_root(item.resolve(strict=False))
            and item.resolve(strict=False).is_file()
        )
    except (OSError, RuntimeError, StopIteration):
        return None, "图片路径不在允许的上传目录或文件不存在"
    try:
        if not candidate.is_file():
            return None, "图片文件不存在"
        if candidate.stat().st_size > MODLENS_MAX_BYTES:
            return None, f"图片文件超过{MODLENS_MAX_BYTES}字节限制"
    except OSError:
        return None, "图片文件不可读"
    if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return None, f"不支持的图片格式: {candidate.suffix.lower()}"
    return str(candidate), None


def _materialize_local_image(value: str) -> tuple[str | None, str | None]:
    """Copy a validated local image through an O_NOFOLLOW descriptor.

    Passing the original path to a child process left a local symlink race
    between validation and execution.  The child now receives a private copy.
    """
    candidate, error = _validate_image_reference(value, allow_remote=False)
    if error or not candidate:
        return None, error
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(candidate, flags)
        stat = os.fstat(fd)
        if not os.path.isfile(candidate) or stat.st_size > MODLENS_MAX_BYTES:
            os.close(fd)
            return None, "图片文件不可读或超过大小限制"
        allowed_root = _allowed_dirs()[0]
        allowed_root.mkdir(parents=True, exist_ok=True)
        suffix = Path(candidate).suffix.lower()
        tmp = tempfile.NamedTemporaryFile(dir=allowed_root, suffix=suffix, delete=False)
        total = 0
        try:
            with os.fdopen(fd, "rb") as src, tmp:
                while True:
                    chunk = src.read(min(1024 * 1024, MODLENS_MAX_BYTES - total + 1))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MODLENS_MAX_BYTES:
                        raise ValueError("图片文件超过大小限制")
                    tmp.write(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
            return tmp.name, None
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            return None, "图片文件复制失败"
    except (OSError, RuntimeError):
        return None, "图片文件不可安全读取"


def _prepare_image_reference(value: str) -> tuple[str | None, str | None, str | None]:
    text = str(value or "").strip()
    if text.lower().startswith(("http://", "https://")):
        prepared, error = _download_remote_image(text)
    else:
        prepared, error = _materialize_local_image(text)
    return prepared, error, prepared


def _check_modlens_available() -> bool:
    """检查 modlens 是否可用。"""
    try:
        result = subprocess.run(
            [MODLENS_CMD, MODLENS_PACKAGE, "doctor"],
            capture_output=True, text=True, timeout=30,
            shell=True if os.name == "nt" else False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def read_image(path: str, prompt: Optional[str] = None) -> dict:
    """读取图片并返回结构化视觉证据。

    Args:
        path: 图片路径（本地绝对路径或 http(s) URL）
        prompt: 可选的额外提示，引导 modlens 关注特定区域

    Returns:
        dict 包含：
        - success: bool
        - ocr_text: str (OCR 提取的全文)
        - layout: list (布局区域列表)
        - semantics: dict (语义信息)
        - raw: dict (modlens 原始输出)
        - error: str (失败时的错误信息)
        - latency_ms: int (耗时毫秒)
    """
    started = time.monotonic()

    if not _MODLENS_SEMAPHORE.acquire(timeout=1):
        return {"success": False, "error": "modlens并发上限已满", "latency_ms": 0}
    path, validation_error, cleanup_path = _prepare_image_reference(path)
    if validation_error:
        _MODLENS_SEMAPHORE.release()
        return {"success": False, "error": validation_error, "latency_ms": 0}

    # 构建 modlens 命令
    cmd = [MODLENS_CMD, MODLENS_PACKAGE, "read-image", "--path", path]
    if prompt:
        cmd.extend(["--prompt", str(prompt)[:MODLENS_MAX_PROMPT_CHARS]])

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(cmd, stdout=stdout_file, stderr=stderr_file, shell=False)
            try:
                process.wait(timeout=MODLENS_TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read(MODLENS_MAX_OUTPUT_BYTES + 1)
            stderr = stderr_file.read(1024 * 1024)
        latency_ms = round((time.monotonic() - started) * 1000)
        if len(stdout) > MODLENS_MAX_OUTPUT_BYTES:
            return {"success": False, "error": f"modlens输出超过{MODLENS_MAX_OUTPUT_BYTES}字节限制", "latency_ms": latency_ms}
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")

        if process.returncode != 0:
            error_msg = (stderr or stdout or "unknown error").strip()[:500]
            return {"success": False, "error": f"modlens 执行失败 (exit {process.returncode}): {error_msg}", "latency_ms": latency_ms}

        # 解析 JSON 输出
        try:
            raw_output = json.loads(stdout)
        except json.JSONDecodeError:
            # 可能输出不是纯 JSON，尝试提取
            stdout = stdout.strip()
            if stdout.startswith("{"):
                try:
                    raw_output = json.loads(stdout[:stdout.rfind("}") + 1])
                except (json.JSONDecodeError, ValueError):
                    return {"success": False, "error": f"modlens 输出无法解析为 JSON: {stdout[:200]}", "latency_ms": latency_ms}
            else:
                return {"success": False, "error": f"modlens 输出格式异常: {stdout[:200]}", "latency_ms": latency_ms}

        # 提取结构化信息
        ocr_text = raw_output.get("ocr", {}).get("full_text", "") or raw_output.get("full_text", "")
        ocr_text = str(ocr_text)[:MODLENS_MAX_OUTPUT_BYTES]
        layout = raw_output.get("layout", []) or raw_output.get("regions", [])
        semantics = raw_output.get("semantics", {}) or {}
        uncertainties = raw_output.get("uncertainties", []) or raw_output.get("uncertainty", [])

        return {
            "success": True,
            "ocr_text": ocr_text,
            "layout": layout,
            "semantics": semantics,
            "uncertainties": uncertainties,
            "raw": raw_output,
            "latency_ms": latency_ms,
        }

    except subprocess.TimeoutExpired:
        latency_ms = round((time.monotonic() - started) * 1000)
        return {"success": False, "error": f"modlens 超时 ({MODLENS_TIMEOUT}s)", "latency_ms": latency_ms}
    except FileNotFoundError:
        return {"success": False, "error": "未找到 Node.js 或 npx，请确保已安装 Node.js", "latency_ms": 0}
    except Exception as e:
        latency_ms = round((time.monotonic() - started) * 1000)
        return {"success": False, "error": f"modlens 调用异常: {type(e).__name__}: {str(e)[:200]}", "latency_ms": latency_ms}
    finally:
        if cleanup_path:
            try:
                os.unlink(cleanup_path)
            except OSError:
                pass
        _MODLENS_SEMAPHORE.release()


def format_for_prompt(vision_result: dict, include_layout: bool = False, include_semantics: bool = True) -> str:
    """将 modlens 视觉结果格式化为适合注入 LLM prompt 的纯文本。

    Args:
        vision_result: read_image() 的返回值
        include_layout: 是否包含布局信息（可能很长）
        include_semantics: 是否包含语义信息

    Returns:
        格式化的文本，可直接拼接到 prompt 中
    """
    if not vision_result.get("success"):
        return f"[图片读取失败: {vision_result.get('error', 'unknown')}]"

    parts = []

    # OCR 文本
    ocr_text = vision_result.get("ocr_text", "").strip()
    if ocr_text:
        parts.append(f"=== 图片文字内容 ===\n{ocr_text}")
    else:
        parts.append("=== 图片文字内容 ===\n（未检测到文字）")

    # 布局信息
    if include_layout and vision_result.get("layout"):
        layout = vision_result["layout"]
        if isinstance(layout, list) and layout:
            layout_lines = []
            for i, region in enumerate(layout[:20]):  # 最多20个区域
                if isinstance(region, dict):
                    text = region.get("text", "") or region.get("content", "")
                    rtype = region.get("type", "") or region.get("label", "")
                    bbox = region.get("bbox", "") or region.get("bounding_box", "")
                    line = f"  [{i+1}]"
                    if rtype:
                        line += f" ({rtype})"
                    if bbox:
                        line += f" @ {bbox}"
                    if text:
                        line += f": {text[:100]}"
                    layout_lines.append(line)
                elif isinstance(region, str):
                    layout_lines.append(f"  [{i+1}] {region[:100]}")
            if layout_lines:
                parts.append(f"=== 布局区域 ({len(layout)}个) ===\n" + "\n".join(layout_lines))

    # 语义信息
    if include_semantics and vision_result.get("semantics"):
        sem = vision_result["semantics"]
        if isinstance(sem, dict) and sem:
            sem_lines = []
            for k, v in sem.items():
                if isinstance(v, (str, int, float, bool)):
                    sem_lines.append(f"  {k}: {v}")
                elif isinstance(v, list) and len(v) <= 5:
                    sem_lines.append(f"  {k}: {v}")
            if sem_lines:
                parts.append(f"=== 语义信息 ===\n" + "\n".join(sem_lines))

    # 不确定性
    uncertainties = vision_result.get("uncertainties", [])
    if uncertainties and isinstance(uncertainties, list):
        unc_str = ", ".join(str(u) for u in uncertainties[:5])
        parts.append(f"=== 不确定项 ===\n{unc_str}")

    return "\n\n".join(parts)


def read_image_to_prompt(path: str, prompt: Optional[str] = None,
                         include_layout: bool = False, include_semantics: bool = True) -> str:
    """一步到位：读取图片并返回可直接注入 prompt 的文本。

    Args:
        path: 图片路径
        prompt: 可选的额外提示
        include_layout: 是否包含布局信息
        include_semantics: 是否包含语义信息

    Returns:
        格式化的文本
    """
    result = read_image(path, prompt)
    return format_for_prompt(result, include_layout=include_layout, include_semantics=include_semantics)


def read_image_base64(path: str) -> Optional[str]:
    """读取本地图片并返回 base64 编码（用于需要内嵌图片的场景）。"""
    path, error = _validate_image_reference(path, allow_remote=False)
    if error or not path:
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")
    except Exception:
        return None


def batch_read_images(paths: list[str], prompt: Optional[str] = None,
                      max_workers: int = 3) -> list[dict]:
    """批量读取多张图片。

    Args:
        paths: 图片路径列表
        prompt: 可选提示（应用于所有图片）
        max_workers: 最大并发数

    Returns:
        每张图片的结果列表
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = [None] * len(paths)

    def _read_one(idx_path):
        idx, path = idx_path
        return idx, read_image(path, prompt)

    max_workers = max(1, min(int(max_workers or 1), MODLENS_MAX_CONCURRENCY))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_read_one, (i, p)) for i, p in enumerate(paths)]
        for future in as_completed(futures):
            try:
                idx, result = future.result()
                results[idx] = result
            except Exception as e:
                # 找到对应的索引
                for i, f in enumerate(futures):
                    if f == future:
                        results[i] = {"success": False, "error": str(e)[:200], "latency_ms": 0}
                        break

    return results


# ─── 自检函数 ───

def self_check() -> dict:
    """自检 modlens 桥接模块是否可用。"""
    available = _check_modlens_available()
    return {
        "modlens_available": available,
        "node_available": _check_node_available(),
        "timeout_seconds": MODLENS_TIMEOUT,
        "supported_formats": sorted(SUPPORTED_EXTENSIONS),
    }


def _check_node_available() -> bool:
    """检查 Node.js 是否可用。"""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=10,
            shell=True if os.name == "nt" else False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


if __name__ == "__main__":
    # 自检
    print("=== modlens 桥接模块自检 ===")
    check = self_check()
    print(json.dumps(check, indent=2, ensure_ascii=False))

    if check["modlens_available"]:
        print("\nmodlens 可用！")
    else:
        print("\nmodlens 不可用，请运行: npm install -g @liustack/modlens")
