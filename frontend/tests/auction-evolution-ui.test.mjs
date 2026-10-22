import assert from 'node:assert/strict';
import test from 'node:test';
import { appliedTuningIds, renderDualAiRuns, renderEvolutionLog, renderAuctionReview } from '../src/features/adaptive.js';
import { dataValidityBadge, renderPaperSelection } from '../src/features/selection.js';

function withTarget(run) {
  const previous=globalThis.document;
  const target={innerHTML:''};
  globalThis.document={getElementById:()=>target};
  try { run(target); } finally { globalThis.document=previous; }
}

test('旧空对象和新版数组均可读取，非法落地记录不能生成操作按钮',()=>{
  assert.deepEqual(appliedTuningIds({}),[]);
  assert.deepEqual(appliedTuningIds(null),[]);
  assert.deepEqual(appliedTuningIds('["tq_breakout"]'),['tq_breakout']);
  assert.equal(appliedTuningIds({unexpected:1}),null);
  withTarget(target=>{
    renderDualAiRuns([{id:6,status:'both_hold',applied_ids:{},reviewers:{}}]);
    assert.match(target.innerHTML,/#6/);
    assert.doesNotMatch(target.innerHTML,/回滚|应用共识/);
    renderDualAiRuns([{id:7,status:'consensus',applied_ids:['tq_breakout'],reviewers:{}}]);
    assert.match(target.innerHTML,/rollbackAdaptiveTunerOverlay/);
  });
});

test('进化日志显示中文初始化与校验含义，不截断未知详情',()=>{
  withTarget(target=>{
    renderEvolutionLog([{event_type:'legacy_bootstrap',created_at:'2026-09-14T09:00:44+08:00',detail:{scope_key:'__global__',validation_state:'validated',violations:[]}}]);
    assert.match(target.innerHTML,/历史参数初始化/);
    assert.match(target.innerHTML,/全局参数 · 校验通过 · 无违规项/);
    renderEvolutionLog([{event_type:'custom',detail:{note:'<script>'+('详细信息'.repeat(30))}}]);
    assert.match(target.innerHTML,/&lt;script&gt;/);
    assert.match(target.innerHTML,new RegExp('详细信息'.repeat(30)));
  });
});

test('选股实际成功时显示已入选及选出时间，并说明开盘重跑策略',()=>{
  withTarget(target=>{
    renderPaperSelection({found:true,trade_date:'2026-10-22',strategies:[{status:'ok',strategy_id:'tq_breakout',updated_at:'2026-10-22T08:50:05+08:00',picks:[{rank_no:1,code:'000001',name:'示例',reasons:[]}]}]});
    assert.match(target.innerHTML,/已入选 1 只/);
    assert.doesNotMatch(target.innerHTML,/未运行/);
    assert.match(target.innerHTML,/选出时间：2026-10-22 08:50:05/);
    assert.match(target.innerHTML,/开盘不会自动重跑此表/);
  });
});

test('行情降级和历史数据缺口分开提示，正常及未知状态不会误报需补',()=>{
  const complete={coverage:{coverage_pct:100,fresh_pct:99.9},factor_cache:{eligible_factor_coverage_pct:99.2}};
  assert.equal(dataValidityBadge({...complete,status:'ok',source_health:{healthy:true}}),'正常');
  assert.equal(dataValidityBadge({...complete,status:'degraded',source_health:{healthy:false}}),'行情降级');
  assert.equal(dataValidityBadge({...complete,status:'ok',live_snapshot:{status:'degraded'}}),'行情降级');
  assert.equal(dataValidityBadge({status:'degraded',coverage:{fresh_pct:80}}),'需补历史数据');
  assert.equal(dataValidityBadge({}),'状态待核验');
});

test('竞价意图与价格复核分开展示，模型文字被转义且不捏造正确率',()=>{
  withTarget(target=>{
    renderAuctionReview({reviews:[{run_id:1,slot:'ai1',code:'000001',intent:'accumulation',direction_verdict:'pending',rationale:'<script>test</script>',evidence_grade:'price_only'}]});
    assert.match(target.innerHTML,/可能吸筹/);
    assert.match(target.innerHTML,/等待后续有效报价/);
    assert.match(target.innerHTML,/仅价格证据/);
    assert.match(target.innerHTML,/&lt;script&gt;/);
    assert.doesNotMatch(target.innerHTML,/准确率.*100/);
  });
});
