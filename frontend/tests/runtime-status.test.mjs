import assert from 'node:assert/strict';
import test from 'node:test';
import { paperEntryGateText } from '../src/features/paper.js';
import { renderPaperRisk } from '../src/features/risk.js';

test('行情门禁通过不代表资金和委托已获准', () => {
  const text=paperEntryGateText({entry_freeze:{enabled:false},allocation_status:'UNAVAILABLE'});
  assert.match(text,/资金分配和委托核验/);
  assert.doesNotMatch(text,/自动开放/);
  assert.match(paperEntryGateText({}),/待核验/);
  assert.match(paperEntryGateText({entry_freeze:{enabled:true,reason:'<unsafe>'}}),/冻结/);
  assert.doesNotMatch(paperEntryGateText({entry_freeze:{enabled:true,reason:'<unsafe>'}}),/<unsafe>/);
});

test('过期快照显示警示且不再将子项描述为可直接使用', () => {
  const target={innerHTML:''};
  const previous=globalThis.document;
  globalThis.document={getElementById:()=>target};
  try {
    const payload={overall:{},data_quality:[{name:'index',status:'fresh'}],snapshot_age_seconds:345,snapshot_ttl_seconds:300};
    renderPaperRisk({...payload,snapshot_stale:false});
    assert.match(target.innerHTML,/可按既有规则使用/);
    renderPaperRisk({...payload,snapshot_stale:true});
    assert.match(target.innerHTML,/风控快照已过期/);
    assert.match(target.innerHTML,/345/);
    assert.doesNotMatch(target.innerHTML,/可按既有规则使用/);
  } finally {globalThis.document=previous;}
});
