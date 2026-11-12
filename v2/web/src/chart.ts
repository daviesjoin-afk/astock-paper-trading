/**
 * Minimal SVG line chart.
 *
 * Hand-rolled instead of pulling in a chart library: this draws one equity
 * curve and a drawdown band, which is ~150 lines of path maths. A charting
 * dependency would ship more code than the rest of the console combined.
 *
 * Renders as SVG so it scales cleanly, is inspectable in devtools, and picks up
 * the theme's CSS variables -- including when the user toggles dark mode,
 * because the colours are read at draw time.
 */

export type CurvePoint = {
  day: string;
  equity_cents: number;
  steady_cents: number;
  aggressive_cents: number;
  steady_return_bps: number;
  aggressive_return_bps: number;
};

export type CurveSpec = {
  points: CurvePoint[];
  initialCents: number;
  /** Which series to draw. `total` is the one that matters. */
  series: "total" | "steady" | "aggressive";
  width: number;
  height: number;
};

const PAD_LEFT = 8;
const PAD_RIGHT = 8;
const PAD_TOP = 12;
const PAD_BOTTOM = 22;

/** Money label that stays readable across five orders of magnitude. */
export function moneyShort(cents: number): string {
  const yuan = cents / 100;
  const abs = Math.abs(yuan);
  if (abs >= 100_000_000) return `${(yuan / 100_000_000).toFixed(2)}亿`;
  if (abs >= 10_000) return `${(yuan / 10_000).toFixed(2)}万`;
  return yuan.toFixed(0);
}

function valueOf(point: CurvePoint, series: CurveSpec["series"]): number {
  switch (series) {
    case "steady":
      return point.steady_return_bps;
    case "aggressive":
      return point.aggressive_return_bps;
    default:
      return point.equity_cents;
  }
}

/**
 * Build the SVG markup for the curve.
 *
 * The y-range always includes the starting equity, so a flat start to the
 * period renders as a flat line rather than being auto-zoomed into noise.
 */
export function renderCurve(spec: CurveSpec): string {
  const { points, initialCents, series, width, height } = spec;
  if (points.length === 0) {
    return `<p class="chart-empty">还没有结算记录。推进到至少一个交易日后这里会出现净值曲线。</p>`;
  }

  const values = points.map((point) => valueOf(point, series));
  // Slot assets include capital transfers. Draw their API-provided returns
  // instead, so moving cash cannot create a jump in a strategy's performance.
  const baseline = series === "total" ? initialCents : 0;
  const label = series === "total" ? moneyShort : (bps: number): string => `${(bps / 100).toFixed(2)}%`;
  const minValue = Math.min(...values, baseline);
  const maxValue = Math.max(...values, baseline);
  // Guard against a zero range (a single flat day) so the scale maths does not
  // divide by zero.
  const span = Math.max(1, maxValue - minValue);

  const plotWidth = Math.max(1, width - PAD_LEFT - PAD_RIGHT);
  const plotHeight = Math.max(1, height - PAD_TOP - PAD_BOTTOM);

  const x = (index: number): number =>
    points.length === 1
      ? PAD_LEFT + plotWidth / 2
      : PAD_LEFT + (index * plotWidth) / (points.length - 1);
  const y = (value: number): number =>
    PAD_TOP + plotHeight - ((value - minValue) / span) * plotHeight;

  const line = values
    .map((value, index) => `${index === 0 ? "M" : "L"}${x(index).toFixed(2)},${y(value).toFixed(2)}`)
    .join(" ");

  const area = `${line} L${x(points.length - 1).toFixed(2)},${(PAD_TOP + plotHeight).toFixed(2)} L${x(0).toFixed(2)},${(PAD_TOP + plotHeight).toFixed(2)} Z`;

  const baseY = y(baseline);
  const lastValue = values[values.length - 1];
  const rising = lastValue >= baseline;
  const stroke = series === "steady" ? "var(--steady)" : series === "aggressive" ? "var(--aggressive)" : rising ? "var(--positive)" : "var(--negative)";

  // Only annotate the extremes and the baseline; more labels than that turn a
  // 250-day curve into a wall of text.
  const firstDay = points[0].day;
  const lastDay = points[points.length - 1].day;

  return `
    <svg class="curve" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
         role="img" aria-label="${seriesLabel(series)}，${firstDay} 至 ${lastDay}，共 ${points.length} 个交易日">
      <defs>
        <linearGradient id="fill-${series}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${stroke}" stop-opacity="0.22" />
          <stop offset="100%" stop-color="${stroke}" stop-opacity="0.02" />
        </linearGradient>
      </defs>
      <line class="curve-baseline" x1="${PAD_LEFT}" y1="${baseY.toFixed(2)}"
            x2="${(width - PAD_RIGHT).toFixed(2)}" y2="${baseY.toFixed(2)}" />
      <path d="${area}" fill="url(#fill-${series})" stroke="none" />
      <path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.8"
            stroke-linejoin="round" stroke-linecap="round" />
      <text class="curve-label" x="${PAD_LEFT}" y="${height - 6}">${firstDay}</text>
      <text class="curve-label curve-label-end" x="${width - PAD_RIGHT}" y="${height - 6}"
            text-anchor="end">${lastDay}</text>
    </svg>
    <div class="curve-scale">
      <span>${label(minValue)}</span>
      <span class="curve-scale-mid">${series === "total" ? "起始" : "剔除资金调配"} ${label(baseline)}</span>
      <span>${label(maxValue)}</span>
    </div>
  `;
}

function seriesLabel(series: CurveSpec["series"]): string {
  switch (series) {
    case "steady":
      return "稳健净值曲线";
    case "aggressive":
      return "激进净值曲线";
    default:
      return "账户净值曲线";
  }
}

/**
 * Per-day change in equity, for the bar strip under the curve.
 *
 * Bars rather than another line: a single bad day is much easier to spot as a
 * spike than as a dip in a smooth curve, and spotting the bad days is the point
 * of having the curve at all.
 */
export function renderDailyBars(points: CurvePoint[], width: number, height: number): string {
  if (points.length < 2) return "";
  const changes: number[] = [];
  for (let index = 1; index < points.length; index += 1) {
    changes.push(points[index].equity_cents - points[index - 1].equity_cents);
  }
  const maxAbs = Math.max(1, ...changes.map((value) => Math.abs(value)));
  const barWidth = Math.max(1, width / changes.length - 1);
  const mid = height / 2;

  const bars = changes
    .map((change, index) => {
      const magnitude = (Math.abs(change) / maxAbs) * (height / 2 - 2);
      const x = index * (width / changes.length);
      const y = change >= 0 ? mid - magnitude : mid;
      const colour = change >= 0 ? "var(--positive)" : "var(--negative)";
      return `<rect x="${x.toFixed(2)}" y="${y.toFixed(2)}" width="${barWidth.toFixed(2)}"
              height="${Math.max(0.6, magnitude).toFixed(2)}" fill="${colour}" opacity="0.75" />`;
    })
    .join("");

  return `<svg class="daily-bars" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none"
               role="img" aria-label="每日盈亏柱状图，共 ${changes.length} 个交易日">
            <line class="bar-mid" x1="0" y1="${mid}" x2="${width}" y2="${mid}" />
            ${bars}
          </svg>`;
}
