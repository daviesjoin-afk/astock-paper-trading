# 已验收 PR 的人工集成步骤

2026-11-04 复核：五份 PR 均为 Draft、未合并，以下完整 HEAD 的所有检查成功。
检查结果只适用于这些提交；后续变更必须重新验证。

| PR | HEAD | 当前目标分支 | 成功检查 |
| --- | --- | --- | --- |
| #346 | `97c79d81e33adb606e1a19e2aeac184b1f6aae39` | master | 12/12 |
| #347 | `580237184962e8befb493834956fc42f12090570` | codex/v2-clean-room-rust-kernel | 13/13 |
| #348 | `9e6c61d589c66ca7440ac4ee0b71a8d32101817a` | codex/v2-quote-evidence | 12/12 |
| #349 | `f3642db1daac8e3c81a29af0e0a554699e5376ed` | codex/v2-source-trust | 12/12 |
| #350 | `37b2710630197bdb66d32b653fd1afb1bf309e57` | codex/v2-reproducible-builds | 12/12 |

## 推荐：保留提交历史的普通合并

仓库允许普通 merge commit。由人工依次审核、合并 #346 → #347 → #348 → #349 → #350。
暂不删除任何上游分支，直至所有下游完成集成。不要同时合并多份或启用自动合并。

每次人工合并一份后，对紧邻的下一份执行：

1. 获取最新远端，记录刚合并后的 master SHA，备份下一份的旧 HEAD。
2. 把下一份 PR 的目标分支改为 master；将最新 master 普通合并到其开发分支并推送。
   上游原提交仍是祖先，因此差异应只剩该 PR 的独立能力。
3. 核对 Files changed：#347 只有行情证据固定与引用；#348 只有来源信任边界；
   #349 只有构建锁定；#350 只有日历与时段。检查无重复引入和 V1 改动。
4. 记录新的完整 HEAD，等待该 HEAD 的全部检查，包括 Rust 单元测试/Release、
   Python 契约、前端、真实 HTTP/SQLite smoke 及全仓检查。旧 HEAD 全绿不能替代。
   CI 若检出合并预览，核对预览树与新 HEAD 的差异，确认测试覆盖实际集成内容。
5. 人工复核新差异和 CI 后，再决定是否合并这一份；然后重复上述步骤。

全部完成后，确认 master 包含五份能力且其 CI 成功，记录稳定基线 SHA。
历史持仓修复应从该基线建立独立 Draft PR；当前只准备验证分支，不增加第六层业务 PR。

## 如果人工选择 squash 或 rebase 合并

这会改写上游提交身份。仅修改下游 base 可能重新显示上游所有文件，不能把它当作独立差异。
#347 还含有合并提交及冲突处理，不能盲目逐个 cherry-pick 或直接 rebase 整条历史。

应先保存该下游原 HEAD 与它已验收的原 base，导出**两者之间**的独立补丁：

```bash
git diff --binary --full-index OLD_BASE OLD_HEAD -- v2/ .github/workflows/v2.yml > delta.patch
```

在刚集成后的 master 上建立临时分支，使用 `git apply --index --3way delta.patch`，
逐项处理仅限 V2 的冲突、审核差异、提交并执行完整验证。补丁文件保存在工作区外或忽略目录。
确认恢复的是完整独立差异，不能丢掉冲突处理中形成的业务约束。

如需保留原 PR，由人工在备份后使用带明确旧 SHA 的 force-with-lease 更新该 PR 分支，
再把 base 改为 master。所有相关 HEAD 都会改变，必须重新审核和运行 CI。
重建后仍按 #346 → #350 顺序处理，不把旧提交的成功记录转记到新提交上。

本文是人工操作说明；本轮没有执行合并、改 base、强制推送已有 PR 或部署。
