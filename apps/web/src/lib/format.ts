// Formatting helpers. A missing value is shown as an em dash, never as zero:
// "no data" and "0" mean different things to someone on call.

export const DASH = "—";

const dateTime = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});
const compact = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const plain = new Intl.NumberFormat();

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? DASH : dateTime.format(date);
}

export function formatRelative(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return DASH;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return DASH;
  const seconds = Math.round((now.getTime() - then) / 1000);
  const future = seconds < 0;
  const span = formatSpan(Math.abs(seconds));
  if (Math.abs(seconds) < 5) return "just now";
  return future ? `in ${span}` : `${span} ago`;
}

/** A length of time in the largest sensible unit: 42s, 3m, 2h 5m, 4d. */
export function formatSpan(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || Number.isNaN(totalSeconds)) return DASH;
  const seconds = Math.max(0, Math.round(totalSeconds));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days}d ${hours % 24}h` : `${days}d`;
}

export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return DASH;
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return value >= 10_000 ? compact.format(value) : plain.format(value);
}

/** A ratio in [0,1] as a percentage. null means "no outcomes yet" and stays a dash. */
export function formatPercent(ratio: number | null | undefined): string {
  if (ratio === null || ratio === undefined) return DASH;
  const percent = ratio * 100;
  // Avoid rounding 99.96% up to a misleading "100%".
  if (percent > 99.9 && percent < 100) return "99.9%";
  return `${percent.toFixed(percent === 100 || percent === 0 ? 0 : 1)}%`;
}

export function shortId(id: string): string {
  return id.length > 13 ? `${id.slice(0, 8)}…${id.slice(-4)}` : id;
}

const ACRONYMS = new Set(["http", "https", "url", "id", "ai", "api", "ssrf", "dns", "tls"]);

/** "http_5xx" -> "HTTP 5xx", "DEAD_LETTERED" -> "Dead lettered". */
export function humanize(code: string | null | undefined): string {
  if (!code) return DASH;
  const words = code
    .replace(/[._]/g, " ")
    .toLowerCase()
    .split(" ")
    .filter(Boolean)
    .map((word) => (ACRONYMS.has(word) ? word.toUpperCase() : word));
  const text = words.join(" ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function parseJson(text: string): { ok: true; value: unknown } | { ok: false; error: string } {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch (error) {
    return { ok: false, error: error instanceof Error ? error.message : "Invalid JSON" };
  }
}
