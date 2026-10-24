import { test, expect } from '../fixtures.js';

for(const mobile of [false,true]){
  test(`资金核验审计用中文说明原因和暂缓动作（${mobile?'手机':'桌面'}）`,async({page},testInfo)=>{
    await page.setViewportSize(mobile?{width:390,height:844}:{width:1440,height:1000});
    const reason=['exact_cycle_asof_nav_unavailable','exact_cycle_asof_pending_capacity_unavailable',
      'exact_headroom_unavailable','exact_market_quote_evidence_unavailable','exact_market_value_by_account_unavailable',
      'exact_market_valued_classification_unavailable','exact_nav_unavailable','exact_pending_by_account_unavailable',
      'exact_pending_total_unavailable','exact_pending_unavailable','exact_strategy_version_return_series_unavailable',
      'exact_used_unavailable'].join('；');
    await page.route('**/api/paper/risk-audit?*',route=>route.fulfill({json:{accounts:[],alerts:[{
      time:'2026-10-22 10:04:40',account_name:'超强主力股',code:'000001',name:'示例标的',level:'watch',reason,
      action:'portfolio_allocation_deferred',quote_validation:'cross_source_unavailable',execution_mode:'active',
      rule_version:'paper-risk-v4',linked_signal:{id:42,label:'待执行委托',intended_date:'2026-10-22'}
    }],pagination:{total:1,has_next:false}}}));
    await page.goto('/');
    await page.getByTestId('main-nav-paper').click();
    await page.locator('#paperModuleTabs [data-paper-view="activity"]').click();
    const host=page.locator('#paperRiskAuditHost');
    await expect(host).toContainText('本周期当前净值无法核验');
    await expect(host).toContainText('资金分配暂缓，等待证据补齐');
    await expect(host).toContainText('待执行委托 #42');
    await expect(host).toContainText('风控规则第4版');
    await expect(host).not.toContainText(/exact_|portfolio_allocation|paper-risk|_待处理|_不可用/);
    await host.scrollIntoViewIfNeeded();
    await page.screenshot({path:testInfo.outputPath('risk-audit-chinese.png'),fullPage:true});
  });
}

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
