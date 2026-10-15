import { test, expect } from '../fixtures.js';

test('风控历史首屏等待数据，筛选查询数据库并按游标翻页', async ({ page }) => {
  const calls=[];
  await page.route('**/api/paper/risk-audit?*', async route => {
    const query=new URL(route.request().url()).searchParams;
    calls.push(Object.fromEntries(query));
    const older=query.get('cursor')==='50';
    await route.fulfill({json:{
      accounts:[{id:'sector_rotation',name:'板块轮动'}],
      alerts:[{id:older?49:51,time:'2026-09-01 10:00:00',account_id:'sector_rotation',
        account_name:'板块轮动',name:older?'第二页标的':'第一页标的',level:'watch',reason:'合成测试事件'}],
      pagination:{total:51,page_size:50,has_next:!older,next_cursor:older?null:'50'}
    }});
  });
  await page.goto('/');
  await page.getByTestId('main-nav-paper').click();
  await page.locator('#paperModuleTabs [data-paper-view="activity"]').click();
  const host=page.locator('#paperRiskAuditHost');
  await expect(host).toContainText('第一页标的');
  await expect(host).toContainText('共 51 条');
  await host.getByRole('button',{name:'下一页',exact:true}).click();
  await expect(host).toContainText('第二页标的');
  await expect(host).toContainText('第 2 页');
  await expect(host.getByRole('button',{name:'下一页',exact:true})).toBeDisabled();
  expect(calls.at(-1).cursor).toBe('50');
  await host.getByLabel('风控日期').fill('2026-09-01');
  await host.getByLabel('风控日期').blur();
  await expect(host).toContainText('第一页标的');
  expect(calls.at(-1).date).toBe('2026-09-01');
  expect(calls.at(-1).cursor).toBeUndefined();
  await expect(host.getByLabel('风控等级').locator('option[value="blocked"]')).toHaveCount(0);
  await page.screenshot({path:'e2e/.artifacts/risk-audit-paging-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  await expect(host.getByLabel('风控日期')).toBeVisible();
  await page.screenshot({path:'e2e/.artifacts/risk-audit-paging-mobile.png',fullPage:true});
});
