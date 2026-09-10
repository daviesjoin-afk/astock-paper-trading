/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
export var charts = {};

export function $(id){ return document.getElementById(id); }

export function chart(id){
  var target=$(id), existing=charts[id];
  if(!target) return null;
  // Charts are optional: the local vendor bundle may be unavailable during a
  // partial static deploy.  Return a no-op adapter so data tables and risk
  // controls remain usable instead of throwing from every chart caller.
  if(typeof echarts==='undefined') return {
    getDom:function(){return target;}, dispose:function(){}, resize:function(){},
    setOption:function(){}, dispatchAction:function(){}, clear:function(){}
  };
  // Dynamic views replace their HTML on refresh. An ECharts instance keeps a
  // reference to its original DOM node, so reusing it would draw off-screen.
  if(existing && existing.getDom()!==target){
    existing.dispose();
    delete charts[id];
  }
  if(!charts[id]) charts[id] = echarts.init(target,null,{renderer:'canvas'});
  return charts[id];
}

export function tableScroll(html, minWidth){
  return '<div class="table-scroll"'+(minWidth?' style="--table-min:'+minWidth+'px"':'')+'>'+html+'</div>';
}

export function setEl(id,html){var el=document.getElementById(id);if(el)el.innerHTML=html;}
