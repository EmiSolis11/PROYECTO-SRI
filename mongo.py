"""
CineMatch — mongo.py
Conexión a MongoDB Atlas para registrar interacciones de usuario:
  - clicks en películas
  - búsquedas realizadas
  - calificaciones con estrellas
  - scroll en carruseles

Uso en Flask:
    from mongo import get_mongo, log_event
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

_mongo_client = None      # singleton — una conexión por proceso Flask


def get_mongo_client():
    """Devuelve el cliente MongoDB (singleton)."""
    global _mongo_client
    if _mongo_client is None:
        try:
            from pymongo import MongoClient
            uri = os.getenv("MONGO_URI", "")
            if not uri:
                raise EnvironmentError(
                    "Define MONGO_URI en .env.\n"
                    "Ejemplo: mongodb+srv://user:pass@cluster.mongodb.net/cinematch"
                )
            _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            # Verificar conexión al iniciar
            _mongo_client.admin.command("ping")
        except ImportError:
            raise ImportError("Instala pymongo:  pip install pymongo dnspython")
    return _mongo_client


def get_mongo():
    """Devuelve la base de datos 'cinematch'."""
    return get_mongo_client()["cinematch"]


# ─────────────────────────────────────────────────────────────────────────────
#  FUNCIONES DE REGISTRO DE EVENTOS
# ─────────────────────────────────────────────────────────────────────────────

def log_event(session_id: str, event_type: str, movie_id: int = None,
              payload: dict = None, country_code: str = None):
    """
    Registra cualquier evento de interacción en la colección 'interactions'.

    event_type válidos:
        'click'       → el usuario hizo click en una película
        'detail_view' → abrió el modal de detalle
        'search'      → realizó una búsqueda
        'rating'      → calificó con estrellas
        'scroll'      → scroll en un carrusel

    Ejemplo:
        log_event("uuid-123", "click", movie_id=42,
                  payload={"carousel": "Lo más visto MX", "position": 2},
                  country_code="MX")
    """
    try:
        db  = get_mongo()
        doc = {
            "session_id":   str(session_id),
            "event_type":   event_type,
            "movie_id":     movie_id,
            "payload":      payload or {},
            "country_code": country_code,
            "created_at":   datetime.now(timezone.utc),
        }
        db["interactions"].insert_one(doc)

        # Actualiza el perfil acumulado de la sesión
        _update_session_profile(session_id, event_type, movie_id, payload)

    except Exception as e:
        # MongoDB es opcional — no romper la app si falla
        print(f"[MongoDB] Warning: no se pudo registrar evento: {e}")


def _update_session_profile(session_id: str, event_type: str,
                             movie_id: int = None, payload: dict = None):
    """
    Actualiza el documento acumulado en 'session_profiles'.
    Lleva la cuenta de géneros clicados, términos buscados y ratings.
    """
    try:
        db  = get_mongo()
        col = db["session_profiles"]
        now = datetime.now(timezone.utc)

        update = {
            "$set":  {"last_active": now},
            "$inc":  {"total_events": 1},
        }

        if event_type == "click" and movie_id:
            update["$addToSet"] = {"clicked_ids": movie_id}

        elif event_type == "rating" and movie_id and payload:
            stars = payload.get("stars")
            if stars:
                update["$set"][f"rated_ids.{movie_id}"] = stars

        elif event_type == "search" and payload:
            term = payload.get("corrected_query") or payload.get("raw_query")
            if term:
                update["$addToSet"] = {"search_terms": term.lower().strip()}

        elif event_type == "scroll" and payload:
            carousel = payload.get("carousel_name", "unknown")
            update["$inc"][f"scroll_counts.{carousel}"] = 1

        col.update_one(
            {"_id": str(session_id)},
            update,
            upsert=True
        )
    except Exception as e:
        print(f"[MongoDB] Warning: no se pudo actualizar perfil: {e}")


def get_session_profile(session_id: str) -> dict:
    """
    Devuelve el perfil acumulado de una sesión.
    Útil para la recomendación por ingeniería inversa.
    """
    try:
        db  = get_mongo()
        doc = db["session_profiles"].find_one({"_id": str(session_id)})
        if doc:
            doc.pop("_id", None)
        return doc or {}
    except Exception:
        return {}


def get_trending_by_country(country_code: str, limit: int = 20) -> list:
    """
    Devuelve los movie_ids más clickeados en las últimas 24h para un país.
    Usado en el carrusel de cold-start "Lo más visto en tu país".
    """
    try:
        from datetime import timedelta
        db  = get_mongo()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

        pipeline = [
            {"$match": {
                "event_type":   "click",
                "country_code": country_code,
                "created_at":   {"$gte": cutoff},
                "movie_id":     {"$ne": None}
            }},
            {"$group": {
                "_id":   "$movie_id",
                "count": {"$sum": 1}
            }},
            {"$sort":  {"count": -1}},
            {"$limit": limit},
        ]
        results = list(db["interactions"].aggregate(pipeline))
        return [r["_id"] for r in results]
    except Exception as e:
        print(f"[MongoDB] Warning: no se pudo obtener trending: {e}")
        return []