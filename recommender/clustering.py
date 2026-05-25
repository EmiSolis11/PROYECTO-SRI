"""
CineMatch — recommender/clustering.py
======================================
Motor de clustering K-Means para agrupar usuarios por preferencias.

Flujo completo:
  1. pull_taste_vectors()   → lee todos los taste_vectors de Supabase
  2. run_kmeans()           → entrena K-Means con scikit-learn
  3. save_centroids()       → guarda los nuevos centroides en la tabla clusters
  4. reassign_all()         → reasigna cada sesión al centroide más cercano
  5. assign_session()       → asigna UNA sesión nueva (llamado desde app.py)

Uso desde Flask (app.py):
    from recommender.clustering import assign_session, maybe_recalculate
    assign_session(session_id)       # al terminar el cold-start
    maybe_recalculate()              # después de cada INSERT en cookie_sessions

Uso como script independiente (forzar recálculo):
    python -m recommender.clustering
"""

import json
import numpy as np
from datetime import datetime, timezone
from sklearn.cluster import KMeans
from sklearn.preprocessing import normalize

# Géneros conocidos — definen las dimensiones del vector
# El orden importa: siempre el mismo para todos los vectores
GENRE_KEYS = [
    "Action", "Adventure", "Animation", "Biography", "Comedy",
    "Crime", "Documentary", "Drama", "Family", "Fantasy",
    "Foreign", "History", "Horror", "Music", "Mystery",
    "Romance", "Sci-Fi", "Sport", "Thriller", "War", "Western"
]

K_CLUSTERS   = 5     # número de clusters (ajustable)
RANDOM_STATE = 42    # reproducibilidad


# ─────────────────────────────────────────────────────────────────────────────
#  VECTORIZACIÓN
# ─────────────────────────────────────────────────────────────────────────────

def taste_vector_to_array(taste_vector: dict) -> np.ndarray:
    """
    Convierte el dict taste_vector de una sesión en un array NumPy
    de dimensión fija (len(GENRE_KEYS)).

    Ejemplo:
        {"Action": 0.9, "Drama": 0.4}
        → [0.9, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.4, 0.0, ...]
    """
    return np.array([taste_vector.get(g, 0.0) for g in GENRE_KEYS], dtype=np.float32)


def array_to_taste_vector(arr: np.ndarray) -> dict:
    """Convierte un array NumPy de vuelta a dict taste_vector."""
    return {g: round(float(v), 4) for g, v in zip(GENRE_KEYS, arr) if v > 0.001}


# ─────────────────────────────────────────────────────────────────────────────
#  LECTURA DE DATOS
# ─────────────────────────────────────────────────────────────────────────────

def pull_taste_vectors(conn) -> tuple[list[str], np.ndarray]:
    """
    Lee todos los taste_vectors de sesiones que completaron el cold-start.
    Devuelve (session_ids, matrix) donde matrix tiene forma (N, len(GENRE_KEYS)).
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, taste_vector
        FROM   cookie_sessions
        WHERE  cold_start_done = TRUE
          AND  taste_vector != '{}'::jsonb
    """)
    rows = cur.fetchall()

    if not rows:
        return [], np.empty((0, len(GENRE_KEYS)), dtype=np.float32)

    session_ids = []
    vectors     = []

    for row in rows:
        tv = row["taste_vector"]
        if isinstance(tv, str):
            tv = json.loads(tv)
        session_ids.append(str(row["session_id"]))
        vectors.append(taste_vector_to_array(tv))

    matrix = np.vstack(vectors)   # shape: (N, 21)
    return session_ids, matrix


# ─────────────────────────────────────────────────────────────────────────────
#  K-MEANS
# ─────────────────────────────────────────────────────────────────────────────

def run_kmeans(matrix: np.ndarray, k: int = K_CLUSTERS) -> KMeans:
    """
    Entrena K-Means sobre la matriz de vectores.
    Normaliza los vectores antes de clustering (coseno ≈ euclidiano sobre L2).
    """
    if len(matrix) < k:
        # Si hay menos sesiones que clusters, reducir K temporalmente
        k = max(1, len(matrix))

    # Normalización L2 → similitud coseno ≈ distancia euclidiana
    matrix_norm = normalize(matrix, norm="l2")

    model = KMeans(
        n_clusters=k,
        init="k-means++",    # inicialización inteligente (convergencia más rápida)
        n_init=10,
        max_iter=300,
        random_state=RANDOM_STATE,
        algorithm="lloyd"
    )
    model.fit(matrix_norm)
    return model


# ─────────────────────────────────────────────────────────────────────────────
#  GUARDAR CENTROIDES EN SUPABASE
# ─────────────────────────────────────────────────────────────────────────────

def get_current_version(conn) -> int:
    """Devuelve la versión activa más reciente de los clusters."""
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(version), 0) AS v FROM clusters WHERE is_active = TRUE")
    row = cur.fetchone()
    return row["v"] if row else 0


def save_centroids(conn, model: KMeans, member_counts: list[int]) -> int:
    """
    Desactiva los clusters anteriores e inserta los nuevos centroides.
    Devuelve la nueva versión.
    """
    cur     = conn.cursor()
    version = get_current_version(conn) + 1

    # Desactivar todos los clusters actuales
    cur.execute("UPDATE clusters SET is_active = FALSE")

    # Insertar los nuevos centroides
    for i, centroid in enumerate(model.cluster_centers_):
        centroid_dict = array_to_taste_vector(centroid)
        cur.execute(
            """INSERT INTO clusters
                   (cluster_number, centroid_vector, member_count, version, is_active)
               VALUES (%s, %s::jsonb, %s, %s, TRUE)""",
            (i, json.dumps(centroid_dict), member_counts[i], version)
        )

    conn.commit()
    print(f"   ✓ {len(model.cluster_centers_)} centroides guardados (versión {version})")
    return version


# ─────────────────────────────────────────────────────────────────────────────
#  REASIGNACIÓN
# ─────────────────────────────────────────────────────────────────────────────

def reassign_all(conn, session_ids: list[str],
                 matrix: np.ndarray, model: KMeans, version: int) -> None:
    """
    Reasigna cada sesión al cluster más cercano usando el modelo entrenado.
    Actualiza la tabla session_cluster.
    """
    cur          = conn.cursor()
    matrix_norm  = normalize(matrix, norm="l2")
    labels       = model.predict(matrix_norm)
    distances    = _euclidean_distances_to_assigned(matrix_norm, model)

    # Obtener los cluster_ids reales de la BD para esta versión
    cur.execute(
        "SELECT cluster_id, cluster_number FROM clusters WHERE version=%s AND is_active=TRUE",
        (version,)
    )
    cluster_map = {row["cluster_number"]: row["cluster_id"] for row in cur.fetchall()}

    for sid, label, dist in zip(session_ids, labels, distances):
        cluster_id = cluster_map.get(int(label))
        if cluster_id is None:
            continue
        cur.execute(
            """INSERT INTO session_cluster
                   (session_id, cluster_id, distance_to_centroid, cluster_version)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (session_id) DO UPDATE SET
                   cluster_id           = EXCLUDED.cluster_id,
                   distance_to_centroid = EXCLUDED.distance_to_centroid,
                   cluster_version      = EXCLUDED.cluster_version,
                   assigned_at          = NOW()""",
            (sid, cluster_id, float(dist), version)
        )

    conn.commit()
    print(f"   ✓ {len(session_ids)} sesiones reasignadas")


def _euclidean_distances_to_assigned(matrix_norm: np.ndarray, model: KMeans) -> np.ndarray:
    """Calcula la distancia euclidiana de cada punto a su centroide asignado."""
    labels    = model.labels_
    centroids = model.cluster_centers_
    dists     = np.linalg.norm(matrix_norm - centroids[labels], axis=1)
    return dists


# ─────────────────────────────────────────────────────────────────────────────
#  ASIGNAR UNA SESIÓN INDIVIDUAL (llamado en tiempo real desde Flask)
# ─────────────────────────────────────────────────────────────────────────────

def assign_session(conn, session_id: str) -> int | None:
    """
    Asigna una sesión individual al centroide activo más cercano.
    Llama a la función SQL assign_session_to_cluster() definida en el schema.
    Devuelve el cluster_id asignado o None si no hay clusters activos.
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT assign_session_to_cluster(%s) AS cluster_id",
            (session_id,)
        )
        row = cur.fetchone()
        conn.commit()
        return row["cluster_id"] if row else None
    except Exception as e:
        print(f"[clustering] Error asignando sesión {session_id}: {e}")
        conn.rollback()
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  RECÁLCULO COMPLETO (cada 100 usuarios nuevos)
# ─────────────────────────────────────────────────────────────────────────────

def full_recalculate(conn) -> dict:
    """
    Ejecuta el pipeline completo de recálculo K-Means:
      1. Lee vectores de Supabase
      2. Entrena K-Means
      3. Guarda nuevos centroides
      4. Reasigna todas las sesiones

    Devuelve un dict con el resumen del proceso.
    """
    print("\n🔄 CineMatch — Recalculando clusters K-Means...")
    start = datetime.now(timezone.utc)

    # 1. Leer datos
    session_ids, matrix = pull_taste_vectors(conn)
    n = len(session_ids)
    print(f"   → {n} sesiones con perfil completo")

    if n < 2:
        print(" !  Menos de 2 sesiones — se mantienen los clusters existentes")
        return {"status": "skipped", "reason": "not_enough_data", "sessions": n}

    # 2. K-Means
    k = min(K_CLUSTERS, n)
    print(f"   → Entrenando K-Means con K={k}...")
    model = run_kmeans(matrix, k=k)

    # Contar miembros por cluster
    labels        = model.labels_
    member_counts = [int(np.sum(labels == i)) for i in range(k)]
    print(f"   → Distribución: {dict(enumerate(member_counts))}")

    # 3. Guardar centroides
    version = save_centroids(conn, model, member_counts)

    # 4. Reasignar
    reassign_all(conn, session_ids, matrix, model, version)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"  Listo en {elapsed:.1f}s\n")

    return {
        "status":        "ok",
        "sessions":      n,
        "k":             k,
        "version":       version,
        "member_counts": member_counts,
        "elapsed_s":     round(elapsed, 2),
    }


def maybe_recalculate(conn) -> bool:
    """
    Verifica si hay un job de recálculo pendiente en cluster_job_log.
    Si lo hay, lo ejecuta y actualiza el estado.
    Devuelve True si se ejecutó, False si no había nada pendiente.

    Llamar esto al final de cada POST /api/session (después de crear la sesión).
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT job_id FROM cluster_job_log WHERE status='pending' ORDER BY job_id LIMIT 1"
    )
    job = cur.fetchone()
    if not job:
        return False

    job_id = job["job_id"]
    cur.execute(
        "UPDATE cluster_job_log SET status='running', started_at=NOW() WHERE job_id=%s",
        (job_id,)
    )
    conn.commit()

    try:
        result = full_recalculate(conn)
        cur.execute(
            "UPDATE cluster_job_log SET status='done', finished_at=NOW() WHERE job_id=%s",
            (job_id,)
        )
        conn.commit()
        return True
    except Exception as e:
        cur.execute(
            "UPDATE cluster_job_log SET status='error', error_message=%s WHERE job_id=%s",
            (str(e), job_id)
        )
        conn.commit()
        print(f"[clustering] Error en recálculo: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  ESTADÍSTICAS (para debugging / panel admin)
# ─────────────────────────────────────────────────────────────────────────────

def cluster_stats(conn) -> list[dict]:
    """
    Devuelve estadísticas de cada cluster activo:
    tamaño, géneros dominantes y avg_rating de películas afines.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT
            c.cluster_id,
            c.cluster_number,
            c.centroid_vector,
            c.member_count,
            c.version,
            c.computed_at,
            COUNT(sc.session_id) AS actual_members
        FROM clusters c
        LEFT JOIN session_cluster sc ON sc.cluster_id = c.cluster_id
        WHERE c.is_active = TRUE
        GROUP BY c.cluster_id
        ORDER BY c.cluster_number
    """)
    rows = cur.fetchall()
    stats = []
    for row in rows:
        cv = row["centroid_vector"]
        if isinstance(cv, str):
            cv = json.loads(cv)
        # Top 3 géneros del centroide
        top_genres = sorted(cv.items(), key=lambda x: x[1], reverse=True)[:3]
        stats.append({
            "cluster_id":     row["cluster_id"],
            "cluster_number": row["cluster_number"],
            "member_count":   row["actual_members"],
            "version":        row["version"],
            "top_genres":     [g for g, _ in top_genres],
            "centroid":       cv,
        })
    return stats


# ─────────────────────────────────────────────────────────────────────────────
#  PUNTO DE ENTRADA (ejecución directa para forzar recálculo)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent))
    from db import get_connection

    conn = get_connection()
    result = full_recalculate(conn)
    print(json.dumps(result, indent=2))

    print("\n📊 Estadísticas por cluster:")
    for s in cluster_stats(conn):
        print(f"  Cluster {s['cluster_number']}: "
              f"{s['member_count']} miembros | "
              f"Géneros: {', '.join(s['top_genres'])}")
    conn.close()