#!/usr/bin/env python3
"""
Must Hear Singles — carátulas desde Deezer

La API pública de búsqueda de Deezer (sin key) devuelve, por canción, la
portada del single/álbum en el que sale. Se valida que título y artista
encajen (mismas claves que el cruce con Last.fm) y se guarda en track_covers.

Límite de Deezer: 50 peticiones / 5 s → vamos a ~8/s con varios hilos.
"""

import json, re, threading, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor

from tools.singles.singles_database import _artist_match, artist_keys, title_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_covers (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id),
    cover       TEXT,               -- 250x250 (NULL = buscado y no encontrado)
    deezer_id   INTEGER,
    album_title TEXT,
    checked_at  INTEGER NOT NULL
);
"""

API          = "https://api.deezer.com/search?"
RATE_PER_SEC = 8
UA           = "rym_lastfm-singles/1.0"
_FEAT        = re.compile(r"\s+(?:featuring|feat\.?|ft\.?|with|con|y|and|&|,|x)\s+.*$", re.I)
_PAREN       = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


class _RateLimiter:
    def __init__(self, per_sec: float):
        self.interval, self.next_at, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


_limiter = _RateLimiter(RATE_PER_SEC)


def _search(q: str, retries: int = 4) -> list[dict]:
    url = API + urllib.parse.urlencode({"q": q, "limit": 15})
    for attempt in range(retries):
        _limiter.wait()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
        except Exception:
            time.sleep(2 ** attempt)
            continue
        if "error" in d:            # cuota excedida → esperar y reintentar
            time.sleep(5 * (attempt + 1))
            continue
        return d.get("data") or []
    return []


def _pick(results: list[dict], title: str, artist: str) -> dict | None:
    """Primer resultado que encaja; mejor si el disco se llama como la canción (= el single)."""
    tk, ak = title_keys(title), artist_keys(artist)
    best = None
    for r in results:
        rt = title_keys(r.get("title_short") or "") | title_keys(r.get("title") or "")
        if not (tk & rt) or not _artist_match(ak, artist_keys(r["artist"]["name"])):
            continue
        if tk & title_keys(r["album"]["title"]):
            return r
        best = best or r
    return best


def find_cover(title: str, artist: str) -> dict | None:
    main_artist = _FEAT.sub("", artist).strip() or artist
    main_title = title.split(" / ")[0].strip()
    for q in (f"{main_artist} {main_title}", f"{main_artist} {_PAREN.sub('', main_title)}"):
        hit = _pick(_search(q), title, artist)
        if hit:
            return hit
    return None


def fetch_covers(conn, limit: int | None = None, retry_missing: bool = False,
                 workers: int = 6, log=print) -> None:
    conn.executescript(SCHEMA)
    cond = "c.track_id IS NULL" + (" OR c.cover IS NULL" if retry_missing else "")
    todo = conn.execute(f"""
        SELECT t.id, t.title, ar.name FROM tracks t
        JOIN artists ar ON ar.id = t.artist_id
        LEFT JOIN track_covers c ON c.track_id = t.id
        WHERE {cond} ORDER BY t.id""").fetchall()
    if limit:
        todo = todo[:limit]
    if not todo:
        log("  🖼  carátulas al día")
        return
    log(f"  🖼  buscando {len(todo)} carátulas en Deezer…")

    def work(row):
        tid, title, artist = row
        return tid, find_cover(title, artist)

    found = done = 0
    with ThreadPoolExecutor(workers) as ex:
        for tid, hit in ex.map(work, todo):
            done += 1
            if hit:
                found += 1
                conn.execute("INSERT OR REPLACE INTO track_covers VALUES (?,?,?,?,?)",
                             (tid, hit["album"]["cover_medium"], hit["id"], hit["album"]["title"],
                              int(time.time())))
            else:
                conn.execute("INSERT OR REPLACE INTO track_covers VALUES (?,NULL,NULL,NULL,?)",
                             (tid, int(time.time())))
            conn.commit()   # commit por fila: no bloquear al otro proceso que escribe en la DB
            if done % 200 == 0:
                log(f"    {done}/{len(todo)} · {found} con carátula")
    conn.commit()
    log(f"  🖼  {found}/{len(todo)} carátulas encontradas")


def load_covers(conn) -> dict[int, dict]:
    """{track_id: {"c": cover_250, "dz": deezer_id}}"""
    conn.executescript(SCHEMA)
    return {tid: {"c": cover, "dz": dz} for tid, cover, dz in conn.execute(
        "SELECT track_id, cover, deezer_id FROM track_covers WHERE cover IS NOT NULL")}
