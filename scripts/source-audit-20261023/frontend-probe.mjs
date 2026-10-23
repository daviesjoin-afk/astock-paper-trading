import http from 'node:http';
import { api } from '../../frontend/src/core/api.js';

// 服务端**立刻回响应头**，正文延迟 300ms 才发送：计时器若只覆盖响应头，请求
// 就不会受 100ms 超时约束（B4）。修复后应在 ~100ms 处 abort。
const server = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.flushHeaders();
  setTimeout(() => res.end('{"ok":true}'), 300);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
try {
  const start = performance.now();
  let outcome;
  try {
    const result = await api(`http://127.0.0.1:${server.address().port}`, {method: 'POST', timeout: 100});
    outcome = {result};
  } catch (err) {
    outcome = {aborted: true, error: err && err.name ? String(err.name) : String(err)};
  }
  console.log(JSON.stringify({
    timeout_ms: 100, body_delay_ms: 300,
    elapsed_ms: Math.round(performance.now() - start), ...outcome,
  }));
} finally {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
