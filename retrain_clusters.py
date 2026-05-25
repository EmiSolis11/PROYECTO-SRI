"""
CineMatch — retrain_clusters.py
================================
Reentrena el modelo K-Means con los datos actuales de Supabase.
K = 5% del total de usuarios (mínimo 2, máximo 50).

Uso:
    python retrain_clusters.py
    python retrain_clusters.py --dry-run   → solo muestra estadísticas, no guarda
"""

import sys
import json
import argparse
import numpy as np
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from db import get_connection

# ── Géneros — mismas dimensiones que clustering.py ────────────────────────────
GENRE_KEYS = [
    "Action", "Adventure", "Animation", "Biography", "Comedy",
    "Crime", "Documentary", "Drama", "Family", "Fantasy",
    "Foreign", "History", "Horror", "Music", "Mystery",
    "Romance", "Sci-Fi", "Sport", "Thriller", "War", "Western"
]

RANDOM_STATE = 42


# =============================================================================
#  PASO 1 — Construir taste_vectors desde los ratings (ingeniería inversa)
#            Para usuarios demo que NO tienen taste_vector propio,
#            lo inferimos desde sus ratings × géneros de las películas.
# =============================================================================

def build_taste_vectors_from_ratings(conn) -> dict:
    """
    Para cada sesión que tiene ratings pero no tiene taste_vector definido,
    construye el vector infiriendo los géneros de las películas calificadas.

    Fórmula:
        peso(género) = Σ(score × ocurrencia_género) / Σ(score)
    Normalizado a [0, 1].

    Devuelve {session_id: {genre: weight}}
    """
    cur = conn.cursor()
    print("   📖 Leyendo ratings y géneros...")

    # Traer todos los ratings con los géneros de la película
    cur.execute("""
        SELECT r.session_id, r.score, m.genres
        FROM   ratings r
        JOIN   movies  m ON m.movie_id = r.movie_id
    """)
    rows = cur.fetchall()
    print(f"   → {len(rows):,} ratings leídos")

    # Acumular pesos por sesión
    session_genre_sum   = {}   # {session_id: {genre: sum_score}}
    session_score_total = {}   # {session_id: total_score}

    for row in rows:
        sid   = str(row["session_id"])
        score = float(row["score"])
        genres = row["genres"]
        if isinstance(genres, str):
            try:    genres = json.loads(genres)
            except: genres = []

        if sid not in session_genre_sum:
            session_genre_sum[sid]   = {}
            session_score_total[sid] = 0.0

        session_score_total[sid] += score
        for genre in genres:
            if genre in GENRE_KEYS:
                session_genre_sum[sid][genre] = (
                    session_genre_sum[sid].get(genre, 0.0) + score
                )

    # Normalizar a [0, 1]
    taste_vectors = {}
    for sid, genre_sums in session_genre_sum.items():
        total = session_score_total[sid]
        if total == 0:
            continue
        tv = {}
        for genre, s in genre_sums.items():
            weight = round(s / total, 4)
            if weight > 0.01:
                tv[genre] = weight
        if tv:
            taste_vectors[sid] = tv

    print(f"   → {len(taste_vectors):,} taste_vectors construidos desde ratings")
    return taste_vectors


def update_taste_vectors_in_db(conn, taste_vectors: dict) -> None:
    """
    Actualiza en Supabase el taste_vector de las sesiones que lo tienen vacío.
    No sobreescribe los que el usuario definió manualmente en el cold-start.
    """
    cur     = conn.cursor()
    updated = 0

    print(f"   💾 Actualizando taste_vectors en Supabase...")

    items = list(taste_vectors.items())
    for i in range(0, len(items), 500):
        batch = items[i:i+500]
        for sid, tv in batch:
            cur.execute(
                """
                UPDATE cookie_sessions
                SET    taste_vector = %s::jsonb
                WHERE  session_id   = %s
                  AND  (taste_vector = '{}'::jsonb OR taste_vector IS NULL)
                """,
                (json.dumps(tv), sid)
            )
            updated += cur.rowcount
        conn.commit()

    print(f"   ✓ {updated:,} sesiones actualizadas con taste_vector inferido")


# =============================================================================
#  PASO 2 — Leer todos los taste_vectors (propios + inferidos)
# =============================================================================

def pull_all_vectors(conn) -> tuple[list[str], np.ndarray]:
    """
    Lee TODOS los taste_vectors disponibles (cold-start + inferidos).
    Devuelve (session_ids, matrix) shape (N, 21).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, taste_vector
        FROM   cookie_sessions
        WHERE  taste_vector != '{}'::jsonb
          AND  taste_vector IS NOT NULL
    """)
    rows = cur.fetchall()

    session_ids = []
    vectors     = []

    for row in rows:
        tv = row["taste_vector"]
        if isinstance(tv, str):
            try:    tv = json.loads(tv)
            except: continue
        if not tv:
            continue

        vec = np.array([tv.get(g, 0.0) for g in GENRE_KEYS], dtype=np.float32)
        if vec.sum() == 0:
            continue

        session_ids.append(str(row["session_id"]))
        vectors.append(vec)

    matrix = np.vstack(vectors) if vectors else np.empty((0, len(GENRE_KEYS)))
    return session_ids, matrix


# =============================================================================
#  PASO 3 — K-Means con K = 5% de usuarios
# =============================================================================

def compute_k(n_users: int) -> int:
    """K = 5% de usuarios, mínimo 2, máximo 50."""
    k = max(2, min(50, round(n_users * 0.05)))
    return k


def run_kmeans(matrix: np.ndarray, k: int):
    """Entrena K-Means con normalización L2."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize

    matrix_norm = normalize(matrix, norm="l2")
    model = KMeans(
        n_clusters=k,
        init="k-means++",
        n_init=15,
        max_iter=500,
        random_state=RANDOM_STATE,
        algorithm="lloyd"
    )
    model.fit(matrix_norm)

    # Inercia = qué tan compactos son los clusters (menor = mejor)
    print(f"   → Inercia del modelo: {model.inertia_:.4f}")
    return model, matrix_norm


# =============================================================================
#  PASO 4 — Guardar centroides y reasignar sesiones
# =============================================================================

def array_to_taste_vector(arr: np.ndarray) -> dict:
    return {g: round(float(v), 4) for g, v in zip(GENRE_KEYS, arr) if v > 0.001}


def save_and_reassign(conn, model, matrix_norm: np.ndarray,
                      session_ids: list[str], k: int) -> int:
    """
    1. Desactiva clusters anteriores
    2. Inserta los nuevos centroides
    3. Reasigna cada sesión al cluster más cercano
    Devuelve la nueva versión.
    """
    cur = conn.cursor()

    # Nueva versión
    cur.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM clusters")
    version = cur.fetchone()["v"]

    # Desactivar anteriores
    cur.execute("UPDATE clusters SET is_active = FALSE")

    # Contar miembros por cluster
    labels        = model.labels_
    member_counts = [int(np.sum(labels == i)) for i in range(k)]

    # Insertar nuevos centroides
    cluster_id_map = {}   # cluster_number → cluster_id en BD
    for i, centroid in enumerate(model.cluster_centers_):
        centroid_dict = array_to_taste_vector(centroid)
        cur.execute(
            """
            INSERT INTO clusters
                (cluster_number, centroid_vector, member_count, version, is_active)
            VALUES (%s, %s::jsonb, %s, %s, TRUE)
            RETURNING cluster_id
            """,
            (i, json.dumps(centroid_dict), member_counts[i], version)
        )
        cluster_id_map[i] = cur.fetchone()["cluster_id"]

    conn.commit()
    print(f"   ✓ {k} centroides guardados (versión {version})")
    print(f"   → Distribución: { {i: member_counts[i] for i in range(k)} }")

    # Reasignar sesiones
    from sklearn.preprocessing import normalize
    distances = np.linalg.norm(
        matrix_norm - model.cluster_centers_[labels], axis=1
    )

    print(f"   🔄 Reasignando {len(session_ids):,} sesiones...")
    batch = []
    for sid, label, dist in zip(session_ids, labels, distances):
        cluster_id = cluster_id_map[int(label)]
        batch.append((sid, cluster_id, float(dist), version))

    for i in range(0, len(batch), 1000):
        cur.executemany(
            """
            INSERT INTO session_cluster
                (session_id, cluster_id, distance_to_centroid, cluster_version)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                cluster_id           = EXCLUDED.cluster_id,
                distance_to_centroid = EXCLUDED.distance_to_centroid,
                cluster_version      = EXCLUDED.cluster_version,
                assigned_at          = NOW()
            """,
            batch[i:i+1000]
        )
        conn.commit()

    print(f"   ✓ {len(session_ids):,} sesiones reasignadas")
    return version


# =============================================================================
#  RESUMEN DE CLUSTERS
# =============================================================================

def print_cluster_summary(conn) -> None:
    cur = conn.cursor()
    cur.execute("""
        SELECT
            c.cluster_number,
            c.member_count,
            c.centroid_vector,
            COUNT(sc.session_id) AS actual_members
        FROM clusters c
        LEFT JOIN session_cluster sc ON sc.cluster_id = c.cluster_id
        WHERE c.is_active = TRUE
        GROUP BY c.cluster_id
        ORDER BY c.cluster_number
    """)
    rows = cur.fetchall()

    print(f"\n{'─'*60}")
    print(f"  {'Cluster':<10} {'Miembros':>10}  {'Top géneros'}")
    print(f"{'─'*60}")

    for row in rows:
        cv = row["centroid_vector"]
        if isinstance(cv, str):
            cv = json.loads(cv)
        top = sorted(cv.items(), key=lambda x: x[1], reverse=True)[:3]
        top_str = ", ".join(f"{g}({v:.2f})" for g, v in top)
        print(f"  Cluster {row['cluster_number']:<3} "
              f"{row['actual_members']:>10,}  {top_str}")
    print(f"{'─'*60}\n")


# =============================================================================
#  MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="CineMatch — Reentrenar K-Means")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo muestra estadísticas, no guarda nada")
    args = parser.parse_args()

    print("\n🤖 CineMatch — Reentrenamiento K-Means")
    print("=" * 50)
    start = datetime.now(timezone.utc)

    # Conectar
    print("\n[1] Conectando a Supabase...")
    conn = get_connection()
    print("   ✓ Conectado.")

    # Construir taste_vectors desde ratings
    print("\n[2] Construyendo taste_vectors desde ratings...")
    taste_vectors = build_taste_vectors_from_ratings(conn)

    if not args.dry_run:
        update_taste_vectors_in_db(conn, taste_vectors)

    # Leer todos los vectores
    print("\n[3] Leyendo vectores para clustering...")
    session_ids, matrix = pull_all_vectors(conn)
    n = len(session_ids)
    print(f"   → {n:,} sesiones con vector válido")

    if n < 2:
        print("   ⚠️  Menos de 2 sesiones con datos. No se puede entrenar.")
        conn.close()
        return

    # Calcular K
    k = compute_k(n)
    print(f"\n[4] Calculando K...")
    print(f"   → Usuarios: {n:,}")
    print(f"   → K = 5% de {n:,} = {k} clusters")

    if args.dry_run:
        print("\n   [DRY RUN] No se guardará nada.")
        print(f"   Se entrenarían {k} clusters con {n:,} sesiones.")
        conn.close()
        return

    # Entrenar
    print(f"\n[5] Entrenando K-Means con K={k}...")
    model, matrix_norm = run_kmeans(matrix, k)

    # Guardar y reasignar
    print(f"\n[6] Guardando centroides y reasignando sesiones...")
    version = save_and_reassign(conn, model, matrix_norm, session_ids, k)

    # Resumen
    print(f"\n[7] Resumen de clusters (versión {version}):")
    print_cluster_summary(conn)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"✅ Reentrenamiento completado en {elapsed:.1f}s\n")
    conn.close()


if __name__ == "__main__":
    main()