/* PR-55：顶层语句与 var 初始化 —— **保持原始顺序**，由 src/app.js 最后 import。
   顺序即执行顺序，重排等于改行为。 */
import { api, apiPost } from "./core/api.js";
import { $, charts } from "./core/dom.js";
import { PAPER_VIEW_KEY, SETTINGS_SECTION_KEY, activatePage, installHashRouting, installWorkspaceTabRails, registerMarketGateLoader, registerPageLoader, registerPaperViewLoader, renderClock, restoreAppNavigation, syncThemeControl, syncWorkspaceTabRails } from "./core/navigation.js";
import { loadAdaptive, loadEvolutionStatus, renderAdaptive, setAdaptiveSection } from "./features/adaptive.js";
import { loadPaperExecution } from "./features/execution.js";
import { loadPaper, loadPaperResearchValidation, loadPaperStrategyCenter, loadPortfolioWorkspace } from "./features/paper.js";
import { loadPaperRisk } from "./features/risk.js";
import { loadDataValidity, loadMarketGate, loadPaperSelection, loadStrategies } from "./features/selection.js";
import { loadSettings } from "./features/settings.js";
import { loadStrategyWorkbench } from "./features/strategies.js";

window.__ASTOCK_ADAPTIVE_UI_BUILD__='20261012-runtime-followup';

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
// Composition root: navigation only routes; it never imports feature modules.
function _once(loader){
  var used=false;
  return function(){ if(used) return Promise.resolve(); used=true; return loader(); };
}
registerMarketGateLoader(loadMarketGate);
// Static selection inputs are fetched once; reopening the page only refreshes
// the persisted selection result. The explicit refresh button reloads all.
registerPageLoader('p-select', _once(loadStrategies));
registerPageLoader('p-select', _once(loadDataValidity));
registerPageLoader('p-select', loadPaperSelection);
registerPageLoader('p-select', loadStrategies, 'refresh');
registerPageLoader('p-select', loadDataValidity, 'refresh');
registerPageLoader('p-select', loadPaperSelection, 'refresh');
registerPageLoader('p-adaptive', loadAdaptive);
registerPageLoader('p-adaptive', loadAdaptive, 'refresh');
registerPageLoader('p-settings', function(refresh){ return loadSettings(!!refresh); });
registerPageLoader('p-settings', function(refresh){ return loadSettings(!!refresh); }, 'refresh');
registerPageLoader('p-strategies', loadStrategyWorkbench);

registerPaperViewLoader('portfolio', function(refresh){ return loadPaper(refresh ? {refresh:true} : undefined); });
registerPaperViewLoader('activity', function(refresh){ return loadPaper(refresh ? {refresh:true} : undefined); });
registerPaperViewLoader('history', function(refresh){ return loadPaper(refresh ? {refresh:true} : undefined); });
registerPaperViewLoader('all', function(refresh){ return loadPaper(refresh ? {refresh:true} : undefined); });
registerPaperViewLoader('strategy', loadPaperStrategyCenter);
registerPaperViewLoader('research', loadPaperResearchValidation);
registerPaperViewLoader('risk', function(refresh){ return loadPaperRisk(!!refresh); });
registerPaperViewLoader('execution', function(refresh){ return loadPaperExecution(!!refresh); });
registerPaperViewLoader('allocation', loadPortfolioWorkspace);

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
