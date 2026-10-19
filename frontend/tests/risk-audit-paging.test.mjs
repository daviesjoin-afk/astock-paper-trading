import assert from 'node:assert/strict';
import test from 'node:test';
import { applyPaperRiskAuditFilter, loadPaperAudit, renderPaperAudit } from '../src/features/risk.js';

test('风控历史使用数据库筛选、游标及最后一次请求，预取 Promise 被正确等待', async () => {
  const previous={document:globalThis.document,fetch:globalThis.fetch};
  const nodes={paperRiskAuditHost:{innerHTML:''},paperRiskAccountFilter:{value:''},paperRiskLevelFilter:{value:''},paperRiskDateFilter:{value:''}};
  const urls=[];
  const payload=(label,next=null)=>({accounts:[],alerts:[{id:1,name:label}],pagination:{total:101,has_next:!!next,next_cursor:next}});
  globalThis.document={getElementById:id=>nodes[id]||null};
  globalThis.fetch=async url=>{urls.push(url);return {ok:true,json:async()=>payload('历史结果','41')};};
  try{
    let release;
    const pending=loadPaperAudit('current',new Promise(resolve=>{release=resolve;}));
    assert.equal(nodes.paperRiskAuditHost.innerHTML,'');
    release({data:payload('预取完成','51')});await pending;
    assert.match(nodes.paperRiskAuditHost.innerHTML,/预取完成/);
    assert.equal(urls.length,0);
    await loadPaperAudit('next');
    assert.match(urls.at(-1),/cursor=51/);
    assert.match(nodes.paperRiskAuditHost.innerHTML,/第 2 页/);
    nodes.paperRiskAccountFilter.value='sector_rotation';nodes.paperRiskDateFilter.value='2026-09-01';
    await applyPaperRiskAuditFilter();
    assert.match(urls.at(-1),/date=2026-09-01/);
    assert.match(urls.at(-1),/account_id=sector_rotation/);
    assert.doesNotMatch(urls.at(-1),/cursor=/);
    assert.match(nodes.paperRiskAuditHost.innerHTML,/第 1 页/);
    let resolveOld;
    globalThis.fetch=url=>new Promise(resolve=>{resolveOld=()=>resolve({ok:true,json:async()=>payload('旧查询结果')});});
    const old=applyPaperRiskAuditFilter();
    globalThis.fetch=async()=>({ok:true,json:async()=>payload('最新查询结果')});
    nodes.paperRiskDateFilter.value='2026-09-02';await applyPaperRiskAuditFilter();
    resolveOld();await old;
    assert.match(nodes.paperRiskAuditHost.innerHTML,/最新查询结果/);
    assert.doesNotMatch(nodes.paperRiskAuditHost.innerHTML,/旧查询结果/);
  }finally{
    for(const [key,value] of Object.entries(previous)){
      if(value===undefined) delete globalThis[key];else globalThis[key]=value;
    }
  }
});

test('空历史、查询总数、真实等级与末页按钮明确显示', () => {
  const html=renderPaperAudit({alerts:[],pagination:{total:0,has_next:false}}, {date:'2026-09-01',level:'watch'});
  assert.match(html,/共 0 条/);
  assert.match(html,/所选条件下没有风控事件/);
  assert.match(html,/id="paperRiskAuditNext"[^>]* disabled/);
  assert.doesNotMatch(html,/value="blocked"/);
  assert.match(html,/value="2026-09-01"/);
});
