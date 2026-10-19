#!/usr/bin/env python3
"""
Must Hear Singles — HTML Generator

Clon de Must Hear para canciones: descarga listas de singles desde Wikipedia
(Billboard, UK, Los 40, Promusicae, Triple J, Grammy, Eurovisión), las cruza con
los scrobbles de cada usuario y genera docs/singles/.

  python3 html_singles.py                          # todo (solo scrapea lo que falta)
  python3 html_singles.py --sources billboard_yearend los40
  python3 html_singles.py --force-scrape            # re-descarga todas las listas
  python3 html_singles.py --no-scrape               # solo recalcula escuchas + HTML
  python3 html_singles.py --no-scrape --no-covers --no-youtube   # solo escuchas + HTML
  python3 html_singles.py --list-sources
"""

import argparse, json, sqlite3, sys, time
from datetime import datetime
from html import escape
from pathlib import Path

from tools.singles.wikipedia_charts import SOURCES, THIS_YEAR, iter_source_lists, scrape_list, wiki_url
from tools.singles import singles_database as sdb
from tools.singles.covers import fetch_covers, load_covers
from tools.singles.youtube import fetch_youtube, load_youtube
from html_must_hear import _mh_user_modal_css, _mh_user_modal_html, _mh_user_modal_btn, _mh_user_modal_js
from html_must_hear import render_root_index_html

DEFAULT_SCROBBLES_DB = "db/lastfm_cache_rym_new_normalized.db"
DEFAULT_SINGLES_DB   = "db/must_hear_singles.db"
DEFAULT_OUT          = "docs/singles"
MUST_HEAR_ROOT       = Path("docs/must_hear")
RECENT_REFRESH_SECS  = 7 * 86400     # listas del año en curso: re-scrapear cada semana


# ── SCRAPING ───────────────────────────────────────────────────────────────────

def _needs_scrape(row, lst: dict, force: bool) -> bool:
    if force or row is None or not row["total_tracks"]:
        return True
    y = lst["year"]
    still_moving = y is None or (y + 10 > THIS_YEAR if lst["decade"] else y >= THIS_YEAR - 1)
    return still_moving and time.time() - (row["last_updated"] or 0) > RECENT_REFRESH_SECS


def scrape_sources(conn, source_keys: list[str], force: bool) -> None:
    for key in source_keys:
        src = SOURCES[key]
        print(f"\n🌐 {src['group_name']}")
        try:
            lists = iter_source_lists(key)
        except Exception as e:
            print(f"  ✗ no se pudo listar la fuente: {e}")
            continue
        print(f"  {len(lists)} listas en Wikipedia")
        for lst in lists:
            if not _needs_scrape(sdb.get_collection(conn, lst["slug"]), lst, force):
                continue
            try:
                items = scrape_list(key, lst)
            except Exception as e:
                print(f"  ✗ {lst['name']}: {e}")
                continue
            if not items:
                print(f"  ⚠ {lst['name']}: sin canciones (¿tabla con otro formato?)")
                continue
            sdb.save_collection(conn, src["group"], lst, wiki_url(lst["lang"], lst["wiki_title"]), items)
            print(f"  ✓ {lst['name']}: {len(items)} canciones")


# ── STATS ──────────────────────────────────────────────────────────────────────

def _pct_by_user(track_ids: list[int], heard: dict, users: list[str]) -> dict[str, float]:
    n = len(track_ids) or 1
    return {u: round(100 * sum(1 for t in track_ids if u in heard.get(t, {})) / n, 1) for u in users}


def _avg(d: dict[str, float]) -> float:
    return round(sum(d.values()) / len(d), 1) if d else 0.0


# ── HTML: SHARED ───────────────────────────────────────────────────────────────

BASE_CSS = """
  :root {
    --bg:#0a0a0a; --surface:#111; --surface2:#161616; --border:#1e1e1e;
    --accent:#c9a227; --muted:#666; --text:#e0e0e0; --ok:#5fb57a;
    --header-h:52px;
  }
  *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); font-family:'DM Sans',sans-serif; min-height:100vh; }
  a { color:inherit; }
  header {
    position:fixed; top:0; left:0; right:0; z-index:100; height:var(--header-h);
    background:rgba(10,10,10,.97); backdrop-filter:blur(12px);
    border-bottom:1px solid var(--border);
    display:flex; align-items:center; gap:14px; padding:0 24px;
  }
  .site-label { font-family:'DM Mono',monospace; font-size:.6rem; letter-spacing:.18em;
    text-transform:uppercase; color:var(--muted); white-space:nowrap; }
  .site-label a { color:var(--muted); text-decoration:none; }
  .site-label a:hover { color:var(--accent); }
  h1 { font-family:'Bebas Neue',sans-serif; font-size:1.6rem; letter-spacing:.06em; line-height:1;
    color:var(--accent); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; }
  .header-meta { font-family:'DM Mono',monospace; font-size:.6rem; color:var(--muted); margin-left:auto; white-space:nowrap; }
  .header-nav-link {
    font-family:'DM Mono',monospace; font-size:.58rem; letter-spacing:.1em; text-transform:uppercase;
    padding:4px 10px; border-radius:3px; border:1px solid var(--border); color:var(--muted);
    text-decoration:none; transition:all .12s; flex-shrink:0;
  }
  .header-nav-link:hover { border-color:var(--accent); color:var(--accent); }
  main { padding:24px 40px 80px; margin-top:var(--header-h); }
  .section-label { font-family:'DM Mono',monospace; font-size:.65rem; letter-spacing:.2em;
    text-transform:uppercase; color:var(--muted); margin-bottom:20px; }
  .intro { color:var(--muted); font-size:.9rem; margin-bottom:22px; max-width:70ch; }
  footer { padding:24px 40px; border-top:1px solid var(--border);
    font-family:'DM Mono',monospace; font-size:.65rem; color:var(--muted); }
  @media (max-width:700px) {
    header { padding:0 16px; gap:10px; }
    .header-meta, .site-label { display:none; }
    main, footer { padding-left:16px; padding-right:16px; }
  }
"""

CARD_CSS = """
  .collections-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; }
  .col-card {
    display:block; text-decoration:none; background:var(--surface); border:1px solid var(--border);
    border-radius:6px; padding:24px 22px 20px; transition:border-color .15s, transform .15s;
    position:relative; overflow:hidden;
  }
  .col-card::after { content:''; position:absolute; top:0; left:0; right:0; height:3px;
    background:var(--accent); transform:scaleX(0); transform-origin:left; transition:transform .25s ease; }
  .col-card:hover { border-color:#333; transform:translateY(-2px); }
  .col-card:hover::after { transform:scaleX(1); }
  .col-name { font-family:'Bebas Neue',sans-serif; font-size:1.55rem; letter-spacing:.05em;
    color:var(--text); margin-bottom:8px; line-height:1.1; }
  .col-desc { font-size:.8rem; color:var(--muted); margin-bottom:12px; line-height:1.4; }
  .col-meta { font-family:'DM Mono',monospace; font-size:.65rem; color:var(--muted); margin-bottom:14px; }
  .col-bar-wrap { height:3px; background:var(--border); border-radius:2px; overflow:hidden; margin-bottom:6px; }
  .col-bar { height:100%; background:var(--accent); border-radius:2px; transition:width .6s ease; }
  .col-pct { font-family:'DM Mono',monospace; font-size:.62rem; color:var(--muted); }
"""

CARD_JS = """
<script>
(function() {
  function paint() {
    let u = null;
    try { u = localStorage.getItem('mh_user'); } catch (e) {}
    document.querySelectorAll('.col-card').forEach(card => {
      const stats = JSON.parse(card.dataset.stats || '{}');
      const pct = (u && u in stats) ? stats[u] : parseFloat(card.dataset.avg || '0');
      card.querySelector('.col-bar').style.width = pct + '%';
      card.querySelector('.col-pct').textContent =
        pct.toFixed(1) + (u && u in stats ? '% escuchado por ' + u : '% media del grupo');
    });
  }
  window.mhPaintCards = paint;
  paint();
})();
</script>"""


def _page(title: str, head_extra: str, header_html: str, body: str, users: list[str],
          on_select_js: str = "", tail: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape(title)}</title>
<link rel="icon" type="image/png" href="/images/discount.png" />
<!-- Umami Analytics -->
<script
    defer
    src="https://cloud.umami.is/script.js"
    data-website-id="5d84fd6c-0760-4a0c-a2d0-ffabb82179f5"
></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>{BASE_CSS}{head_extra}{_mh_user_modal_css()}
</style>
</head>
<body>
{_mh_user_modal_html(users)}
<header>
{header_html}
  {_mh_user_modal_btn()}
</header>
{body}
<footer>Generado {datetime.now().strftime('%Y-%m-%d %H:%M')} · Listas de Wikipedia · Escuchas de Last.fm</footer>
{_mh_user_modal_js(users, on_select_js)}
{tail}
</body>
</html>
"""


def _card(href: str, name: str, meta: str, stats: dict, desc: str = "") -> str:
    avg = _avg(stats)
    stats_attr = escape(json.dumps(stats, ensure_ascii=False), quote=True)
    desc_html = f'<div class="col-desc">{escape(desc)}</div>' if desc else ""
    return f"""
      <a class="col-card" href="{escape(href)}" data-avg="{avg}" data-stats="{stats_attr}">
        <div class="col-name">{escape(name)}</div>{desc_html}
        <div class="col-meta">{escape(meta)}</div>
        <div class="col-bar-wrap"><div class="col-bar" style="width:{avg}%"></div></div>
        <div class="col-pct">{avg:.1f}% media del grupo</div>
      </a>"""


# ── HTML: INDEXES ──────────────────────────────────────────────────────────────

def render_root_index(groups: list[dict], users: list[str]) -> str:
    cards = "".join(_card(f"{g['group']}/index.html", g["group_name"],
                          f"{g['n_lists']} listas · {g['n_tracks']} canciones", g["stats"], g["description"])
                    for g in groups)
    header = """  <div class="site-label"><a href="../must_hear/index.html">← Must Hear</a></div>
  <h1>Must Hear · Singles</h1>
  <div class="header-meta">""" + f"{len(groups)} colecciones" + """</div>
  <a class="header-nav-link" href="../must_hear/index.html">Álbumes</a>"""
    body = f"""<main>
  <p class="intro">Las listas de canciones más importantes de la historia, sacadas de Wikipedia y
  cruzadas con lo que ha escuchado cada uno en Last.fm.</p>
  <div class="collections-grid">{cards}
  </div>
</main>"""
    return _page("Must Hear — Singles", CARD_CSS, header, body, users, "mhPaintCards();", CARD_JS)


def render_group_index(group: dict, lists: list[dict], users: list[str]) -> str:
    cards = "".join(_card(f"{l['slug']}.html", l["name"], f"{l['total_tracks']} canciones", l["stats"])
                    for l in lists)
    header = f"""  <div class="site-label"><a href="../index.html">← Singles</a></div>
  <h1>{escape(group['group_name'])}</h1>
  <div class="header-meta">{len(lists)} listas</div>"""
    body = f"""<main>
  <p class="intro">{escape(group['description'])}</p>
  <div class="collections-grid">{cards}
  </div>
</main>"""
    return _page(f"Singles — {group['group_name']}", CARD_CSS, header, body, users,
                 "mhPaintCards();", CARD_JS)


# ── HTML: LIST PAGE ────────────────────────────────────────────────────────────

LIST_CSS = """
  :root { --heard:#e8ff47; --pending:#ff4747; --panel:390px; --header-h:88px; --gap:6px; }
  header { right:var(--panel); height:var(--header-h); flex-direction:column; align-items:stretch;
    gap:0; padding:0; border-right:1px solid var(--border); }
  .hdr-r1, .hdr-r2 { display:flex; align-items:center; gap:12px; padding:0 16px; height:44px; min-width:0; }
  .hdr-r1 { padding-right:120px; }
  .hdr-r2 { border-top:1px solid var(--border); }
  header .mh-usr-btn { position:absolute; top:10px; right:16px; }
  .progress { width:170px; flex-shrink:0; }
  .progress-txt { font-family:'DM Mono',monospace; font-size:.6rem; color:var(--muted); margin-bottom:4px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .progress-txt b { color:var(--heard); font-weight:500; }
  .progress-bar { height:3px; background:var(--border); border-radius:2px; overflow:hidden; }
  .progress-bar div { height:100%; background:var(--heard); transition:width .4s; }
  .controls { display:flex; align-items:center; gap:8px; margin-left:auto; min-width:0; }
  .seg { display:flex; border:1px solid var(--border); border-radius:3px; overflow:hidden; flex-shrink:0; }
  .seg button { background:none; border:none; color:var(--muted); font-family:'DM Mono',monospace;
    font-size:.6rem; letter-spacing:.08em; padding:5px 10px; cursor:pointer; text-transform:uppercase; }
  .seg button + button { border-left:1px solid var(--border); }
  .seg button.on { background:rgba(232,255,71,.06); color:var(--heard); }
  .ctl { background:var(--surface); border:1px solid var(--border); color:var(--text); border-radius:3px;
    font-family:'DM Mono',monospace; font-size:.64rem; padding:5px 8px; flex-shrink:0; }
  input.ctl { width:160px; }
  .ctl:focus { outline:none; border-color:var(--heard); }
  .grid-sizer { display:flex; align-items:center; gap:6px; flex-shrink:0; }
  .grid-sizer span { font-family:'DM Mono',monospace; font-size:.6rem; color:var(--muted); min-width:24px; text-align:right; }
  #gridSlider { -webkit-appearance:none; appearance:none; width:70px; height:3px; background:var(--border);
    border-radius:2px; outline:none; cursor:pointer; }
  #gridSlider::-webkit-slider-thumb { -webkit-appearance:none; width:11px; height:11px; border-radius:50%; background:var(--heard); }
  #gridSlider::-moz-range-thumb { width:11px; height:11px; border-radius:50%; background:var(--heard); border:none; }

  main { margin-right:var(--panel); padding:16px 20px 60px; }
  #grid { display:grid; grid-template-columns:repeat(var(--cols,8),1fr); gap:var(--gap); }
  .year-sep { grid-column:1/-1; font-family:'Bebas Neue',sans-serif; font-size:1.4rem; letter-spacing:.06em;
    color:var(--accent); padding:14px 2px 2px; border-bottom:1px solid var(--border); margin-bottom:4px; }
  .year-sep:first-child { padding-top:0; }
  .card { position:relative; aspect-ratio:1; border-radius:3px; overflow:hidden; cursor:pointer;
    background:var(--surface); transition:transform .15s; content-visibility:auto; contain-intrinsic-size:150px; }
  .card:hover { transform:scale(1.05); z-index:10; }
  .card.active { outline:2px solid var(--heard); outline-offset:2px; z-index:11; }
  .card img { width:100%; height:100%; object-fit:cover; display:block; opacity:0; transition:opacity .25s; }
  .card img.ok { opacity:1; }
  .card.dim img.ok { opacity:.35; }
  .card::before { content:''; position:absolute; top:5px; right:5px; width:7px; height:7px; border-radius:50%; z-index:3; }
  .card.heard::before   { background:var(--heard);   box-shadow:0 0 5px var(--heard); }
  .card.pending::before { background:var(--pending); box-shadow:0 0 5px var(--pending); }
  .card-ph { position:absolute; inset:0; display:flex; flex-direction:column; justify-content:center;
    padding:10px; background:repeating-linear-gradient(135deg,#121212 0 8px,#151515 8px 16px); }
  .card-ph b { font-family:'Bebas Neue',sans-serif; font-size:1rem; letter-spacing:.03em; line-height:1.05;
    color:#bbb; overflow:hidden; display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; }
  .card-ph i { font-style:normal; font-size:.6rem; color:var(--muted); margin-top:4px;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .card.has-img .card-ph { display:none; }
  .card-overlay { position:absolute; inset:0; z-index:2; display:flex; flex-direction:column; justify-content:flex-end;
    padding:7px; background:linear-gradient(0deg,rgba(0,0,0,.92) 0%,rgba(0,0,0,0) 60%); opacity:0; transition:opacity .18s; }
  .card:hover .card-overlay { opacity:1; }
  .card-num { font-family:'Bebas Neue',sans-serif; font-size:.85rem; color:var(--heard); line-height:1; }
  .card-title { font-size:.62rem; font-weight:500; color:#fff; line-height:1.3; overflow:hidden;
    display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }
  .card-artist { font-size:.56rem; color:#999; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .card-rank { position:absolute; top:4px; left:5px; z-index:3; font-family:'Bebas Neue',sans-serif; font-size:.8rem;
    color:#fff; text-shadow:0 0 4px #000, 0 0 2px #000; }
  .empty { grid-column:1/-1; padding:40px 4px; color:var(--muted); font-family:'DM Mono',monospace; font-size:.75rem; }

  #panel { position:fixed; top:0; right:0; bottom:0; width:var(--panel); background:#0c0c0c;
    border-left:1px solid var(--border); z-index:200; display:flex; flex-direction:column; overflow:hidden; }
  .panel-topbar { height:var(--header-h); border-bottom:1px solid var(--border); display:flex; align-items:center;
    padding:0 16px; flex-shrink:0; font-family:'DM Mono',monospace; font-size:.6rem; letter-spacing:.18em;
    text-transform:uppercase; color:var(--muted); }
  .panel-cover { position:relative; aspect-ratio:1; max-height:300px; flex-shrink:0; background:var(--surface); overflow:hidden; }
  .panel-cover img { width:100%; height:100%; object-fit:cover; display:block; }
  .panel-cover .card-ph b { font-size:2rem; -webkit-line-clamp:4; }
  .panel-cover .card-ph i { font-size:.8rem; }
  .panel-status { position:absolute; bottom:8px; left:8px; font-family:'DM Mono',monospace; font-size:.6rem;
    letter-spacing:.12em; text-transform:uppercase; padding:2px 7px; border-radius:2px; z-index:3; }
  .panel-status.heard { background:var(--heard); color:#000; }
  .panel-status.pending { background:var(--pending); color:#fff; }
  .panel-body { flex:1; overflow-y:auto; padding:16px; scrollbar-width:thin; scrollbar-color:var(--border) transparent; }
  .panel-empty { height:100%; display:flex; align-items:center; justify-content:center; text-align:center;
    font-family:'DM Mono',monospace; font-size:.75rem; color:var(--muted); }
  .p-num { font-family:'DM Mono',monospace; font-size:.62rem; color:var(--heard); letter-spacing:.15em; margin-bottom:4px; }
  .p-title { font-family:'Bebas Neue',sans-serif; font-size:1.6rem; letter-spacing:.04em; line-height:1.05; margin-bottom:3px; }
  .p-artist { font-size:.85rem; color:#999; margin-bottom:2px; }
  .p-meta { font-family:'DM Mono',monospace; font-size:.66rem; color:var(--muted); margin-bottom:12px; line-height:1.6; }
  .badge { display:inline-block; padding:1px 6px; border-radius:3px; border:1px solid var(--border); }
  .badge.win { border-color:var(--accent); color:var(--accent); }
  .p-label { font-family:'DM Mono',monospace; font-size:.58rem; letter-spacing:.18em; text-transform:uppercase;
    color:var(--muted); margin:14px 0 6px; }
  .p-users { display:flex; flex-wrap:wrap; gap:5px; }
  .p-user { font-family:'DM Mono',monospace; font-size:.6rem; padding:2px 8px; border-radius:2px;
    border:1px solid var(--border); color:var(--muted); }
  .p-user.on { background:rgba(232,255,71,.08); border-color:rgba(232,255,71,.25); color:rgba(232,255,71,.85); }
  .p-user.me { outline:1px solid var(--text); outline-offset:1px; }
  .p-links { display:flex; gap:7px; flex-wrap:wrap; margin-top:14px; }
  .p-links a { font-family:'DM Mono',monospace; font-size:.62rem; letter-spacing:.08em; text-transform:uppercase;
    padding:4px 10px; border-radius:3px; border:1px solid var(--border); color:var(--muted); text-decoration:none; }
  .p-links a:hover { color:var(--heard); border-color:var(--heard); }
  #pAudio { width:100%; height:32px; margin-top:12px; display:none; }
  .p-links button { font-family:'DM Mono',monospace; font-size:.62rem; letter-spacing:.08em; text-transform:uppercase;
    padding:4px 10px; border-radius:3px; cursor:pointer; background:rgba(255,0,0,.08);
    border:1px solid rgba(255,70,70,.45); color:#ff6b6b; }
  .p-links button:hover { background:rgba(255,0,0,.16); }
  .p-yt { margin-top:14px; border-radius:4px; overflow:hidden; background:var(--surface);
    border:1px solid var(--border); aspect-ratio:16/9; display:none; }
  .p-yt.on { display:block; }
  .p-yt iframe { display:block; width:100%; height:100%; border:0; }
  .panel-close { display:none; position:absolute; top:12px; right:12px; width:34px; height:34px; border-radius:50%;
    background:rgba(255,255,255,.07); border:1px solid var(--border); color:var(--text); font-size:1rem;
    cursor:pointer; align-items:center; justify-content:center; z-index:10; }
  footer { margin-right:var(--panel); }

  @media (max-width:800px) {
    header { right:0; height:auto; }
    .hdr-r1 { padding-right:110px; }
    .hdr-r2 { height:auto; flex-wrap:wrap; padding:8px 12px; gap:8px; }
    .progress { width:100%; }
    .controls { margin-left:0; width:100%; overflow-x:auto; scrollbar-width:none; }
    input.ctl { width:120px; }
    main { margin-right:0; padding:12px 10px 60px; }
    footer { margin-right:0; }
    #panel { inset:0; width:100%; border-left:none; transform:translateY(105%);
      transition:transform .28s cubic-bezier(.4,0,.2,1); }
    #panel.open { transform:translateY(0); }
    .panel-topbar { height:52px; }
    .panel-close { display:flex; }
  }
"""

LIST_JS = r"""
<script>
(function() {
  const D = DATA, USERS = D.users, NU = USERS.length;
  const $ = id => document.getElementById(id);
  const mobile = () => matchMedia('(max-width:800px)').matches;
  let filter = 'all', sort = 'rank', q = '', cur = null;
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const fold = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  D.tracks.forEach((t, i) => { t.i = i; t.nh = Object.keys(t.h).length; t.q = fold(t.t + ' ' + t.a); });

  function user() { try { return localStorage.getItem('mh_user'); } catch (e) { return null; } }

  // Carátulas: solo se pide la imagen cuando la tarjeta se acerca al viewport,
  // y el grid se pinta por lotes para que listas de 1000+ no bloqueen el primer render.
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return;
    const img = e.target; io.unobserve(img);
    img.onload = () => { img.classList.add('ok'); img.parentElement.classList.add('has-img'); };
    img.src = img.dataset.src;
  }), { rootMargin: '400px' });

  function card(t, u) {
    const mine = u ? !!t.h[u] : t.nh > 0;
    return `<div class="card ${mine ? 'heard' : 'pending'}${u && !mine ? ' dim' : ''}" data-i="${t.i}">
      <div class="card-ph"><b>${esc(t.t)}</b><i>${esc(t.a)}</i></div>
      ${t.c ? `<img data-src="${esc(t.c)}" alt="" decoding="async">` : ''}
      ${t.r != null ? `<div class="card-rank">${t.r}</div>` : ''}
      <div class="card-overlay"><div class="card-num">${t.r != null ? '#' + t.r : ''}${t.y && D.showYear ? ' · ' + t.y : ''}</div>
        <div class="card-title">${esc(t.t)}</div><div class="card-artist">${esc(t.a)}</div></div>
    </div>`;
  }

  let renderToken = 0;
  function render() {
    const u = user();
    const heard = t => u ? !!t.h[u] : t.nh > 0;
    let list = D.tracks.filter(t => (filter === 'all' || (filter === 'heard') === heard(t)) && (!q || t.q.includes(q)));
    if (sort === 'pop') list = [...list].sort((a, b) => b.nh - a.nh || a.i - b.i);
    if (sort === 'year') list = [...list].sort((a, b) => (a.y || 0) - (b.y || 0) || a.i - b.i);

    const total = D.tracks.length;
    let pct, txt;
    if (u) {
      const n = D.tracks.filter(t => t.h[u]).length;
      pct = 100 * n / total;
      txt = `<b>${n}</b>/${total} · ${pct.toFixed(1)}% ${esc(u)}`;
    } else {
      const avg = USERS.reduce((s, x) => s + D.tracks.filter(t => t.h[x]).length, 0) / (NU || 1);
      pct = 100 * avg / total;
      txt = `media <b>${pct.toFixed(1)}%</b> · ${D.tracks.filter(t => t.nh).length}/${total} alguien`;
    }
    $('progTxt').innerHTML = txt;
    $('progBar').style.width = pct + '%';
    $('fHeard').textContent = u ? 'Escuchadas' : 'Alguien';
    $('fPending').textContent = u ? 'Pendientes' : 'Nadie';

    const parts = [];
    let lastY;
    const sepByYear = D.yearSeparators && (sort === 'rank' || sort === 'year');
    for (const t of list) {
      if (sepByYear && t.y !== lastY) { parts.push(`<div class="year-sep">${t.y ?? '—'}</div>`); lastY = t.y; }
      parts.push(card(t, u));
    }
    const grid = $('grid'), token = ++renderToken;
    io.disconnect();
    grid.innerHTML = parts.length ? '' : '<div class="empty">Nada por aquí.</div>';
    const CHUNK = 240;
    (function chunk(i) {
      if (token !== renderToken) return;
      grid.insertAdjacentHTML('beforeend', parts.slice(i, i + CHUNK).join(''));
      grid.querySelectorAll('img[data-src]:not([src])').forEach(img => io.observe(img));
      if (i + CHUNK < parts.length) requestAnimationFrame(() => chunk(i + CHUNK));
      else if (cur) grid.querySelector(`.card[data-i="${cur.i}"]`)?.classList.add('active');
    })(0);
  }

  function openPanel(t) {
    cur = t;
    const u = user(), mine = u ? !!t.h[u] : t.nh > 0;
    document.querySelectorAll('.card.active').forEach(c => c.classList.remove('active'));
    document.querySelector(`.card[data-i="${t.i}"]`)?.classList.add('active');
    const big = t.c ? t.c.replace('/250x250-', '/500x500-') : '';
    $('pCover').innerHTML = `<div class="card-ph"><b>${esc(t.t)}</b><i>${esc(t.a)}</i></div>`
      + (big ? `<img src="${esc(big)}" alt="" onload="this.previousElementSibling.style.display='none'">` : '')
      + `<span class="panel-status ${mine ? 'heard' : 'pending'}">${u ? (mine ? 'Escuchada · ' + t.h[u] + ' plays' : 'Pendiente') : t.nh + '/' + NU + ' usuarios'}</span>`;
    const meta = [];
    if (t.y) meta.push(t.y);
    if (t.n === 'winner') meta.push('<span class="badge win">🏆 Ganadora</span>');
    else if (t.n === 'nominee') meta.push('<span class="badge">Nominada</span>');
    else if (t.n) meta.push(esc(t.n));
    if (t.w) meta.push(t.w + (t.w === 1 ? ' semana' : ' semanas') + ' nº1');
    const users = USERS.map(x => `<span class="p-user${t.h[x] ? ' on' : ''}${x === u ? ' me' : ''}">${esc(x)}${t.h[x] ? ' · ' + t.h[x] : ''}</span>`).join('');
    const qs = encodeURIComponent(t.a + ' ' + t.t);
    const lfm = 'https://www.last.fm/music/' + encodeURIComponent(t.a.split(/ (?:featuring|feat\.|with|con) /i)[0]) + '/_/' + encodeURIComponent(t.t.split(' / ')[0]);
    $('pBody').innerHTML = `
      ${t.r != null ? `<div class="p-num">#${t.r}</div>` : ''}
      <div class="p-title">${esc(t.t)}</div>
      <div class="p-artist">${esc(t.a)}</div>
      <div class="p-meta">${meta.join(' · ')}</div>
      <audio id="pAudio" controls preload="none"></audio>
      <div class="p-label">Escuchada por ${t.nh}/${NU}</div>
      <div class="p-users">${users}</div>
      <div class="p-links">
        ${t.yt ? `<button id="pYtBtn" type="button">▶ YouTube</button>`
               : `<a href="https://www.youtube.com/results?search_query=${qs}" target="_blank" rel="noopener">Buscar en YouTube</a>`}
        <a href="${lfm}" target="_blank" rel="noopener">Last.fm</a>
        ${t.dz ? `<a href="https://www.deezer.com/track/${t.dz}" target="_blank" rel="noopener">Deezer</a>` : ''}
      </div>
      <div class="p-yt" id="pYt"></div>`;
    if (t.yt) $('pYtBtn').onclick = () => {
      $('pAudio').pause();
      $('pYt').innerHTML = `<iframe src="https://www.youtube.com/embed/${t.yt}?autoplay=1&rel=0" allow="autoplay; encrypted-media; picture-in-picture" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen title="${esc(t.t)}"></iframe>`;
      $('pYt').classList.add('on');
      $('pYtBtn').remove();
      $('pYt').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    };
    if (t.dz) preview(t);
    $('panel').classList.add('open');
  }

  // Preview de 30 s: las URLs de Deezer caducan, así que se piden al abrir (JSONP, sin CORS).
  function preview(t) {
    const cb = 'dz' + t.dz, s = document.createElement('script');
    window[cb] = d => {
      delete window[cb]; s.remove();
      const a = $('pAudio');
      if (cur === t && a && d && d.preview) { a.src = d.preview; a.style.display = 'block'; }
    };
    s.src = `https://api.deezer.com/track/${t.dz}?output=jsonp&callback=${cb}`;
    s.onerror = () => { delete window[cb]; s.remove(); };
    document.body.appendChild(s);
  }

  $('grid').onclick = e => { const c = e.target.closest('.card'); if (c) openPanel(D.tracks[+c.dataset.i]); };
  $('pClose').onclick = () => $('panel').classList.remove('open');
  document.querySelectorAll('#filters button').forEach(b => b.onclick = () => {
    filter = b.dataset.f;
    document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('on', x === b));
    render();
  });
  $('sort').onchange = e => { sort = e.target.value; render(); };
  let qt;
  $('search').oninput = e => { clearTimeout(qt); qt = setTimeout(() => { q = fold(e.target.value.trim()); render(); }, 150); };
  function setCols(v) {
    $('grid').style.setProperty('--cols', v);
    $('gridLbl').textContent = v + '×';
    try { localStorage.setItem('sg_cols', v); } catch (e) {}
  }
  $('gridSlider').oninput = e => setCols(e.target.value);
  let cols = mobile() ? 3 : 8;
  try { cols = +localStorage.getItem('sg_cols') || cols; } catch (e) {}
  $('gridSlider').value = cols; setCols(cols);
  const fitHeader = () => document.querySelector('main').style.marginTop = document.querySelector('header').offsetHeight + 'px';
  addEventListener('resize', fitHeader); fitHeader();

  window.mhRenderList = () => { render(); if (cur) openPanel(cur); };
  render();
  if (!mobile() && D.tracks.length) openPanel(D.tracks[0]);
})();
</script>"""


def render_list_page(group: dict, coll: dict, tracks: list[dict], heard: dict, users: list[str],
                     covers: dict[int, dict], videos: dict[int, str]) -> str:
    multi_year = len({t["year"] for t in tracks if t["year"]}) > 1
    data = {
        "users": users,
        "showYear": multi_year,
        "yearSeparators": multi_year,
        "tracks": [{
            "r": t["rank"], "t": t["title"], "a": t["artist"], "y": t["year"],
            "w": t["weeks"], "n": t["note"] or "",
            "h": heard.get(t["track_id"], {}),
            **covers.get(t["track_id"], {}),
            **({"yt": videos[t["track_id"]]} if t["track_id"] in videos else {}),
        } for t in tracks],
    }
    data_js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    header = f"""  <div class="hdr-r1">
    <div class="site-label"><a href="index.html">← {escape(group['group_name'])}</a></div>
    <h1>{escape(coll['name'])}</h1>
    <div class="header-meta">{len(tracks)} canciones</div>
    <a class="header-nav-link" href="{escape(coll['source_url'])}" target="_blank" rel="noopener">Wikipedia ↗</a>
  </div>
  <div class="hdr-r2">
    <div class="progress"><div class="progress-txt" id="progTxt"></div><div class="progress-bar"><div id="progBar"></div></div></div>
    <div class="controls">
      <div class="seg" id="filters">
        <button data-f="all" class="on">Todas</button><button data-f="heard" id="fHeard">Escuchadas</button><button data-f="pending" id="fPending">Pendientes</button>
      </div>
      <select class="ctl" id="sort" aria-label="Ordenar">
        <option value="rank">Orden de la lista</option>
        <option value="pop">Más escuchadas</option>
        <option value="year">Año</option>
      </select>
      <input class="ctl" id="search" type="search" placeholder="Buscar…">
      <div class="grid-sizer"><input type="range" id="gridSlider" min="3" max="16" aria-label="Columnas"><span id="gridLbl"></span></div>
    </div>
  </div>"""
    body = """<main><div id="grid"></div></main>
<aside id="panel">
  <div class="panel-topbar">Canción</div>
  <button class="panel-close" id="pClose" aria-label="Cerrar">✕</button>
  <div class="panel-cover" id="pCover"></div>
  <div class="panel-body" id="pBody"><div class="panel-empty">Pulsa una carátula</div></div>
</aside>"""
    tail = f"<script>const DATA = {data_js};</script>{LIST_JS}"
    return _page(f"Singles — {coll['name']}", LIST_CSS, header, body, users, "mhRenderList();", tail)


# ── MAIN ───────────────────────────────────────────────────────────────────────

def generate_html(conn, out_dir: Path, users: list[str], source_keys: list[str]) -> None:
    heard = sdb.load_heard(conn)
    covers = load_covers(conn)
    videos = load_youtube(conn)
    colls = sdb.load_collections(conn)
    by_group: dict[str, list[dict]] = {}
    for c in colls:
        by_group.setdefault(c["group_slug"], []).append(c)

    groups_summary = []
    for key, src in SOURCES.items():
        gcolls = by_group.get(src["group"], [])
        if not gcolls:
            continue
        gdir = out_dir / src["group"]
        gdir.mkdir(parents=True, exist_ok=True)
        all_ids: set[int] = set()
        for c in gcolls:
            tracks = sdb.load_collection_tracks(conn, c["id"])
            ids = [t["track_id"] for t in tracks]
            all_ids.update(ids)
            c["stats"] = _pct_by_user(ids, heard, users)
            c["total_tracks"] = len(tracks)
            if key in source_keys:
                (gdir / f"{c['slug']}.html").write_text(
                    render_list_page(src, c, tracks, heard, users, covers, videos), encoding="utf-8")
        (gdir / "index.html").write_text(render_group_index(src, gcolls, users), encoding="utf-8")
        groups_summary.append({**src, "n_lists": len(gcolls), "n_tracks": len(all_ids),
                               "stats": _pct_by_user(list(all_ids), heard, users)})
        print(f"  📄 {src['group_name']}: {len(gcolls)} listas")

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "index.html").write_text(render_root_index(groups_summary, users), encoding="utf-8")
    print(f"  📄 {out_dir / 'index.html'}")
    update_must_hear_index(groups_summary, users)


def update_must_hear_index(groups: list[dict], users: list[str]) -> None:
    """Upsert each singles group into the Must Hear root index (docs/must_hear/index.html)."""
    meta_file = MUST_HEAR_ROOT / ".collections_meta.json"
    if not meta_file.exists():
        return
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    slugs = {f"singles_{g['group']}" for g in groups}
    collections = [c for c in json.loads(meta_file.read_text()) if c["slug"] not in slugs]
    for g in groups:
        collections.append({
            "slug":    f"singles_{g['group']}",
            "name":    f"Singles · {g['group_name']}",
            "users":   len(users),
            "total":   g["n_tracks"],
            "unit":    "canciones",
            "avg_pct": _avg(g["stats"]),
            "updated": generated,
            "url":     f"../singles/{g['group']}/index.html",
        })
    collections.sort(key=lambda c: c["name"])
    meta_file.write_text(json.dumps(collections, ensure_ascii=False, indent=2))
    (MUST_HEAR_ROOT / "index.html").write_text(
        render_root_index_html(collections, generated, users=users), encoding="utf-8")
    print(f"  📋 must_hear index → {MUST_HEAR_ROOT / 'index.html'} ({len(collections)} collections)")


def main():
    ap = argparse.ArgumentParser(description="Must Hear Singles — listas de canciones desde Wikipedia")
    ap.add_argument("--db", default=DEFAULT_SCROBBLES_DB, help="DB normalizada de scrobbles")
    ap.add_argument("--singles-db", default=DEFAULT_SINGLES_DB, help="DB propia de singles")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Directorio de salida")
    ap.add_argument("--sources", nargs="*", choices=list(SOURCES), help="Fuentes a procesar (por defecto todas)")
    ap.add_argument("--users", nargs="*", help="Usuarios (por defecto todos los de la DB de scrobbles)")
    ap.add_argument("--force-scrape", action="store_true", help="Re-descargar todas las listas")
    ap.add_argument("--no-scrape", action="store_true", help="No tocar Wikipedia; solo escuchas + HTML")
    ap.add_argument("--no-covers", action="store_true", help="No buscar carátulas nuevas en Deezer")
    ap.add_argument("--retry-covers", action="store_true", help="Reintentar las canciones sin carátula")
    ap.add_argument("--no-youtube", action="store_true", help="No buscar vídeos nuevos con yt-dlp")
    ap.add_argument("--retry-youtube", action="store_true", help="Reintentar las canciones sin vídeo")
    ap.add_argument("--list-sources", action="store_true", help="Mostrar las fuentes disponibles")
    args = ap.parse_args()

    if args.list_sources:
        for k, s in SOURCES.items():
            print(f"  {k:24s} {s['group_name']} — {s['description']}")
        return

    source_keys = args.sources or list(SOURCES)
    conn = sdb.open_db(args.singles_db)
    if not args.no_scrape:
        scrape_sources(conn, source_keys, args.force_scrape)
    if not args.no_covers:
        print("\n🖼  Carátulas (Deezer)")
        fetch_covers(conn, retry_missing=args.retry_covers)
    if not args.no_youtube:
        print("\n🎬 Vídeos (YouTube)")
        fetch_youtube(conn, retry_missing=args.retry_youtube)

    scr = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    all_users = sdb.scrobble_users(scr)
    users = [u for u in all_users if u in args.users] if args.users else all_users
    print(f"\n🎧 Cruzando con scrobbles de {len(users)} usuarios")
    sdb.refresh_user_heard(conn, scr, users)
    scr.close()

    print("\n🖨  Generando HTML")
    generate_html(conn, Path(args.out), all_users, source_keys)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
