/* PR-55：顶层语句与 var 初始化 —— **保持原始顺序**，由 src/app.js 最后 import。
   顺序即执行顺序，重排等于改行为。 */
import { $, charts } from "./core/dom.js";
import { PAPER_VIEW_KEY, SETTINGS_SECTION_KEY, activatePage, installHashRouting, installWorkspaceTabRails, registerMarketGateLoader, registerPageLoader, registerPaperViewLoader, renderClock, restoreAppNavigation, syncThemeControl, syncWorkspaceTabRails } from "./core/navigation.js";
import { installAdaptiveOperatorActions, loadAdaptive, loadEvolutionStatus, setAdaptiveSection } from "./features/adaptive.js";
import { loadPaperExecution } from "./features/execution.js";
import { installPaperActions, loadPaper, loadPaperResearchValidation, loadPaperStrategyCenter, loadPortfolioWorkspace } from "./features/paper.js";
import { installPaperRiskActions, loadPaperRisk } from "./features/risk.js";
import { installSelectionActions, loadDataValidity, loadMarketGate, loadPaperSelection, loadStrategies } from "./features/selection.js";
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

/* 资金分摊 / 调参的应用与回滚由 p-adaptive 自己持有（data-adaptive-action 委托），
   启动文件不再挂这四个 window 全局、也不再承接业务写操作。 */
installAdaptiveOperatorActions(document);
installPaperRiskActions(document);
installPaperActions(document);
installSelectionActions(document);

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
