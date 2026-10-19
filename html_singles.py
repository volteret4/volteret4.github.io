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
  python3 html_singles.py --list-sources
"""

import argparse, json, sqlite3, sys, time
from datetime import datetime
from html import escape
from pathlib import Path

from tools.singles.wikipedia_charts import SOURCES, THIS_YEAR, iter_source_lists, scrape_list, wiki_url
from tools.singles import singles_database as sdb
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
  .toolbar { display:flex; flex-wrap:wrap; gap:10px 16px; align-items:center; margin-bottom:18px; }
  .progress { flex:1 1 260px; min-width:0; }
  .progress-txt { font-family:'DM Mono',monospace; font-size:.7rem; color:var(--muted); margin-bottom:6px; }
  .progress-txt b { color:var(--text); font-weight:500; }
  .progress-bar { height:4px; background:var(--border); border-radius:2px; overflow:hidden; }
  .progress-bar div { height:100%; background:var(--accent); transition:width .4s; }
  .seg { display:flex; border:1px solid var(--border); border-radius:4px; overflow:hidden; }
  .seg button { background:none; border:none; color:var(--muted); font-family:'DM Mono',monospace;
    font-size:.62rem; letter-spacing:.06em; padding:6px 10px; cursor:pointer; text-transform:uppercase; }
  .seg button + button { border-left:1px solid var(--border); }
  .seg button.on { background:var(--surface2); color:var(--accent); }
  .ctl { background:var(--surface); border:1px solid var(--border); color:var(--text); border-radius:4px;
    font-family:'DM Mono',monospace; font-size:.68rem; padding:6px 9px; }
  input.ctl { width:200px; max-width:100%; }
  .ctl:focus { outline:none; border-color:var(--accent); }
  .tracks { border-top:1px solid var(--border); }
  .trk { display:grid; grid-template-columns:52px minmax(0,1fr) 110px 150px 64px 60px;
    align-items:center; gap:12px; padding:9px 10px; border-bottom:1px solid var(--border);
    border-left:2px solid transparent; }
  .trk:hover { background:var(--surface); }
  .trk.mine { border-left-color:var(--ok); }
  .trk.dim .t-main { opacity:.55; }
  .t-rank { font-family:'DM Mono',monospace; font-size:.75rem; color:var(--muted); text-align:right; }
  .t-title { font-size:.95rem; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .t-artist { font-size:.8rem; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .t-meta { font-family:'DM Mono',monospace; font-size:.62rem; color:var(--muted); line-height:1.5; }
  .badge { display:inline-block; padding:1px 6px; border-radius:3px; border:1px solid var(--border); }
  .badge.win { border-color:var(--accent); color:var(--accent); }
  .dots { display:flex; flex-wrap:wrap; gap:3px; }
  .dot { width:8px; height:8px; border-radius:50%; background:var(--border); }
  .dot.on { background:var(--accent); }
  .dot.me { outline:1px solid var(--text); outline-offset:1px; }
  .t-plays { font-family:'DM Mono',monospace; font-size:.7rem; text-align:right; color:var(--ok); }
  .t-links { display:flex; gap:8px; justify-content:flex-end; }
  .t-links a { font-family:'DM Mono',monospace; font-size:.6rem; color:var(--muted); text-decoration:none;
    border:1px solid var(--border); border-radius:3px; padding:2px 5px; }
  .t-links a:hover { color:var(--accent); border-color:var(--accent); }
  .year-sep { font-family:'Bebas Neue',sans-serif; font-size:1.3rem; letter-spacing:.06em;
    color:var(--accent); padding:16px 10px 6px; border-bottom:1px solid var(--border); }
  .empty { padding:40px 10px; color:var(--muted); font-size:.9rem; }
  @media (max-width:800px) {
    .trk { grid-template-columns:28px minmax(0,1fr) auto auto; gap:8px; padding:9px 4px; }
    .t-meta, .dots { display:none; }
    input.ctl { width:100%; }
  }
"""

LIST_JS = r"""
<script>
(function() {
  const D = DATA, USERS = D.users, NU = USERS.length;
  const $ = id => document.getElementById(id);
  let filter = 'all', sort = 'rank', q = '';
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const fold = s => s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  D.tracks.forEach(t => { t.nh = Object.keys(t.h).length; t.q = fold(t.t + ' ' + t.a); });

  function user() { try { return localStorage.getItem('mh_user'); } catch (e) { return null; } }

  function meta(t) {
    const out = [];
    if (t.n === 'winner') out.push('<span class="badge win">🏆 Ganadora</span>');
    else if (t.n === 'nominee') out.push('<span class="badge">Nominada</span>');
    else if (t.n) out.push(esc(t.n));
    if (t.w) out.push(t.w + (t.w === 1 ? ' semana' : ' semanas') + ' nº1');
    if (t.y && D.showYear && sort === 'pop') out.push(t.y);   // con separadores el año ya se ve
    return out.join('<br>');
  }

  function row(t, u) {
    const mine = u && t.h[u];
    const dots = USERS.map(x => `<span class="dot${t.h[x] ? ' on' : ''}${x === u ? ' me' : ''}" title="${esc(x)}${t.h[x] ? ': ' + t.h[x] + ' plays' : ''}"></span>`).join('');
    const qs = encodeURIComponent(t.a + ' ' + t.t);
    const lfm = 'https://www.last.fm/music/' + encodeURIComponent(t.a.split(/ (?:featuring|feat\.|with|con) /i)[0]) + '/_/' + encodeURIComponent(t.t.split(' / ')[0]);
    const plays = u ? (mine ? mine + '▶' : '') : (t.nh ? t.nh + '/' + NU : '');
    return `<div class="trk${mine ? ' mine' : ''}${u && !mine ? ' dim' : ''}">
      <div class="t-rank">${t.r ?? ''}</div>
      <div class="t-main"><div class="t-title">${esc(t.t)}</div><div class="t-artist">${esc(t.a)}</div></div>
      <div class="t-meta">${meta(t)}</div>
      <div class="dots" title="${t.nh}/${NU} usuarios">${dots}</div>
      <div class="t-plays" title="${u ? 'Tus reproducciones' : 'Usuarios que la han escuchado'}">${plays}</div>
      <div class="t-links"><a href="https://www.youtube.com/results?search_query=${qs}" target="_blank" rel="noopener" title="Buscar en YouTube">YT</a><a href="${lfm}" target="_blank" rel="noopener" title="Last.fm">LFM</a></div>
    </div>`;
  }

  function render() {
    const u = user();
    const heard = t => u ? !!t.h[u] : t.nh > 0;
    let list = D.tracks.filter(t => (filter === 'all' || (filter === 'heard') === heard(t)) && (!q || t.q.includes(q)));
    if (sort === 'pop') list = [...list].sort((a, b) => b.nh - a.nh || a.r - b.r);
    if (sort === 'year') list = [...list].sort((a, b) => (a.y || 0) - (b.y || 0) || a.r - b.r);

    const total = D.tracks.length;
    let pct, txt;
    if (u) {
      const n = D.tracks.filter(t => t.h[u]).length;
      pct = 100 * n / total;
      txt = `<b>${esc(u)}</b> ha escuchado <b>${n}</b> de ${total} · ${pct.toFixed(1)}%`;
    } else {
      const avg = USERS.reduce((s, x) => s + D.tracks.filter(t => t.h[x]).length, 0) / (NU || 1);
      const any = D.tracks.filter(t => t.nh).length;
      pct = 100 * avg / total;
      txt = `Media del grupo <b>${pct.toFixed(1)}%</b> · ${any} de ${total} las ha escuchado alguien`;
    }
    $('progTxt').innerHTML = txt;
    $('progBar').style.width = pct + '%';
    $('fHeard').textContent = u ? 'Escuchadas' : 'Alguien';
    $('fPending').textContent = u ? 'Pendientes' : 'Nadie';

    let html = '', lastY = null;
    const sepByYear = D.yearSeparators && (sort === 'rank' || sort === 'year');
    for (const t of list) {
      if (sepByYear && t.y !== lastY) { html += `<div class="year-sep">${t.y ?? '—'}</div>`; lastY = t.y; }
      html += row(t, u);
    }
    $('tracks').innerHTML = html || '<div class="empty">Nada por aquí.</div>';
  }

  document.querySelectorAll('#filters button').forEach(b => b.onclick = () => {
    filter = b.dataset.f;
    document.querySelectorAll('#filters button').forEach(x => x.classList.toggle('on', x === b));
    render();
  });
  $('sort').onchange = e => { sort = e.target.value; render(); };
  $('search').oninput = e => { q = fold(e.target.value.trim()); render(); };
  window.mhRenderList = render;
  render();
})();
</script>"""


def render_list_page(group: dict, coll: dict, tracks: list[dict], heard: dict, users: list[str]) -> str:
    multi_year = len({t["year"] for t in tracks if t["year"]}) > 1
    data = {
        "users": users,
        "showYear": multi_year,
        "yearSeparators": multi_year,
        "tracks": [{
            "r": t["rank"], "t": t["title"], "a": t["artist"], "y": t["year"],
            "w": t["weeks"], "n": t["note"] or "",
            "h": heard.get(t["track_id"], {}),
        } for t in tracks],
    }
    data_js = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    header = f"""  <div class="site-label"><a href="index.html">← {escape(group['group_name'])}</a></div>
  <h1>{escape(coll['name'])}</h1>
  <div class="header-meta">{len(tracks)} canciones</div>
  <a class="header-nav-link" href="{escape(coll['source_url'])}" target="_blank" rel="noopener">Wikipedia ↗</a>"""
    body = """<main>
  <div class="toolbar">
    <div class="progress"><div class="progress-txt" id="progTxt"></div><div class="progress-bar"><div id="progBar"></div></div></div>
    <div class="seg" id="filters">
      <button data-f="all" class="on">Todas</button><button data-f="heard" id="fHeard">Escuchadas</button><button data-f="pending" id="fPending">Pendientes</button>
    </div>
    <select class="ctl" id="sort" aria-label="Ordenar">
      <option value="rank">Orden de la lista</option>
      <option value="pop">Más escuchadas en el grupo</option>
      <option value="year">Año</option>
    </select>
    <input class="ctl" id="search" type="search" placeholder="Buscar canción o artista…">
  </div>
  <div class="tracks" id="tracks"></div>
</main>"""
    tail = f"<script>const DATA = {data_js};</script>{LIST_JS}"
    return _page(f"Singles — {coll['name']}", LIST_CSS, header, body, users, "mhRenderList();", tail)


# ── MAIN ───────────────────────────────────────────────────────────────────────

def generate_html(conn, out_dir: Path, users: list[str], source_keys: list[str]) -> None:
    heard = sdb.load_heard(conn)
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
                    render_list_page(src, c, tracks, heard, users), encoding="utf-8")
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
