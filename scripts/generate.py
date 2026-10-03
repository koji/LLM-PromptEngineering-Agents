#!/usr/bin/env python3
"""Generate category pages and the browse site from data/entries.yml.

Stdlib only — no third-party dependencies.

Usage:
    python3 scripts/generate.py [--check] [--site-out DIR] [--data PATH]

    (default)   Regenerate docs/categories/*/readme.md in place and build
                the static site into ./site/
    --check     Regenerate to a temp dir and fail if any committed category
                page differs (used by CI).
    --site-out  Directory for the static site (default: site).
    --data      Path to entries.yml (default: data/entries.yml).

Schema documentation: docs/data-schema.md
"""

import argparse
import difflib
import html
import json
import os
import re
import shutil
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Minimal YAML subset parser (covers what our emitter + careful hand edits use)
# ---------------------------------------------------------------------------

class YamlError(Exception):
    pass


def _parse_scalar(text):
    t = text.strip()
    if t == "" or t == "null" or t == "~":
        return None
    if t == "true":
        return True
    if t == "false":
        return False
    if t.startswith('"'):
        if len(t) < 2 or not t.endswith('"'):
            raise YamlError(f"unterminated double-quoted scalar: {t!r}")
        inner = t[1:-1]
        out = []
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner):
                n = inner[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(n, n))
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    if t.startswith("'"):
        if len(t) < 2 or not t.endswith("'"):
            raise YamlError(f"unterminated single-quoted scalar: {t!r}")
        return t[1:-1].replace("''", "'")
    if t.startswith("[") and t.endswith("]"):
        return _parse_flow_list(t)
    if t.startswith("{") and t.endswith("}"):
        return _parse_flow_map(t)
    return t


def _split_flow(text):
    """Split a flow collection's inner text on top-level commas."""
    parts, depth, cur, quote = [], 0, [], None
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            cur.append(ch)
        elif ch in "[{":
            depth += 1
            cur.append(ch)
        elif ch in "]}":
            depth -= 1
            cur.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur or not parts:
        parts.append("".join(cur).strip())
    return [p for p in parts if p != ""]


def _parse_flow_list(text):
    return [_parse_scalar(p) for p in _split_flow(text[1:-1])]


def _parse_flow_map(text):
    d = {}
    for p in _split_flow(text[1:-1]):
        k, _, v = p.partition(":")
        d[k.strip()] = _parse_scalar(v)
    return d


def parse_yaml_subset(text):
    """Parse the small YAML subset used by data/entries.yml."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {n}: tabs are not allowed for indentation")
        indent = len(raw) - len(raw.lstrip(" "))
        # crude guard against folded/literal blocks we do not support
        if stripped.endswith("|") or stripped.endswith(">"):
            raise YamlError(
                f"line {n}: block scalars (|, >) are not supported; "
                "keep values on a single line"
            )
        lines.append((indent, stripped, n))
    if not lines:
        return {}
    node, idx = _parse_block(lines, 0, lines[0][0])
    if idx != len(lines):
        raise YamlError(f"line {lines[idx][2]}: unexpected content")
    return node


def _parse_block(lines, i, indent):
    if lines[i][1].startswith("- ") or lines[i][1] == "-":
        return _parse_list(lines, i, indent)
    return _parse_map(lines, i, indent)


def _nested_block(lines, i, indent):
    """Parse a nested block value after `key:` (may sit at same indent for lists)."""
    if i < len(lines) and (
        lines[i][0] > indent
        or lines[i][1].startswith("- ")
        or lines[i][1] == "-"
    ):
        return _parse_block(lines, i, lines[i][0])
    return None, i


def _parse_list(lines, i, indent):
    items = []
    while i < len(lines) and lines[i][0] == indent and (
        lines[i][1].startswith("- ") or lines[i][1] == "-"
    ):
        content = lines[i][1][2:].strip() if lines[i][1].startswith("- ") else ""
        n = lines[i][2]
        i += 1
        if content == "":
            item, i = _nested_block(lines, i, indent)
            items.append(item)
            continue
        # "- key: value" starts an inline mapping
        key, sep, rest = content.partition(":")
        if sep and not key.startswith(('"', "'")) and (
            rest == "" or rest.startswith(" ")
        ):
            item = {}
            if rest.strip() == "":
                item[key.strip()], i = _nested_block(lines, i, indent)
            else:
                item[key.strip()] = _parse_scalar(rest)
            # continuation lines of this mapping live deeper than the dash
            while i < len(lines) and lines[i][0] > indent:
                ind, text, n = lines[i][0], lines[i][1], lines[i][2]
                if text.startswith("- ") or text == "-":
                    raise YamlError(
                        f"line {n}: unexpected list item inside mapping"
                    )
                k, s, v = text.partition(":")
                if not s or k != k.strip() or (v != "" and not v.startswith(" ")):
                    raise YamlError(
                        f"line {n}: expected 'key: value', got {text!r}"
                    )
                kk = k.strip()
                if kk in item:
                    raise YamlError(f"line {n}: duplicate key {kk!r}")
                if v.strip() == "":
                    item[kk], i = _nested_block(lines, i + 1, ind)
                else:
                    item[kk] = _parse_scalar(v)
                    i += 1
            items.append(item)
        else:
            items.append(_parse_scalar(content))
    return items, i


def _parse_map(lines, i, indent):
    mapping = {}
    while i < len(lines) and lines[i][0] == indent:
        if lines[i][1].startswith("- ") or lines[i][1] == "-":
            raise YamlError(f"line {lines[i][2]}: unexpected list item in mapping")
        k, sep, v = lines[i][1].partition(":")
        n = lines[i][2]
        if not sep or k != k.strip() or (v != "" and not v.startswith(" ")):
            raise YamlError(f"line {n}: expected 'key: value', got {lines[i][1]!r}")
        key = k.strip()
        if key in mapping:
            raise YamlError(f"line {n}: duplicate key {key!r}")
        if v.strip() == "":
            mapping[key], i = _nested_block(lines, i + 1, indent)
        else:
            mapping[key] = _parse_scalar(v)
            i += 1
    return mapping, i

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

LAYOUTS = ("table", "sections", "list")


def validate(data):
    errors = []
    cats = data.get("categories")
    entries = data.get("entries")
    if not isinstance(cats, list) or not cats:
        errors.append("top-level 'categories' must be a non-empty list")
        cats = []
    if not isinstance(entries, list):
        errors.append("top-level 'entries' must be a list")
        entries = []

    cat_by_slug = {}
    for ci, c in enumerate(cats):
        where = f"categories[{ci}]"
        if not isinstance(c, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        for req in ("slug", "title", "layout"):
            if not c.get(req):
                errors.append(f"{where}: missing required key '{req}'")
        slug = c.get("slug")
        if slug in cat_by_slug:
            errors.append(f"{where}: duplicate slug '{slug}'")
        cat_by_slug[slug] = c
        if c.get("layout") not in LAYOUTS:
            errors.append(f"{where}: layout must be one of {LAYOUTS}")
        if c.get("layout") == "table":
            for si, s in enumerate(c.get("sections") or []):
                sw = f"{where}.sections[{si}]"
                if not isinstance(s, dict):
                    errors.append(f"{sw}: must be a mapping")
                    continue
                if s.get("format") == "prose":
                    if not s.get("name"):
                        errors.append(f"{sw}: prose section needs a name")
                    continue
                if not s.get("fields") or not s.get("headers"):
                    errors.append(f"{sw}: 'fields' and 'headers' are required")
                elif len(s["fields"]) != len(s["headers"]):
                    errors.append(f"{sw}: fields/headers length mismatch")
                for sub in s.get("subsections") or []:
                    if not sub.get("name") or not sub.get("fields") or not sub.get("headers"):
                        errors.append(f"{sw}: subsection needs name/fields/headers")
                    elif len(sub["fields"]) != len(sub["headers"]):
                        errors.append(f"{sw}: subsection fields/headers length mismatch")

    seen_entries = set()
    for ei, e in enumerate(entries):
        where = f"entries[{ei}]"
        if not isinstance(e, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        name = e.get("name")
        cat = e.get("category")
        if not name:
            errors.append(f"{where}: missing 'name'")
        if cat not in cat_by_slug:
            errors.append(f"{where} ({name!r}): unknown category '{cat}'")
            continue
        cdef = cat_by_slug[cat]
        sec = e.get("section")
        sub = e.get("subsection")
        if cdef.get("layout") == "table":
            valid_secs = [s.get("name") for s in cdef.get("sections") or []]
            if sec not in valid_secs:
                errors.append(
                    f"{where} ({name!r}): section {sec!r} not in {valid_secs}"
                )
            elif sub:
                sdef = next(s for s in cdef["sections"] if s.get("name") == sec)
                valid_subs = [x.get("name") for x in sdef.get("subsections") or []]
                if sub not in valid_subs:
                    errors.append(
                        f"{where} ({name!r}): subsection {sub!r} not in {valid_subs}"
                    )
        url = e.get("url")
        if url and not re.match(r"^https?://", str(url)):
            errors.append(f"{where} ({name!r}): url should start with http(s)://")
        tags = e.get("tags")
        if tags is not None and not isinstance(tags, list):
            errors.append(f"{where} ({name!r}): 'tags' must be a list")
        key = (cat, sec, sub, name, e.get("url"))
        if key in seen_entries:
            errors.append(f"{where} ({name!r}): duplicate entry")
        seen_entries.add(key)

    if errors:
        raise SystemExit("entries.yml validation failed:\n- " + "\n- ".join(errors))
    return cat_by_slug


# ---------------------------------------------------------------------------
# Markdown rendering (category pages)
# ---------------------------------------------------------------------------

BACK_LINK = "[Back to README](../../readme.md)"


def _source_cell(entry):
    url = entry.get("url") or ""
    label = entry.get("url_label") or ""
    if url and label:
        return f"[{label}]({url})"
    if url:
        return url
    if label:
        return f"[{label}]()"
    return "-"


def _row_cells(entry, fields, name_bold):
    cells = []
    extra = entry.get("extra") or {}
    for f in fields:
        if f == "name":
            name = entry.get("name") or ""
            cells.append(f"**{name}**" if name_bold else name)
        elif f == "summary":
            cells.append(entry.get("summary") or "-")
        elif f == "source":
            cells.append(_source_cell(entry))
        elif f == "url":
            cells.append(entry.get("url") or "")
        elif f.startswith("extra."):
            cells.append(str(extra.get(f.split(".", 1)[1], "") or ""))
        else:
            cells.append(str(entry.get(f, "") or ""))
    return cells


def _render_table(headers, rows, header_bold):
    import unicodedata

    def width(s):
        # approximate display width: East-Asian wide/fullwidth and most emoji = 2
        w = 0
        for ch in s:
            w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        return w

    def pad(s, n):
        return s + " " * max(0, n - width(s))

    hc = [f"**{h}**" if header_bold else h for h in headers]
    widths = [width(c) for c in hc]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], width(c))
    out = []
    out.append("| " + " | ".join(pad(c, w) for c, w in zip(hc, widths)) + " |")
    out.append("| " + " | ".join("-" * w for w in widths) + " |")
    for r in rows:
        out.append("| " + " | ".join(pad(c, w) for c, w in zip(r, widths)) + " |")
    return out


def render_category_md(cat, entries):
    slug = cat["slug"]
    mine = [e for e in entries if e.get("category") == slug]
    lines = []
    if cat.get("image"):
        alt = cat.get("image_alt") or cat["title"]
        lines.append(f"![{alt}](./{cat['image']})")
        lines.append("")
    lines.append(f"# {cat['title']}")
    lines.append("")
    if cat.get("description"):
        lines.append(cat["description"])
        lines.append("")
    lines.append(BACK_LINK)
    lines.append("")

    layout = cat["layout"]
    if layout == "table":
        for s in cat.get("sections") or []:
            sec_name = s.get("name")
            if s.get("format") == "prose":
                # prose-style section: each entry gets its own ## block
                for e in mine:
                    if (e.get("section") or None) != sec_name:
                        continue
                    lines.append(f"## {e['name']}")
                    lines.append("")
                    if e.get("summary"):
                        lines.append(f"{e['summary']}  ")
                    if e.get("url"):
                        lines.append(e["url"])
                    lines.append("")
                continue
            if sec_name:
                lines.append(f"## {sec_name}")
                lines.append("")
            # main table rows (entries without subsection)
            rows = [
                _row_cells(e, s["fields"], s.get("name_bold", False))
                for e in mine
                if (e.get("section") or None) == sec_name
                and not e.get("subsection")
            ]
            lines.extend(_render_table(s["headers"], rows, s.get("name_bold", False)))
            lines.append("")
            for sub in s.get("subsections") or []:
                sub_rows = [
                    _row_cells(e, sub["fields"], sub.get("name_bold", False))
                    for e in mine
                    if (e.get("section") or None) == sec_name
                    and e.get("subsection") == sub["name"]
                ]
                lines.append(f"### {sub['name']}")
                lines.append("")
                lines.extend(
                    _render_table(sub["headers"], sub_rows, sub.get("name_bold", False))
                )
                lines.append("")
    elif layout == "sections":
        for e in mine:
            lines.append(f"## {e['name']}")
            lines.append("")
            if e.get("summary"):
                lines.append(f"{e['summary']}  ")
            if e.get("url"):
                lines.append(e["url"])
            lines.append("")
    elif layout == "list":
        for e in mine:
            if e.get("url"):
                lines.append(f"- {e['url']}")
        if mine:
            lines.append("")
    return "\n".join(lines)


def regenerate_markdown(data, cat_by_slug, dest_root):
    """Write docs/categories/<slug>/readme.md under dest_root (repo root)."""
    changed = []
    for cat in data["categories"]:
        path = os.path.join(dest_root, "docs", "categories", cat["slug"], "readme.md")
        new = render_category_md(cat, data["entries"])
        old = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                old = f.read()
        if old != new:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            changed.append(path)
    return changed

# ---------------------------------------------------------------------------
# Static site builder
# ---------------------------------------------------------------------------

SITE_CSS = """
:root {
  --bg: #ffffff; --fg: #1a1a1a; --muted: #6b7280; --card: #f8fafc;
  --border: #e5e7eb; --accent: #2563eb; --chip: #eef2ff;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f172a; --fg: #e2e8f0; --muted: #94a3b8; --card: #1e293b;
    --border: #334155; --accent: #60a5fa; --chip: #1e3a8a;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
    "Helvetica Neue", Arial, sans-serif;
  background: var(--bg); color: var(--fg); line-height: 1.5;
}
.wrap { max-width: 1100px; margin: 0 auto; padding: 0 16px 64px; }
header.top { padding: 40px 0 16px; }
header.top h1 { margin: 0 0 8px; font-size: 1.9rem; }
header.top p { margin: 0; color: var(--muted); }
.searchbar { position: sticky; top: 0; z-index: 10; background: var(--bg);
  padding: 12px 0; border-bottom: 1px solid var(--border); }
.searchbar input {
  width: 100%; padding: 12px 16px; font-size: 1rem; border-radius: 10px;
  border: 1px solid var(--border); background: var(--card); color: var(--fg);
}
.filters { padding: 12px 0 4px; display: flex; flex-wrap: wrap; gap: 8px; }
.chip {
  border: 1px solid var(--border); background: var(--card); color: var(--fg);
  border-radius: 999px; padding: 6px 12px; font-size: 0.85rem; cursor: pointer;
}
.chip small { color: var(--muted); }
.chip.active { background: var(--accent); border-color: var(--accent); color: #fff; }
.chip.active small { color: #fff; opacity: 0.8; }
.chip.tag.active { background: var(--chip); border-color: var(--accent); color: var(--accent); }
.chip.tag.active small { color: var(--accent); }
.section-label { font-size: 0.8rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 14px 0 2px; }
h2.cat { margin: 36px 0 4px; font-size: 1.4rem; }
.cat-desc { color: var(--muted); margin: 0 0 12px; }
.cat img.banner { width: 100%; max-height: 220px; object-fit: cover;
  border-radius: 12px; border: 1px solid var(--border); }
h3.sub { margin: 20px 0 4px; font-size: 1.05rem; color: var(--muted); }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px; margin-top: 12px; }
.card { background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px; }
.card h3 { margin: 0 0 6px; font-size: 1rem; }
.card h3 a { color: var(--fg); text-decoration: none; }
.card h3 a:hover { color: var(--accent); text-decoration: underline; }
.card p { margin: 0 0 8px; font-size: 0.9rem; color: var(--muted); }
.card .meta { display: flex; flex-wrap: wrap; gap: 6px; }
.badge { font-size: 0.72rem; border-radius: 999px; padding: 2px 8px;
  background: var(--chip); color: var(--accent); }
.table-scroll { overflow-x: auto; margin-top: 12px;
  border: 1px solid var(--border); border-radius: 12px; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem;
  background: var(--card); }
th, td { text-align: left; padding: 10px 12px; border-bottom: 1px solid var(--border);
  vertical-align: top; }
thead th { color: var(--muted); font-weight: 600; white-space: nowrap;
  background: var(--card); }
tbody tr:last-child td { border-bottom: none; }
td a { color: var(--accent); }
td { min-width: 120px; }
.result-count { color: var(--muted); margin: 20px 0 4px; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
  color: var(--muted); font-size: 0.85rem; }
footer a { color: var(--accent); }
"""

SITE_JS = """
const DATA = __DATA_JSON__;
const state = { q: "", cat: "all", tags: new Set() };
const $ = (s) => document.querySelector(s);

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function match(e) {
  if (state.cat !== "all" && e.category !== state.cat) return false;
  for (const t of state.tags) {
    if (!(e.tags || []).includes(t)) return false;
  }
  if (state.q) {
    const hay = [e.name, e.summary, e.category, (e.tags || []).join(" ")]
      .join(" ").toLowerCase();
    for (const tok of state.q.toLowerCase().split(/\\s+/)) {
      if (tok && !hay.includes(tok)) return false;
    }
  }
  return true;
}

function card(e) {
  const tags = (e.tags || []).map((t) => `<span class="badge">${esc(t)}</span>`).join("");
  const name = e.url
    ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.name)}</a>`
    : esc(e.name);
  const extra = e.extra
    ? Object.entries(e.extra).map(([k, v]) => `<span class="badge">${esc(k)}: ${esc(v)}</span>`).join("")
    : "";
  return `<div class="card"><h3>${name}</h3>` +
    (e.summary ? `<p>${esc(e.summary)}</p>` : "") +
    `<div class="meta">${tags}${extra}</div></div>`;
}

function cellHtml(e, field, nameBold) {
  if (field === "name") {
    const n = nameBold ? `<strong>${esc(e.name)}</strong>` : esc(e.name);
    return e.url
      ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${n}</a>`
      : n;
  }
  if (field === "summary") return esc(e.summary || "-");
  if (field === "source") {
    if (e.url) {
      const label = e.url_label || e.url;
      return `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(label)}</a>`;
    }
    return esc(e.url_label || "-");
  }
  if (field === "url") {
    return e.url
      ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.url)}</a>`
      : "";
  }
  if (field.startsWith("extra.")) {
    return esc(((e.extra || {})[field.slice(6)] || ""));
  }
  return esc(e[field] || "");
}

function tableHtml(sec, items) {
  const th = sec.headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const rows = items.map((e) => {
    const tds = sec.fields
      .map((f) => `<td>${cellHtml(e, f, sec.name_bold)}</td>`)
      .join("");
    return `<tr>${tds}</tr>`;
  }).join("");
  return `<div class="table-scroll"><table><thead><tr>${th}</tr></thead>` +
    `<tbody>${rows}</tbody></table></div>`;
}

function renderCategory(c, items) {
  if (c.layout === "table") {
    let h = "";
    for (const s of c.sections || []) {
      const secItems = items.filter((e) => (e.section || "") === (s.name || ""));
      if (!secItems.length) continue;
      if (s.format === "prose") {
        h += `<div class="grid">${secItems.map(card).join("")}</div>`;
        continue;
      }
      if (s.name) h += `<h3 class="sub">${esc(s.name)}</h3>`;
      const main = secItems.filter((e) => !e.subsection);
      const subs = s.subsections || [];
      if (main.length || !subs.length) h += tableHtml(s, main);
      for (const sub of subs) {
        const subItems = secItems.filter((e) => e.subsection === sub.name);
        if (!subItems.length) continue;
        h += `<h3 class="sub">${esc(sub.name)}</h3>`;
        h += tableHtml(sub, subItems);
      }
    }
    return h;
  }
  return `<div class="grid">${items.map(card).join("")}</div>`;
}

function catTitle(slug) {
  const c = DATA.categories.find((c) => c.slug === slug);
  return c ? c.title : slug;
}

function render() {
  const list = DATA.entries.filter(match);
  const main = $("#results");
  const filtering = state.q || state.cat !== "all" || state.tags.size > 0;
  let h = "";
  if (filtering) {
    h += `<div class="result-count">${list.length} result${list.length === 1 ? "" : "s"}</div>`;
    if (!list.length) h += `<div class="empty">No matches. Try a different search.</div>`;
  }
  const cats = state.cat === "all"
    ? DATA.categories
    : DATA.categories.filter((c) => c.slug === state.cat);
  for (const c of cats) {
    const items = list.filter((e) => e.category === c.slug);
    if (!items.length) continue;
    h += `<h2 class="cat">${esc(c.title)}</h2>`;
    if (!filtering) {
      if (c.image) h += `<img class="banner" loading="lazy" src="images/${esc(c.image)}" alt="${esc(c.title)}">`;
      if (c.description) h += `<p class="cat-desc">${esc(c.description)}</p>`;
    }
    h += renderCategory(c, items);
  }
  main.innerHTML = h;
  document.querySelectorAll("#catchips .chip").forEach((el) => {
    el.classList.toggle("active", el.dataset.cat === state.cat);
  });
  document.querySelectorAll("#tagchips .chip").forEach((el) => {
    el.classList.toggle("active", state.tags.has(el.dataset.tag));
  });
}

function init() {
  const counts = {};
  DATA.entries.forEach((e) => { counts[e.category] = (counts[e.category] || 0) + 1; });
  $("#catchips").innerHTML =
    `<button class="chip" data-cat="all">All <small>${DATA.entries.length}</small></button>` +
    DATA.categories.map((c) =>
      `<button class="chip" data-cat="${esc(c.slug)}">${esc(c.title)} <small>${counts[c.slug] || 0}</small></button>`
    ).join("");
  const tcounts = {};
  DATA.entries.forEach((e) => (e.tags || []).forEach((t) => { tcounts[t] = (tcounts[t] || 0) + 1; }));
  const tags = Object.keys(tcounts).sort();
  $("#tagchips").innerHTML = tags.map((t) =>
    `<button class="chip tag" data-tag="${esc(t)}">${esc(t)} <small>${tcounts[t]}</small></button>`
  ).join("");
  $("#catchips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip"); if (!b) return;
    state.cat = b.dataset.cat; render();
  });
  $("#tagchips").addEventListener("click", (ev) => {
    const b = ev.target.closest(".chip"); if (!b) return;
    const t = b.dataset.tag;
    if (state.tags.has(t)) state.tags.delete(t); else state.tags.add(t);
    render();
  });
  $("#q").addEventListener("input", (ev) => { state.q = ev.target.value; render(); });
  render();
}
document.addEventListener("DOMContentLoaded", init);
"""

SITE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ — Browse</title>
<meta name="description" content="__DESC__">
<style>__CSS__</style>
</head>
<body>
<div class="wrap">
<header class="top">
<h1>__TITLE__</h1>
<p>__DESC__ Search across all entries, filter by category and tags.</p>
</header>
</div>
<div class="searchbar"><div class="wrap" style="padding-bottom:0">
<input id="q" type="search" placeholder="Search models, tools, frameworks…" autocomplete="off">
</div></div>
<div class="wrap">
<div class="section-label">Categories</div>
<div class="filters" id="catchips"></div>
<div class="section-label">Tags</div>
<div class="filters" id="tagchips"></div>
<main id="results"></main>
<footer>
Generated from <a href="__REPO_URL__/blob/main/data/entries.yml"><code>data/entries.yml</code></a>.
Contributions welcome — see the <a href="__REPO_URL__">repository</a>.
</footer>
</div>
<script>__JS__</script>
</body>
</html>
"""


def build_site(data, out_dir, repo_url):
    os.makedirs(out_dir, exist_ok=True)
    # copy images
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)
    for cat in data["categories"]:
        img = cat.get("image")
        if not img:
            continue
        src = os.path.join(
            REPO_ROOT, "docs", "categories", cat["slug"], img
        )
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(img_dir, img))

    payload = {
        "categories": [
            {
                "slug": c["slug"],
                "title": c["title"],
                "description": c.get("description") or "",
                "image": c.get("image") or "",
                "layout": c["layout"],
                "sections": [
                    {
                        "name": s.get("name"),
                        "format": s.get("format", "table"),
                        "headers": s.get("headers") or [],
                        "fields": s.get("fields") or [],
                        "name_bold": s.get("name_bold", False),
                        "subsections": [
                            {
                                "name": x.get("name"),
                                "headers": x.get("headers") or [],
                                "fields": x.get("fields") or [],
                                "name_bold": x.get("name_bold", False),
                            }
                            for x in s.get("subsections") or []
                        ],
                    }
                    for s in c.get("sections") or []
                ],
            }
            for c in data["categories"]
        ],
        "entries": data["entries"],
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # guard against </script> inside data
    data_json = data_json.replace("</", "<\\/")

    js = SITE_JS.replace("__DATA_JSON__", data_json)
    page = SITE_HTML.replace("__TITLE__", "LLM · Prompt Engineering · Agents")
    page = page.replace(
        "__DESC__",
        "Curated links for ChatGPT, LLMs, prompt engineering, agent frameworks, "
        "coding tools, libraries, and adjacent resources.",
    )
    page = page.replace("__CSS__", SITE_CSS)
    page = page.replace("__JS__", js)
    page = page.replace("__REPO_URL__", repo_url)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    # keep GitHub Pages from running Jekyll
    open(os.path.join(out_dir, ".nojekyll"), "w").close()
    return os.path.join(out_dir, "index.html")

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

REPO_URL = "https://github.com/koji/LLM-PromptEngineering-Agents"


def load_data(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    data = parse_yaml_subset(text)
    if not isinstance(data, dict):
        raise SystemExit("entries.yml: top level must be a mapping")
    return data


def cmd_check(data, cat_by_slug):
    tmp = tempfile.mkdtemp(prefix="gen-check-")
    try:
        regenerate_markdown(data, cat_by_slug, tmp)
        problems = []
        for cat in data["categories"]:
            rel = os.path.join("docs", "categories", cat["slug"], "readme.md")
            gen = os.path.join(tmp, rel)
            cur = os.path.join(REPO_ROOT, rel)
            with open(gen, encoding="utf-8") as f:
                new = f.read()
            old = ""
            if os.path.exists(cur):
                with open(cur, encoding="utf-8") as f:
                    old = f.read()
            if old != new:
                problems.append(rel)
                diff = difflib.unified_diff(
                    old.splitlines(), new.splitlines(),
                    fromfile=f"committed/{rel}", tofile=f"generated/{rel}",
                    lineterm="",
                )
                print("\n".join(list(diff)[:60]))
        if problems:
            print(
                "\nCategory pages are out of sync with data/entries.yml:\n  "
                + "\n  ".join(problems)
                + "\nRun `python3 scripts/generate.py` and commit the result.",
                file=sys.stderr,
            )
            return 1
        print("OK: category pages are in sync with data/entries.yml")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="fail if committed pages differ from generated ones")
    ap.add_argument("--site-out", default=os.path.join(REPO_ROOT, "site"),
                    help="output directory for the static site")
    ap.add_argument("--data", default=os.path.join(REPO_ROOT, "data", "entries.yml"),
                    help="path to entries.yml")
    ap.add_argument("--repo-url", default=REPO_URL, help="repository URL for links")
    args = ap.parse_args(argv)

    try:
        data = load_data(args.data)
    except (YamlError, OSError) as e:
        raise SystemExit(f"failed to load {args.data}: {e}")
    cat_by_slug = validate(data)

    if args.check:
        return cmd_check(data, cat_by_slug)

    changed = regenerate_markdown(data, cat_by_slug, REPO_ROOT)
    for p in changed:
        print("updated", os.path.relpath(p, REPO_ROOT))
    index = build_site(data, args.site_out, args.repo_url)
    print(f"site -> {os.path.relpath(index, REPO_ROOT)} "
          f"({len(data['entries'])} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
