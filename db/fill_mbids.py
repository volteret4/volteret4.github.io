import sqlite3
import ssl
import requests
import time
import re
import os
import argparse
import itertools
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context

MAIN_DB  = "db/must_hear.db"
CACHE_DB = "db/lasfm_cache_rym_new_normalized.db"
RATE_LIMIT = 1.1
db_lock = Lock()

HEADERS = {
    "User-Agent": "mbid-filler/1.2 (frodobolson@disroot.org)"
}

class _TLS12Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

_retry = Retry(total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503, 504])
_session = requests.Session()
_session.mount("https://", _TLS12Adapter(max_retries=_retry))

# ── Normalización ────────────────────────────────────────────────────────────

def normalize(s):
    if not s: return ""
    s = s.lower().strip()
    if s.startswith("the "): s = s[4:]
    return s

# Elimina sufijos comunes que confunden la búsqueda:
# "(Remastered)", "(Deluxe Edition)", "[Live]", "- EP", etc.
_NOISE_RE = re.compile(
    r'\s*[\(\[]\s*(?:remaster(?:ed)?|deluxe|expanded|anniversary|'
    r'edition|version|re-?issue|bonus|special|live|ep|lp|single|'
    r'\d{4}\s*remaster|\d{4})\b[^\)\]]*[\)\]]'
    r'|\s*-\s*(?:EP|Single|Live|Remastered|Deluxe Edition)$',
    re.IGNORECASE
)

def simplify(s):
    """Elimina ruido de paréntesis/corchetes y normaliza."""
    if not s: return ""
    s = _NOISE_RE.sub("", s).strip()
    s = re.sub(r'\s+', ' ', s)
    return s

def strip_the(s):
    """Devuelve versión sin 'The ' inicial."""
    if s.lower().startswith("the "):
        return s[4:].strip()
    return s

# Caracteres especiales Lucene que deben escaparse dentro de una phrase query
_LUCENE_SPECIAL = re.compile(r'([\\"])')

def lucene_escape(s):
    """Escapa backslash y comillas dobles para usarlos dentro de "phrase query"."""
    return _LUCENE_SPECIAL.sub(r'\\\1', s)

# Elimina todo lo que no sea alfanumérico, espacio o guión — fallback máximo
_NON_ALNUM_RE = re.compile(r"[^\w\s\-]", re.UNICODE)

def strip_special(s):
    """Elimina caracteres especiales dejando solo letras, números, espacios y guiones."""
    return re.sub(r'\s+', ' ', _NON_ALNUM_RE.sub(' ', s)).strip()

# ── Búsquedas MusicBrainz ────────────────────────────────────────────────────

def _mb_get(endpoint, params, proxy=None):
    """GET genérico a la API MB con manejo de errores y rate-limit 429."""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = _session.get(
            f"https://musicbrainz.org/ws/2/{endpoint}",
            headers=HEADERS, params={**params, "fmt": "json"},
            timeout=20, proxies=proxies
        )
        if r.status_code == 429:
            time.sleep(5)
            r = _session.get(
                f"https://musicbrainz.org/ws/2/{endpoint}",
                headers=HEADERS, params={**params, "fmt": "json"},
                timeout=20, proxies=proxies
            )
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _build_query(field_artist, artist, field_album, album):
    """Construye query Lucene con escape correcto de caracteres especiales."""
    ea = lucene_escape(artist)
    eb = lucene_escape(album)
    return f'{field_artist}:"{ea}" AND {field_album}:"{eb}"'


def search_release_group(artist, album, proxy=None, fuzzy=False):
    """Busca en /release-group/ — devuelve rg_mbid directo."""
    if fuzzy:
        ea, eb = lucene_escape(artist), lucene_escape(album)
        query = f'artist:"{ea}"~ AND releasegroup:"{eb}"~'
    else:
        query = _build_query("artist", artist, "releasegroup", album)
    data = _mb_get("release-group/", {"query": query, "limit": 3}, proxy)
    if not data:
        return None
    for rg in data.get("release-groups", []):
        if rg.get("id"):
            return rg["id"]
    return None


def search_release(artist, album, proxy=None):
    """Busca en /release/ — devuelve (release_mbid, rg_mbid)."""
    data = _mb_get("release/",
                   {"query": _build_query("artist", artist, "release", album), "limit": 3},
                   proxy)
    if not data:
        return None, None
    for rel in data.get("releases", []):
        rid = rel.get("id")
        rgid = rel.get("release-group", {}).get("id")
        if rid or rgid:
            return rid, rgid
    return None, None


def get_rg_from_mbid(mbid, proxy=None):
    """Obtiene el release-group MBID a partir de un release MBID."""
    data = _mb_get(f"release/{mbid}", {"inc": "release-groups"}, proxy)
    if data:
        return data.get("release-group", {}).get("id")
    return None


def get_from_cache(cache_conn, artist, album):
    try:
        row = cache_conn.execute("""
            SELECT a.mbid, a.release_group_mbid
            FROM albums a
            JOIN artists ar ON a.artist_id = ar.id
            WHERE LOWER(ar.name) = ? AND LOWER(a.name) = ?
              AND (a.mbid IS NOT NULL OR a.release_group_mbid IS NOT NULL)
            LIMIT 1
        """, (normalize(artist), normalize(album))).fetchone()
        return row
    except Exception:
        return None


def find_mbids(artist, album, proxy, cache_conn):
    """
    Prueba varias estrategias para obtener (release_mbid, rg_mbid).
    Devuelve el primer resultado no vacío, o (None, None).

    Orden de estrategias:
      1. Cache local
      2. RG endpoint  — nombre exacto
      3. Release endpoint — nombre exacto  → extrae RG
      4. RG endpoint  — nombre simplificado (sin ruido de edición)
      5. Release endpoint — nombre simplificado
      6. RG endpoint  — artista sin "The"
      7. Release endpoint — artista sin "The"
    """
    # 1. Cache
    if cache_conn:
        cached = get_from_cache(cache_conn, artist, album)
        if cached and (cached[0] or cached[1]):
            return cached[0], cached[1]

    seen = set()
    variations = []

    def _add(a, b):
        key = (a.lower().strip(), b.lower().strip())
        if key not in seen and a.strip() and b.strip():
            seen.add(key)
            variations.append((a, b))

    # Primera línea de nombres multi-línea (artistas/álbumes con doble escritura,
    # p.ej. japonés/coreano: "Band Name\n밴드 이름")
    a_first = artist.split('\n')[0].strip()
    b_first = album.split('\n')[0].strip()

    # Álbum sin subtítulo tras ':' o ' / ' (p.ej. "Album: Deluxe Edition" → "Album")
    b_nosub = re.sub(r'\s*[:/]\s+.+$', '', b_first).strip() or b_first

    # 1. Nombres originales
    _add(artist, album)

    # 2. Primera línea (más probable que coincida con MB)
    _add(a_first, b_first)
    _add(a_first, b_nosub)

    # 3. Simplificados (sin ruido de edición)
    sa, sb = simplify(a_first), simplify(b_first)
    _add(sa, sb)
    _add(sa, b_first)
    _add(a_first, sb)
    _add(sa, b_nosub)

    # 4. Sin "The" en el artista
    na = strip_the(a_first)
    _add(na, b_first)
    _add(na, sb)
    _add(na, b_nosub)

    # 5. Completamente saneados (solo alfanumérico) — último recurso
    xa, xb = strip_special(a_first), strip_special(b_first)
    _add(xa, xb)
    _add(xa, strip_special(sb))
    _add(strip_special(na), xb)
    _add(xa, strip_special(b_nosub))

    best_rid = None  # guardamos el mejor release MBID parcial por si no hay RG

    for art, alb in variations:
        time.sleep(RATE_LIMIT)
        rg = search_release_group(art, alb, proxy)
        if rg:
            return best_rid, rg

        time.sleep(RATE_LIMIT)
        rid, rgid = search_release(art, alb, proxy)
        if rgid:
            return rid, rgid
        if rid:
            # Tenemos release MBID pero no RG inline — guardar y seguir buscando
            if not best_rid:
                best_rid = rid
            # Intentar lookup del RG desde este release MBID
            time.sleep(RATE_LIMIT)
            rgid = get_rg_from_mbid(rid, proxy)
            if rgid:
                return rid, rgid
            # Sin RG aun — continuar con la siguiente variación

    # Fuzzy fallback: primera línea saneada con búsqueda aproximada
    for art, alb in [(a_first, b_first), (sa, sb), (xa, xb)]:
        if not art or not alb:
            continue
        time.sleep(RATE_LIMIT)
        rg = search_release_group(art, alb, proxy, fuzzy=True)
        if rg:
            return best_rid, rg

    return best_rid, None


# ── Procesado por hilo ───────────────────────────────────────────────────────

def process_album(album_data, proxy, cache_path):
    album_id, album_name, artist_name = album_data

    main_conn  = sqlite3.connect(MAIN_DB, timeout=30)
    cache_conn = sqlite3.connect(cache_path) if cache_path else None

    try:
        with db_lock:
            row = main_conn.execute(
                "SELECT mbid, release_group_mbid FROM albums WHERE id=?", (album_id,)
            ).fetchone()
        if not row:
            return "skipped"

        existing_mbid, existing_rg = row

        release_mbid = existing_mbid
        rg_mbid      = existing_rg

        # Si ya tenemos MBID pero no RG, intentar obtener RG del MBID
        if existing_mbid and not existing_rg:
            time.sleep(RATE_LIMIT)
            rg_mbid = get_rg_from_mbid(existing_mbid, proxy)

        # Si aún falta algo, buscar
        if not rg_mbid:
            release_mbid, rg_mbid = find_mbids(artist_name, album_name, proxy, cache_conn)

        # Actualizar DB
        if rg_mbid or (release_mbid and release_mbid != existing_mbid):
            with db_lock:
                dup = None
                if rg_mbid:
                    dup = main_conn.execute(
                        "SELECT id FROM albums WHERE release_group_mbid=? AND id!=?",
                        (rg_mbid, album_id)
                    ).fetchone()

                if dup:
                    main_conn.execute(
                        "UPDATE albums SET release_group_mbid=? WHERE id=?",
                        (rg_mbid, album_id)
                    )
                    status = "merged"
                else:
                    main_conn.execute(
                        "UPDATE albums SET mbid=COALESCE(mbid,?), release_group_mbid=COALESCE(release_group_mbid,?) WHERE id=?",
                        (release_mbid, rg_mbid, album_id)
                    )
                    status = "updated" if rg_mbid else "mbid_only"
                main_conn.commit()
            return status

        return "not_found"

    except Exception as e:
        return f"error: {e}"
    finally:
        main_conn.close()
        if cache_conn:
            cache_conn.close()


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxies", type=str, help="Lista de proxies separada por comas")
    parser.add_argument("--only-missing-rg", action="store_true",
                        help="Solo álbumes sin release_group_mbid (default)")
    parser.add_argument("--also-missing-mbid", action="store_true",
                        help="También álbumes sin ningún mbid")
    parser.add_argument("--start-from", type=int, default=0,
                        help="Saltar los primeros N álbumes de la lista (para reanudar)")
    args = parser.parse_args()

    if not args.proxies:
        print("❌ Error: Debes proporcionar al menos un proxy con --proxies")
        return

    p_list = [p.strip() for p in args.proxies.split(",")]
    num_threads = len(p_list)
    proxy_cycle = itertools.cycle(p_list)

    where_clause = "WHERE albums.release_group_mbid IS NULL"
    if args.also_missing_mbid:
        where_clause = "WHERE albums.release_group_mbid IS NULL OR albums.mbid IS NULL"

    main_conn = sqlite3.connect(MAIN_DB)
    rows = main_conn.execute(f"""
        SELECT albums.id, albums.name, artists.name
        FROM albums
        JOIN artists ON albums.artist_id = artists.id
        {where_clause}
        ORDER BY albums.id
    """).fetchall()
    main_conn.close()

    if args.start_from:
        rows = rows[args.start_from:]
        print(f"⏩ Saltando primeros {args.start_from} álbumes, empezando desde el #{args.start_from + 1}")

    print(f"🚀 Iniciando {num_threads} hilos para {len(rows)} álbumes...")

    counts = {"updated": 0, "mbid_only": 0, "merged": 0, "not_found": 0, "skipped": 0, "error": 0}

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [
            executor.submit(process_album, row, next(proxy_cycle),
                            CACHE_DB if os.path.exists(CACHE_DB) else None)
            for row in rows
        ]

        for i, future in enumerate(futures):
            res = future.result()
            key = res if res in counts else ("error" if res.startswith("error") else "not_found")
            counts[key] += 1
            if (i + 1) % 50 == 0 or (i + 1) == len(rows):
                print(f"  [{i+1}/{len(rows)}] updated={counts['updated']} "
                      f"mbid_only={counts['mbid_only']} merged={counts['merged']} "
                      f"not_found={counts['not_found']} errors={counts['error']}")

    print(f"\n✨ Completado. Resumen final:")
    for k, v in counts.items():
        if v:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
