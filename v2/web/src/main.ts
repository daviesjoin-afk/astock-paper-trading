type Portfolio = {
  cash_cents: number;
  positions: Array<{symbol: string; quantity: number}>;
  runs_total: number;
};
type Run = {
  id: number;
  request: {signal_id: string; signal: {side: string}};
  outcome: {status: string; reason: string};
};

const cycleId = new URLSearchParams(location.search).get("cycle_id")?.trim() ?? "";
const scoped = (path: string): string => {
  if (!cycleId) throw new Error("请在地址中指定实验，例如 ?cycle_id=experiment-A");
  return path + "?cycle_id=" + encodeURIComponent(cycleId);
};

const find = (id: string): HTMLElement => {
  const node = document.getElementById(id);
  if (!node) throw new Error("missing DOM node " + id);
  return node;
};
const setStatus = (message: string, isError = false): void => {
  const node = find("status");
  node.textContent = " " + message;
  node.className = isError ? "error" : "muted";
};
async function get<T>(url: string): Promise<T> {
  const response = await fetch(url, {cache: "no-store"});
  if (!response.ok) throw new Error(url + ": HTTP " + response.status);
  return await response.json() as T;
}
function renderRows(id: string, rows: Array<Array<string>>): void {
  const target = find(id);
  target.replaceChildren();
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of row) {
      const td = document.createElement("td");
      td.textContent = value; // No HTML interpolation from external data.
      tr.appendChild(td);
    }
    target.appendChild(tr);
  }
}
async function refresh(): Promise<void> {
  try {
    const [portfolio, runs] = await Promise.all([
      get<Portfolio>(scoped("/v2/portfolio")), get<Run[]>(scoped("/v2/simulations"))
    ]);
    find("cash").textContent = (portfolio.cash_cents / 100).toLocaleString("zh-CN", {
      style: "currency", currency: "CNY"
    });
    find("positions-count").textContent = String(portfolio.positions.length);
    find("run-count").textContent = String(portfolio.runs_total);
    renderRows("positions", portfolio.positions.map(p => [p.symbol, String(p.quantity)]));
    renderRows("runs", runs.map(r => [
      String(r.id), r.request.signal_id, r.request.signal.side,
      r.outcome.status, r.outcome.reason
    ]));
    setStatus("已读取实验 " + cycleId + " 的状态");
  } catch (error) {
    setStatus(String(error), true);
  }
}
find("refresh").addEventListener("click", () => void refresh());
find("replay").addEventListener("click", async () => {
  try {
    const result = await get<{ok: boolean; runs_verified: number}>(scoped("/v2/replay"));
    setStatus(result.ok ? "回放校验通过：" + result.runs_verified + " 次决策" : "回放校验失败", !result.ok);
  } catch (error) { setStatus(String(error), true); }
});
find("experiment").textContent = cycleId || "未指定";
void refresh();
