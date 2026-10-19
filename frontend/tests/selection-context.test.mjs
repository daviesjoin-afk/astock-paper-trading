import assert from 'node:assert/strict';
import test from 'node:test';
import { renderSelectionResult, selectionContextNotice } from '../src/features/selection.js';

test('待补充数据明确提示，不解释为没有合格股票', () => {
  const html=selectionContextNotice({context_pending:['finance','sentiment']});
  assert.match(html,/财报、情绪数据待补充/);
  assert.match(html,/请稍后再次选股/);
  assert.match(html,/不能据此判断没有合格股票/);
  assert.equal(selectionContextNotice({}), '');
  assert.equal(selectionContextNotice({context_pending:[]}), '');
  assert.equal(selectionContextNotice({context_pending:['<script>','__proto__']}), '');
});

test('未齐数据不显示刚刚完成，数据齐全时恢复完成状态', () => {
  const target={innerHTML:''};
  const previous=globalThis.document;
  globalThis.document={getElementById:()=>target};
  try{
    const result={picks:[],count:0,executable_count:0,watchlist_count:0};
    renderSelectionResult({...result,context_pending:['finance']},false);
    assert.match(target.innerHTML,/数据未齐/);
    assert.doesNotMatch(target.innerHTML,/刚刚完成/);
    renderSelectionResult({...result,context_pending:[]},false);
    assert.match(target.innerHTML,/刚刚完成/);
    assert.doesNotMatch(target.innerHTML,/数据待补充/);
  }finally{
    if(previous===undefined) delete globalThis.document;
    else globalThis.document=previous;
  }
});
