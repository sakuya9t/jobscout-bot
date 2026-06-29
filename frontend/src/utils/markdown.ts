// Minimal, XSS-safe Markdown → HTML for the tailored-resume subset the model emits
// (#/##/### headings, -/*/+ and 1. lists, **bold**/*italic*/`code`, ---, links). All
// text is HTML-escaped first, so LLM output can never inject markup — the result is safe
// to use with v-html. Ported verbatim from the classic position_detail page.

function escapeHtml(s: string): string {
  return (s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] as string));
}

function mdInline(s: string): string {
  let out = escapeHtml(s);
  out = out.replace(/`([^`]+)`/g, (_m, c) => `<code>${c}</code>`);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  out = out.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, t, href) =>
    /^(https?:\/\/|mailto:)/i.test(String(href).trim())
      ? `<a href="${String(href).trim()}" target="_blank" rel="noopener">${t}</a>`
      : m);
  return out;
}

export function mdToHtml(md: string | null | undefined): string {
  const lines = (md || "").replace(/\r\n?/g, "\n").split("\n");
  let html = "";
  let para: string[] = [];
  let i = 0;
  const flush = () => {
    if (para.length) {
      html += `<p>${para.map(mdInline).join("<br>")}</p>`;
      para = [];
    }
  };
  const isItem = (l: string) => /^\s*([-*+]|\d+[.)])\s+/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      const lvl = Math.min(h[1].length, 3);
      html += `<h${lvl}>${mdInline(h[2])}</h${lvl}>`;
      i++;
      continue;
    }
    if (isItem(line)) {
      flush();
      const tag = /^\s*\d+[.)]\s+/.test(line) ? "ol" : "ul";
      html += `<${tag}>`;
      while (i < lines.length && isItem(lines[i])) {
        html += `<li>${mdInline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""))}</li>`;
        i++;
      }
      html += `</${tag}>`;
      continue;
    }
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flush();
      html += "<hr>";
      i++;
      continue;
    }
    if (/^\s*$/.test(line)) {
      flush();
      i++;
      continue;
    }
    para.push(line);
    i++;
  }
  flush();
  return html;
}
