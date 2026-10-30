#!/usr/bin/env python3
"""Oracle Cloud 服务器运维客户端（Windows 侧，paramiko 直连，不依赖 sshpass/rsync）。

子命令
    probe        端口 + SSH banner + （能登录时）系统体检
    watch        轮询 22 端口，一旦可达立即退出 0（用于等 OCI 安全列表放行）
    exec -- CMD  远程执行命令（自动改用 sudo）
    push         把当前 HEAD 打 git archive 包上传并解包到 /tmp/astock-src
    bootstrap    上传 deploy/oracle/bootstrap-ubuntu.sh 并分阶段执行
    logs         tail 容器日志 / 调度日志
    health       容器状态 + /api/health + cron + 磁盘
    key          把本机公钥追加到服务器 authorized_keys（之后可免密登录）

连接参数来源（优先级：命令行 > 环境变量 > 仓库根 .env.oracle）
    ORACLE_HOST / ORACLE_USER / ORACLE_PASSWORD / ORACLE_PORT
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONF_FILE = REPO / ".env.oracle"


def load_conf() -> dict[str, str]:
    conf: dict[str, str] = {}
    if CONF_FILE.is_file():
        for raw in CONF_FILE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    for key in ("ORACLE_HOST", "ORACLE_USER", "ORACLE_PASSWORD", "ORACLE_PORT"):
        if os.environ.get(key):
            conf[key] = os.environ[key]
    conf.setdefault("ORACLE_PORT", "22")
    conf.setdefault("ORACLE_USER", "ubuntu")
    return conf


def tcp_open(host: str, port: int, timeout: float = 6.0) -> tuple[bool, str]:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True, ""
    except Exception as exc:  # noqa: BLE001 - 诊断场景需要原始异常名
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        s.close()


def connect(conf: dict[str, str]):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        conf["ORACLE_HOST"],
        port=int(conf["ORACLE_PORT"]),
        username=conf["ORACLE_USER"],
        password=conf.get("ORACLE_PASSWORD") or None,
        timeout=20,
        banner_timeout=20,
        auth_timeout=20,
        look_for_keys=True,
        allow_agent=False,
    )
    return client


def sudo_ok(client) -> bool:
    _, out = run(client, "sudo -n true && echo SUDO_NOPASS", check=False)
    return "SUDO_NOPASS" in out


def run(client, cmd: str, check: bool = True, timeout: float | None = None) -> tuple[int, str]:
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    rc = stdout.channel.recv_exit_status()
    merged = out + (("\n[stderr] " + err) if err.strip() else "")
    if check and rc != 0:
        raise RuntimeError(f"远端命令失败 rc={rc}: {cmd}\n{merged}")
    return rc, merged


def sudo_run(client, cmd: str, password: str, check: bool = True) -> tuple[int, str]:
    if sudo_ok(client):
        return run(client, f"sudo -E bash -lc {shq(cmd)}", check=check)
    return run(client, f"printf '%s\\n' {shq(password)} | sudo -S -E bash -lc {shq(cmd)}", check=check)


def shq(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


# ─────────────────────────────────────────────────────────── subcommands ──
def cmd_probe(conf: dict[str, str], _args) -> int:
    host = conf["ORACLE_HOST"]
    print(f"目标 {host}  user={conf['ORACLE_USER']}")
    print("── TCP 探测 ──")
    for port in (22, 80, 443, 18600):
        ok, err = tcp_open(host, port)
        print(f"  {port:<6} {'OPEN' if ok else 'CLOSED/TIMEOUT  ' + err}")

    ok22, _ = tcp_open(host, 22)
    if not ok22:
        print("\n结论：22 端口不可达 —— 报文在 OCI 边缘被丢弃，登录无从谈起。")
        print("排查顺序：① OCI 控制台实例是否 Running；② 公网 IP 是否还挂在这台实例上；")
        print("③ 安全列表 / NSG 是否放行 0.0.0.0/0 TCP 22；④ 实例内 iptables 是否 REJECT 掉。")
        return 2

    try:
        client = connect(conf)
    except Exception as exc:  # noqa: BLE001
        print(f"\n22 可达但登录失败：{type(exc).__name__}: {exc}")
        print("常见原因：PasswordAuthentication no（Oracle 镜像默认只认密钥）/ 密码错 / 用户不存在。")
        return 3

    with client:
        script = (
            "echo HOST=$(hostname); echo USER=$(whoami); "
            "echo OS=$(. /etc/os-release && echo $PRETTY_NAME); "
            "echo KERNEL=$(uname -r); echo ARCH=$(uname -m); "
            "echo CPU=$(nproc); echo MEM=$(awk '/MemTotal/{printf \\\"%.1fG\\\", $2/1024/1024}' /proc/meminfo); "
            "echo SWAP=$(free -g | awk '/Swap/{print $2\\\"G\\\"}'); "
            "echo DISK=$(df -h / | awk 'NR==2{print $4\\\" free of \\\"$2}'); "
            "echo TZ=$(timedatectl show -p Timezone --value 2>/dev/null); "
            "echo DOCKER=$(command -v docker >/dev/null && docker --version || echo none); "
            "echo COMPOSE=$(docker compose version --short 2>/dev/null || echo none); "
            "echo SUDO=$(sudo -n true 2>/dev/null && echo nopasswd || echo password)"
        )
        print("\n── 系统体检 ──")
        print(run(client, f"bash -lc {shq(script)}")[1])
    return 0


def cmd_watch(conf: dict[str, str], args) -> int:
    host = conf["ORACLE_HOST"]
    deadline = time.time() + args.timeout
    n = 0
    while time.time() < deadline:
        n += 1
        ok, err = tcp_open(host, int(conf["ORACLE_PORT"]), timeout=8)
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] try#{n} {'OPEN' if ok else 'closed'} {err}", flush=True)
        if ok:
            print("22 端口已放行，可以继续 push/bootstrap。")
            return 0
        time.sleep(args.interval)
    print(f"{args.timeout}s 内仍未放行（最后状态 closed），停止轮询。")
    return 2


def cmd_exec(conf: dict[str, str], args) -> int:
    cmd = " ".join(args.cmd) if args.cmd else sys.stdin.read()
    with connect(conf) as client:
        rc, out = sudo_run(client, cmd, conf.get("ORACLE_PASSWORD", ""), check=False)
        print(out)
    return rc


def cmd_push(conf: dict[str, str], args) -> int:
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    tar_path = Path(os.environ.get("TEMP", "/tmp")) / "astock-src.tar.gz"
    subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar.gz", "-o", str(tar_path), "HEAD"],
        check=True,
    )
    size_mb = tar_path.stat().st_size / 1024 / 1024
    print(f"打包 HEAD={sha} → {tar_path} ({size_mb:.1f} MB)")
    with connect(conf) as client:
        sftp = client.open_sftp()
        remote = "/tmp/astock-src.tar.gz"
        sftp.put(str(tar_path), remote)
        sftp.close()
        remote_src = args.src_dir
        run(client, f"rm -rf {shq(remote_src)} && mkdir -p {shq(remote_src)} && "
                    f"tar xzf {remote} -C {shq(remote_src)} && ls {shq(remote_src)} | head -5")
        print(f"已解包到 {remote_src}")
    return 0


def cmd_bootstrap(conf: dict[str, str], args) -> int:
    script = REPO / "deploy" / "oracle" / "bootstrap-ubuntu.sh"
    if not script.is_file():
        print(f"缺少 {script}", file=sys.stderr)
        return 2
    sha = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    with connect(conf) as client:
        sftp = client.open_sftp()
        sftp.put(str(script), "/tmp/bootstrap-ubuntu.sh")
        sftp.close()
        envs = " ".join(
            f"{k}={shq(v)}" for k, v in args.env.items()
        ) if args.env else ""
        cmd = (
            f"{envs} bash /tmp/bootstrap-ubuntu.sh --stage {args.stage} --commit {sha}"
            + (" --force" if args.force else "")
            + (" --skip-build" if args.skip_build else "")
        )
        print(f"远程执行（stage={args.stage}, commit={sha[:7]}）…", flush=True)
        rc, out = sudo_run(client, cmd, conf.get("ORACLE_PASSWORD", ""), check=False)
        print(out)
    return rc


def cmd_logs(conf: dict[str, str], args) -> int:
    with connect(conf) as client:
        parts = [
            "echo '== docker ps =='; docker ps --format '{{.Names}}\t{{.Status}}' 2>/dev/null",
            f"echo '== astock-codex 最后 {args.lines} 行 =='; docker logs astock-codex --tail {args.lines} 2>&1",
            f"echo '== scheduler.log 最后 {args.lines} 行 =='; "
            f"tail -n {args.lines} {shq(args.app_dir + '/reports/scheduler.log')} 2>/dev/null",
        ]
        print(run(client, f"bash -lc {shq('; '.join(parts))}", check=False)[1])
    return 0


def cmd_health(conf: dict[str, str], args) -> int:
    d = shq(args.app_dir)
    script = f"""
echo "== 容器 =="; docker ps --format '{{{{.Names}}}}\t{{{{.Status}}}}' 2>/dev/null
echo "== API =="; curl -s -o /dev/null -w 'http_code=%{{http_code}}\\n' --max-time 8 http://127.0.0.1:18600/api/health
echo "== cron =="; ls -l /etc/cron.d/astock-codex /etc/cron.d/astock-three-strategy-start 2>/dev/null || echo missing
echo "== 磁盘 =="; df -h / | tail -1
echo "== 内存 =="; free -h | head -3
echo "== 部署身份 =="; cat {d}/.deploy-source.json 2>/dev/null || echo missing
"""
    with connect(conf) as client:
        print(run(client, f"bash -lc {shq(script)}", check=False)[1])
    return 0


def cmd_key(conf: dict[str, str], _args) -> int:
    pub = next(
        (p for p in (Path.home() / ".ssh" / "id_ed25519.pub", Path.home() / ".ssh" / "id_rsa.pub") if p.is_file()),
        None,
    )
    if pub is None:
        print("本机没有 ~/.ssh/id_ed25519.pub 或 id_rsa.pub，先 ssh-keygen -t ed25519 生成。", file=sys.stderr)
        return 2
    key = pub.read_text(encoding="utf-8").strip()
    with connect(conf) as client:
        run(client, "mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys "
                    "&& chmod 600 ~/.ssh/authorized_keys")
        sftp = client.open_sftp()
        with sftp.open("/home/" + conf["ORACLE_USER"] + "/.ssh/authorized_keys", "a") as fh:
            fh.write(key + "\n")
        sftp.close()
    print(f"已追加 {pub.name} 到服务器 authorized_keys（之后可免密登录）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Oracle 服务器运维客户端")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="端口 + SSH + 系统体检").set_defaults(func=cmd_probe)

    w = sub.add_parser("watch", help="轮询 22 端口直到可达")
    w.add_argument("--interval", type=int, default=30)
    w.add_argument("--timeout", type=int, default=3600)
    w.set_defaults(func=cmd_watch)

    e = sub.add_parser("exec", help="远程执行命令")
    e.add_argument("cmd", nargs=argparse.REMAINDER)
    e.set_defaults(func=cmd_exec)

    pu = sub.add_parser("push", help="git archive 上传源码")
    pu.add_argument("--src-dir", default="/tmp/astock-src")
    pu.set_defaults(func=cmd_push)

    b = sub.add_parser("bootstrap", help="服务器初始化 / 上线")
    b.add_argument("--stage", default="all", choices=["base", "docker", "app", "all"])
    b.add_argument("--force", action="store_true")
    b.add_argument("--skip-build", action="store_true")
    b.add_argument("--env", action="append", default=[], metavar="K=V")
    b.set_defaults(func=cmd_bootstrap)

    lg = sub.add_parser("logs", help="容器与调度日志")
    lg.add_argument("--lines", type=int, default=60)
    lg.add_argument("--app-dir", default="/opt/astock-codex")
    lg.set_defaults(func=cmd_logs)

    h = sub.add_parser("health", help="容器 / API / cron / 磁盘")
    h.add_argument("--app-dir", default="/opt/astock-codex")
    h.set_defaults(func=cmd_health)

    sub.add_parser("key", help="安装本机公钥").set_defaults(func=cmd_key)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if args.cmd == "bootstrap" and args.env:
        args.env = dict(item.split("=", 1) for item in args.env)
    conf = load_conf()
    if not conf.get("ORACLE_HOST"):
        print("缺少 ORACLE_HOST（写仓库根 .env.oracle 或设环境变量）", file=sys.stderr)
        return 2
    return args.func(conf, args)


if __name__ == "__main__":
    sys.exit(main())
