import { useId, useMemo, useState } from "react";
import type { SeriesPoint } from "../api/types";
import { formatCount } from "../lib/format";

/**
 * Events per bucket as stacked columns (part-to-whole over time).
 *
 * Colour: the three series use categorical slots (blue / orange / aqua), not
 * red and green. Red next to green fails colour-vision separation; this trio
 * was checked with the palette validator in both themes. A legend is always
 * shown, the same values are available as a table, and each column is a
 * keyboard-focusable target with a tooltip, so nothing depends on colour or on
 * hovering.
 */

const SERIES = [
  { key: "delivered", label: "Delivered", color: "var(--series-1)" },
  { key: "failed", label: "Failed", color: "var(--series-2)" },
  { key: "other", label: "Other (in flight, held, filtered)", color: "var(--series-3)" },
] as const;

type SeriesKey = (typeof SERIES)[number]["key"];
type Row = { at: string; label: string; total: number } & Record<SeriesKey, number>;

const W = 720;
const H = 200;
const PAD = { top: 10, right: 8, bottom: 24, left: 36 };
const GAP = 2; // surface gap between stacked segments

function niceMax(value: number): number {
  if (value <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * magnitude >= value / 4) ?? 10;
  return Math.ceil(value / (step * magnitude)) * step * magnitude;
}

function bucketLabel(iso: string, bucket: "hour" | "day"): string {
  const date = new Date(iso);
  return bucket === "hour"
    ? date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function TrendChart({ points, bucket }: { points: SeriesPoint[]; bucket: "hour" | "day" }) {
  const [active, setActive] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const titleId = useId();

  const rows = useMemo<Row[]>(
    () =>
      points.map((p) => ({
        at: p.at,
        label: bucketLabel(p.at, bucket),
        delivered: p.delivered,
        failed: p.failed,
        other: Math.max(0, p.received - p.delivered - p.failed),
        total: p.received,
      })),
    [points, bucket],
  );

  const max = niceMax(Math.max(1, ...rows.map((r) => r.total)));
  const innerW = W - PAD.left - PAD.right;
  const innerH = H - PAD.top - PAD.bottom;
  const slot = innerW / Math.max(rows.length, 1);
  const barW = Math.min(24, Math.max(4, slot * 0.6));
  const y = (value: number) => PAD.top + innerH - (value / max) * innerH;
  const ticks = [0, max / 2, max];
  const labelEvery = Math.max(1, Math.ceil(rows.length / 8));
  const current = active === null ? null : rows[active];

  return (
    <div className="stack-sm">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="legend" aria-hidden={asTable}>
          {SERIES.map((s) => (
            <span key={s.key}>
              <span className="swatch" style={{ background: s.color }} />
              {s.label}
            </span>
          ))}
        </div>
        <button
          type="button"
          className="btn btn-sm"
          aria-pressed={asTable}
          onClick={() => setAsTable((v) => !v)}
        >
          {asTable ? "View as chart" : "View as table"}
        </button>
      </div>

      {asTable ? (
        <div className="table-wrap">
          <table>
            <caption className="sr-only">Events per {bucket}</caption>
            <thead>
              <tr>
                <th scope="col">{bucket === "hour" ? "Hour" : "Day"}</th>
                <th scope="col" className="num">
                  Received
                </th>
                <th scope="col" className="num">
                  Delivered
                </th>
                <th scope="col" className="num">
                  Failed
                </th>
                <th scope="col" className="num">
                  Other
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.at}>
                  <th scope="row">{r.label}</th>
                  <td className="num">{r.total}</td>
                  <td className="num">{r.delivered}</td>
                  <td className="num">{r.failed}</td>
                  <td className="num">{r.other}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="chart" onMouseLeave={() => setActive(null)}>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-labelledby={titleId}>
            <title id={titleId}>
              Events received per {bucket}, split into delivered, failed and other. Use “View as table” for
              exact values.
            </title>
            {ticks.map((tick) => (
              <g key={tick}>
                <line
                  x1={PAD.left}
                  x2={W - PAD.right}
                  y1={y(tick)}
                  y2={y(tick)}
                  stroke={tick === 0 ? "var(--axis)" : "var(--grid)"}
                  strokeWidth={1}
                />
                <text x={PAD.left - 6} y={y(tick) + 4} textAnchor="end">
                  {formatCount(tick)}
                </text>
              </g>
            ))}
            {rows.map((row, index) => {
              const cx = PAD.left + slot * index + slot / 2;
              let base = 0;
              const top = SERIES.filter((s) => row[s.key] > 0).at(-1)?.key;
              return (
                <g key={row.at}>
                  {SERIES.map((s) => {
                    const value = row[s.key];
                    if (value <= 0) return null;
                    const y0 = y(base);
                    const y1 = y(base + value);
                    base += value;
                    const height = Math.max(1, y0 - y1 - (s.key === top ? 0 : GAP));
                    const radius = s.key === top ? Math.min(4, height) : 0;
                    const yTop = y0 - height;
                    // Rounded only at the data end (top of the stack); square at the baseline.
                    const d = `M${cx - barW / 2},${y0} V${yTop + radius} Q${cx - barW / 2},${yTop} ${cx - barW / 2 + radius},${yTop} H${cx + barW / 2 - radius} Q${cx + barW / 2},${yTop} ${cx + barW / 2},${yTop + radius} V${y0} Z`;
                    return (
                      <path
                        key={s.key}
                        d={d}
                        fill={s.color}
                        opacity={active === null || active === index ? 1 : 0.45}
                      />
                    );
                  })}
                  {index % labelEvery === 0 && (
                    <text x={cx} y={H - 6} textAnchor="middle">
                      {row.label}
                    </text>
                  )}
                  {/* Hit target spans the whole slot, not just the painted column. */}
                  <rect
                    x={PAD.left + slot * index}
                    y={PAD.top}
                    width={slot}
                    height={innerH}
                    fill="transparent"
                    tabIndex={0}
                    aria-label={`${row.label}: ${row.total} received, ${row.delivered} delivered, ${row.failed} failed, ${row.other} other`}
                    onMouseEnter={() => setActive(index)}
                    onFocus={() => setActive(index)}
                    onBlur={() => setActive(null)}
                  />
                </g>
              );
            })}
          </svg>
          {current && active !== null && (
            <div
              className="chart-tooltip"
              role="status"
              style={{
                left: `clamp(0px, calc(${((PAD.left + slot * active + slot / 2) / W) * 100}% - 75px), calc(100% - 160px))`,
                top: 0,
              }}
            >
              <div className="secondary">{current.label}</div>
              {SERIES.map((s) => (
                <div className="t-row" key={s.key}>
                  <span>
                    <span className="t-key" style={{ background: s.color }} />
                    {s.label.split(" (")[0]}
                  </span>
                  <strong>{current[s.key]}</strong>
                </div>
              ))}
              <div className="t-row secondary">
                <span>Received</span>
                <strong>{current.total}</strong>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
