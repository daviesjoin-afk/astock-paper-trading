/* PR-55：由 frontend/app.js 拆分（纯搬运，逻辑/文案未改） */
// 模拟盘组合/委托/档案三个工作区共用一份 overview。以前每次切换子页签都会
// 重新请求完整 dashboard 并整体重建 DOM，是「切页卡顿」的最大来源。
// 现在改为 stale-while-revalidate：切换时先立即渲染上一次的数据（零等待），
// 60 秒内不再发请求；过期后在后台静默刷新，数据没有变化就不重绘。
// 下单/撤单/暂停/恢复/重置/保存设置等写操作必须用 loadPaper({force:true})
// 绕过缓存拿到最新账本。
export var PAPER_NAV_TTL_MS=60000;
