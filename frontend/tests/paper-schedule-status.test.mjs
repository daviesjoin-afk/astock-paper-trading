import assert from 'node:assert/strict';
import test from 'node:test';

import { paperScheduleStatusLine } from '../src/features/paper.js';

/* 调度状态条必须如实区分两种探测口径：
   - schtasks：逐个计划任务真实探测，可以报"已注册 N 个任务"；
   - file_exists：只能证明调度文件存在，cron 是否触发要看最近运行记录。
   历史缺口：界面只有 installed 一个布尔，Linux 上会误导操作员。 */

test('Linux：只声明调度文件存在，并明说不能证明 cron 已触发', () => {
  const html = paperScheduleStatusLine({
    installed: true,
    probe_mode: 'file_exists',
    schedule_file: '/etc/cron.d/astock-codex',
    runtime_status: '已执行',
    runtime_failed_count: 0,
    latest_runs: [{ started_at: '2026-10-20 15:05:00', finished_at: '2026-10-20 15:06:12' }],
  });
  assert.match(html, /调度文件存在/);
  assert.match(html, /文件存在不等于 cron 已触发/);
  assert.match(html, /\/etc\/cron\.d\/astock-codex/);
  assert.match(html, /最近运行 2026-10-20 15:06:12/);
  assert.match(html, /data-probe-mode="file_exists"/);
  assert.doesNotMatch(html, /已安装/);
});

test('Windows：按真实任务探测报告已注册数量', () => {
  const html = paperScheduleStatusLine({
    installed: true,
    probe_mode: 'schtasks',
    tasks: ['A', 'B', 'C', 'D', 'E', 'F'],
    runtime_status: '已执行',
    runtime_failed_count: 1,
    latest_intraday_runs: [{ started_at: '2026-10-20 10:00:00' }],
  });
  assert.match(html, /已注册 6 个计划任务/);
  assert.match(html, /失败 1 次/);
  assert.match(html, /最近运行 2026-10-20 10:00:00/);
  assert.match(html, /data-probe-mode="schtasks"/);
});

test('缺失/未知状态不编造：未安装与未发布 probe_mode 都明说', () => {
  const missing = paperScheduleStatusLine({ installed: false, probe_mode: 'file_exists' });
  assert.match(missing, /未检测到调度文件/);
  assert.match(missing, /tag-warn/);
  const unknown = paperScheduleStatusLine({});
  assert.match(unknown, /调度状态未知/);
  assert.match(unknown, /data-probe-mode="unknown"/);
});

test('后端字段进 HTML 前转义', () => {
  const html = paperScheduleStatusLine({
    installed: true,
    probe_mode: 'file_exists',
    schedule_file: '<img src=x onerror=alert(1)>',
    runtime_status: '<script>alert(1)</script>',
    runtime_failed_count: 0,
  });
  assert.doesNotMatch(html, /<img/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
});
