#!/usr/bin/env bash
# 部署 profile 的**唯一**判定入口。
#
# 背景（模块化审计 §7）：`install-centos9.sh` 里曾有两段互相不一致的守卫 ——
# 前一段只接受 `native`，后一段接受 `native|native-centos9` 并拒绝 Docker。
# 结果是文档推荐的 `ASTOCK_DEPLOY_PROFILE=native-centos9` 会在前一段被直接拒绝。
# 判定只留这一份实现，脚本 `source` 它，测试也 `source` 它。
#
# 本文件被 source 时**不许有副作用**：不 set -e、不读写文件、不 echo。

# 原生 profile 的闭集。`native` 是历史别名，落盘一律归一成 `native-centos9`。
ASTOCK_NATIVE_PROFILES="native native-centos9"

resolve_deploy_profile() {
  local requested="${ASTOCK_DEPLOY_PROFILE:-native-centos9}"
  case "${requested}" in
    native|native-centos9)
      printf '%s\n' 'native-centos9'
      return 0
      ;;
    docker|docker-compose|docker-compose-server)
      printf '拒绝：ASTOCK_DEPLOY_PROFILE=%s 属于 Docker profile。\n' "${requested}" >&2
      printf '请使用 Docker 部署流程；本脚本只安装 native-centos9 profile。\n' >&2
      return 2
      ;;
    *)
      printf '未知 ASTOCK_DEPLOY_PROFILE=%s；可选值：%s。\n' \
        "${requested}" "${ASTOCK_NATIVE_PROFILES}" >&2
      return 2
      ;;
  esac
}
