"""
CineMatch — recommender/hybrid.py
====================================
Motor híbrido que combina:
  1. Colaborativo por cluster   (quién es similar a ti)
  2. Basado en contenido        (qué películas se parecen a tus gustos)
  3. Boosting por frescura      (películas recientes +bonus)
  4. Boosting por popularidad   (las más vistas en tu país)

Resultado final: lista rankeada de películas con score, fuente y explicación.
"""

import json
import numpy as np
from dataclasses import dataclass, field


@dataclass
class RecommendedMovie:
    movie_id:    int
    title:       str
    year:        int | None
    genres:      list
    poster_url:  str | None
    avg_rating:  float
    director:    str | None

    # Scores individuales
    collab_score:  float = 0.0
    content_score: float = 0.0
    boost_score:   float = 0.0

    # Score final (calculado por HybridEngine)
    final_score:   float = 0.0

    # Fuente principal de la recomendación
    source:        str   = "popular"      # collaborative | content | popular

    # Texto explicativo para el usuario (caja blanca)
    explanation:   str   = ""


class HybridEngine:
    """
    Motor de recomendación híbrido.

    Pesos configurables:
        ALPHA  → peso del score colaborativo (cluster)
        BETA   → peso del score basado en contenido
        GAMMA  → peso del boost (frescura + popularidad)
    """

    ALPHA = 0.50   # colaborativo
    BETA  = 0.35   # contenido
    GAMMA = 0.15   # boosting

    # Bonus de frescura por año de producción
    FRESHNESS_BONUS = {
        2026: 0.25,
        2025: 0.20,
        2024: 0.12,
        2023: 0.08,
        2022: 0.05,
    }

    def __init__(self, conn, content_recommender=None):
        self.conn = conn
        self.cr   = content_recommender   # instancia de ContentRecommender

    # ── ENTRADA PRINCIPAL ─────────────────────────────────────────────────────

    def recommend(self, session_id: str, limit: int = 15,
                  country_code: str = "") -> list[RecommendedMovie]:
        """
        Genera la lista de recomendaciones híbridas para una sesión.
        """
        # 1. Datos de la sesión
        sess = self._get_session(session_id)
        if not sess:
            return self._fallback_popular(limit, country_code)

        taste_vector = sess.get("taste_vector") or {}
        if isinstance(taste_vector, str):
            taste_vector = json.loads(taste_vector)

        cluster_id   = sess.get("cluster_id")
        rated_ids    = self._get_rated_ids(session_id)

        # 2. Candidatos de cada fuente
        collab_movies  = self._collaborative_candidates(cluster_id, rated_ids, limit * 2)
        content_movies = self._content_candidates(taste_vector, rated_ids, limit * 2)

        # 3. Merge y score final
        merged = self._merge(collab_movies, content_movies, rated_ids)

        # 4. Boost
        country_views  = self._get_country_views(country_code) if country_code else {}
        merged         = self._apply_boost(merged, country_views)

        # 5. Rank
        merged.sort(key=lambda m: m.final_score, reverse=True)

        # 6. Explicación
        for m in merged:
            m.explanation = self._build_explanation(m, taste_vector)

        return merged[:limit]

    # ── COLABORATIVO POR CLUSTER ──────────────────────────────────────────────

    def _collaborative_candidates(self, cluster_id, rated_ids, limit) -> list[dict]:
        if not cluster_id:
            return []
        cur = self.conn.cursor()
        cur.execute("""
            SELECT m.movie_id, m.title, m.year, m.genres, m.poster_url,
                   m.avg_rating, m.director, m.views_by_country,
                   v.avg_score_in_cluster AS collab_score
            FROM   v_movie_cluster_affinity v
            JOIN   movies m ON m.movie_id = v.movie_id
            WHERE  v.cluster_id = %s
              AND  m.movie_id != ALL(%s)
              AND  m.poster_url IS NOT NULL
              AND  v.avg_score_in_cluster >= 3.0
            ORDER  BY v.avg_score_in_cluster DESC, v.raters_in_cluster DESC
            LIMIT  %s
        """, (cluster_id, rated_ids or [0], limit))
        return [dict(r) for r in cur.fetchall()]

    # ── CONTENIDO ─────────────────────────────────────────────────────────────

    def _content_candidates(self, taste_vector, rated_ids, limit) -> list[dict]:
        if not self.cr or not self.cr.fitted or not taste_vector:
            return self._popular_candidates(rated_ids, limit)

        recs = self.cr.recommend_by_vector(taste_vector, n=limit, exclude_ids=rated_ids)
        return recs

    def _popular_candidates(self, rated_ids, limit) -> list[dict]:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT movie_id, title, year, genres, poster_url,
                   avg_rating, director, views_by_country
            FROM   movies
            WHERE  movie_id != ALL(%s) AND poster_url IS NOT NULL
            ORDER  BY avg_rating DESC, rating_count DESC
            LIMIT  %s
        """, (rated_ids or [0], limit))
        return [dict(r) for r in cur.fetchall()]

    # ── MERGE ─────────────────────────────────────────────────────────────────

    def _merge(self, collab: list, content: list,
               rated_ids: list) -> list[RecommendedMovie]:
        """
        Combina listas de candidatos evitando duplicados.
        Asigna scores individuales normalizados.
        """
        seen    = set(rated_ids)
        pool    = {}   # movie_id → RecommendedMovie

        # Normalizar scores colaborativos al rango [0, 1]
        collab_scores = [m.get("collab_score", 0) for m in collab]
        c_max = max(collab_scores) if collab_scores else 1
        c_max = c_max or 1

        for m in collab:
            mid = m["movie_id"]
            if mid in seen:
                continue
            seen.add(mid)
            rm = self._to_rec(m)
            rm.collab_score = m.get("collab_score", 0) / c_max
            rm.source = "collaborative"
            pool[mid] = rm

        # Normalizar content scores
        cont_scores = [m.get("content_score", 0) for m in content]
        t_max = max(cont_scores) if cont_scores else 1
        t_max = t_max or 1

        for m in content:
            mid = m["movie_id"]
            if mid in seen:
                continue
            seen.add(mid)
            rm = self._to_rec(m)
            rm.content_score = m.get("content_score", 0) / t_max
            rm.source = "content"
            pool[mid] = rm

        # Para películas que están en ambas listas, combinar scores
        for m in content:
            mid = m["movie_id"]
            if mid in pool and pool[mid].source == "collaborative":
                pool[mid].content_score = m.get("content_score", 0) / t_max

        return list(pool.values())

    # ── BOOSTING ──────────────────────────────────────────────────────────────

    def _apply_boost(self, movies: list[RecommendedMovie],
                     country_views: dict) -> list[RecommendedMovie]:
        """
        Aplica multiplicadores de boost y calcula final_score.

        final_score = ALPHA * collab + BETA * content + GAMMA * boost
        """
        # Normalizar country_views para el boost de popularidad
        view_vals = list(country_views.values()) if country_views else [1]
        v_max     = max(view_vals) if view_vals else 1

        for m in movies:
            # Boost 1: Frescura
            freshness = self.FRESHNESS_BONUS.get(m.year, 0.0)

            # Boost 2: Popularidad en el país
            popularity = 0.0
            if country_views and m.movie_id in country_views:
                popularity = country_views[m.movie_id] / v_max

            # Boost 3: Rating alto (bonus si avg_rating > 4.0)
            rating_bonus = max(0.0, (m.avg_rating - 4.0) / 5.0) if m.avg_rating else 0.0

            m.boost_score  = min(1.0, freshness + popularity * 0.5 + rating_bonus * 0.3)

            m.final_score  = (
                self.ALPHA * m.collab_score +
                self.BETA  * m.content_score +
                self.GAMMA * m.boost_score
            )

        return movies

    # ── EXPLICABILIDAD (CAJA BLANCA) ──────────────────────────────────────────

    def _build_explanation(self, m: RecommendedMovie, taste_vector: dict) -> str:
        """
        Genera una explicación en lenguaje natural para cada recomendación.
        """
        g = m.genres[:2] if m.genres else []
        genres_str = " y ".join(g) if g else "este género"

        if m.source == "collaborative":
            pct = round(m.collab_score * 100)
            return (
                f"A personas con gustos similares a los tuyos les encantó esta película "
                f"(puntuación promedio {m.avg_rating:.1f}★ en tu grupo)"
            )

        if m.source == "content":
            # ¿Qué géneros del taste_vector coinciden?
            matching = [g for g in m.genres if taste_vector.get(g, 0) > 0.3]
            if matching:
                return f"Coincide con tus géneros favoritos: {', '.join(matching[:2])}"
            if m.director and any(m.director in str(v) for v in taste_vector.values()):
                return f"Es del director {m.director}, que sueles disfrutar"
            return f"Tiene alta afinidad con tu perfil de gustos en {genres_str}"

        if m.boost_score > 0.15 and m.year and m.year >= 2024:
            return f"Estreno reciente ({m.year}) muy popular en tu región"

        return f"Una de las más valoradas en {genres_str}"

    # ── FALLBACK ──────────────────────────────────────────────────────────────

    def _fallback_popular(self, limit: int, country_code: str) -> list[RecommendedMovie]:
        """Recomendaciones cuando no hay sesión ni perfil (cold-start total)."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT movie_id, title, year, genres, poster_url,
                   avg_rating, director, views_by_country,
                   COALESCE((views_by_country->>%s)::INTEGER, 0) AS country_views
            FROM   movies
            WHERE  poster_url IS NOT NULL
            ORDER  BY COALESCE((views_by_country->>%s)::INTEGER, 0) DESC,
                      avg_rating DESC
            LIMIT  %s
        """, (country_code, country_code, limit))
        rows = [dict(r) for r in cur.fetchall()]
        result = []
        for m in rows:
            rm = self._to_rec(m)
            rm.final_score = m.get("avg_rating", 0) / 5.0
            rm.source      = "popular"
            rm.explanation = "Tendencia en tu región"
            result.append(rm)
        return result

    # ── HELPERS ───────────────────────────────────────────────────────────────

    def _get_session(self, session_id: str) -> dict | None:
        cur = self.conn.cursor()
        cur.execute("""
            SELECT cs.taste_vector, cs.country_code, sc.cluster_id
            FROM   cookie_sessions cs
            LEFT   JOIN session_cluster sc ON sc.session_id = cs.session_id
            WHERE  cs.session_id = %s
        """, (session_id,))
        row = cur.fetchone()
        return dict(row) if row else None

    def _get_rated_ids(self, session_id: str) -> list[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT movie_id FROM ratings WHERE session_id=%s", (session_id,))
        return [r["movie_id"] for r in cur.fetchall()]

    def _get_country_views(self, country_code: str) -> dict:
        """Devuelve {movie_id: views} para el país dado."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT movie_id,
                   COALESCE((views_by_country->>%s)::INTEGER, 0) AS views
            FROM   movies
            WHERE  (views_by_country->>%s) IS NOT NULL
        """, (country_code, country_code))
        return {r["movie_id"]: r["views"] for r in cur.fetchall()}

    def _to_rec(self, m: dict) -> RecommendedMovie:
        g = m.get("genres", [])
        if isinstance(g, str):
            try:    g = json.loads(g)
            except: g = []
        return RecommendedMovie(
            movie_id   = m["movie_id"],
            title      = m.get("title", ""),
            year       = m.get("year"),
            genres     = g,
            poster_url = m.get("poster_url"),
            avg_rating = float(m.get("avg_rating") or 0),
            director   = m.get("director"),
        )