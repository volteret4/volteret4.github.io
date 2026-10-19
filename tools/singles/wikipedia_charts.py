#!/usr/bin/env python3
"""
Wikipedia singles charts — scraper

Descarga listas de canciones (Billboard, UK, Los 40, Promusicae, Triple J,
Grammy, Eurovisión...) desde la API de Wikipedia y las convierte en una lista
de dicts {rank, title, artist, year, weeks, note}.

Cada fuente (SOURCES) define:
  - group / group_name : agrupación en docs/singles/<group>/
  - lang               : en | es
  - lists()            : genera (slug, nombre, título_wikipedia, año) candidatos
  - all_tables         : True si la lista está repartida en varias tablas (Grammy)
  - winners            : True si la 1ª fila de cada año es el ganador (Grammy)

Standalone (debug):  python3 -m tools.singles.wikipedia_charts "Billboard Year-End Hot 100 singles of 1985"
"""

import json, re, sys, time, urllib.parse, urllib.request, urllib.error
from bs4 import BeautifulSoup

UA = "MustHearSingles/1.0 (https://volteret4.github.io/; rym_lastfm)"
REQUEST_DELAY = 1.0          # Wikimedia pide no machacar la API
_last_request = 0.0

THIS_YEAR = time.localtime().tm_year


# ── HTTP ───────────────────────────────────────────────────────────────────────

def _api(lang: str, params: dict, retries: int = 5) -> dict:
    global _last_request
    params = {**params, "format": "json", "formatversion": 2}
    url = f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        wait = REQUEST_DELAY - (time.time() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                delay = int(e.headers.get("Retry-After") or 0) or 5 * (attempt + 1)
                print(f"    ⏳ Wikipedia {e.code}, reintentando en {delay}s…")
                time.sleep(delay)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    return {}


def resolve_titles(lang: str, titles: list[str]) -> dict[str, str | None]:
    """Devuelve {título_pedido: título_canónico | None si no existe} (sigue redirecciones)."""
    out: dict[str, str | None] = {}
    for i in range(0, len(titles), 50):
        chunk = titles[i:i + 50]
        d = _api(lang, {"action": "query", "titles": "|".join(chunk), "redirects": 1})
        q = d.get("query", {})
        alias = {}
        for key in ("normalized", "redirects"):
            for r in q.get(key, []):
                alias[r["from"]] = r["to"]
        existing = {p["title"] for p in q.get("pages", []) if not p.get("missing") and not p.get("invalid")}
        for t in chunk:
            cur = t
            for _ in range(4):
                if cur not in alias:
                    break
                cur = alias[cur]
            out[t] = cur if cur in existing else None
    return out


def fetch_page_html(lang: str, title: str) -> str:
    d = _api(lang, {"action": "parse", "page": title, "prop": "text",
                    "redirects": 1, "disablelimitreport": 1, "disableeditsection": 1})
    return (d.get("parse") or {}).get("text", "")


def fetch_template_links(lang: str, template: str) -> list[tuple[str, str]]:
    """[(título_enlazado, texto_visible)] de los wikilinks [[A|B]] de una plantilla navbox."""
    d = _api(lang, {"action": "parse", "page": template, "prop": "wikitext"})
    wt = (d.get("parse") or {}).get("wikitext", "")
    return re.findall(r"\[\[([^|\]]+)\|([^\]]+)\]\]", wt)


def wiki_url(lang: str, title: str) -> str:
    return f"https://{lang}.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ── TABLE PARSING ──────────────────────────────────────────────────────────────

TITLE_KEYS  = ("title", "song", "single", "record", "canción", "cancion", "sencillo", "tema")
ARTIST_KEYS = ("artist", "performer", "intérprete", "interprete", "cantante", "grupo")
RANK_KEYS   = ("no.", "#", "rank", "pos", "position", "puesto", "nº", "n.º")
YEAR_KEYS   = ("year", "año")
DATE_KEYS   = ("date", "reached", "week ending", "fecha", "issue")
WEEKS_KEYS  = ("weeks", "semanas")
COUNTRY_KEYS = ("country", "país", "pais", "nation")

_FOOTNOTE = re.compile(r"\[\s*[^\]]{0,12}\s*\]")


def _clean(text: str) -> str:
    text = _FOOTNOTE.sub("", text or "")
    text = text.replace("\u200b", "").replace("\xa0", " ")
    text = re.sub(r"[†‡§¶*♦◊※♪^]+", "", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\s+([,.)!?:;])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    return text.strip()


def _clean_title(text: str) -> str:
    t = _clean(text)
    # '" Goodnight Irene "' → 'Goodnight Irene' ; dobles caras '"A" / "B"' → 'A / B'
    t = re.sub(r'\s*["“”]\s*', '"', t)
    t = re.sub(r'"\s*/\s*"', " / ", t)
    t = t.strip().lstrip('"')
    # 'Call Me"♪ (1980)' → 'Call Me' (marcas tras la comilla de cierre)
    if '"' in t and " / " not in t:
        t = t.split('"', 1)[0]
    t = t.strip('"').strip()
    return re.sub(r"\s+", " ", t)


def _col_index(headers: list[str], keys: tuple, exclude: tuple = ()) -> int | None:
    for i, h in enumerate(headers):
        hl = h.lower()
        if any(x in hl for x in exclude):
            continue
        if any(hl == k or hl.startswith(k) or f" {k}" in f" {hl}" for k in keys):
            return i
    return None


def _expand(table) -> list[list[tuple]]:
    """Expande rowspan/colspan. Cada celda → (texto, es_th, es_origen, elemento)."""
    grid: list[list[tuple]] = []
    pending: dict[int, list] = {}          # col → [filas_restantes, celda]
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        row: list[tuple] = []
        cells = tr.find_all(["th", "td"], recursive=False)
        col = 0
        ci = 0
        while ci < len(cells) or col in pending:
            if col in pending:
                left, cell = pending[col]
                row.append((cell[0], cell[1], False, cell[3]))
                if left <= 1:
                    del pending[col]
                else:
                    pending[col][0] -= 1
                col += 1
                continue
            c = cells[ci]
            ci += 1
            for sup in c.find_all("sup", class_="reference"):
                sup.decompose()
            text = c.get_text(" ", strip=True)
            try:
                rs = int(re.sub(r"\D", "", c.get("rowspan", "1")) or 1)
                cs = int(re.sub(r"\D", "", c.get("colspan", "1")) or 1)
            except ValueError:
                rs, cs = 1, 1
            cell = (text, c.name == "th", True, c)
            for k in range(min(cs, 20)):
                row.append(cell if k == 0 else (text, cell[1], False, c))
                if rs > 1:
                    pending[col] = [rs - 1, cell]
                col += 1
        grid.append(row)
    return grid


def _is_header_row(row) -> bool:
    return len(row) >= 2 and sum(1 for c in row if c[1]) >= max(2, len(row) - 1)


def _header_info(grid) -> tuple[int, list[str]] | None:
    """Índice de la última fila de cabecera y nombres de columna (fusiona cabeceras de 2 filas)."""
    for i, row in enumerate(grid[:4]):
        if _is_header_row(row):
            headers = [_clean(c[0]) for c in row]
            while i + 1 < len(grid) and _is_header_row(grid[i + 1]) and \
                    not re.match(r"^\d+$", _clean(grid[i + 1][0][0])):
                i += 1
                headers = [_clean(c[0]) for c in grid[i]]
            return i, headers
    return None


def parse_tables(html: str, all_tables: bool = False, winners: bool = False,
                 default_year: int | None = None) -> list[dict]:
    """Extrae canciones de las tablas wikitable con columnas título + artista."""
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for table in soup.select("table.wikitable"):
        grid = _expand(table)
        hi = _header_info(grid)
        if not hi:
            continue
        h_idx, headers = hi
        c_title  = _col_index(headers, TITLE_KEYS, exclude=("label", "discográfica", "weeks", "semanas"))
        c_artist = _col_index(headers, ARTIST_KEYS, exclude=("songwriter", "compositor", "autor"))
        if c_title is None or c_artist is None:
            continue
        c_rank  = _col_index(headers, RANK_KEYS)
        c_year  = _col_index(headers, YEAR_KEYS)
        c_date  = _col_index(headers, DATE_KEYS)
        c_weeks = _col_index(headers, WEEKS_KEYS)
        c_ctry  = _col_index(headers, COUNTRY_KEYS)
        cur_year = default_year
        found = 0
        for row in grid[h_idx + 1:]:
            texts = [_clean(c[0]) for c in row]
            # Fila de sección: «1980» ocupando toda la fila
            if row and len({id(c[3]) for c in row}) == 1:
                m = re.match(r"^(\d{4})\b", texts[0])
                if m:
                    cur_year = int(m.group(1))
                continue
            if len(row) <= max(c_title, c_artist):
                continue
            if all(c[1] for c in row):          # cabecera repetida
                continue
            title  = _clean_title(row[c_title][0])
            artist = _clean(row[c_artist][0]).strip('"“” ')
            if not title or not artist or title.lower() in ("—", "-", "n/a", "no award"):
                continue
            year = cur_year
            for ci in (c_year, c_date):
                if ci is not None and ci < len(row):
                    m = re.search(r"\b(19\d{2}|20\d{2})\b", texts[ci])
                    if m:
                        year = int(m.group(1))
                        break
            rank = None
            if c_rank is not None and c_rank < len(row):
                m = re.match(r"^\s*(\d+)", texts[c_rank])
                rank = int(m.group(1)) if m else None
            weeks = None
            if c_weeks is not None and c_weeks < len(row):
                m = re.match(r"^\s*(\d+)", texts[c_weeks])
                weeks = int(m.group(1)) if m else None
            note = ""
            if winners and c_year is not None:
                note = "winner" if row[c_year][2] else "nominee"
            elif c_ctry is not None and c_ctry < len(row):
                note = texts[c_ctry]
            items.append({"rank": rank, "title": title, "artist": artist,
                          "year": year, "weeks": weeks, "note": note})
            found += 1
        if found and not all_tables:
            break
    return _dedupe(items)


def _dedupe(items: list[dict]) -> list[dict]:
    """Listas de nº1 semanales repiten canción: se queda la primera y suma apariciones."""
    seen: dict[tuple, dict] = {}
    out = []
    for it in items:
        key = (re.sub(r"\W", "", it["title"].lower()), re.sub(r"\W", "", it["artist"].lower()))
        if key in seen:
            prev = seen[key]
            prev["_count"] += 1
            if it["note"] == "winner":
                prev["note"] = "winner"
            continue
        it["_count"] = 1
        seen[key] = it
        out.append(it)
    ranked = any(it["rank"] for it in out)
    for i, it in enumerate(out, 1):
        if not ranked or it["rank"] is None:
            it["rank"] = i
        if it["weeks"] is None and it["_count"] > 1:
            it["weeks"] = it["_count"]
        del it["_count"]
    return out


# ── SOURCES ────────────────────────────────────────────────────────────────────

DECADES = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020]


def _billboard_yearend_lists():
    links = fetch_template_links("en", "Template:Hot 100 year-end charts")
    seen = set()
    for target, label in links:
        m = re.fullmatch(r"\s*(\d{4})\s*", label)
        if m and target not in seen:
            seen.add(target)
            y = int(m.group(1))
            yield f"billboard_yearend_{y}", f"Billboard Year-End {y}", target, y


SOURCES: dict[str, dict] = {
    "billboard_yearend": {
        "group": "billboard_year_end", "group_name": "Billboard Year-End Hot 100",
        "lang": "en",
        "description": "Las canciones más exitosas de cada año en EE. UU. según Billboard.",
        "lists": _billboard_yearend_lists,
    },
    "billboard_number_ones": {
        "decades": True,
        "group": "billboard_number_ones", "group_name": "Billboard Hot 100 · Números 1",
        "lang": "en",
        "description": "Todas las canciones que llegaron al nº1 del Billboard Hot 100, por décadas.",
        "lists": lambda: ((f"billboard_n1_{d}s", f"Billboard Hot 100 nº1 · {d}s",
                           f"List of Billboard Hot 100 number-one singles of the {d}s", d)
                          for d in DECADES),
    },
    "uk_number_ones": {
        "decades": True,
        "group": "uk_number_ones", "group_name": "UK Singles Chart · Números 1",
        "lang": "en",
        "description": "Los números 1 de la lista oficial de singles del Reino Unido, por décadas.",
        "lists": lambda: ((f"uk_n1_{d}s", f"UK nº1 · {d}s",
                           f"List of UK singles chart number ones of the {d}s", d)
                          for d in DECADES),
    },
    "los40": {
        "group": "los40_number_ones", "group_name": "Los 40 Principales · Números 1",
        "lang": "es",
        "description": "Los números 1 semanales de Los 40 Principales (España), por años.",
        "lists": lambda: ((f"los40_{y}", f"Los 40 nº1 · {y}",
                           [f"Anexo:Los números uno de Los 40 Principales (España) {y}",
                            f"Anexo:Los números uno de Los 40 (España) {y}",
                            f"Anexo:Los números uno de LOS40 (España) {y}"], y)
                          for y in range(1966, THIS_YEAR + 1)),
    },
    "promusicae": {
        "group": "promusicae_number_ones", "group_name": "Promusicae · Números 1",
        "lang": "es",
        "description": "Los sencillos número 1 de la lista oficial de ventas de España (Promusicae).",
        "lists": lambda: ((f"promusicae_{y}", f"Promusicae nº1 · {y}",
                           [f"Anexo:Sencillos número uno en España en {y}",
                            f"Anexo:Sencillos número uno en España de {y}",
                            f"Anexo:Sencillos número uno de Top 100 Canciones de {y}"], y)
                          for y in range(1990, THIS_YEAR + 1)),
    },
    "triplej": {
        "group": "triple_j_hottest_100", "group_name": "Triple J Hottest 100",
        "lang": "en",
        "description": "La votación anual de la radio australiana Triple J: las 100 canciones del año.",
        "lists": lambda: ((f"triplej_{y}", f"Triple J Hottest 100 · {y}",
                           f"Triple J's Hottest 100 of {y}", y)
                          for y in range(1993, THIS_YEAR + 1)),
    },
    "grammy": {
        "group": "grammy_awards", "group_name": "Grammy · Canciones",
        "lang": "en",
        "description": "Ganadores y nominados a Grabación del Año y Canción del Año.",
        "all_tables": True, "winners": True,
        "lists": lambda: iter([
            ("grammy_record_of_the_year", "Grammy · Record of the Year",
             "Grammy Award for Record of the Year", None),
            ("grammy_song_of_the_year", "Grammy · Song of the Year",
             "Grammy Award for Song of the Year", None),
        ]),
    },
    "eurovision": {
        "group": "eurovision", "group_name": "Eurovisión",
        "lang": "en",
        "description": "Todas las canciones ganadoras del Festival de Eurovisión.",
        "lists": lambda: iter([
            ("eurovision_winners", "Eurovisión · Ganadoras",
             "List of Eurovision Song Contest winners", None),
        ]),
    },
}


def iter_source_lists(source_key: str) -> list[dict]:
    """Lista candidatos de una fuente y descarta los que no existen en Wikipedia."""
    src = SOURCES[source_key]
    # Cada candidato puede traer varios títulos alternativos: vale el primero que exista
    cands = [(slug, name, [t] if isinstance(t, str) else t, year) for slug, name, t, year in src["lists"]()]
    resolved = resolve_titles(src["lang"], [t for c in cands for t in c[2]])
    out = []
    for slug, name, titles, year in cands:
        canon = next((resolved[t] for t in titles if resolved.get(t)), None)
        if canon:
            out.append({"slug": slug, "name": name, "wiki_title": canon,
                        "year": year, "lang": src["lang"], "decade": src.get("decades", False)})
    return out


def scrape_list(source_key: str, lst: dict) -> list[dict]:
    src = SOURCES[source_key]
    html = fetch_page_html(lst["lang"], lst["wiki_title"])
    # Para décadas (1980) no forzamos año: lo dan las filas de sección/fecha
    default_year = None if lst["decade"] else lst["year"]
    return parse_tables(html, all_tables=src.get("all_tables", False),
                        winners=src.get("winners", False), default_year=default_year)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        lang = "en"
        if arg.startswith("es:"):
            lang, arg = "es", arg[3:]
        items = parse_tables(fetch_page_html(lang, arg),
                             all_tables="Grammy" in arg, winners="Grammy" in arg)
        print(f"== {arg}: {len(items)} canciones")
        for it in items[:8] + (["…"] if len(items) > 8 else []) + items[-3:]:
            print("  ", it)
