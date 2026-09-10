/* PR-55：顶层语句与 var 初始化 —— **保持原始顺序**，由 src/app.js 最后 import。
   顺序即执行顺序，重排等于改行为。 */
import { api, apiPost } from "./core/api.js";
import { $, charts } from "./core/dom.js";
import { PAPER_VIEW_KEY, SETTINGS_SECTION_KEY, activatePage, installHashRouting, installWorkspaceTabRails, renderClock, restoreAppNavigation, syncThemeControl, syncWorkspaceTabRails } from "./core/navigation.js";
import { loadEvolutionStatus, renderAdaptive, setAdaptiveSection } from "./features/adaptive.js";
import { loadPaper } from "./features/paper.js";
import { loadPaperRisk } from "./features/risk.js";
import { loadDataValidity, loadMarketGate, loadStrategies } from "./features/selection.js";
import { normalizeActiveStrategyCopy } from "./features/strategies.js";

window.__ASTOCK_ADAPTIVE_UI_BUILD__='20260910-strategy-console-v1';

window._paperWorkspace=sessionStorage.getItem(PAPER_VIEW_KEY)||'portfolio';

window._settingsSection=sessionStorage.getItem(SETTINGS_SECTION_KEY)||'simulation';

document.querySelectorAll('.tab').forEach(function(t){
  t.setAttribute('aria-current',t.classList.contains('active')?'page':'false');
  t.onclick=function(){activatePage(t.dataset.page);};
});

window.addEventListener('popstate',restoreAppNavigation);

window.addEventListener('resize',syncWorkspaceTabRails,{passive:true});

window.applyAdaptiveAllocation=async function(decisionId){
  if(!window.confirm('确认把 Bandit 决策 #'+decisionId+' 的策略权重应用为共享资金池分摊？'))return;
  try{renderAdaptive(await apiPost('/api/adaptive/allocation/apply?decision_id='+decisionId+'&approved_by=human-ui&confirmed=true'));}
  catch(e){alert('应用失败：'+(e.message||e));}
};

window.rollbackAdaptiveAllocation=async function(accountId){
  if(!window.confirm('确认回滚 '+accountId+' 的资金分摊覆盖到上一版？'))return;
  try{renderAdaptive(await apiPost('/api/adaptive/allocation/rollback?account_id='+encodeURIComponent(accountId)+'&confirmed=true'));}
  catch(e){alert('回滚失败：'+(e.message||e));}
};

window.applyAdaptiveTunerProposal=async function(runId){
  if(!window.confirm('确认把双AI共识运行 #'+runId+' 的提案应用为选股因子覆盖？'))return;
  try{await apiPost('/api/adaptive/tuner/apply?run_id='+runId+'&approved_by=human-ui&confirmed=true');await loadEvolutionStatus();}
  catch(e){alert('应用失败：'+(e.message||e));}
};

window.rollbackAdaptiveTunerOverlay=async function(accountId){
  if(!window.confirm('确认回滚 '+accountId+' 的 AI 调参覆盖到 apply 前状态？'))return;
  try{await apiPost('/api/adaptive/tuner/rollback?account_id='+encodeURIComponent(accountId)+'&confirmed=true');await loadEvolutionStatus();}
  catch(e){alert('回滚失败：'+(e.message||e));}
};

/* 页面加载时自动执行 */
setTimeout(function(){if(document.getElementById('aiStatusCards')) loadEvolutionStatus();},1000);

renderClock();

setInterval(renderClock,1000);

window._paperOrderSide = 'buy';

// ---------- 个股分析 ----------
loadStrategies();

loadDataValidity();

loadMarketGate();

installWorkspaceTabRails();

installHashRouting();
restoreAppNavigation();

document.addEventListener('click', function(e){
  var adaptiveTab=e.target.closest && e.target.closest('#p-adaptive .adaptive-section-tab');
  if(adaptiveTab){
    e.preventDefault();
    setAdaptiveSection(adaptiveTab.dataset.section,adaptiveTab);
    return;
  }
});

if(typeof MutationObserver!=='undefined'){
  // 只规范化本批新增的节点。此前每次 DOM 变更都会 TreeWalker 全量遍历
  // p-paper + p-adaptive 的所有文本节点；大盘一次渲染有几十次 innerHTML
  // 写入，等于几十次全树扫描，是切页/渲染卡顿的主要脚本开销之一。
  var strategyCopyObserver=new MutationObserver(function(mutations){
    for(var i=0;i<mutations.length;i++){
      var added=mutations[i].addedNodes;
      for(var j=0;j<added.length;j++){
        var node=added[j];
        if(node.nodeType===Node.ELEMENT_NODE) normalizeActiveStrategyCopy(node);
        else if(node.nodeType===Node.TEXT_NODE){
          var value=node.nodeValue||'';
          var normalized=value.replace(/(二|两|三|四|五)套(当前)?(模拟账户|策略)/g,'已启用策略').replace(/五策略/g,'已启用策略');
          if(normalized!==value) node.nodeValue=normalized;
        }
      }
    }
  });
  strategyCopyObserver.observe(document.body,{childList:true,subtree:true});
}

if(localStorage.getItem('darkMode')==='true') document.body.classList.add('dark');

syncThemeControl();

// Keep the two live ledger views current without requiring a full-page reload.
// loadPaper() already shares an in-flight request, so a slow response cannot
// create overlapping overview calls or overwrite a newer render.  Risk and
// research keep their own refresh policies and are intentionally not polled
// here.
setInterval(function(){
  var paper=$('p-paper');
  if(!paper || !paper.classList.contains('active')) return;
  if(window._paperWorkspace==='risk') loadPaperRisk(false);
  else if(window._paperWorkspace==='portfolio'||window._paperWorkspace==='activity') loadPaper();
}, 180000);

setInterval(loadMarketGate,180000);

window.onresize = function(){ Object.keys(charts).forEach(function(k){ charts[k].resize(); }); };
