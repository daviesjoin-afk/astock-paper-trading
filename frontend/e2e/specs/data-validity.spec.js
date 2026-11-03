import { test, expect } from '../fixtures.js';

test('选股页合并重复读取，慢空响应不能覆盖有效结果', async ({ page }) => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  let calls = 0;
  await page.route('**/api/paper-selection', async route => {
    calls += 1;
    if (calls === 1) await pending;
    await route.fulfill({ json: {
      found: true, trade_date: '2026-10-22', strategies: [{
        label: '策略1', strategy_id: 'tq_breakout', strategy_name: '短线日内做T',
        status: 'ok', updated_at: '2026-10-22T08:50:05+08:00',
        picks: [{ rank_no: 1, code: '000001', name: '示例个股', price: 10, pct: 1, score: 2.5 }],
      }],
    } });
  });
  await page.goto('/');
  await page.locator('#p-select .module-tab[data-strategy=""]').click();
  release();
  await expect(page.locator('#selectResult')).toContainText('已入选 1 只');
  await expect(page.locator('#selectResult')).toContainText('示例个股');
  expect(calls).toBe(1);
});

for (const dark of [false, true]) {
  test(`数据有效性面板使用 authority 覆盖且在${dark ? '暗色' : '亮色'}窄屏可读`, async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.route('**/api/data-validity', route => route.fulfill({ json: {
      status: 'degraded',
      coverage: { universe: 5557, kline_files: 5552, coverage_pct: 99.9, fresh_pct: 99.8 },
      reference: { expected_reference_date: '2026-11-02', latest_trade_date: '2026-11-02' },
      live_snapshot: {
        rows: 5300, valid_today_rows: 5000, fresh_ratio: 0.94,
        min_fresh_ratio: 0.9, saved_at: '2026-11-03T06:00:00+00:00',
        age_seconds: 60, status: 'degraded',
      },
      market_data: { data_fact: {
        status: 'degraded', observed_at: '2026-11-03T14:00:00+08:00',
      } },
      factor_cache: { eligible_factor_coverage_pct: 99.7, factor_date: '2026-11-02' },
      source_health: { healthy: true },
      auction_tracking: {}, warnings: [], incremental_update: {},
    } }));
    await page.goto('/');
    if (dark) await page.evaluate(() => { localStorage.setItem('darkMode', 'true'); document.body.classList.add('dark'); });
    const cards = page.locator('#dataValidityCards');
    await expect(cards).toContainText('94.0%');
    await expect(cards).toContainText('5000 / 5300 行');
    await expect(cards).toContainText('2026-11-03T14:00:00+08:00');
    await expect(cards).not.toContainText('90.0%');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    const contrast = await page.locator('#dataValidityDetails').evaluate(el => {
      const color = getComputedStyle(el).color.match(/\d+/g).map(Number);
      const background = getComputedStyle(el).backgroundColor.match(/\d+/g).map(Number);
      const luminance = rgb => rgb.slice(0, 3).map(v => {
        const s = v / 255;
        return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      }).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
      const a = luminance(color), b = luminance(background);
      return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
    });
    expect(contrast).toBeGreaterThanOrEqual(4.5);
  });
}
