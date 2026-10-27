# -*- coding: utf-8 -*-
"""部署 profile 判定必须只有一处实现，且行为与文档一致。

模块化审计 §7 的记录：`install-centos9.sh` 里有两段互相矛盾的守卫 —— 前一段
只接受 ``native``，后一段接受 ``native|native-centos9``。照 `deploy/README.md`
显式传 ``native-centos9`` 会在前一段被拒绝，而脚本后半段却按 native-centos9 干活。

现在判定收在 `deploy/lib/deploy_profile.sh` 的 ``resolve_deploy_profile`` 里，
脚本 source 它，本测试也 source 它 —— 测的是**真正被部署用的那份实现**，
不是一份复制品。
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "deploy_profile.sh"
INSTALL = ROOT / "deploy" / "install-centos9.sh"
def _find_bash():
    """Prefer whatever ``bash`` this box actually has on PATH.

    旧的 ProgramFiles 猜测在 Git 装到别处（例如用户级 hermes/git）时会让整个守卫
    静默 skip —— 跳过等于没守。
    """
    candidate = shutil.which("bash")
    if candidate:
        return candidate
    if os.name == "nt":
        program_files = os.environ.get("ProgramFiles")
        if program_files:
            guess = Path(program_files) / "Git/bin/bash.exe"
            if guess.is_file():
                return str(guess)
    return None


BASH = _find_bash()


@unittest.skipUnless(BASH and Path(BASH).is_file(), "bash unavailable")
class DeployProfileGuardTests(unittest.TestCase):
    def _resolve(self, profile):
        environment = {k: v for k, v in os.environ.items()
                       if k != "ASTOCK_DEPLOY_PROFILE"}
        if profile is not None:
            environment["ASTOCK_DEPLOY_PROFILE"] = profile
        script = 'source "$1"; resolve_deploy_profile'
        return subprocess.run(
            [BASH, "-c", script, "bash", str(LIB)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=environment, timeout=60,
        )

    def test_documented_profiles_resolve_to_native_centos9(self):
        for requested in (None, "native", "native-centos9"):
            with self.subTest(requested=requested):
                result = self._resolve(requested)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("native-centos9\n", result.stdout)

    def test_docker_profiles_are_refused_before_anything_changes(self):
        for requested in ("docker", "docker-compose", "docker-compose-server"):
            with self.subTest(requested=requested):
                result = self._resolve(requested)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("Docker profile", result.stderr)
                self.assertIn(requested, result.stderr)

    def test_unknown_profile_is_refused_with_the_closed_set(self):
        result = self._resolve("native-debian")
        self.assertEqual(2, result.returncode)
        self.assertIn("未知", result.stderr)
        self.assertIn("native-centos9", result.stderr)

    def test_installer_has_no_second_copy_of_the_decision(self):
        text = INSTALL.read_text(encoding="utf-8")
        self.assertIn('source "${SOURCE_DIR}/deploy/lib/deploy_profile.sh"', text)
        self.assertIn('DEPLOY_PROFILE="$(resolve_deploy_profile)"', text)
        # 旧的两段守卫必须消失：一旦有人再写一份 case/if 判定，这里就红。
        self.assertNotIn('case "${DEPLOY_PROFILE}" in', text)
        self.assertNotIn('${ASTOCK_DEPLOY_PROFILE:-native}', text)

    def test_both_scripts_parse(self):
        for path in (LIB, INSTALL):
            with self.subTest(path=path.name):
                result = subprocess.run([BASH, "-n", str(path)],
                                        capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=60)
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
