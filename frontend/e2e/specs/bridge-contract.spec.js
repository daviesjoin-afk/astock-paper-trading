// PR-57：inline handler 桥接契约回归。
//
// 背景：评审发现两个真实缺陷——空态 CTA 调用的 wbCloneFirstBuiltin 从未实现、
// settings 的 inline 重试调用了不存在的 saveAiKey，两者都会在用户点击时抛
// ReferenceError。这类缺陷的根因是"inline onclick 依赖 window.* 桥接"，
// 因此这里直接锁住桥接契约：DOM 里出现的 onclick 处理器必须在 window 上存在。
import { test, expect, openWorkbench, gotoPage } from "../fixtures.js";

/** 从页面 DOM 收集所有 inline onclick 里调用的顶层函数名。 */
async function collectInlineHandlers(page) {
  return page.evaluate(() => {
    const names = new Set();
    document.querySelectorAll("[onclick]").forEach((node) => {
      const code = node.getAttribute("onclick") || "";
      for (const m of code.matchAll(/([A-Za-z_$][\w$]*)\s*\(/g)) {
        names.add(m[1]);
      }
    });
    return Array.from(names);
  });
}

test.describe("inline handler 桥接契约", () => {
  test("策略工坊：DOM 上的 onclick 处理器都已在 window 上桥接", async ({ page }) => {
    await openWorkbench(page);
    const names = await collectInlineHandlers(page);
    const missing = await page.evaluate(
      (list) => list.filter((n) => typeof window[n] !== "function"),
      names,
    );
    expect(missing, `策略工坊里存在未桥接的 inline 处理器：${missing.join(", ")}`).toEqual([]);
    expect(names.length, "未采集到任何 inline 处理器，说明采集逻辑失效（测试将是空转）").toBeGreaterThan(0);
  });

  test("设置中心：DOM 上的 onclick 处理器都已在 window 上桥接", async ({ page }) => {
    await gotoPage(page, "settings-nav");
    await expect(page.getByTestId("settings-result")).not.toContainText("正在读取");
    const names = await collectInlineHandlers(page);
    const missing = await page.evaluate(
      (list) => list.filter((n) => typeof window[n] !== "function"),
      names,
    );
    expect(missing, `设置中心里存在未桥接的 inline 处理器：${missing.join(", ")}`).toEqual([]);
    expect(names.length, "未采集到任何 inline 处理器，说明采集逻辑失效（测试将是空转）").toBeGreaterThan(0);
  });

  test("关键桥接函数显式存在（空态复制内置策略 / AI 配置保存）", async ({ page }) => {
    await openWorkbench(page);
    const present = await page.evaluate(() => ({
      cloneFirstBuiltin: typeof window.wbCloneFirstBuiltin,
      cloneStrategy: typeof window.wbCloneStrategy,
      saveSettingsKey: typeof window.saveSettingsKey,
    }));
    expect(present.cloneFirstBuiltin).toBe("function");
    expect(present.cloneStrategy).toBe("function");
    expect(present.saveSettingsKey).toBe("function");
  });
});
