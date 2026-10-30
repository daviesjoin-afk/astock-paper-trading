import { test, expect } from '../fixtures.js';

test.use({ operatorUnlocked: true });

test('真实刷新接口只持久登记并立即返回已有快照', async ({ page }) => {
  await page.goto('/');
  await page.getByTestId('main-nav-paper').click();
  await page.locator('#paperModuleTabs [data-paper-view="risk"]').click();
  const before = await (await page.request.get('/api/paper/risk-overview')).json();
  const [response] = await Promise.all([
    page.waitForResponse(r => r.url().endsWith('/api/paper/risk-refresh') && r.request().method() === 'POST'),
    page.locator('#paperRiskRefresh').click(),
  ]);
  expect(response.status()).toBe(200);
  const after = await response.json();
  expect(after.asof).toBe(before.asof);
  expect(after.snapshot_refresh.status).toBe('queued');
  expect(after.refreshing).toBe(true);
  await expect(page.locator('#paperRiskResult')).toContainText('展示快照：等待刷新');
});

for (const mobile of [false, true]) {
  test(`展示快照排队、失败和完成均保留来源时间（${mobile ? '手机' : '桌面'}）`, async ({ page }, testInfo) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    let phase = 'queued';
    let posts = 0;
    const payload = () => ({
      asof: '2026-10-30 10:00:00', accounts: [], position_queue: [],
      snapshot_stale: true, snapshot_age_seconds: 420, snapshot_ttl_seconds: 300,
      refreshing: phase === 'queued', initializing: false,
      data_quality: [{ name: '行情', source: '离线样本', observed_at: '2026-10-30 09:59:00',
        age_seconds: 480, status: 'stale', coverage_pct: null }],
      snapshot_refresh: { status: phase, last_completed_at: '2026-10-30 10:00:00',
        last_error: phase === 'failed' ? 'provider <failure>' : null },
    });
    await page.route('**/api/paper/risk-overview', route => route.fulfill({ json: payload() }));
    await page.route('**/api/paper/risk-refresh', route => {
      expect(route.request().method()).toBe('POST');
      posts += 1;
      return route.fulfill({ json: payload() });
    });
    await page.goto('/');
    await page.getByTestId('main-nav-paper').click();
    await page.locator('#paperModuleTabs [data-paper-view="risk"]').click();
    const view = page.locator('#paperRiskResult');
    await expect(view).toContainText('展示快照：等待刷新');
    await expect(view).toContainText('2026-10-30 09:59:00');
    phase = 'failed';
    await page.locator('#paperRiskRefresh').click();
    await expect(view).toContainText('展示快照：刷新失败');
    await expect(view).toContainText('provider <failure>');
    await expect(view).toContainText('保留已有快照');
    await expect(view).toContainText('快照 2026-10-30 10:00:00');
    await expect(view.locator('failure')).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.screenshot({ path: testInfo.outputPath('snapshot-refresh-failed.png'), fullPage: true });
    phase = 'completed';
    await page.locator('#paperRiskRefresh').click();
    await expect(view).toContainText('展示快照：刷新完成');
    await expect(view).not.toContainText('provider <failure>');
    expect(posts).toBe(2);
    await page.screenshot({ path: testInfo.outputPath('snapshot-refresh-completed.png'), fullPage: true });
  });
}
