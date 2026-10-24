import { test, expect } from '../fixtures.js';

async function seed(page){
  await page.route('**/api/adaptive/dual-ai/status',route=>route.fulfill({json:{slots:{},consensus_rules:{weight_magnitude_ratio:0.6},auction_review:{tracking:{captured_at:'2026-10-22T09:24:00+08:00',observations:4,tracked_stocks:25},reviews:[{run_id:6,slot:'ai1',code:'000001',intent:'support',expected_direction:'up',confidence:55,evidence_grade:'price_only',direction_verdict:'supported',change_pct:0.8,observed_at:'2026-10-22T09:40:00+08:00',rationale:'竞价价格逐步抬升，暂作护盘假设',counter_evidence:'缺少虚拟匹配量，无法确认真实意图'}]}}}));
  await page.route('**/api/adaptive/dual-ai/runs?*',route=>route.fulfill({json:{runs:[{id:6,status:'both_hold',mode:'intraday',trigger:'manual',created_at:'2026-10-22T09:10:00+08:00',applied_ids:{},reviewers:{ai1:{status:'ok'},ai2:{status:'ok'}}}]}}));
  await page.route('**/api/adaptive/evolution/log?*',route=>route.fulfill({json:{log:[{event_type:'legacy_bootstrap',created_at:'2026-09-14T09:00:44+08:00',detail:{scope_key:'__global__',validation_state:'validated',violations:[]}}]}}));
  await page.route('**/api/paper-selection',route=>route.fulfill({json:{found:true,trade_date:'2026-10-22',strategies:[{label:'策略1',strategy_id:'tq_breakout',strategy_name:'短线日内做T',status:'ok',updated_at:'2026-10-22T08:50:05+08:00',picks:[{rank_no:1,code:'000001',name:'示例个股',price:10,pct:1,score:2.5,reasons:['示例候选']}]}]}}));
  await page.route('**/api/data-validity',route=>route.fulfill({json:{status:'degraded',coverage:{universe:5557,kline_files:5551,coverage_pct:100,fresh_pct:99.9},live_snapshot:{rows:5555,valid_today_rows:5555,status:'degraded'},factor_cache:{eligible_factor_coverage_pct:99.2},source_health:{healthy:false},warnings:['行情源暂时降级']}}));
}

for(const mobile of [false,true]){
  test(`竞价与进化页面状态可读，视觉模块已移除（${mobile?'手机':'桌面'}）`,async({page},testInfo)=>{
    await page.setViewportSize(mobile?{width:390,height:900}:{width:1440,height:1100});
    await seed(page);
    await page.goto('/');
    await expect(page.locator('#dataValidityStatus')).toHaveText('行情降级');
    await page.locator('#p-select .module-tab[data-strategy=""]').click();
    await expect(page.locator('#selectResult')).toContainText('已入选 1 只');
    await expect(page.locator('#selectResult')).toContainText('选出时间：2026-10-22 08:50:05');
    await expect(page.locator('#selectResult')).toContainText('开盘不会自动重跑此表');
    await page.locator('.tab[data-page="p-adaptive"]').click();
    await page.locator('[data-section="ai"]').click();
    await expect(page.locator('#aiRunsTable')).toContainText('#6');
    await expect(page.locator('#aiRunsTable')).not.toContainText('加载调参记录失败');
    await expect(page.locator('#aiLogTable')).toContainText('历史参数初始化');
    await expect(page.locator('#aiLogTable')).toContainText('全局参数 · 校验通过 · 无违规项');
    await expect(page.locator('#aiAuctionReview')).toContainText('可能护盘');
    await expect(page.locator('#aiAuctionReview')).toContainText('后续价格方向支持');
    await expect(page.locator('#aiAuctionReview')).toContainText('仅价格证据');
    await expect(page.locator('#aiModlensStatus,#aiImageUrl,#aiModlensResult')).toHaveCount(0);
    await expect(page.locator('#adaptiveResult')).not.toContainText('modlens');
    await page.screenshot({path:testInfo.outputPath('evolution-ui.png'),fullPage:true});
  });
}
