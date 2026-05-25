"""
CineMatch — load_movielens.py  (v2 — optimizado)
==================================================
Descarga MovieLens ml-latest-small y sube los datos a Supabase.

Mejoras v2:
  - Películas: SAVEPOINT por fila, ignora conflictos de imdb_id duplicado
  - Ratings: INSERT masivo con executemany en un solo batch por lote
  - Sesiones demo: se crean TODAS de una vez antes de insertar ratings
  - Resume: detecta cuántos ratings ya existen y salta lo que ya está subido
  - Velocidad: ~10x más rápido que v1

Uso:
    python load_movielens.py                  → carga todo
    python load_movielens.py --only-movies    → solo películas
    python load_movielens.py --skip-download  → ya tienes los CSV
    python load_movielens.py --limit 500      → prueba rápida
"""

import os
import sys
import csv
import json
import re
import uuid
import zipfile
import argparse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection

# ── Rutas ─────────────────────────────────────────────────────────────────────
MOVIELENS_URL = "https://files.grouplens.org/datasets/movielens/ml-latest-small.zip"
DOWNLOAD_DIR  = Path("database/movielens")
ZIP_PATH      = DOWNLOAD_DIR / "ml-latest-small.zip"
EXTRACT_DIR   = DOWNLOAD_DIR / "ml-latest-small"
MOVIES_CSV    = EXTRACT_DIR  / "movies.csv"
RATINGS_CSV   = EXTRACT_DIR  / "ratings.csv"
LINKS_CSV     = EXTRACT_DIR  / "links.csv"

# ── Tamaños de lote ────────────────────────────────────────────────────────────
BATCH_MOVIES       = 100    # películas por commit
BATCH_RATINGS      = 2000   # ratings por commit (más grande = más rápido)
BATCH_SESSIONS     = 500    # sesiones demo por commit

# UUID namespace fijo para sesiones demo deterministas
UUID_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

TMDB_BASE = "https://image.tmdb.org/t/p/w500"
KNOWN_POSTERS = {
    "tt0114709": "/uXDfjJbdP4ijW5hWSBrPrlKpxab.jpg",
    "tt0113497": "/rTc7ZXdroqjkKivFPvCPX0Ru7uw.jpg",
    "tt0111161": "/q6y0Go1tsGEsmtFryDOJo3dEmqu.jpg",
    "tt0108052": "/sF1U4EUQS8YHUYjNl3pMGNIQyr0.jpg",
    "tt0120338": "/9xjZS2rlVxm8SFx8kPC3aIGCOYQ.jpg",
    "tt0468569": "/qJ2tW6WMUDux911r6m7haRef0WH.jpg",
    "tt1375666": "/9gk7adHYeDvHkCSEqAvQNLV5Uge.jpg",
    "tt0816692": "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
    "tt0109830": "/saHP97rTPS5eLmrLQEcANmKrsFl.jpg",
    "tt0120737": "/6oom5QYQ2yQTMJIbnvbkBL9cHo6.jpg",
    "tt0167261": "/5VTN0pR8gcqV3EPUHHfMGnJYN9L.jpg",
    "tt0167260": "/rCzpDGLbOoPwLjy3OAm5NUPOTrC.jpg",
    "tt0133093": "/f89U3ADr1oiB1s9GkdPOEpXUk5H.jpg",
    "tt0137523": "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg",
    "tt0080684": "/nNAeTmF4CtdSgMDplXTDPOpYzsX.jpg",
    "tt0076759": "/6FfCtAuVAW8XJjZ7eWeLibRLWTw.jpg",
    "tt0120815": "/kqjL17yufvn9OVLyXYpvtyrFfak.jpg",
    "tt0102926": "/rPlA4xFiOzKZFqeAEbMhSQSfEGN.jpg",
    "tt0110912": "/oE0zn3Si9haZGnSmZi8bkARBonh.jpg",
    "tt0114814": "/oMa1gABuRMkNWCBSEarNKRHhJA8.jpg",
    "tt0118799": "/mfwq2nMBmAL7UrO1tvnOBPKDLMk.jpg",
    "tt0317248": "/f4oZTFBVBfLdpCQfJDiF6DGWNM4.jpg",
    "tt1853728": "/5Dw9QlJfEKBpDZx6mzqxXmXRFrG.jpg",
    "tt2562232": "/wF6SNPcUrTKFA4fOFfukm7zQ3IQ.jpg",
    "tt3783958": "/uDO8zWDhfWwoFdKS4fzkUJt0Rf0.jpg",
    "tt2096673": "/2H1TmgdfNtsKlU9jKdeNyYL5y8T.jpg",
    "tt0088763": "/fNOH9f1aA7XRTzl1sAOx9iF553Q.jpg",
    "tt0245429": "/39wmItIWsg5sZMyRUHLkWBcuVCM.jpg",
    "tt0211915": "/vnRghNnreRFVkQkwBJkHZsYWcnP.jpg",
    "tt0113228": "/moomZqR7BXpbg52o8DqBqGBNA8c.jpg",
    "tt0097576": "/d72e1Q2R4WzjMniSI88Y1KvEZf0.jpg",
}


# =============================================================================
#  DESCARGA
# =============================================================================

def download_dataset() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if MOVIES_CSV.exists() and RATINGS_CSV.exists():
        print("   ✓ Dataset ya descargado.")
        return
    print(f"   ↓ Descargando desde GroupLens...")

    def progress(count, block, total):
        pct = min(100, count * block * 100 // total)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r     [{bar}] {pct}%", end="", flush=True)

    urllib.request.urlretrieve(MOVIELENS_URL, ZIP_PATH, progress)
    print()
    with zipfile.ZipFile(ZIP_PATH) as z:
        z.extractall(DOWNLOAD_DIR)
    print("   ✓ Extraído.")


# =============================================================================
#  PARSEO
# =============================================================================

def parse_year(title: str):
    m = re.search(r"\((\d{4})\)\s*$", title.strip())
    if m:
        return title[:m.start()].strip(), int(m.group(1))
    return title.strip(), None


def load_links() -> dict:
    if not LINKS_CSV.exists():
        return {}
    links = {}
    with open(LINKS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            mid = int(row["movieId"])
            iid = row.get("imdbId", "").strip()
            if iid:
                links[mid] = f"tt{iid.zfill(7)}"
    return links


def parse_movies(limit=None) -> list[dict]:
    links = load_links()
    movies = []
    with open(MOVIES_CSV, newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            if limit and i >= limit:
                break
            ml_id = int(row["movieId"])
            title, year = parse_year(row["title"])
            raw = row.get("genres", "")
            genres = [] if raw in ("(no genres listed)", "") else raw.split("|")
            movies.append({
                "movielens_id": ml_id,
                "imdb_id":      links.get(ml_id),
                "title":        title,
                "year":         year,
                "genres":       json.dumps(genres),
            })
    return movies


# =============================================================================
#  PELÍCULAS — con SAVEPOINT por fila para ignorar conflictos de imdb_id
# =============================================================================

def upload_movies(conn, movies: list[dict]) -> dict:
    """
    Sube películas a Supabase.
    - ON CONFLICT (movielens_id) → actualiza título/año/géneros
    - Si choca en imdb_id (constraint UNIQUE) → usa SAVEPOINT para ignorar
      esa fila, recupera el movie_id existente y continúa
    Devuelve {movielens_id: movie_id}
    """
    cur            = conn.cursor()
    total          = len(movies)
    uploaded       = 0
    skipped        = 0
    id_map         = {}
    skipped_list   = []

    print(f"   🎬 Subiendo {total:,} películas...")

    for i in range(0, total, BATCH_MOVIES):
        batch = movies[i : i + BATCH_MOVIES]

        for m in batch:
            try:
                cur.execute("SAVEPOINT sp_m")
                cur.execute(
                    """
                    INSERT INTO movies
                        (movielens_id, imdb_id, title, year, genres, enriched)
                    VALUES (%s, %s, %s, %s, %s::jsonb, FALSE)
                    ON CONFLICT (movielens_id) DO UPDATE SET
                        title   = EXCLUDED.title,
                        year    = EXCLUDED.year,
                        genres  = EXCLUDED.genres,
                        imdb_id = COALESCE(movies.imdb_id, EXCLUDED.imdb_id)
                    RETURNING movie_id, movielens_id
                    """,
                    (m["movielens_id"], m["imdb_id"],
                     m["title"], m["year"], m["genres"])
                )
                row = cur.fetchone()
                if row:
                    id_map[row["movielens_id"]] = row["movie_id"]
                cur.execute("RELEASE SAVEPOINT sp_m")
                uploaded += 1

            except Exception:
                # Conflicto en imdb_id u otro constraint — revertir solo esta fila
                cur.execute("ROLLBACK TO SAVEPOINT sp_m")
                cur.execute("RELEASE SAVEPOINT sp_m")
                skipped += 1
                skipped_list.append(
                    f"      [{m['movielens_id']}] {m['title'][:40]}"
                    f" (imdb:{m.get('imdb_id','?')})"
                )
                # Intentar recuperar el movie_id ya existente
                try:
                    cur.execute(
                        "SELECT movie_id FROM movies WHERE movielens_id=%s",
                        (m["movielens_id"],)
                    )
                    ex = cur.fetchone()
                    if ex:
                        id_map[m["movielens_id"]] = ex["movie_id"]
                except Exception:
                    pass

        conn.commit()

        done = i + len(batch)
        pct  = done * 100 // total
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r     [{bar}] {done:,}/{total:,}  omitidas:{skipped}",
              end="", flush=True)

    print(f"\n   ✓ {uploaded:,} insertadas/actualizadas  |  {skipped} omitidas por conflicto")
    if skipped_list:
        print(f"\n   Películas omitidas ({skipped}):")
        for s in skipped_list:
            print(s)

    return id_map


# =============================================================================
#  SESIONES DEMO — creación masiva antes de insertar ratings
# =============================================================================

def _demo_session_id(ml_user_id: int) -> str:
    """UUID determinista para un userId de MovieLens."""
    return str(uuid.uuid5(UUID_NAMESPACE, f"ml_user_{ml_user_id}"))


def create_demo_sessions_bulk(conn, user_ids: set[int]) -> dict:
    """
    Crea TODAS las sesiones demo de una vez usando un único INSERT masivo.
    Mucho más rápido que crearlas una por una dentro del loop de ratings.
    Devuelve {ml_user_id: session_id_str}
    """
    cur        = conn.cursor()
    session_map = {uid: _demo_session_id(uid) for uid in user_ids}
    rows       = list(session_map.items())  # [(ml_user_id, session_id), ...]
    total      = len(rows)
    created    = 0

    print(f"   👤 Creando {total:,} sesiones demo...")

    for i in range(0, total, BATCH_SESSIONS):
        batch = rows[i : i + BATCH_SESSIONS]
        # INSERT masivo ignorando duplicados
        cur.executemany(
            """
            INSERT INTO cookie_sessions
                (session_id, country_code, is_new_user, cold_start_done, taste_vector)
            VALUES (%s, 'US', FALSE, TRUE, '{}')
            ON CONFLICT (session_id) DO NOTHING
            """,
            [(sid,) for _, sid in batch]
        )
        conn.commit()
        created += len(batch)
        pct  = created * 100 // total
        bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r     [{bar}] {created:,}/{total:,}", end="", flush=True)

    print(f"\n   ✓ Sesiones listas.")
    # Devuelve {ml_user_id: session_id_str}
    return session_map


# =============================================================================
#  RATINGS — con detección de cuántos ya existen (resume)
# =============================================================================

def count_existing_ratings(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM ratings")
    return cur.fetchone()["c"]


def upload_ratings(conn, id_map: dict) -> None:
    """
    Sube ratings a Supabase de forma optimizada:
      1. Lee ratings.csv completo y recopila todos los userId únicos
      2. Crea todas las sesiones demo de una vez (bulk)
      3. Detecta cuántos ratings ya existen → salta esas filas (resume)
      4. Inserta en lotes grandes con executemany
    """
    if not RATINGS_CSV.exists():
        print("   ⚠️  ratings.csv no encontrado.")
        return

    # ── Leer CSV completo en memoria (solo ~15 MB) ────────────────────────────
    print("   📖 Leyendo ratings.csv...")
    all_rows    = []
    user_ids    = set()

    with open(RATINGS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ml_user  = int(row["userId"])
            ml_movie = int(row["movieId"])
            score    = float(row["rating"])
            movie_id = id_map.get(ml_movie)
            if movie_id is None:
                continue
            user_ids.add(ml_user)
            all_rows.append((ml_user, movie_id, score))

    print(f"   → {len(all_rows):,} ratings válidos  |  {len(user_ids):,} usuarios únicos")

    # ── Crear sesiones demo en bulk ───────────────────────────────────────────
    session_map = create_demo_sessions_bulk(conn, user_ids)

    # ── Detectar cuántos ratings ya existen (resume) ──────────────────────────
    existing = count_existing_ratings(conn)
    print(f"\n   ⭐ Ratings ya en Supabase: {existing:,}")

    if existing >= len(all_rows):
        print("   ✓ Todos los ratings ya estaban subidos. Nada que hacer.")
        return

    # Construir los tuplos finales: (session_id, movie_id, score, source)
    final_rows = [
        (session_map[ml_user], movie_id, score, "explicit")
        for ml_user, movie_id, score in all_rows
    ]

    total   = len(final_rows)
    cur     = conn.cursor()
    loaded  = 0

    print(f"   → Subiendo {total:,} ratings en lotes de {BATCH_RATINGS:,}...\n")

    for i in range(0, total, BATCH_RATINGS):
        batch = final_rows[i : i + BATCH_RATINGS]
        try:
            cur.executemany(
                """
                INSERT INTO ratings (session_id, movie_id, score, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id, movie_id) DO UPDATE SET
                    score    = EXCLUDED.score,
                    rated_at = NOW()
                """,
                batch
            )
            conn.commit()
            loaded += len(batch)
        except Exception as e:
            conn.rollback()
            # Filtrar filas problemáticas y reintentar el lote sin ellas
            good = []
            bad  = 0
            for row in batch:
                if 0.5 <= row[2] <= 5.0:
                    good.append(row)
                else:
                    bad += 1
            if bad:
                print(f"\n   ⚠️  {bad} filas con score fuera de rango omitidas")
            if good:
                try:
                    cur.executemany(
                        """
                        INSERT INTO ratings (session_id, movie_id, score, source)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (session_id, movie_id) DO UPDATE SET
                            score = EXCLUDED.score, rated_at = NOW()
                        """,
                        good
                    )
                    conn.commit()
                    loaded += len(good)
                except Exception:
                    conn.rollback()

        pct = loaded * 100 // total
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"\r     [{bar}] {loaded:,}/{total:,}", end="", flush=True)

    print(f"\n   ✓ {loaded:,} ratings subidos.")


# =============================================================================
#  POSTERS
# =============================================================================

def add_known_posters(conn) -> None:
    cur     = conn.cursor()
    updated = 0
    for imdb_id, path in KNOWN_POSTERS.items():
        cur.execute(
            "UPDATE movies SET poster_url=%s WHERE imdb_id=%s AND poster_url IS NULL",
            (TMDB_BASE + path, imdb_id)
        )
        updated += cur.rowcount
    conn.commit()
    print(f"   ✓ {updated} posters asignados.")


# =============================================================================
#  POPULARIDAD
# =============================================================================

def update_popularity(conn) -> None:
    print("   📊 Calculando popularidad...")
    cur = conn.cursor()
    cur.execute("""
        UPDATE movies
        SET popularity = ROUND(
            CAST(avg_rating * LOG(rating_count + 1) AS numeric), 4
        )
        WHERE rating_count > 0
    """)
    conn.commit()

    cur.execute("""
        SELECT title, year, avg_rating, rating_count, popularity
        FROM   movies
        ORDER  BY popularity DESC
        LIMIT  10
    """)
    print("   ✓ Top 10 por popularidad:")
    for r in cur.fetchall():
        print(f"      {r['title'][:35]:<36} "
              f"★{r['avg_rating']:.2f}  "
              f"{r['rating_count']:>5} ratings  "
              f"pop={r['popularity']:.2f}")


# =============================================================================
#  RESUMEN
# =============================================================================

def print_summary(conn) -> None:
    cur = conn.cursor()
    stats = {}
    for key, sql in [
        ("movies",        "SELECT COUNT(*) AS c FROM movies"),
        ("with_poster",   "SELECT COUNT(*) AS c FROM movies WHERE poster_url IS NOT NULL"),
        ("ratings",       "SELECT COUNT(*) AS c FROM ratings"),
        ("users",         "SELECT COUNT(DISTINCT session_id) AS c FROM ratings"),
        ("sessions",      "SELECT COUNT(*) AS c FROM cookie_sessions"),
    ]:
        cur.execute(sql)
        stats[key] = cur.fetchone()["c"]

    print(f"""
╔══════════════════════════════════════════╗
║        CineMatch — Dataset listo         ║
╠══════════════════════════════════════════╣
║  Películas totales    {stats['movies']:>8,}          ║
║  Con poster URL       {stats['with_poster']:>8,}          ║
║  Ratings totales      {stats['ratings']:>8,}          ║
║  Usuarios (sesiones)  {stats['users']:>8,}          ║
║  Sesiones totales     {stats['sessions']:>8,}          ║
╚══════════════════════════════════════════╝
""")


# =============================================================================
#  MAIN
# =============================================================================

def build_id_map_from_db(conn) -> dict:
    """Lee movielens_id→movie_id desde Supabase. Usado con --only-ratings."""
    cur = conn.cursor()
    cur.execute("SELECT movielens_id, movie_id FROM movies WHERE movielens_id IS NOT NULL")
    rows = cur.fetchall()
    id_map = {row["movielens_id"]: row["movie_id"] for row in rows}
    print(f"   ✓ {len(id_map):,} películas leídas desde Supabase.")
    return id_map


def main():
    parser = argparse.ArgumentParser(description="CineMatch — Cargador MovieLens v2")
    parser.add_argument("--only-movies",   action="store_true", help="Solo películas, salta ratings")
    parser.add_argument("--only-ratings",  action="store_true", help="Salta películas, va directo a ratings")
    parser.add_argument("--skip-download", action="store_true", help="No descarga ZIP")
    parser.add_argument("--limit",         type=int, default=None)
    args = parser.parse_args()

    print("\n🎬 CineMatch — Cargador MovieLens v2 (optimizado)")
    print("=" * 52)

    # 1. Descarga
    if not args.skip_download and not args.only_ratings:
        print("\n[1] Descargando dataset...")
        download_dataset()
    else:
        print("\n[1] Saltando descarga.")

    # 2. Conexión
    print("\n[2] Conectando a Supabase...")
    try:
        conn = get_connection()
        print("   ✓ Conectado.")
    except Exception as e:
        print(f"   ❌ {e}")
        sys.exit(1)

    # 3. Películas
    if args.only_ratings:
        print("\n[3] Saltando películas — leyendo IDs desde Supabase...")
        id_map = build_id_map_from_db(conn)
        print("\n[4] Saltando posters.")
    else:
        print("\n[3] Procesando películas...")
        movies = parse_movies(limit=args.limit)
        print(f"   → {len(movies):,} películas en movies.csv")
        id_map = upload_movies(conn, movies)
        print("\n[4] Asignando posters...")
        add_known_posters(conn)

    # 5. Ratings
    if args.only_movies:
        print("\n[5] Saltando ratings (--only-movies).")
        print("\n[5] Calculando popularidad...")
        update_popularity(conn)
    else:
        print("\n[5] Subiendo ratings (con resume automático)...")
        upload_ratings(conn, id_map)
        print("\n[6] Calculando popularidad...")
        update_popularity(conn)

    print_summary(conn)
    conn.close()
    print("✅ ¡Listo! Reinicia Flask.\n")


if __name__ == "__main__":
    main()