"""
CineMatch — app.py
Servidor Flask principal.

Endpoints implementados en este Sprint:
    GET  /                      → sirve index.html
    GET  /api/ping              → health check (verifica BD y Mongo)
    POST /api/session           → crea o recupera sesión por cookie
    POST /api/cold-start        → guarda preferencias del formulario inicial
    GET  /api/movies/trending   → películas más vistas en el país del usuario
    GET  /api/movies/search     → búsqueda con corrección de texto
    GET  /api/movies/<id>       → detalle de una película
    POST /api/rate              → calificar una película (1-5 estrellas)
    POST /api/interact          → registrar click / scroll / detail_view
    GET  /api/recommend         → recomendaciones híbridas (cluster + contenido)
"""

import os
import uuid
import json
import requests as req_lib
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, request, jsonify, make_response,
                   render_template, g)
from flask_cors import CORS
from dotenv import load_dotenv

from db    import get_db, init_app as db_init_app, execute, query
from mongo import log_event, get_session_profile, get_trending_by_country
from recommender.clustering import assign_session, maybe_recalculate
from recommender.content    import get_content_recommender, fit_from_db
from recommender.hybrid     import HybridEngine

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURACIÓN
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-insecuro-cambiar")

CORS(app, supports_credentials=True)   # permite cookies desde el frontend
db_init_app(app)                       # registra close_db en teardown

@app.before_request
def load_content_model():
    """Entrena el modelo de contenido la primera vez que llega un request."""
    if not get_content_recommender().fitted:
        try:
            fit_from_db(get_db())
        except Exception as e:
            print(f"[content] Warning: {e}")

COOKIE_NAME     = "cm_session"
COOKIE_MAX_AGE  = 365 * 24 * 3600     # 1 año en segundos
GEO_TIMEOUT     = int(os.getenv("GEO_TIMEOUT_SECONDS", 3))


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def get_session_id() -> str | None:
    """Lee el UUID de sesión desde la cookie."""
    return request.cookies.get(COOKIE_NAME)


def require_session(f):
    """Decorador: devuelve 401 si no hay cookie de sesión válida."""
    @wraps(f)
    def decorated(*args, **kwargs):
        sid = get_session_id()
        if not sid:
            return jsonify({"error": "Sin sesión. Llama primero a /api/session"}), 401
        g.session_id = sid
        return f(*args, **kwargs)
    return decorated


def geolocate_ip(ip: str) -> dict:
    """
    Detecta país y ciudad desde la IP usando ip-api.com (gratis, sin API key).
    Devuelve {"country_code":"MX","country_name":"Mexico","city":"Guadalajara"}
    o dict vacío si falla.
    """
    if ip in ("127.0.0.1", "::1", "localhost"):
        return {"country_code": "MX", "country_name": "Mexico", "city": "Local"}
    try:
        r = req_lib.get(
            f"http://ip-api.com/json/{ip}?fields=status,country,countryCode,city",
            timeout=GEO_TIMEOUT
        )
        data = r.json()
        if data.get("status") == "success":
            return {
                "country_code": data.get("countryCode", ""),
                "country_name": data.get("country", ""),
                "city":         data.get("city", ""),
            }
    except Exception:
        pass
    return {}


def client_ip() -> str:
    """Obtiene la IP real del cliente considerando proxies."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def set_session_cookie(response, session_id: str):
    """Agrega la cookie de sesión a la respuesta."""
    response.set_cookie(
        COOKIE_NAME,
        value=str(session_id),
        max_age=COOKIE_MAX_AGE,
        httponly=True,          # no accesible desde JS (seguridad)
        samesite="Lax",         # protege contra CSRF básico
        secure=os.getenv("FLASK_ENV") == "production",
    )
    return response


def rows_to_list(rows) -> list:
    """Convierte RealDictRow de psycopg2 a lista de dicts JSON-serializables."""
    result = []
    for row in (rows or []):
        d = dict(row)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        result.append(d)
    return result


def row_to_dict(row) -> dict:
    if not row:
        return {}
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
    return d


# ─────────────────────────────────────────────────────────────────────────────
#  RUTAS — FRONTEND
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─────────────────────────────────────────────────────────────────────────────
#  RUTAS — API
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/ping")
def ping():
    """
    Health check — siempre devuelve 200 para que Railway no mate el deploy.
    Verifica las conexiones pero no falla si alguna no está lista aún.
    """
    status = {"ok": True, "postgres": False, "mongo": False,
               "timestamp": datetime.now(timezone.utc).isoformat()}
    try:
        query("SELECT 1")
        status["postgres"] = True
    except Exception as e:
        status["postgres_error"] = str(e)

    try:
        from mongo import get_mongo_client
        get_mongo_client().admin.command("ping")
        status["mongo"] = True
    except Exception as e:
        status["mongo_error"] = str(e)

    # Siempre 200 — Railway solo necesita saber que el proceso está vivo
    return jsonify(status), 200


# ── SESIÓN ────────────────────────────────────────────────────────────────────

@app.route("/api/session", methods=["POST"])
def create_or_get_session():
    """
    Crea una nueva sesión o devuelve la existente.
    - Si la cookie ya existe y la sesión está en la BD → devuelve la sesión.
    - Si no hay cookie o la sesión no existe → crea una nueva con geolocalización.

    Respuesta:
        {
          "session_id": "uuid",
          "is_new_user": true,
          "cold_start_done": false,
          "country_code": "MX",
          "country_name": "Mexico"
        }
    """
    existing_sid = get_session_id()

    if existing_sid:
        row = query(
            "SELECT * FROM cookie_sessions WHERE session_id = %s",
            (existing_sid,), one=True
        )
        if row:
            # Actualizar last_seen
            execute(
                "UPDATE cookie_sessions SET last_seen = NOW() WHERE session_id = %s",
                (existing_sid,)
            )
            resp = make_response(jsonify(row_to_dict(row)))
            return resp

    # Crear nueva sesión
    new_sid  = str(uuid.uuid4())
    geo      = geolocate_ip(client_ip())
    user_agent = request.headers.get("User-Agent", "")[:200]

    result = execute(
        """INSERT INTO cookie_sessions
               (session_id, country_code, country_name, city,
                is_new_user, cold_start_done, user_agent)
           VALUES (%s, %s, %s, %s, TRUE, FALSE, %s)
           RETURNING session_id, country_code, country_name,
                     is_new_user, cold_start_done""",
        (new_sid,
         geo.get("country_code", ""),
         geo.get("country_name", ""),
         geo.get("city", ""),
         user_agent),
        returning=True
    )

    data = row_to_dict(result)
    resp = make_response(jsonify(data))
    set_session_cookie(resp, new_sid)
    return resp, 201


# ── COLD-START ────────────────────────────────────────────────────────────────

@app.route("/api/cold-start", methods=["POST"])
@require_session
def save_cold_start():
    """
    Guarda las preferencias del formulario de onboarding.

    Body JSON esperado:
        {
          "genres": ["Sci-Fi", "Action", "Drama"],
          "directors": ["Christopher Nolan"],          (opcional)
          "seed_ratings": [{"movie_id": 1, "score": 5}]  (opcional)
        }

    Construye el taste_vector y lo guarda en la sesión.
    Luego asigna la sesión al cluster más cercano.
    """
    sid  = g.session_id
    body = request.get_json(silent=True) or {}

    genres       = body.get("genres", [])
    directors    = body.get("directors", [])
    seed_ratings = body.get("seed_ratings", [])  # lista de {movie_id, score}

    if not genres:
        return jsonify({"error": "Selecciona al menos un género"}), 400

    # Construir taste_vector: género → peso normalizado (0.0–1.0)
    # Los primeros géneros seleccionados tienen mayor peso
    taste_vector = {}
    total = len(genres)
    for i, genre in enumerate(genres):
        # Peso decreciente: primer género = 1.0, último ≈ 0.5
        taste_vector[genre] = round(1.0 - (i / (total * 2)), 3)

    # Guardar seed_ratings en la tabla ratings
    for sr in seed_ratings:
        mid   = sr.get("movie_id")
        score = sr.get("score")
        if mid and score and 1 <= score <= 5:
            execute(
                """INSERT INTO ratings (session_id, movie_id, score, source)
                   VALUES (%s, %s, %s, 'explicit')
                   ON CONFLICT (session_id, movie_id)
                   DO UPDATE SET score = EXCLUDED.score, rated_at = NOW()""",
                (sid, mid, float(score))
            )

    # Actualizar sesión
    execute(
        """UPDATE cookie_sessions
           SET taste_vector    = %s::jsonb,
               cold_start_done = TRUE,
               is_new_user     = FALSE
           WHERE session_id = %s""",
        (json.dumps(taste_vector), sid)
    )

    # Asignar al cluster más cercano (función SQL definida en schema)
    try:
        cluster_result = query(
            "SELECT assign_session_to_cluster(%s) AS cluster_id",
            (sid,), one=True
        )
        cluster_id = cluster_result["cluster_id"] if cluster_result else None
    except Exception:
        cluster_id = None

    return jsonify({
        "success":      True,
        "taste_vector": taste_vector,
        "cluster_id":   cluster_id,
        "message":      "Perfil guardado. ¡Ya tienes recomendaciones personalizadas!"
    })


# ── PELÍCULAS ─────────────────────────────────────────────────────────────────

@app.route("/api/movies/trending")
def trending_movies():
    """
    Devuelve películas populares en el país del usuario.
    Si hay sesión usa el country_code de la BD.
    Si no hay sesión, usa la geolocalización de la IP.

    Query params:
        limit  (int, default 20)
        offset (int, default 0)
    """
    limit  = min(int(request.args.get("limit",  20)), 50)
    offset = int(request.args.get("offset", 0))

    # Determinar país
    country_code = ""
    sid = get_session_id()
    if sid:
        row = query(
            "SELECT country_code FROM cookie_sessions WHERE session_id = %s",
            (sid,), one=True
        )
        if row:
            country_code = row["country_code"] or ""

    if not country_code:
        geo = geolocate_ip(client_ip())
        country_code = geo.get("country_code", "")

    # Primero busca en MongoDB (trending últimas 24h)
    trending_ids = get_trending_by_country(country_code, limit=limit) if country_code else []

    if trending_ids:
        # Trae las películas en el orden de trending
        placeholders = ",".join(["%s"] * len(trending_ids))
        movies = query(
            f"""SELECT movie_id, title, year, genres, poster_url,
                       avg_rating, rating_count, views_total
                FROM movies
                WHERE movie_id IN ({placeholders}) AND poster_url IS NOT NULL
                ORDER BY views_total DESC""",
            tuple(trending_ids)
        )
    else:
        # Fallback: más populares globalmente por avg_rating y vistas
        movies = query(
            """SELECT movie_id, title, year, genres, poster_url,
                      avg_rating, rating_count, views_total,
                      COALESCE((views_by_country->>%s)::INTEGER, 0) AS country_views
               FROM   movies
               WHERE  poster_url IS NOT NULL
               ORDER  BY COALESCE((views_by_country->>%s)::INTEGER, 0) DESC,
                         avg_rating DESC, rating_count DESC
               LIMIT  %s OFFSET %s""",
            (country_code, country_code, limit, offset)
        )

    return jsonify({
        "country_code": country_code,
        "movies":       rows_to_list(movies),
        "source":       "trending" if trending_ids else "popular"
    })


@app.route("/api/movies/search")
def search_movies():
    """
    Búsqueda full-text en título y sinopsis.
    La corrección ortográfica se hace en el frontend (JS);
    aquí llega el query ya corregido en 'q' y el original en 'raw'.

    Query params:
        q      (str) query corregido
        raw    (str) query original del usuario
        limit  (int, default 10)
    """
    q_corrected = request.args.get("q", "").strip()
    q_raw       = request.args.get("raw", q_corrected).strip()
    limit       = min(int(request.args.get("limit", 10)), 30)
    sid         = get_session_id()

    if not q_corrected:
        return jsonify({"error": "Parámetro 'q' requerido"}), 400

    # Búsqueda con FTS de PostgreSQL + ILIKE como fallback
    results = query(
        """SELECT movie_id, title, year, genres, poster_url,
                  avg_rating, director,
                  ts_rank(
                      to_tsvector('english', COALESCE(title,'') || ' ' || COALESCE(synopsis,'')),
                      plainto_tsquery('english', %s)
                  ) AS rank
           FROM   movies
           WHERE  to_tsvector('english', COALESCE(title,'') || ' ' || COALESCE(synopsis,''))
                  @@ plainto_tsquery('english', %s)
              OR  title ILIKE %s
           ORDER  BY rank DESC, avg_rating DESC
           LIMIT  %s""",
        (q_corrected, q_corrected, f"%{q_corrected}%", limit)
    )

    result_ids  = [r["movie_id"] for r in (results or [])]
    clicked_id  = None  # se actualiza cuando el usuario hace click

    # Guardar búsqueda en PostgreSQL
    if sid:
        execute(
            """INSERT INTO search_log
                   (session_id, raw_query, corrected_query,
                    results_count, result_movie_ids)
               VALUES (%s, %s, %s, %s, %s::jsonb)""",
            (sid, q_raw, q_corrected,
             len(result_ids), json.dumps(result_ids))
        )
        # Registrar en MongoDB
        log_event(sid, "search", payload={
            "raw_query":       q_raw,
            "corrected_query": q_corrected,
            "results_count":   len(result_ids),
        })

    return jsonify({
        "query":     q_corrected,
        "corrected": q_corrected != q_raw,
        "movies":    rows_to_list(results),
    })


@app.route("/api/movies/<int:movie_id>")
def movie_detail(movie_id: int):
    """Detalle completo de una película."""
    movie = query("SELECT * FROM movies WHERE movie_id = %s", (movie_id,), one=True)
    if not movie:
        return jsonify({"error": "Película no encontrada"}), 404

    # Registrar vista de detalle
    sid = get_session_id()
    if sid:
        geo = {}
        row = query("SELECT country_code FROM cookie_sessions WHERE session_id=%s",
                    (sid,), one=True)
        country = row["country_code"] if row else ""

        log_event(sid, "detail_view", movie_id=movie_id,
                  payload={"title": movie["title"]},
                  country_code=country)

        # Incrementar contador de vistas en PostgreSQL (función SQL)
        if country:
            execute("SELECT increment_movie_view(%s, %s)", (movie_id, country))

    # Rating del usuario para esta película
    user_rating = None
    if sid:
        r = query(
            "SELECT score FROM ratings WHERE session_id=%s AND movie_id=%s",
            (sid, movie_id), one=True
        )
        user_rating = r["score"] if r else None

    result = row_to_dict(movie)
    result["user_rating"] = user_rating
    return jsonify(result)


# ── CALIFICACIONES ────────────────────────────────────────────────────────────

@app.route("/api/rate", methods=["POST"])
@require_session
def rate_movie():
    """
    Guarda o actualiza la calificación de una película.

    Body JSON:
        {"movie_id": 42, "score": 4.5}
    """
    sid  = g.session_id
    body = request.get_json(silent=True) or {}

    movie_id = body.get("movie_id")
    score    = body.get("score")

    if not movie_id or score is None:
        return jsonify({"error": "movie_id y score son requeridos"}), 400
    if not (1.0 <= float(score) <= 5.0):
        return jsonify({"error": "score debe estar entre 1.0 y 5.0"}), 400

    execute(
        """INSERT INTO ratings (session_id, movie_id, score, source)
           VALUES (%s, %s, %s, 'explicit')
           ON CONFLICT (session_id, movie_id)
           DO UPDATE SET score = EXCLUDED.score, rated_at = NOW()""",
        (sid, int(movie_id), float(score))
    )

    # Registrar en MongoDB
    row = query("SELECT country_code FROM cookie_sessions WHERE session_id=%s",
                (sid,), one=True)
    country = row["country_code"] if row else ""
    log_event(sid, "rating", movie_id=int(movie_id),
              payload={"stars": float(score)},
              country_code=country)

    # Actualizar taste_vector con el nuevo rating (ingeniería inversa)
    _update_taste_vector_from_rating(sid, int(movie_id), float(score))

    return jsonify({"success": True, "movie_id": movie_id, "score": score})


def _update_taste_vector_from_rating(session_id: str, movie_id: int, score: float):
    """
    Actualiza el taste_vector de la sesión basándose en los géneros
    de la película calificada (ingeniería inversa de comportamiento).

    Si el usuario da 5 estrellas a una película de Sci-Fi,
    aumenta el peso de Sci-Fi en su vector.
    """
    try:
        # Obtener géneros de la película
        movie = query("SELECT genres FROM movies WHERE movie_id=%s",
                      (movie_id,), one=True)
        if not movie:
            return

        genres = movie["genres"]
        if isinstance(genres, str):
            genres = json.loads(genres)

        # Obtener taste_vector actual
        sess = query(
            "SELECT taste_vector FROM cookie_sessions WHERE session_id=%s",
            (session_id,), one=True
        )
        if not sess:
            return

        tv = sess["taste_vector"] or {}
        if isinstance(tv, str):
            tv = json.loads(tv)

        # Ajustar pesos: normalizar score a [-0.2, +0.2]
        delta = (score - 3.0) / 10.0   # -0.2 para score=1, +0.2 para score=5

        for genre in genres:
            current = tv.get(genre, 0.5)
            updated = max(0.0, min(1.0, current + delta))
            tv[genre] = round(updated, 3)

        execute(
            "UPDATE cookie_sessions SET taste_vector=%s::jsonb WHERE session_id=%s",
            (json.dumps(tv), session_id)
        )

        # Re-asignar cluster si el vector cambió significativamente
        if abs(delta) >= 0.15:
            query("SELECT assign_session_to_cluster(%s)", (session_id,), one=True)

    except Exception as e:
        print(f"[taste_vector] Warning: {e}")


# ── INTERACCIONES ─────────────────────────────────────────────────────────────

@app.route("/api/interact", methods=["POST"])
def log_interaction():
    """
    Registra un evento de interacción en MongoDB.
    No requiere sesión (anónimo también registra).

    Body JSON:
        {
          "event_type": "click",
          "movie_id": 42,
          "payload": {"carousel": "Lo más visto MX", "position": 2}
        }
    """
    sid  = get_session_id()
    body = request.get_json(silent=True) or {}

    event_type = body.get("event_type")
    movie_id   = body.get("movie_id")
    payload    = body.get("payload", {})

    valid_events = {"click", "detail_view", "search", "rating", "scroll"}
    if event_type not in valid_events:
        return jsonify({"error": f"event_type debe ser uno de: {valid_events}"}), 400

    country = ""
    if sid:
        row = query("SELECT country_code FROM cookie_sessions WHERE session_id=%s",
                    (sid,), one=True)
        country = row["country_code"] if row else ""

    log_event(
        session_id=sid or "anonymous",
        event_type=event_type,
        movie_id=int(movie_id) if movie_id else None,
        payload=payload,
        country_code=country
    )

    # Incrementar vistas si es click en película
    if event_type == "click" and movie_id and country:
        execute("SELECT increment_movie_view(%s, %s)", (int(movie_id), country))

    return jsonify({"success": True})


# ── RECOMENDACIONES ───────────────────────────────────────────────────────────

@app.route("/api/recommend")
@require_session
def recommend():
    """
    Motor de recomendación híbrido completo.
    Usa HybridEngine: clustering colaborativo + contenido TF-IDF + boosting.
    """
    sid     = g.session_id
    limit   = min(int(request.args.get("limit", 10)), 30)

    # Obtener país de la sesión
    sess_row = query(
        "SELECT country_code FROM cookie_sessions WHERE session_id=%s",
        (sid,), one=True
    )
    country = sess_row["country_code"] if sess_row else ""

    # Motor híbrido: clustering + contenido + boosting
    engine = HybridEngine(get_db(), get_content_recommender())
    recs   = engine.recommend(sid, limit=limit, country_code=country)

    movies_out = [{
        "movie_id":    r.movie_id,
        "title":       r.title,
        "year":        r.year,
        "genres":      r.genres,
        "poster_url":  r.poster_url,
        "avg_rating":  r.avg_rating,
        "director":    r.director,
        "rec_source":  r.source,
        "rec_score":   round(r.final_score, 4),
        "explanation": r.explanation,
    } for r in recs]

    top_explanation = movies_out[0]["explanation"] if movies_out else "Recomendaciones para ti"

    return jsonify({
        "movies":      movies_out,
        "strategy":    "hybrid",
        "explanation": top_explanation,
    })


# ─────────────────────────────────────────────────────────────────────────────
#  ARRANQUE
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port  = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_ENV", "development") == "development"
    print(f"\n🎬 CineMatch corriendo en http://localhost:{port}")
    print(f"   Modo: {'desarrollo' if debug else 'producción'}\n")
    app.run(host="0.0.0.0", port=port, debug=debug)
