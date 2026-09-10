/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 跨模块依赖（由原单文件作用域推导）
import { $, charts } from "./dom.js";
import { loadAdaptive } from "../features/adaptive.js";
import { loadPaperExecution } from "../features/execution.js";
import { loadPaper, loadPaperResearchValidation, loadPaperStrategyCenter } from "../features/paper.js";
import { loadPaperRisk } from "../features/risk.js";
import { loadDataValidity, loadMarketGate, loadPaperSelection, loadStrategies } from "../features/selection.js";
import { loadSettings } from "../features/settings.js";
import { loadStrategyWorkbench } from "../features/strategies.js";

export var APP_PAGE_KEY='astock.activePage', PAPER_VIEW_KEY='astock.paperView', SETTINGS_SECTION_KEY='astock.settingsSection';

export function activatePage(page, options){
  options=options||{};
  var tab=document.querySelector('.tab[data-page="'+page+'"]');
  if(!tab){page='p-select';tab=document.querySelector('.tab[data-page="p-select"]');}
  document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');x.setAttribute('aria-current','false');});
  document.querySelectorAll('.page').forEach(function(x){x.classList.remove('active');});
  tab.classList.add('active');tab.setAttribute('aria-current','page');$(page).classList.add('active');
  sessionStorage.setItem(APP_PAGE_KEY,page);
  if(options.writeHash!==false){
    history.replaceState(null,'','#'+page.replace(/^p-/,'')+(page==='p-paper'?'/'+(window._paperWorkspace||'portfolio'):(page==='p-settings'?'/'+(window._settingsSection||'simulation'):'')));
  }
  if(page==='p-adaptive') loadAdaptive();
  if(page==='p-settings') loadSettings();
  if(page==='p-strategies'&&typeof loadStrategyWorkbench==='function') loadStrategyWorkbench();
  // 进入策略选股页（含首次加载默认落在本页）时读取一次结果，否则占位文案会一直停在“正在读取…”。
  if(page==='p-select'&&typeof loadPaperSelection==='function') loadPaperSelection();
  if(page==='p-paper'){
    var paperTab=document.querySelector('#p-paper [data-paper-view="'+(window._paperWorkspace||'portfolio')+'"]');
    showPaperWorkspace(window._paperWorkspace||'portfolio',paperTab,{restore:true});
  }
  // 只重绘当前可见页面上的图表：对隐藏页的图表做 resize 纯属浪费，
  // 也是切页瞬间的小卡顿源；rAF 合并到下一帧，避免阻塞本次切换。
  requestAnimationFrame(function(){
    Object.keys(charts).forEach(function(k){
      var el=document.getElementById(k);
      if(el&&el.offsetParent) charts[k].resize();
    });
  });
}

export function restoreAppNavigation(){
  var parts=String(location.hash||'').replace(/^#/,'').split('/');
  var page=parts[0]?'p-'+parts[0]:(sessionStorage.getItem(APP_PAGE_KEY)||'p-select');
  if(page==='p-paper'&&(parts[1]==='adaptive'||sessionStorage.getItem(PAPER_VIEW_KEY)==='adaptive')){
    page='p-adaptive';
    sessionStorage.removeItem(PAPER_VIEW_KEY);
  }
  if(page==='p-paper'&&parts[1]) window._paperWorkspace=parts[1];
  if(page==='p-settings'&&parts[1]&&['simulation','risk','strategy','evolution','execution'].indexOf(parts[1])>=0){ window._settingsSection=parts[1]; sessionStorage.setItem(SETTINGS_SECTION_KEY,parts[1]); }
  activatePage(page,{writeHash:false});
}

/* The two deep workspaces have more tabs than a narrow browser can show.
   Native scrollbars are often configured as overlay-only on Windows, so keep a
   visible, keyboard-accessible rail in addition to the browser scrollbar. */
export function workspaceTabStrip(id){ return $(id); }

export function updateWorkspaceTabRail(id){
  var strip=workspaceTabStrip(id), rail=document.querySelector('[data-tab-rail="'+id+'"]');
  if(!strip||!rail) return;
  var track=rail.querySelector('.workspace-tab-track'), thumb=rail.querySelector('.workspace-tab-thumb');
  var max=Math.max(0,strip.scrollWidth-strip.clientWidth), needed=max>2;
  rail.hidden=!needed;
  if(!needed||!track||!thumb) return;
  var ratio=Math.max(.12,Math.min(1,strip.clientWidth/strip.scrollWidth));
  var usable=Math.max(0,track.clientWidth-(track.clientWidth*ratio));
  var progress=max?strip.scrollLeft/max:0;
  thumb.style.width=(ratio*100)+'%';
  thumb.style.transform='translateX('+(usable*progress)+'px)';
  track.setAttribute('aria-valuemin','0');
  track.setAttribute('aria-valuemax',String(Math.round(max)));
  track.setAttribute('aria-valuenow',String(Math.round(strip.scrollLeft)));
  track.setAttribute('aria-valuetext','当前在三级菜单的 '+Math.round(progress*100)+'%');
}

export function syncWorkspaceTabRails(){
  document.querySelectorAll('[data-tab-scroll]').forEach(function(strip){ updateWorkspaceTabRail(strip.id); });
}

export function scrollWorkspaceTabs(id,direction){
  var strip=workspaceTabStrip(id); if(!strip) return;
  var amount=Math.max(220,Math.round(strip.clientWidth*.72))*Number(direction||1);
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  strip.scrollBy({left:amount,behavior:reduce?'auto':'smooth'});
}

export function beginWorkspaceTabRail(event,id){
  var strip=workspaceTabStrip(id), track=event.currentTarget;
  if(!strip||!track) return;
  event.preventDefault();
  var move=function(moveEvent){
    var rect=track.getBoundingClientRect();
    var ratio=Math.max(0,Math.min(1,(moveEvent.clientX-rect.left)/Math.max(1,rect.width)));
    strip.scrollLeft=ratio*Math.max(0,strip.scrollWidth-strip.clientWidth);
  };
  var finish=function(){ window.removeEventListener('pointermove',move); window.removeEventListener('pointerup',finish); };
  move(event);
  window.addEventListener('pointermove',move);
  window.addEventListener('pointerup',finish,{once:true});
}

export function handleWorkspaceTabRailKey(event,id){
  if(['ArrowLeft','ArrowRight','Home','End'].indexOf(event.key)<0) return;
  event.preventDefault();
  var strip=workspaceTabStrip(id); if(!strip) return;
  var reduce=window.matchMedia&&window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if(event.key==='Home') strip.scrollTo({left:0,behavior:reduce?'auto':'smooth'});
  else if(event.key==='End') strip.scrollTo({left:strip.scrollWidth,behavior:reduce?'auto':'smooth'});
  else scrollWorkspaceTabs(id,event.key==='ArrowRight'?1:-1);
}

export function installWorkspaceTabRails(){
  document.querySelectorAll('[data-tab-scroll]').forEach(function(strip){
    if(strip.dataset.tabRailInstalled==='1') return;
    strip.dataset.tabRailInstalled='1';
    var queued=false;
    strip.addEventListener('scroll',function(){
      if(queued) return; queued=true;
      requestAnimationFrame(function(){ queued=false; updateWorkspaceTabRail(strip.id); });
    },{passive:true});
  });
  syncWorkspaceTabRails();
}

export function renderClock(){ $('clock').textContent = new Date().toLocaleString('zh-CN',{hour12:false}); }

export function showPaperWorkspace(view,button,options){
  options=options||{};
  var VIEWS=['strategy','research','portfolio','activity','history','risk','execution'];
  if(VIEWS.indexOf(view)<0) view='portfolio';
  VIEWS.forEach(function(key){ var panel=$('paper'+key.charAt(0).toUpperCase()+key.slice(1)+'View'); if(panel) panel.hidden=key!==view; });
  document.querySelectorAll('#p-paper [data-paper-view]').forEach(function(item){ var selected=item.dataset.paperView===view; item.classList.toggle('active',selected); item.setAttribute('aria-selected',selected?'true':'false'); });
  window._paperWorkspace=view;
  sessionStorage.setItem(PAPER_VIEW_KEY,view);
  if(!options.restore) history.replaceState(null,'','#paper/'+view);
  if(view==='strategy') loadPaperStrategyCenter(); else if(view==='research') loadPaperResearchValidation(); else if(view==='risk') loadPaperRisk(false); else if(view==='execution') loadPaperExecution(); else loadPaper();
}

// 暗色模式切换
export async function refreshApp(){
  if(window._manualRefreshInFlight) return;
  window._manualRefreshInFlight=true;
  var button=document.querySelector('.page-refresh'), original=button&&button.textContent;
  if(button){ button.disabled=true; button.textContent='刷新中…'; }
  try{
    await loadMarketGate();
    var page=(document.querySelector('.page.active')||{}).id;
    if(page==='p-paper'){
      var view=window._paperWorkspace||'portfolio';
      if(view==='risk') await loadPaperRisk(true);
      else if(view==='strategy') await loadPaperStrategyCenter();
      else if(view==='research') await loadPaperResearchValidation();
      else if(view==='execution') await loadPaperExecution(true);
      else await loadPaper({refresh:true});
    }else if(page==='p-select'){
      var jobs=[];
      if(typeof loadStrategies==='function') jobs.push(loadStrategies());
      if(typeof loadDataValidity==='function') jobs.push(loadDataValidity());
      if(typeof loadPaperSelection==='function') jobs.push(loadPaperSelection());
      await Promise.all(jobs);
    }else if(page==='p-adaptive'&&typeof loadAdaptive==='function'){
      await loadAdaptive();
    }else if(page==='p-settings'&&typeof loadSettings==='function'){
      await loadSettings(true);
    }else{
      window.location.reload();
    }
  }finally{
    window._manualRefreshInFlight=false;
    if(button){ button.disabled=false; button.textContent=original||'刷新页面'; }
  }
}

export function toggleDark(){
  document.body.classList.toggle('dark');
  localStorage.setItem('darkMode', document.body.classList.contains('dark'));
  syncThemeControl();
  // ECharts 需要重绘
  Object.keys(charts).forEach(function(k){ charts[k].resize(); });
}

export function syncThemeControl(){
  var dark=document.body.classList.contains('dark'),button=$('themeToggle');
  if(!button) return;
  button.setAttribute('aria-pressed',dark?'true':'false');
  button.setAttribute('aria-label',dark?'切换浅色模式':'切换深色模式');
  button.title=dark?'切换浅色模式':'切换深色模式';
}
