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

const find = (id: string): HTMLElement => {
  const node = document.getElementById(id);
  if (!node) throw new Error("missing DOM node " + id);
  return node;
};
const status = (message: string, isError = false): void => {
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
      get<Portfolio>("/v2/portfolio"), get<Run[]>("/v2/simulations")
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
    status("已读取 V2 最新状态");
  } catch (error) {
    status(String(error), true);
  }
}
find("refresh").addEventListener("click", () => void refresh());
find("replay").addEventListener("click", async () => {
  try {
    const result = await get<{ok: boolean; runs_verified: number}>("/v2/replay");
    status(result.ok ? "回放校验通过：" + result.runs_verified + " 次决策" : "回放校验失败", !result.ok);
  } catch (error) { status(String(error), true); }
});
void refresh();
