#!/usr/bin/env python3
"""
Estadísticas por periodos en una sola página (docs/periodos/index.html).

Cada periodo se guarda como docs/periodos/data/<clave>.js (window.__STATS__ = {...})
y docs/periodos/data/index.js lista los periodos disponibles. La página carga el
periodo indicado en ?p=<clave>.

Claves: semanal-0..3, mensual-YYYY-MM, anual-YYYY

Por defecto solo genera lo que falta o está desactualizado:
  - las 4 semanas (siempre)
  - meses y años sin fichero, o generados cuando la base aún no cubría
    el periodo entero (data/estado.json guarda qué periodos están completos)

Uso:
  python3 html_periodos.py            # incremental
  python3 html_periodos.py --all      # regenerar todo
  python3 html_periodos.py --since 2024
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from html_temporal import generate_stats
from tools.temp.temp_html_generator import HTMLGenerator

DEFAULT_DB = 'db/lastfm_cache_rym_new_normalized.db'
OUT_DIR = Path('docs/periodos')
DATA_DIR = OUT_DIR / 'data'

MESES = ['', 'Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
         'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
SEMANAS = ['Esta semana', 'Semana pasada', 'Hace dos semanas', 'Hace tres semanas']


def scrobble_range(db_path: str) -> tuple[datetime, datetime]:
    """Primer y último scrobble de la base (todas las tablas de usuario)."""
    conn = sqlite3.connect(db_path)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'scrobbles_%'")]
    rows = [conn.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM {t} WHERE timestamp > 1000000000").fetchone()
            for t in tables]
    conn.close()
    first = min(r[0] for r in rows if r[0])
    last = max(r[1] for r in rows if r[1])
    return datetime.fromtimestamp(first), datetime.fromtimestamp(last)


def period_end(key: str) -> datetime:
    parts = key.split('-')
    if parts[0] == 'mensual':
        y, m = int(parts[1]), int(parts[2])
        return datetime(y + (m == 12), m % 12 + 1, 1)
    return datetime(int(parts[1]) + 1, 1, 1)


def needs_update(key: str, force: bool, complete: dict) -> bool:
    return force or not (DATA_DIR / f'{key}.js').exists() or not complete.get(key)


def write_period(key: str, period_type: str, users: list, db_path: str, **kwargs) -> bool:
    stats, *_ = generate_stats(period_type, users, db_path, **kwargs)
    if not stats or not stats.get('total_scrobbles'):
        print(f'   ⚠️ {key}: sin datos')
        return False
    (DATA_DIR / f'{key}.js').write_text(
        'window.__STATS__=' + json.dumps(stats, ensure_ascii=False, separators=(',', ':')) + ';',
        encoding='utf-8')
    print(f'   💾 {key}.js ({stats["total_scrobbles"]:,} scrobbles)')
    return True


def write_manifest() -> None:
    keys = sorted(p.stem for p in DATA_DIR.glob('*.js') if p.stem != 'index')
    periods = {'semanal': [], 'mensual': [], 'anual': []}
    for k in keys:
        parts = k.split('-')
        if parts[0] == 'semanal':
            label = SEMANAS[int(parts[1])]
        elif parts[0] == 'mensual':
            label = f'{MESES[int(parts[2])]} {parts[1]}'
        else:
            label = parts[1]
        periods[parts[0]].append({'key': k, 'label': label})
    periods['semanal'].sort(key=lambda p: p['key'])
    periods['mensual'].sort(key=lambda p: p['key'], reverse=True)
    periods['anual'].sort(key=lambda p: p['key'], reverse=True)
    (DATA_DIR / 'index.js').write_text(
        'window.__PERIODS__=' + json.dumps(periods, ensure_ascii=False) + ';', encoding='utf-8')
    print(f'📋 index.js: {len(periods["mensual"])} meses, {len(periods["anual"])} años')


def main():
    parser = argparse.ArgumentParser(description='Genera la página única de estadísticas por periodos')
    parser.add_argument('--db', default=DEFAULT_DB)
    parser.add_argument('--all', action='store_true', help='Regenerar todos los periodos')
    parser.add_argument('--since', type=int, default=None, help='Primer año a generar')
    args = parser.parse_args()

    users = [u.strip() for u in os.getenv('LASTFM_USERS', '').split(',') if u.strip()]
    if not users:
        sys.exit('❌ LASTFM_USERS no definida')
    if not os.path.exists(args.db):
        sys.exit(f'❌ Base de datos no encontrada: {args.db}')

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    first, last = scrobble_range(args.db)
    print(f'🗄  Scrobbles del {first:%Y-%m-%d} al {last:%Y-%m-%d}')
    start_year = args.since or first.year
    state_file = DATA_DIR / 'estado.json'
    try:
        complete = json.loads(state_file.read_text())
    except (FileNotFoundError, ValueError):
        complete = {}

    for w in range(4):
        print(f'\n🔹 {SEMANAS[w]}')
        write_period(f'semanal-{w}', 'weekly', users, args.db, week_offset=w)

    for year in range(now.year, start_year - 1, -1):
        last_month = now.month if year == now.year else 12
        for month in range(last_month, 0, -1):
            key = f'mensual-{year}-{month:02d}'
            if needs_update(key, args.all, complete):
                if write_period(key, 'monthly', users, args.db, month=month, year=year):
                    complete[key] = last >= period_end(key)
        key = f'anual-{year}'
        if needs_update(key, args.all, complete):
            if write_period(key, 'yearly', users, args.db, year=year):
                complete[key] = last >= period_end(key)
        state_file.write_text(json.dumps(complete, indent=1, sort_keys=True))

    write_manifest()
    (OUT_DIR / 'index.html').write_text(
        HTMLGenerator.create_html({}, users, 'periodo', 'periodos', template=True), encoding='utf-8')
    print(f'✅ {OUT_DIR / "index.html"}')


if __name__ == '__main__':
    main()
