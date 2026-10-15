// Safe-by-construction Markdown renderer for LLM output.
// Every piece of text is HTML-escaped first; only a fixed set of tags is ever produced; links must be
// http(s) and open in a new tab without referrer. Raw HTML in the input is shown as text, never parsed.
// Citations like [3] become buttons the report viewer can wire to the source list.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function safeUrl(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch {
    return null;
  }
}

function inline(src, opts) {
  // 1) pull out code spans and links so their contents are not re-processed
  const slots = [];
  const keep = (html) => `\u0000${slots.push(html) - 1}\u0000`;
  let s = String(src);
  s = s.replace(/`([^`]+)`/g, (_, code) => keep(`<code>${escapeHtml(code)}</code>`));
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (m, label, url) => {
    const href = safeUrl(url);
    if (!href) return keep(escapeHtml(label));
    return keep(`<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${inline(label, opts)}</a>`);
  });
  s = escapeHtml(s);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/(^|[^_\w])_([^_\s][^_]*)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  if (opts.citations) {
    s = s.replace(/\[(\d{1,3})\]/g, (_, n) => {
      const st = opts.citationStatus?.(Number(n)) || "";
      return `<button type="button" class="cite ${st}" data-n="${n}" aria-label="Source ${n}">${n}</button>`;
    });
  }
  s = s.replace(/https?:\/\/[^\s<]+[^\s<.,;:)!?]/g, (url, offset, all) =>
    all.slice(Math.max(0, offset - 6), offset).includes('href="') ? url :
      `<a href="${url}" target="_blank" rel="noopener noreferrer">${url}</a>`);
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[Number(i)]);
}

function splitRow(line) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
}

/**
 * render(markdown, {citations, citationStatus}) -> HTML string (safe to assign to innerHTML)
 */
export function render(md, opts = {}) {
  const lines = String(md ?? "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${inline(para.join(" "), opts)}</p>`);
    para.length = 0;
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      flush();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<pre><code>${escapeHtml(buf.join("\n"))}</code></pre>`);
      i++;
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flush();
      const lvl = Math.min(h[1].length, 4);
      out.push(`<h${lvl}>${inline(h[2].replace(/\s*#+\s*$/, ""), opts)}</h${lvl}>`);
      i++;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      out.push("<hr>");
      i++;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(splitRow(lines[i++]));
      out.push(`<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c, opts)}</th>`).join("")}` +
        `</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c, opts)}</td>`).join("")}</tr>`).join("")}` +
        "</tbody></table></div>");
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${render(buf.join("\n"), opts)}</blockquote>`);
      continue;
    }
    const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m) {
          items.push({ depth: Math.floor(m[1].replace(/\t/g, "  ").length / 2), text: m[3] });
          i++;
        } else if (/^\s{2,}\S/.test(lines[i]) && items.length) {
          items[items.length - 1].text += " " + lines[i].trim();
          i++;
        } else break;
      }
      const tag = ordered ? "ol" : "ul";
      let html = `<${tag}>`;
      let depth = 0;
      for (const it of items) {
        while (it.depth > depth) { html += `<${tag}>`; depth++; }
        while (it.depth < depth) { html += `</${tag}>`; depth--; }
        html += `<li>${inline(it.text, opts)}</li>`;
      }
      while (depth-- > 0) html += `</${tag}>`;
      out.push(html + `</${tag}>`);
      continue;
    }
    if (!line.trim()) {
      flush();
      i++;
      continue;
    }
    para.push(line.trim());
    i++;
  }
  flush();
  return out.join("\n");
}

export function plainText(md) {
  return String(md ?? "").replace(/[#*_`>|-]/g, " ").replace(/\[(\d+)\]/g, "").replace(/\s+/g, " ").trim();
}
