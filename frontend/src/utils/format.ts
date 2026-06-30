// Date/label formatters ported verbatim (behavior-wise) from app/templates/dashboard.html.
// Note: listed_at/created_at are naive-UTC ISO strings; `new Date(value)` parses them in
// local time, exactly as the original code did — kept identical for parity.

export function fmtDate(value: string | null | undefined): string {
  if (!value) return "No saved scan yet";
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function fmtListed(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return "Listed today";
  if (days === 1) return "Listed yesterday";
  if (days < 30) return `Listed ${days} days ago`;
  return `Listed ${d.toLocaleDateString([], { dateStyle: "medium" })}`;
}

export function fmtApplied(value: string | null | undefined): string {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days <= 0) return "Applied today";
  if (days === 1) return "Applied yesterday";
  if (days < 30) return `Applied ${days} days ago`;
  return `Applied ${d.toLocaleDateString([], { dateStyle: "medium" })}`;
}

export interface KitIcon { icon: string; title: string; }

export function kitIcon(status: string | null | undefined): KitIcon | null {
  if (status === "generating") return { icon: "⏳", title: "Generating application kit…" };
  if (status === "ok") return { icon: "✨", title: "Application kit ready" };
  if (status === "error") return { icon: "⚠️", title: "Application kit failed — open the job to retry" };
  return null;
}
