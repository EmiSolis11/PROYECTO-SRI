"""
CineMatch — recommender/content.py
====================================
Filtrado basado en contenido usando TF-IDF sobre sinopsis
y codificación One-Hot sobre géneros.

Uso desde app.py:
    from recommender.content import ContentRecommender
    cr = ContentRecommender()
    cr.fit(movies)                        # lista de dicts de películas
    similar = cr.recommend(movie_id, n=10)
    by_taste = cr.recommend_by_vector(taste_vector, n=10)
"""

import json
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.preprocessing import MultiLabelBinarizer

from recommender.clustering import GENRE_KEYS


class ContentRecommender:
    """
    Motor de recomendación basado en contenido.
    Combina TF-IDF de sinopsis + One-Hot de géneros
    en un vector híbrido ponderado.
    """

    # Pesos de cada componente en el vector final
    WEIGHT_TFIDF  = 0.5   # sinopsis
    WEIGHT_GENRES = 0.5   # géneros One-Hot

    def __init__(self):
        self.movies       = []          # lista de dicts
        self.movie_index  = {}          # movie_id → índice en self.movies
        self.tfidf_matrix = None        # shape (N, vocab)
        self.genre_matrix = None        # shape (N, len(GENRE_KEYS))
        self.hybrid_matrix= None        # shape (N, combinado)
        self._tfidf       = TfidfVectorizer(
            max_features=5000,
            stop_words="english",
            ngram_range=(1, 2),
            min_df=1,
        )
        self._mlb = MultiLabelBinarizer(classes=GENRE_KEYS)
        self.fitted = False

    # ── FIT ──────────────────────────────────────────────────────────────────

    def fit(self, movies: list[dict]) -> None:
        """
        Entrena el modelo con la lista de películas.
        movies: lista de dicts con keys movie_id, title, synopsis, genres.
        """
        if not movies:
            return

        self.movies = movies
        self.movie_index = {m["movie_id"]: i for i, m in enumerate(movies)}

        # 1. TF-IDF sobre sinopsis (+ título para películas sin sinopsis)
        corpus = []
        for m in movies:
            text = " ".join(filter(None, [
                m.get("title", ""),
                m.get("synopsis", ""),
                m.get("director", ""),
            ]))
            corpus.append(text.lower() if text.strip() else "unknown")

        tfidf_raw = self._tfidf.fit_transform(corpus).toarray()

        # 2. One-Hot de géneros
        genre_lists = []
        for m in movies:
            g = m.get("genres", [])
            if isinstance(g, str):
                try:    g = json.loads(g)
                except: g = []
            genre_lists.append([x for x in g if x in GENRE_KEYS])

        self._mlb.fit([GENRE_KEYS])
        genre_raw = self._mlb.transform(genre_lists).astype(np.float32)

        # 3. Normalización L2 de cada componente
        def l2(m):
            norms = np.linalg.norm(m, axis=1, keepdims=True)
            norms[norms == 0] = 1
            return m / norms

        self.tfidf_matrix  = l2(tfidf_raw.astype(np.float32))
        self.genre_matrix  = l2(genre_raw)

        # 4. Vector híbrido ponderado
        self.hybrid_matrix = np.hstack([
            self.tfidf_matrix  * self.WEIGHT_TFIDF,
            self.genre_matrix  * self.WEIGHT_GENRES,
        ])

        self.fitted = True

    # ── RECOMENDAR POR PELÍCULA SIMILAR ──────────────────────────────────────

    def recommend(self, movie_id: int, n: int = 10,
                  exclude_ids: list = None) -> list[dict]:
        """
        Devuelve las N películas más similares a movie_id.
        Usa similitud coseno sobre el vector híbrido.
        """
        if not self.fitted or movie_id not in self.movie_index:
            return []

        idx      = self.movie_index[movie_id]
        query    = self.hybrid_matrix[idx].reshape(1, -1)
        sims     = cosine_similarity(query, self.hybrid_matrix)[0]
        sims[idx]= -1   # excluir la película misma

        exclude = set(exclude_ids or [])
        ranked  = np.argsort(sims)[::-1]

        results = []
        for i in ranked:
            m = self.movies[i]
            if m["movie_id"] in exclude:
                continue
            results.append({**m, "content_score": round(float(sims[i]), 4)})
            if len(results) >= n:
                break
        return results

    # ── RECOMENDAR POR TASTE VECTOR ───────────────────────────────────────────

    def recommend_by_vector(self, taste_vector: dict, n: int = 10,
                            exclude_ids: list = None) -> list[dict]:
        """
        Recomienda películas basándose en el taste_vector del usuario.
        Convierte el taste_vector en un vector One-Hot de géneros
        y calcula similitud coseno contra el genre_matrix.
        """
        if not self.fitted:
            return []

        # Construir query vector en el espacio de géneros
        genre_query = np.array(
            [taste_vector.get(g, 0.0) for g in GENRE_KEYS],
            dtype=np.float32
        ).reshape(1, -1)

        # Normalizar
        norm = np.linalg.norm(genre_query)
        if norm > 0:
            genre_query /= norm

        # Solo usar la parte de géneros del hybrid_matrix para esta query
        # (el taste_vector no tiene componente TF-IDF)
        genre_part = self.hybrid_matrix[:, -len(GENRE_KEYS):]
        sims = cosine_similarity(genre_query, genre_part)[0]

        exclude = set(exclude_ids or [])
        ranked  = np.argsort(sims)[::-1]

        results = []
        for i in ranked:
            m = self.movies[i]
            if m["movie_id"] in exclude:
                continue
            results.append({**m, "content_score": round(float(sims[i]), 4)})
            if len(results) >= n:
                break
        return results

    # ── SIMILITUD ENTRE DOS PELÍCULAS (para películas nuevas) ─────────────────

    def similarity(self, movie_id_a: int, movie_id_b: int) -> float:
        """
        Devuelve la similitud coseno entre dos películas.
        Útil para el cold-start de ítems nuevos sin ratings.
        """
        if not self.fitted:
            return 0.0
        if movie_id_a not in self.movie_index or movie_id_b not in self.movie_index:
            return 0.0
        ia = self.movie_index[movie_id_a]
        ib = self.movie_index[movie_id_b]
        a  = self.hybrid_matrix[ia].reshape(1, -1)
        b  = self.hybrid_matrix[ib].reshape(1, -1)
        return float(cosine_similarity(a, b)[0][0])


# ─────────────────────────────────────────────────────────────────────────────
#  SINGLETON — una instancia compartida por toda la app Flask
# ─────────────────────────────────────────────────────────────────────────────

_recommender_instance: ContentRecommender | None = None


def get_content_recommender() -> ContentRecommender:
    """Devuelve la instancia singleton del ContentRecommender."""
    global _recommender_instance
    if _recommender_instance is None:
        _recommender_instance = ContentRecommender()
    return _recommender_instance


def fit_from_db(conn) -> ContentRecommender:
    """
    Carga todas las películas de Supabase y entrena el modelo.
    Llamar una vez al arrancar Flask (en app.py).

    Ejemplo en app.py:
        from recommender.content import fit_from_db
        with app.app_context():
            fit_from_db(get_db())
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT movie_id, title, year, genres, synopsis,
               director, avg_rating, poster_url
        FROM   movies
        ORDER  BY movie_id
    """)
    movies = [dict(r) for r in cur.fetchall()]

    cr = get_content_recommender()
    cr.fit(movies)
    print(f"   ✓ ContentRecommender entrenado con {len(movies)} películas")
    return cr