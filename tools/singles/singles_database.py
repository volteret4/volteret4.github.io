#!/usr/bin/env python3
"""
Must Hear Singles — base de datos + cruce con scrobbles

DB propia (db/must_hear_singles.db), separada de la de álbumes:
  artists, tracks, collections, collection_tracks, users, user_heard

El cruce con Last.fm se hace contra lastfm_cache_rym_new_normalized.db
(tablas scrobbles_<user> → tracks/artists) por título + artista normalizados.
"""

import re, sqlite3, time, unicodedata

SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    name  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS tracks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    artist_id INTEGER NOT NULL REFERENCES artists(id),
    title     TEXT    NOT NULL,
    year      INTEGER,
    UNIQUE (artist_id, title)
);
CREATE TABLE IF NOT EXISTS collections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slug         TEXT NOT NULL UNIQUE,
    group_slug   TEXT NOT NULL,
    name         TEXT NOT NULL,
    year         INTEGER,
    lang         TEXT,
    wiki_title   TEXT,
    source_url   TEXT,
    total_tracks INTEGER DEFAULT 0,
    last_updated INTEGER
);
CREATE TABLE IF NOT EXISTS collection_tracks (
    collection_id INTEGER NOT NULL REFERENCES collections(id),
    track_id      INTEGER NOT NULL REFERENCES tracks(id),
    rank          INTEGER,
    year          INTEGER,
    weeks         INTEGER,
    note          TEXT,
    PRIMARY KEY (collection_id, track_id)
);
CREATE TABLE IF NOT EXISTS users (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS user_heard (
    user_id        INTEGER NOT NULL REFERENCES users(id),
    track_id       INTEGER NOT NULL REFERENCES tracks(id),
    plays          INTEGER NOT NULL,
    first_heard_at INTEGER,
    PRIMARY KEY (user_id, track_id)
);
CREATE INDEX IF NOT EXISTS idx_sg_ct_collection ON collection_tracks(collection_id);
CREATE INDEX IF NOT EXISTS idx_sg_uh_track      ON user_heard(track_id);
"""


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=60)   # carátulas y vídeos pueden escribir a la vez
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def get_collection(conn, slug: str):
    return conn.execute("SELECT * FROM collections WHERE slug=?", (slug,)).fetchone()


def save_collection(conn, group_slug: str, lst: dict, source_url: str, items: list[dict]) -> int:
    """Guarda (o reemplaza) una lista scrapeada."""
    now = int(time.time())
    conn.execute("""
        INSERT INTO collections (slug, group_slug, name, year, lang, wiki_title, source_url,
                                 total_tracks, last_updated)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(slug) DO UPDATE SET
            group_slug=excluded.group_slug, name=excluded.name, year=excluded.year,
            lang=excluded.lang, wiki_title=excluded.wiki_title, source_url=excluded.source_url,
            total_tracks=excluded.total_tracks, last_updated=excluded.last_updated
    """, (lst["slug"], group_slug, lst["name"], lst["year"], lst["lang"], lst["wiki_title"],
          source_url, len(items), now))
    cid = conn.execute("SELECT id FROM collections WHERE slug=?", (lst["slug"],)).fetchone()[0]
    conn.execute("DELETE FROM collection_tracks WHERE collection_id=?", (cid,))
    for it in items:
        conn.execute("INSERT OR IGNORE INTO artists (name) VALUES (?)", (it["artist"],))
        aid = conn.execute("SELECT id FROM artists WHERE name=?", (it["artist"],)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO tracks (artist_id, title, year) VALUES (?,?,?)",
                     (aid, it["title"], it["year"]))
        tid = conn.execute("SELECT id FROM tracks WHERE artist_id=? AND title=?",
                           (aid, it["title"])).fetchone()[0]
        conn.execute("""INSERT OR IGNORE INTO collection_tracks
                        (collection_id, track_id, rank, year, weeks, note) VALUES (?,?,?,?,?,?)""",
                     (cid, tid, it["rank"], it["year"], it["weeks"], it["note"]))
    conn.commit()
    return cid


def load_collections(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM collections ORDER BY group_slug, year, slug")]


def load_collection_tracks(conn, collection_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute("""
        SELECT t.id AS track_id, t.title, ar.name AS artist,
               ct.rank, COALESCE(ct.year, t.year) AS year, ct.weeks, ct.note
        FROM collection_tracks ct
        JOIN tracks  t  ON t.id  = ct.track_id
        JOIN artists ar ON ar.id = t.artist_id
        WHERE ct.collection_id = ?
        ORDER BY ct.rank, t.id
    """, (collection_id,))]


# ── NORMALIZACIÓN / MATCHING ───────────────────────────────────────────────────

_PAREN   = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_FEAT_T  = re.compile(r"\s+(feat\.?|ft\.?|featuring|with)\s+.*$", re.I)
_DASH    = re.compile(r"\s+[-–—]\s+.*$")
_ART_SEP = re.compile(r"\s*(?:,|&|\+|/|;|\bfeaturing\b|\bfeat\.?|\bft\.?|\bwith\b|\band\b|"
                      r"\bvs\.?|\bx\b|\by\b|\bcon\b|\be\b)\s*", re.I)


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w]", "", s.lower().replace("&", "and"))


def title_keys(title: str) -> set[str]:
    """Claves de un título: completo, sin paréntesis/sufijos, y cada cara de un doble A."""
    keys = set()
    parts = [title] + ([p for p in title.split("/") if p.strip()] if "/" in title else [])
    for p in parts:
        for variant in (p, _PAREN.sub("", p), _DASH.sub("", _PAREN.sub("", p)),
                        _FEAT_T.sub("", _PAREN.sub("", p))):
            k = _fold(variant)
            if k:
                keys.add(k)
    return keys


def artist_keys(artist: str) -> set[str]:
    a = re.sub(r"^the\s+", "", (artist or "").strip(), flags=re.I)
    keys = {_fold(a), _fold(artist)}
    for part in _ART_SEP.split(artist or ""):
        part = re.sub(r"^the\s+", "", part.strip(), flags=re.I)
        k = _fold(part)
        if len(k) >= 4:
            keys.add(k)
    return {k for k in keys if k}


def _artist_match(wiki_keys: set[str], scrobble_artist_keys: set[str]) -> bool:
    for w in wiki_keys:
        for s in scrobble_artist_keys:
            if w == s:
                return True
            if len(w) >= 4 and len(s) >= 4 and (w in s or s in w):
                return True
    return False


def _user_table(username: str) -> str:
    return "scrobbles_" + re.sub(r"[^a-z0-9]", "_", username.lower()).strip("_")


def scrobble_users(scr_conn) -> list[str]:
    tables = {r[0] for r in scr_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [u for (u,) in scr_conn.execute("SELECT username FROM users ORDER BY username COLLATE NOCASE")
            if _user_table(u) in tables]


def load_user_track_index(scr_conn, username: str) -> dict[str, list]:
    """{clave_título: [(claves_artista, plays, first_ts), ...]} para un usuario."""
    rows = scr_conn.execute(f"""
        SELECT t.name, ar.name, COUNT(*), MIN(s.timestamp)
        FROM {_user_table(username)} s
        JOIN tracks  t  ON t.id  = s.track_id
        JOIN artists ar ON ar.id = s.artist_id
        GROUP BY s.track_id
    """).fetchall()
    idx: dict[str, list] = {}
    akeys_cache: dict[str, set] = {}
    for tname, aname, plays, first in rows:
        ak = akeys_cache.get(aname)
        if ak is None:
            ak = akeys_cache[aname] = artist_keys(aname)
        for k in title_keys(tname):
            idx.setdefault(k, []).append((ak, plays, first))
    return idx


def match_track(user_idx: dict, title: str, artist: str) -> tuple[int, int | None]:
    """Devuelve (plays, primer_scrobble) sumando todas las versiones que encajan."""
    wk = artist_keys(artist)
    plays, first, seen = 0, None, set()
    for k in title_keys(title):
        for entry in user_idx.get(k, ()):
            if id(entry) in seen:
                continue
            ak, p, f = entry
            if _artist_match(wk, ak):
                seen.add(id(entry))
                plays += p
                first = f if first is None else min(first, f)
    return plays, first


def refresh_user_heard(conn, scr_conn, users: list[str], log=print) -> None:
    """Recalcula user_heard para todas las canciones de la DB de singles."""
    tracks = conn.execute("""SELECT t.id, t.title, ar.name FROM tracks t
                             JOIN artists ar ON ar.id = t.artist_id""").fetchall()
    for u in users:
        conn.execute("INSERT OR IGNORE INTO users (username) VALUES (?)", (u,))
        uid = conn.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()[0]
        idx = load_user_track_index(scr_conn, u)
        conn.execute("DELETE FROM user_heard WHERE user_id=?", (uid,))
        n = 0
        for tid, title, artist in tracks:
            plays, first = match_track(idx, title, artist)
            if plays:
                n += 1
                conn.execute("INSERT INTO user_heard VALUES (?,?,?,?)", (uid, tid, plays, first))
        conn.commit()
        log(f"  👤 {u}: {n}/{len(tracks)} canciones escuchadas")


def load_heard(conn) -> dict[int, dict[str, int]]:
    """{track_id: {username: plays}}"""
    out: dict[int, dict[str, int]] = {}
    for tid, user, plays in conn.execute("""
            SELECT uh.track_id, u.username, uh.plays
            FROM user_heard uh JOIN users u ON u.id = uh.user_id"""):
        out.setdefault(tid, {})[user] = plays
    return out
