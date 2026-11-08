# /// script
# requires-python = ">=3.10"
# dependencies = ["markdown==3.7"]
# ///
"""Build docs/site/index.html, the single-page reading view of the Markdown docs.

The Markdown files are the source of truth. After editing any of them, run:

    uv run docs/site/build.py

The output is one self-contained HTML file (inline CSS and JS; images are
referenced relatively from docs/images; Mermaid is loaded from a pinned CDN URL).
"""

from __future__ import annotations

import html
import re
import struct
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "site" / "index.html"
MERMAID_URL = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"

# (doc id, path, nav label, group)
DOCS = [
    ("readme", "README.md", "Overview", "main"),
    ("findings", "docs/findings.md", "Findings", "main"),
    ("proxy-design", "docs/proxy-design.md", "Proxy design", "main"),
    (
        "rb-google-workspace-admin",
        "docs/runbooks/google-workspace-admin.md",
        "Google Workspace admin",
        "runbook",
    ),
    ("rb-claude-org", "docs/runbooks/claude-org.md", "Claude org (Owner)", "runbook"),
    (
        "rb-claude-code-managed-settings",
        "docs/runbooks/claude-code-managed-settings.md",
        "Claude Code managed settings",
        "runbook",
    ),
    ("rb-payhawk", "docs/runbooks/payhawk.md", "Payhawk", "runbook"),
    ("local-development", "docs/local-development.md", "Local development", "runbook"),
    ("configuration", "docs/configuration.md", "Configuration", "runbook"),
    ("deploy", "DEPLOY.md", "Deploy", "runbook"),
    ("nexos-setup", "docs/nexos-setup.md", "nexos.ai setup", "runbook"),
    ("security-model", "docs/security-model.md", "Security model", "runbook"),
    ("questions", "docs/questions.md", "Questions for leadership", "main"),
]
PATH_TO_ID = {p: i for i, p, _, _ in DOCS}


def gh_slug(text: str) -> str:
    """GitHub-style heading slug, so Markdown anchors keep working."""
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def resolve_link(doc_path: str, href: str) -> str:
    if re.match(r"^[a-z]+:", href):
        return href
    path, _, anchor = href.partition("#")
    if not path:
        target = doc_path
    else:
        target = str((Path(doc_path).parent / path).as_posix())
        parts = []
        for seg in target.split("/"):
            if seg == "..":
                parts.pop()
            elif seg not in ("", "."):
                parts.append(seg)
        target = "/".join(parts)
    doc_id = PATH_TO_ID.get(target)
    if doc_id is None:
        return href
    return f"#{doc_id}--{anchor}" if anchor else f"#{doc_id}"


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as fh:
        head = fh.read(24)
    return struct.unpack(">II", head[16:24])


def render(doc_id: str, doc_path: str) -> tuple[str, str, list[tuple[str, str]]]:
    src = (ROOT / doc_path).read_text(encoding="utf-8")
    md = markdown.Markdown(
        extensions=["tables", "fenced_code", "sane_lists", "toc"],
        extension_configs={"toc": {"slugify": lambda value, sep: f"{doc_id}--{gh_slug(value)}"}},
    )
    body = md.convert(src)

    # Title: first h1, removed from the body (shown as the section heading).
    m = re.search(r"<h1 id=\"[^\"]*\">(.*?)</h1>", body, re.S)
    title = m.group(1) if m else doc_id
    if m:
        body = body[: m.start()] + body[m.end() :]

    # Mermaid fences -> <pre class="mermaid">.
    body = re.sub(
        r'<pre><code class="language-mermaid">(.*?)</code></pre>',
        lambda mm: (
            f'<pre class="mermaid" data-src="{html.escape(html.unescape(mm.group(1)), quote=True)}">{mm.group(1)}</pre>'
        ),
        body,
        flags=re.S,
    )

    # Image + "Source:" caption paragraph -> <figure>.
    def fig(mm: re.Match) -> str:
        alt, src_ = mm.group("alt"), mm.group("src")
        name = src_.rsplit("/", 1)[-1]
        rel = f"../images/{name}"
        cap = mm.group("cap") or ""
        w, h = png_size(ROOT / "docs" / "images" / name)
        return (
            f'<figure><a href="{rel}" target="_blank" rel="noopener"><img src="{rel}" alt="{alt}" width="{w}" height="{h}" style="aspect-ratio:{w}/{h}" loading="lazy"></a>'
            + (f"<figcaption>{cap}</figcaption>" if cap else "")
            + "</figure>"
        )

    body = re.sub(
        r'<p><img alt="(?P<alt>[^"]*)" src="(?P<src>[^"]+)"\s*/?></p>\s*(?:<p><em>(?P<cap>.*?)</em></p>)?',
        fig,
        body,
        flags=re.S,
    )

    # Rewrite links between docs to in-page anchors; external links open in a new tab.
    def link(mm: re.Match) -> str:
        href = html.unescape(mm.group(1))
        if re.match(r"^https?:", href):
            return f'<a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener"'
        if href.startswith("#"):
            return f'<a href="#{doc_id}--{href[1:]}"'
        return f'<a href="{html.escape(resolve_link(doc_path, href), quote=True)}"'

    body = re.sub(r'<a href="([^"]+)"', link, body)

    # Wide tables scroll horizontally inside their own box.
    body = body.replace("<table>", '<div class="table-wrap"><table>').replace(
        "</table>", "</table></div>"
    )

    h2s = re.findall(r'<h2 id="([^"]+)">(.*?)</h2>', body, re.S)
    return title, body, [(i, re.sub(r"<[^>]+>", "", t)) for i, t in h2s]


def mark_comparison_table(body: str) -> str:
    """Give the README comparison table an id and a filter box."""
    anchor = 'id="readme--comparison-of-solutions"'
    i = body.find(anchor)
    if i < 0:
        return body
    j = body.find('<div class="table-wrap"><table>', i)
    controls = (
        '<div class="table-tools">'
        '<label for="cmp-filter">Filter</label>'
        '<input id="cmp-filter" type="search" placeholder="e.g. nexos, Enterprise, Hard, GWA" autocomplete="off">'
        '<span id="cmp-count" aria-live="polite"></span>'
        '<span class="hint">Click a column header to sort.</span>'
        "</div>"
    )
    return (
        body[:j]
        + controls
        + body[j:].replace("<table>", '<table id="compare" class="sortable">', 1)
    )


CSS = r"""
:root{--bg:#fbfaf7;--panel:#ffffff;--text:#1d1f23;--muted:#5b6170;--line:#e3e1db;--accent:#2f5bd3;--accent-soft:#e8eefc;--code:#f2f1ed;--mark:#fff1a8;--nav-w:290px;color-scheme:light}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#15171b;--panel:#1c1f24;--text:#e6e6e3;--muted:#a3a8b3;--line:#2e323a;--accent:#86a8ff;--accent-soft:#232b40;--code:#23262c;--mark:#5a4d12;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#15171b;--panel:#1c1f24;--text:#e6e6e3;--muted:#a3a8b3;--line:#2e323a;--accent:#86a8ff;--accent-soft:#232b40;--code:#23262c;--mark:#5a4d12;color-scheme:dark}
*{box-sizing:border-box}
html{scroll-padding-top:16px}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.6 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
code{font:0.88em/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:0.1em 0.35em;border-radius:4px;overflow-wrap:anywhere}
pre{background:var(--code);padding:14px 16px;border-radius:8px;overflow:auto;line-height:1.45}
pre code{background:none;padding:0;overflow-wrap:normal}
.layout{display:grid;grid-template-columns:var(--nav-w) minmax(0,1fr);min-height:100vh}
nav.side{position:sticky;top:0;height:100vh;overflow:auto;border-right:1px solid var(--line);background:var(--panel);padding:20px 16px 40px}
nav.side .brand{font-weight:700;font-size:1.05rem;margin:0 0 4px}
nav.side .sub{color:var(--muted);font-size:0.85rem;margin:0 0 16px}
nav.side h4{font-size:0.72rem;letter-spacing:0.08em;text-transform:uppercase;color:var(--muted);margin:18px 0 6px}
nav.side ul{list-style:none;margin:0;padding:0}
nav.side li a{display:block;padding:4px 8px;border-radius:6px;color:var(--text);text-decoration:none;font-size:0.92rem}
nav.side li a:hover,nav.side li a.active{background:var(--accent-soft);color:var(--accent)}
nav.side li ul a{font-size:0.84rem;color:var(--muted);padding-left:18px}
.theme-btn{margin-top:18px;width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);cursor:pointer;font:inherit;font-size:0.9rem}
main{padding:28px 40px 80px;max-width:1180px;width:100%;min-width:0}
section.doc{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 32px 28px;margin:0 0 28px;min-width:0}
section.doc>h1{font-size:1.75rem;line-height:1.25;margin:24px 0 8px}
h2{font-size:1.35rem;margin:32px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:1.1rem;margin:24px 0 8px}
blockquote{margin:16px 0;padding:10px 16px;border-left:4px solid var(--accent);background:var(--accent-soft);border-radius:0 8px 8px 0}
blockquote p{margin:4px 0}
.table-wrap{overflow-x:auto;margin:14px 0;border:1px solid var(--line);border-radius:8px;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:0.9rem}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;text-align:left}
th{background:var(--code);position:sticky;top:0}
tr:last-child td{border-bottom:none}
#compare{min-width:980px}
#compare th{cursor:pointer;user-select:none;white-space:nowrap}
#compare th[aria-sort="ascending"]::after{content:" \25B2";font-size:0.7em}
#compare th[aria-sort="descending"]::after{content:" \25BC";font-size:0.7em}
.table-tools{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:12px 0 0}
.table-tools input{flex:1 1 220px;min-width:0;padding:7px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text);font:inherit}
.table-tools .hint,#cmp-count{color:var(--muted);font-size:0.85rem}
figure{margin:16px 0 22px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:#fff}
figure img{display:block;width:100%;height:auto}
figcaption{padding:8px 12px;font-size:0.85rem;color:var(--muted);background:var(--panel);border-top:1px solid var(--line)}
pre.mermaid{background:var(--panel);border:1px solid var(--line);text-align:center;overflow-x:auto}
details.runbook{border:1px solid var(--line);border-radius:12px;background:var(--panel);margin:0 0 16px}
details.runbook>summary{cursor:pointer;padding:16px 24px;font-size:1.2rem;font-weight:650;list-style:none;display:flex;align-items:center;gap:10px}
details.runbook>summary::-webkit-details-marker{display:none}
details.runbook>summary::before{content:"\25B8";color:var(--muted);transition:transform .15s}
details.runbook[open]>summary::before{transform:rotate(90deg)}
details.runbook>summary .tag{font-size:0.72rem;font-weight:600;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
details.runbook>.doc-body{padding:0 28px 24px}
.group-title{font-size:1.4rem;margin:36px 0 14px}
.topbar,.backdrop{display:none}
@media (max-width:900px){
  .layout{grid-template-columns:minmax(0,1fr)}
  nav.side{position:fixed;inset:0 25% 0 0;z-index:20;transform:translateX(-102%);transition:transform .2s;height:100%;box-shadow:0 0 30px rgba(0,0,0,.25)}
  body.nav-open nav.side{transform:none}
  .topbar{display:flex;position:sticky;top:0;z-index:10;align-items:center;gap:10px;padding:10px 16px;background:var(--panel);border-bottom:1px solid var(--line)}
  .topbar button{border:1px solid var(--line);background:var(--bg);color:var(--text);border-radius:8px;padding:6px 10px;font:inherit;white-space:nowrap;flex:none}
  .topbar .t{flex:1 1 auto;min-width:0}
  .backdrop{display:block;position:fixed;inset:0;z-index:15;background:rgba(0,0,0,.35);opacity:0;pointer-events:none;transition:opacity .2s}
  body.nav-open .backdrop{opacity:1;pointer-events:auto}
  .topbar .t{font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  main{padding:16px 16px 60px}
  section.doc{padding:4px 16px 20px}
  details.runbook>summary{padding:14px 16px}
  details.runbook>.doc-body{padding:0 16px 20px}
  html{scroll-padding-top:64px}
}
@media print{
  nav.side,.topbar,.table-tools,.theme-btn{display:none!important}
  .layout{display:block}
  main{padding:0;max-width:none}
  body{background:#fff;color:#000;font-size:11pt}
  section.doc,details.runbook{border:none;padding:0;break-before:page}
  details.runbook>summary::before{content:""}
  .table-wrap{overflow:visible;border:none}
  #compare{min-width:0}
  th{position:static}
  figure,pre,table{break-inside:avoid}
  a{color:#000}
}
"""

JS = r"""
(function(){
  var root=document.documentElement;
  // Theme: system default, toggle overrides, remembered per viewer.
  function current(){var t=root.getAttribute('data-theme');if(t)return t;return matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light'}
  try{var saved=localStorage.getItem('theme');if(saved)root.setAttribute('data-theme',saved)}catch(e){}
  function label(){var d=current()==='dark';document.querySelectorAll('.theme-btn').forEach(function(b){b.textContent=d?'Light mode':'Dark mode'});document.querySelectorAll('.theme-btn-top').forEach(function(b){b.textContent=d?'Light':'Dark';b.setAttribute('aria-label',d?'Switch to light mode':'Switch to dark mode')})}
  document.querySelectorAll('.theme-btn,.theme-btn-top').forEach(function(b){b.addEventListener('click',function(){var n=current()==='dark'?'light':'dark';root.setAttribute('data-theme',n);try{localStorage.setItem('theme',n)}catch(e){}label();renderMermaid()})});
  label();

  // Mermaid, re-rendered on theme change.
  function renderMermaid(){
    if(!window.mermaid)return;
    var nodes=document.querySelectorAll('pre.mermaid');
    nodes.forEach(function(n){n.removeAttribute('data-processed');n.textContent=n.getAttribute('data-src')});
    mermaid.initialize({startOnLoad:false,theme:current()==='dark'?'dark':'default',securityLevel:'strict',flowchart:{htmlLabels:true}});
    mermaid.run({nodes:nodes}).catch(function(e){console.warn('mermaid',e)});
  }
  if(window.mermaid)mermaid.initialize({startOnLoad:false});
  window.addEventListener('load',renderMermaid);

  // Open a collapsed runbook when navigating into it.
  function openFor(hash){
    if(!hash||hash.length<2)return;
    var el=document.getElementById(decodeURIComponent(hash.slice(1)));
    if(!el)return;
    var d=el.closest('details');
    if(d&&!d.open){d.open=true;setTimeout(function(){el.scrollIntoView()},0)}
  }
  window.addEventListener('hashchange',function(){openFor(location.hash)});
  openFor(location.hash);

  // Print: expand everything.
  var wasClosed=[];
  window.addEventListener('beforeprint',function(){wasClosed=[].filter.call(document.querySelectorAll('details'),function(d){return !d.open});wasClosed.forEach(function(d){d.open=true})});
  window.addEventListener('afterprint',function(){wasClosed.forEach(function(d){d.open=false})});

  // Mobile nav.
  var body=document.body;
  var tb=document.querySelector('.nav-toggle');
  if(tb)tb.addEventListener('click',function(){body.classList.toggle('nav-open');tb.setAttribute('aria-expanded',body.classList.contains('nav-open'))});
  function closeNav(){body.classList.remove('nav-open');if(tb)tb.setAttribute('aria-expanded','false')}
  document.querySelectorAll('nav.side a').forEach(function(a){a.addEventListener('click',closeNav)});
  var bd=document.querySelector('.backdrop');if(bd)bd.addEventListener('click',closeNav);
  document.addEventListener('keydown',function(e){if(e.key==='Escape')closeNav()});

  // Comparison table: filter and sort.
  var table=document.getElementById('compare');
  if(table){
    var rows=[].slice.call(table.tBodies[0].rows);
    var input=document.getElementById('cmp-filter'),count=document.getElementById('cmp-count');
    function filter(){
      var q=input.value.trim().toLowerCase(),shown=0;
      rows.forEach(function(r){var ok=!q||q.split(/\s+/).every(function(w){return r.textContent.toLowerCase().indexOf(w)>=0});r.hidden=!ok;if(ok)shown++});
      count.textContent=shown+' of '+rows.length+' shown';
    }
    input.addEventListener('input',filter);filter();
    [].forEach.call(table.tHead.rows[0].cells,function(th,i){
      th.tabIndex=0;th.setAttribute('role','columnheader');
      function sort(){
        var dir=th.getAttribute('aria-sort')==='ascending'?'descending':'ascending';
        [].forEach.call(table.tHead.rows[0].cells,function(o){o.removeAttribute('aria-sort')});
        th.setAttribute('aria-sort',dir);
        var k=dir==='ascending'?1:-1;
        rows.sort(function(a,b){var x=a.cells[i].textContent.trim(),y=b.cells[i].textContent.trim();var nx=parseFloat(x),ny=parseFloat(y);if(!isNaN(nx)&&!isNaN(ny)&&/^\d/.test(x)&&/^\d/.test(y))return (nx-ny)*k;return x.localeCompare(y)*k});
        rows.forEach(function(r){table.tBodies[0].appendChild(r)});
      }
      th.addEventListener('click',sort);th.addEventListener('keydown',function(e){if(e.key==='Enter'||e.key===' '){e.preventDefault();sort()}});
    });
  }

  // Highlight the nav entry for the section in view.
  var links={};document.querySelectorAll('nav.side a[href^="#"]').forEach(function(a){links[a.getAttribute('href').slice(1)]=a});
  if('IntersectionObserver' in window){
    var io=new IntersectionObserver(function(es){es.forEach(function(e){if(e.isIntersecting&&links[e.target.id]){Object.values(links).forEach(function(a){a.classList.remove('active')});links[e.target.id].classList.add('active')}})},{rootMargin:'0px 0px -70% 0px'});
    Object.keys(links).forEach(function(id){var el=document.getElementById(id);if(el)io.observe(el)});
  }
})();
"""


def main() -> None:
    rendered = {doc_id: render(doc_id, path) for doc_id, path, _, _ in DOCS}
    readme_title, readme_body, _ = rendered["readme"]
    rendered["readme"] = (readme_title, mark_comparison_table(readme_body), rendered["readme"][2])

    nav_main, nav_rb, sections_main, sections_rb = [], [], [], []
    for doc_id, path, label, group in DOCS:
        title, body, h2s = rendered[doc_id]
        sub = "".join(f'<li><a href="#{i}">{t}</a></li>' for i, t in h2s) if group == "main" else ""
        item = (
            f'<li><a href="#{doc_id}">{html.escape(label)}</a>'
            + (f"<ul>{sub}</ul>" if sub else "")
            + "</li>"
        )
        src_note = f'<p class="src-note" style="color:var(--muted);font-size:.85rem;margin:4px 0 0">Source: <code>{path}</code></p>'
        if group == "runbook":
            nav_rb.append(item)
            sections_rb.append(
                f'<details class="runbook" id="{doc_id}"><summary>{title}<span class="tag">runbook</span></summary>'
                f'<div class="doc-body">{src_note}{body}</div></details>'
            )
        else:
            nav_main.append(item)
            block = f'<section class="doc" id="{doc_id}"><h1>{title}</h1>{src_note}{body}</section>'
            sections_main.append((doc_id, block))

    # The questions for leadership come last, after the runbooks.
    last = {"questions"}
    before = [b for i, b in sections_main if i not in last]
    after = [b for i, b in sections_main if i in last]
    nav_main_items = [n for n in nav_main if not any(f'href="#{i}"' in n for i in last)]
    nav_after = [n for n in nav_main if any(f'href="#{i}"' in n for i in last)]

    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Read-only MCP Findings</title>
<meta name="description" content="How to keep Gmail and Payhawk MCP tools read-only in Claude Team/Enterprise and nexos.ai: findings, runbooks and a proxy design proposal.">
<style>{CSS}</style>
</head>
<body>
<!-- Generated by docs/site/build.py from the Markdown docs. Do not edit by hand. -->
<div class="topbar"><button class="nav-toggle" aria-expanded="false" aria-controls="side">Menu</button><span class="t">Read-only MCP findings</span><button class="theme-btn-top" type="button">Dark</button></div>
<div class="backdrop" aria-hidden="true"></div>
<div class="layout">
<nav class="side" id="side" aria-label="Contents">
<p class="brand">Read-only MCP findings</p>
<p class="sub">Gmail and Payhawk in Claude and nexos.ai. Sources checked 2026-11-08.</p>
<h4>Documents</h4><ul>{"".join(nav_main_items)}</ul>
<h4>Runbooks</h4><ul>{"".join(nav_rb)}</ul>
<h4>Decisions</h4><ul>{"".join(nav_after)}</ul>
<button class="theme-btn" type="button">Dark mode</button>
</nav>
<main>
{"".join(before)}
<h2 class="group-title" id="runbooks">Runbooks</h2>
{"".join(sections_rb)}
{"".join(after)}
</main>
</div>
<script src="{MERMAID_URL}"></script>
<script>{JS}</script>
</body>
</html>
"""
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page) // 1024} KiB)")


if __name__ == "__main__":
    main()
