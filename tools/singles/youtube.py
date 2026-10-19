#!/usr/bin/env python3
"""
Must Hear Singles — vídeo de YouTube por canción (yt-dlp)

Búsqueda "plana" (--flat-playlist): no resuelve cada vídeo, así que tarda ~1 s
y aun así da título y duración para descartar directos y recopilatorios.
Resultado en track_youtube: yt_id = '' → buscado sin resultado útil.
Errores de red / 429 no se guardan, para reintentarlos en la siguiente pasada.
"""

import subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor

from tools.singles.singles_database import _fold, title_keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_youtube (
    track_id    INTEGER PRIMARY KEY REFERENCES tracks(id),
    yt_id       TEXT NOT NULL,
    searched_at INTEGER NOT NULL
);
"""

MIN_SECS, MAX_SECS = 60, 900
_backoff_until = 0.0
_backoff_lock = threading.Lock()


def _pick(lines: list[str], title: str) -> str:
    tk = {k for k in title_keys(title) if len(k) >= 3}
    ok = []
    for line in lines:
        vid, _, rest = line.partition("|")
        vtitle, _, dur = rest.rpartition("|")
        if len(vid) != 11 or not dur.isdigit() or not MIN_SECS <= int(dur) <= MAX_SECS:
            continue
        if any(k in _fold(vtitle) for k in tk):
            return vid
        ok.append(vid)
    return ok[0] if ok else ""


def search_video(title: str, artist: str) -> str | None:
    """yt_id, '' si no hay vídeo útil, None si falló la red (no cachear)."""
    global _backoff_until
    wait = _backoff_until - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    cmd = ["yt-dlp", "--flat-playlist", "--quiet", "--no-warnings", "--socket-timeout", "8",
           "--print", "%(id)s|%(title)s|%(duration)s", f"ytsearch5:{artist} - {title}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        if "429" in r.stderr or "Too Many Requests" in r.stderr:
            with _backoff_lock:
                _backoff_until = time.monotonic() + 60
        return None
    return _pick(r.stdout.splitlines(), title)


def fetch_youtube(conn, limit: int | None = None, retry_missing: bool = False,
                  workers: int = 4, log=print) -> None:
    conn.executescript(SCHEMA)
    cond = "y.track_id IS NULL" + (" OR y.yt_id = ''" if retry_missing else "")
    # Primero las canciones de listas más pequeñas (se completan antes páginas enteras)
    todo = conn.execute(f"""
        SELECT t.id, t.title, ar.name FROM tracks t
        JOIN artists ar ON ar.id = t.artist_id
        LEFT JOIN track_youtube y ON y.track_id = t.id
        LEFT JOIN (SELECT ct.track_id, MIN(c.total_tracks) AS size FROM collection_tracks ct
                   JOIN collections c ON c.id = ct.collection_id GROUP BY ct.track_id) s ON s.track_id = t.id
        WHERE {cond}
        ORDER BY s.size, t.id""").fetchall()
    if limit:
        todo = todo[:limit]
    if not todo:
        log("  🎬 vídeos al día")
        return
    log(f"  🎬 buscando {len(todo)} vídeos en YouTube (yt-dlp, {workers} hilos)…")

    def work(row):
        tid, title, artist = row
        return tid, search_video(title, artist)

    found = done = failed = 0
    with ThreadPoolExecutor(workers) as ex:
        for tid, vid in ex.map(work, todo):
            done += 1
            if vid is None:
                failed += 1
            else:
                found += bool(vid)
                conn.execute("INSERT OR REPLACE INTO track_youtube VALUES (?,?,?)",
                             (tid, vid, int(time.time())))
            conn.commit()   # commit por fila: no bloquear al otro proceso que escribe en la DB
            if done % 100 == 0:
                log(f"    {done}/{len(todo)} · {found} con vídeo · {failed} errores")
    conn.commit()
    log(f"  🎬 {found}/{len(todo)} vídeos encontrados ({failed} errores, se reintentan la próxima vez)")


def load_youtube(conn) -> dict[int, str]:
    conn.executescript(SCHEMA)
    return dict(conn.execute("SELECT track_id, yt_id FROM track_youtube WHERE yt_id != ''"))
