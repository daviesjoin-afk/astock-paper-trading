# V2 web dashboard: presentation notes

The browser surface is a **read-only presenter**. It renders the V2 HTTP read
models (`/v2/experiments`, `/v2/portfolio`, `/v2/simulations`, `/v2/replay`,
`/v2/experiments/{id}/research-reproduction`). It never writes, never derives a
valuation, a return, a ranking, or an execution rule, and it never invents an
experiment. See `MODULE_BOUNDARIES.md` for the ownership rules this file assumes.

## Files

| Path | Role |
| --- | --- |
| `v2/web/index.html` | Static document: top bar, side navigation, panels, notices, footer |
| `v2/web/src/main.ts` | Fetch, state, rendering, view switching, verification actions |
| `v2/web/src/format.ts` | Business wording for kernel identifiers (cents, sides, reasons) |
| `v2/web/src/styles.css` | Design tokens and all component styles (light + dark) |
| `v2/web/e2e/*.spec.ts` | Browser behaviour, accessibility, resilience, evidence capture |

Raw identifiers (`strategy_version_id`, pack id, digests) stay inside the
collapsed “技术细节” block or inside a `title` tooltip. Everything else is
business wording.

## Design tokens

`:root` holds the light palette, `html[data-theme="dark"]` overrides the same
names, so components never hard-code a colour. Key tokens:

- surfaces: `--bg`, `--surface`, `--surface-2`, `--surface-3`
- text: `--text`, `--text-soft`, `--muted`
- accent: `--accent` (text/icon), `--accent-fill` + `--accent-ink` (buttons)
- semantics: `--ok`, `--warn`, `--danger` with `*-soft` backgrounds
- shape: `--radius-sm|--radius|--radius-lg`, `--shadow-1|--shadow-2`
- type: `--sans`, `--mono`; numbers use tabular figures

Contrast targets: body/muted text ≥ 4.5:1, large numerals ≥ 3:1, non-text UI
borders ≥ 3:1. The axe suite enforces WCAG A/AA on all six panels in both themes
at desktop and mobile widths, plus the notice states (empty catalogue, service
error, unknown experiment), an applied as-of scope, a filtered decision table,
the shortcut dialog, and an inline validation error in dark mode.

## Layout

- Sticky top bar: brand, experiment selector, refresh, theme toggle, shortcut
  reference. Below 340px the three controls become icon-only to stay on one row.
- Sticky side rail (≥1101px); collapses to a horizontal strip at 1100px and to a
  three-column grid at 560px so every destination stays visible on a phone.
- One panel per view, hash-driven (`#/overview`, `#/positions`, …), so a view is
  linkable and survives reload.
- Shareable state lives in the URL: `?cycle_id=` selects the experiment and
  `?result=filled|rejected` mirrors the decision filter. The context bar's
  “复制链接” button copies the exact `location.href`, so a colleague opens the
  same experiment, view and filter.
- Sticky experiment context bar (desktop) keeps “which experiment” on screen
  while a long decision table scrolls.
- Footer closes the page; `body` is a flex column so short views do not float.

Breakpoints: 1100px (rail → strip), 900px (4 → 2 KPI columns), 720px (tablet
padding, stacked controls), 560px (rail grid), 480px (compact type), 420px
(button labels), 360px (single-column KPIs, icon-only controls).

## Facts must stay attached to their scope

- A portfolio read is issued **once per refresh** with the applied `asof_day`
  (`portfolioQuery`). The screen never chains a current read and a historical
  read, so a current book cannot be rendered under a historical label.
- `renderPortfolio()` rejects a response whose `cycle_id` or `asof_day` differs
  from the request (`ScopeMismatchError`) and shows an error instead of
  relabelling facts.
- `asofDay` is the date the user asked for; `loadedScope` is what the API
  actually returned and what the positions table, its count line, and the
  overview `kpi-scope` line all read. A failed as-of query keeps the previous
  scope on screen and says which scope is still displayed.
- Decision times are rendered with `Intl.DateTimeFormat` fixed to
  `Asia/Shanghai`, so `…T06:00:00Z`, `…T14:00:00+08:00` and
  `…T20:00:00-08:00` produce the China wall-clock values the ledger uses. The
  decisions panel states “中国时间（UTC+8）”.
- Verification state is bound to a data epoch: a successful reload marks earlier
  passes as `stale` (“需要重新验证”), and every check carries its own request
  sequence, so a late answer for another experiment cannot repaint the screen.
  `ok !== true` is a failure, and the report's `cycle_id` must match.
- Verification controls have their own lifecycle: a reload, experiment switch or
  notice invalidates the in-flight check (`invalidateVerification`), which bumps
  the sequence, clears `aria-busy` and re-enables the button immediately. A late
  answer can therefore neither repaint nor leave a control disabled.
- The catalogue asks for the API maximum (`limit=500`). When a `cycle_id` is not
  in that window the UI probes `GET /v2/experiments/{id}` before deciding it is
  missing, distinguishes “not listed” from “does not exist”, and reports a
  damaged catalogue (`total > 0`, nothing readable) instead of “还没有实验”.
  Partially damaged catalogues stay usable with a visible warning.

## Paper account console (ADR 005)

`#/account` (deep link `?account=<id>`) reads the account catalogue and one
account's slots and orders. It is a fact viewer only: no create form, no order
form, no valuation.

- Header: initial capital, reserve and account cash (reserve + both slot cash).
- Two slot cards: strategy label, weight, budget, cash, order count and the
  positions derived from that slot's fills only.
- Recent orders: time (China), slot, signal label, side, result and either the
  fill (quantity, price, fees) or the rejection reason. Raw signal ids stay in
  the cell tooltip.
- Creation: “新建账户” opens a small form (account id, capital in yuan, strategy A/B
  chosen from `GET /v2/strategies`, fixed 45/45/10). The browser sends only
  reviewed strategy ids; the server resolves artifacts. Duplicate ids, invalid
  capital and identical strategies are refused with an explicit message.
- Experiment performance: the evolution view can query one experiment's
  reviewed-series metrics (profit, return, drawdown, fees, trades) or show the
  explicit “缺少审核价格集合” reason; id/day validation is inline.
- Evolution events now show the base/candidate experiments they judged
  (“基准 … · 候选 …”) when the event was recorded with those links.
- Shadow comparison: the evolution view can compare two experiments at the
  signal level (base/candidate ids → matched, base-only and candidate-only
  signals). It is a read-only research tool; empty inputs are rejected inline.
- Evolution history: a dedicated view (shortcut 8) lists promotion/rollback
  events with candidate id, status pill, decision time and the rule reasons.
  Candidate/status/experiment filters narrow the loaded page client-side; the
  date range is sent to the server (`from_day`/`to_day`, China day) so it also
  applies to “加载更早” pages (the
  experiment filter matches either the recorded base or candidate cycle) (“没有匹配的事件” when
  nothing matches). The log loads the newest 20 events and “加载更早” follows a
  (decided_at, event_id) cursor. Empty logs and fetch failures explain
  themselves; the log is read-only.
- Account advance: a date input plus “推进账户到该日” drives every running
  session on the account in one call (paused sessions stay paused); the status
  line reports steps/fills/rejections.
- Account overview: a row per account (cash, slot weights, active session
  count, audit verdict); clicking a row switches the console to that account.
- Price sets: 实验信息 shows the loaded catalogue (id, trust, pack, coverage,
  entry count, digest prefix) and a coverage checker for explicit pack/symbol/
  day inputs; reviewed-only rules are stated in the panel.
- When no reviewed price set exists the landing page shows a warning pointing at
  `data/price-set-examples` and `scripts/publish_price_set.sh`, and states that
  valuation, performance and proposals stay unavailable.
- Overview status: the landing page prints account count, active sessions and
  price sets split by trust (审核/合成) and the evolution-event count from
  `/v2/health`, plus the latest event; a failed probe shows “—”,
  never a stale number.
- Strategy versions: the account page prints the current version and switch
  count per slot, lists the append-only switch history, and records a switch
  (slot, target version, explicit effective time, reason) from the console.
- The account console (data hint, switch panel and form) is scanned by axe in
  its settled state, not only immediately after navigation.
- Data availability: the account page states the reviewed/synthetic price-set
  split from `/v2/health`; when reviewed == 0 it says outright that valuation,
  performance and proposals stay unavailable.
- Account performance: one line with profit, return, drawdown, fees, trades and
  days when reviewed prices cover the known days; hidden (never zero-filled)
  when they do not.
- Coverage: the account header states whether reviewed prices cover the held
  symbols on the known days (“完整 n/n” or “x/y · 缺口 …”); gaps are warnings,
  never silently treated as covered.
- Account advance: the session area advances every runnable session to an
  explicit trading day and reports how many sessions moved, steps, fills and
  rejections.
- Since last visit: the header compares order/session/switch counters plus the
  global evolution-event count with what this browser last saw (localStorage,
  counts only, no clock) and prints only the deltas that changed (or
  “无变化”) once both probes have loaded; first visit stays silent.
- Run status: the account header shows only account-scoped session counts
  (running/paused/done); global facts (price sets, latest evolution event) live
  on the overview page, so the two views no longer duplicate each other.
- Allocation proposal: when at least two distinct days are known (trade days,
  else the account's inception day), the panel shows the proposed weights,
  reserve and per-slot rationale from /allocation-proposal, plus “执行再平衡”
  which posts targets + the proposal digest with an explicit applied_at.
  Insufficient evidence hides the block; a rationale that discloses a version
  switch inside the window gets an inline “版本切换” badge. Applying asks for
  confirmation first and spells out each slot's transfer (¥ and weight delta)
  plus the resulting budget per slot.
- Exposure: “仓位与集中度” prints each slot's cost utilisation and either
  “无集中度超限” or the breaching symbols with value vs limit; cost basis only,
  never a market value.
- Rebalances: applied transfers are listed newest-first as
  `A→B ¥… · 45.0%/45.0% → 35.0%/55.0% · date`; the block stays hidden when no
  rebalance has been applied.
- Equity series: when the account's order days are covered by a reviewed price
  set, the panel prints “覆盖 n 个交易日 · 最大回撤 … · 盈亏 …” and one line per
  day. Without coverage the block stays hidden (no partial or interpolated
  series).
- Performance: when a reviewed series is available, the equity block also
  prints 收益（bps）/ 回撤 / 费用 / 成交笔数 from the performance endpoint; the
  line stays hidden without coverage.
- Audit: the account panel runs the read-only ledger audit on load and prints
  “账本自检：通过/失败 · 摘要”; a collapsible “审计详情” lists every check with
  its pass/fail marker and detail, so a failure is diagnosable in place.
- Risk boundaries: every slot card prints the account's cost-basis limits
  (single order / single symbol / slot cash floor) so a rejected order can be
  explained without opening the API.
- Valuation (valued): when a reviewed set covers the book, the banner switches
  to “估值可用”, and the detail line prints market value, equity, cash and
  profit (profit only when the set is reviewed). Synthetic sets never show
  profit.
- Valuation (unavailable): the account panel always states that valuation is unavailable
  (`No reviewed price set`) and, below it, the exact snapshot request: the
  as-of day (last decision day, else the account's created day), the missing
  `reviewed_price_set` and the (symbol, pack) pairs a price set would need to
  cover. No market value, NAV or return is ever rendered.
- Advancing: a running session offers “推进到该日” with an explicit trading day;
  only signals on or before that China day are executed, intermediate days move
  nothing.
- Session creation: “新建会话” picks a slot (A/B) and a signal source experiment
  filtered by that slot's reviewed strategy; the session id defaults from the
  experiment but stays editable. A slot without a matching experiment shows a
  hint and disables the submit instead of sending an invalid request.
- Sessions: each session card shows status (未开始/运行中/已暂停/已停止/已完成),
  the explicit clock and cursor progress, plus the controls that are legal for
  that status (启动 / 暂停 / 继续 / 运行到结束 / 停止). Controls POST only to
  `/v2/paper-sessions/{id}/{action}`; the panel reloads the account afterwards
  so orders and cash never drift from the ledger.
- Empty catalogue, missing account and fetch failures are distinct states; a
  failed read never renders a different account.
- The page only issues GETs. Everything shown is derived from the account spec
  and the slot order ledgers; the reserve is never spent.

## Components

- **KPI cards** state one fact each and never compute an implied return.
- **Tables** right-align numeric columns, keep headers sticky, and scroll inside
  the panel on desktop so a 100-row window does not push the page.
- **Status pills** are spans inside cells; styling a `<td>` as a pill breaks row
  baseline alignment.
- **Decision rows** lead with the stored record id (`#id`) so a row can be cited
  even when the signal id shape is unknown; unknown signal shapes render a stable
  short fingerprint instead of a shared “模拟信号” label.
- **Technical details** stay collapsed but carry a keyboard-reachable
  “复制技术细节” button that copies experiment, strategy, input, pack and policy
  identifiers as `label: value` lines.
- **Data-pack labels** keep the pack suffix (`离线样本包 #1`, `… #2`), so two
  packs are never shown as the same fact.
- **Decision filters** (`全部 / 已成交 / 已拒绝`) narrow the rendered rows, carry
  their own counts, survive experiment switches, and are written to the URL;
  filtering never mutates stored facts.
- **Verification states** (`idle / running / ok / fail`) are mirrored on the
  overview card so the two independent checks stay visible from anywhere.
- **Notices** (loading, service error, empty catalogue, unknown experiment) own
  the screen; navigation is disabled while a notice is showing. Empty tables use
  a flat surface with a centred icon rather than a decorative pattern.
- **Loading** uses a shimmer skeleton, never a fake row of data; while a reload
  is in flight `html[data-busy="true"]` spins the refresh icon and the workspace
  carries `aria-busy`.

## Keyboard and printing

Shortcuts are ignored while a field has focus, while a modal is open, and while
a notice owns the screen (nothing is navigable there anyway):

| Key | Action |
| --- | --- |
| `1`–`6` | Switch to overview, positions, decisions, replay, reproduction, experiment |
| `/` | Focus the experiment selector |
| `r` | Refresh the current experiment |
| `t` | Toggle light / dark |
| `?` | Open the shortcut reference |
| `Esc` | Close the reference |

The reference is a native `<dialog>` (`#shortcuts`) opened from the top bar; the
browser returns focus to that button on close.

Printing (`@media print`) drops navigation, controls and footers, forces the
light palette, removes shadows, neutralises the accent card tint, releases the
desktop table scroller, and keeps panels from breaking across pages. Only the
current view prints, so a printed sheet always belongs to one experiment.

## Theme selection

An explicit choice in the toggle is stored in `localStorage["astock-v2-theme"]`
and wins from then on. With no stored choice the page follows
`prefers-color-scheme` and keeps following it until the user picks a side.

## Contract with the tests

`data-testid` attributes are the test contract: `experiment-select`,
`cycle-hint`, `refresh`, `theme-toggle`, `nav-*`, `panel-*`, `summary-*`,
`kpi-*`, `positions-body`, `sellable-header`, `positions-empty-state`,
`sellable-header`, `asof-*`, `runs-body`, `runs-empty-state`, `recent-list`,
`verify-replay`, `verify-reproduction`, `*-status`, `*-detail`, `tech-*`,
`service-*`, `loading`, `empty-experiments`, `unknown-experiment`,
`live-status`, `copy-demo`, `copy-link`, `copy-tech`, `shortcuts`,
`shortcuts-open`, `shortcuts-close`, `catalogue-broken`, `catalogue-retry`,
`catalogue-warning`, `kpi-scope`, `positions-count`, `broken-detail`. Renaming one requires updating the specs.

`#runs-filtered-empty`, `#recent-note`, `#overview-replay`,
`#overview-reproduction`, `#context-*`, `#last-refresh` and `#recent-empty` are
addressed by id in the specs.

## Evidence screenshots

```bash
cd v2/web
npm run build
CAPTURE_UI=1 CAPTURE_PREFIX=after npx playwright test e2e/screenshots.spec.ts
```

Captures land in `v2/docs/assets/ui/`. They are evidence, not build output, so
they only run when `CAPTURE_UI=1` is set. `before-*` keeps the pre-change state.

## Accessibility guarantees

- Skip link moves focus into `main`; `#live` is a polite, atomic status region.
- Every interactive control is a real button/select/input with a visible focus
  ring; nothing is click-only.
- Motion is decorative only: panels animate transform, never opacity, so a
  contrast audit mid-animation still sees final colours. `prefers-reduced-motion`
  reduces it to zero.
- `forced-colors: active` keeps borders and the active rail item visible.
- The shortcut sheet is a modal dialog: axe scans it while open, Escape closes
  it, and focus returns to the opener.

## Known limits

- `input[type="date"]` follows the browser locale, so a zh-CN page can render a
  `mm/dd/yyyy` placeholder in an en-US browser; values are still sent verbatim.
- The overview shows the newest three decisions; the decision panel owns the
  full 100-row window and its filters.
- No chart is drawn: prices and valuations are outside this surface's boundary.
