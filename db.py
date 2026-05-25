"""
CineMatch — db.py
Conexión a Supabase (PostgreSQL) con soporte para:
  - DATABASE_URL completa
  - Parámetros separados (DB_HOST, DB_PORT, etc.)

Uso en Flask:
    from db import get_db, close_db, init_app
    init_app(app)

    @app.route("/ejemplo")
    def ejemplo():
        db  = get_db()
        cur = db.cursor()
        cur.execute("SELECT * FROM movies LIMIT 5")
        rows = cur.fetchall()
        return jsonify(rows)
"""

import os
import psycopg2
import psycopg2.extras          # RealDictCursor → acceso por nombre de columna
from dotenv import load_dotenv

load_dotenv()                   # carga .env automáticamente


# ─────────────────────────────────────────────────────────────────────────────
#  DSN — construye la cadena de conexión desde las variables de entorno
# ─────────────────────────────────────────────────────────────────────────────

def _build_dsn() -> str:
    """
    Prioridad:
      1. DATABASE_URL  (una sola variable — ideal para Railway / Render)
      2. DB_HOST + DB_PORT + DB_NAME + DB_USER + DB_PASSWORD
    """
    url = os.getenv("DATABASE_URL")
    if url:
        # Supabase pooler usa el puerto 6543 (transaction mode).
        # Si la URL llega con puerto 5432, lo corregimos automáticamente.
        return url.replace(":5432/", ":6543/")

    host     = os.getenv("DB_HOST", "").strip()
    port     = os.getenv("DB_PORT", "6543").strip()
    dbname   = os.getenv("DB_NAME", "postgres").strip()
    user     = os.getenv("DB_USER", "").strip()
    password = os.getenv("DB_PASSWORD", "").strip()

    if not host or not user or not password:
        raise EnvironmentError(
            "Faltan credenciales de BD.\n"
            "Define DATABASE_URL  —o—  DB_HOST + DB_USER + DB_PASSWORD en .env"
        )

    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


# ─────────────────────────────────────────────────────────────────────────────
#  CONEXIÓN
# ─────────────────────────────────────────────────────────────────────────────

def get_connection() -> psycopg2.extensions.connection:
    """
    Abre una nueva conexión con RealDictCursor para acceder
    a las columnas por nombre (como sqlite3.Row).

    Supabase Pooler (puerto 6543) usa 'transaction mode':
      - autocommit = False  → debemos llamar conn.commit() o conn.rollback()
      - No soporta prepared statements → use_native_table_prefix=False
      - No soporta server-side cursors con nombre
    """
    dsn  = _build_dsn()
    conn = psycopg2.connect(
        dsn,
        cursor_factory=psycopg2.extras.RealDictCursor,
        # Deshabilita prepared statements (requerido por Supabase Pooler)
        options="-c default_transaction_isolation=read committed"
    )
    conn.autocommit = False
    return conn


def get_db():
    """
    Helper para Flask: devuelve la conexión guardada en flask.g.
    Si no existe, abre una nueva.

    Ejemplo de uso en una ruta:
        db  = get_db()
        cur = db.cursor()
        cur.execute("SELECT 1")
    """
    try:
        from flask import g
        if "db" not in g:
            g.db = get_connection()
        return g.db
    except RuntimeError:
        # Llamado fuera de contexto Flask (scripts, tests)
        return get_connection()


def close_db(error=None):
    """Cierra la conexión al terminar el request. Registrado en init_app."""
    try:
        from flask import g
        db = g.pop("db", None)
        if db is not None:
            if error:
                db.rollback()
            db.close()
    except RuntimeError:
        pass


def init_app(app):
    """
    Registra close_db en Flask.
    Llama esto UNA VEZ en app.py:

        from db import init_app
        init_app(app)
    """
    app.teardown_appcontext(close_db)


# ─────────────────────────────────────────────────────────────────────────────
#  HELPER — ejecutar query y devolver lista de dicts
# ─────────────────────────────────────────────────────────────────────────────

def query(sql: str, params: tuple = (), one: bool = False):
    """
    Atajo para queries de solo lectura.
    Devuelve un dict si one=True, o lista de dicts si one=False.

    Ejemplo:
        movie = query("SELECT * FROM movies WHERE movie_id = %s", (1,), one=True)
        all_movies = query("SELECT * FROM movies LIMIT 10")
    """
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    return cur.fetchone() if one else cur.fetchall()


def execute(sql: str, params: tuple = (), returning: bool = False):
    """
    Atajo para INSERT / UPDATE / DELETE con commit automático.
    Si returning=True devuelve el primer campo del RETURNING.

    Ejemplo:
        new_id = execute(
            "INSERT INTO cookie_sessions (country_code) VALUES (%s) RETURNING session_id",
            ("MX",), returning=True
        )
    """
    db  = get_db()
    cur = db.cursor()
    cur.execute(sql, params)
    result = cur.fetchone() if returning else None
    db.commit()
    return result


# ─────────────────────────────────────────────────────────────────────────────
#  VERIFICACIÓN — test directo desde terminal
# ─────────────────────────────────────────────────────────────────────────────

def _test_connection():
    print("\n🔌 CineMatch — Probando conexión a Supabase...")
    try:
        conn = get_connection()
        cur  = conn.cursor()

        # 1. Versión de PostgreSQL
        cur.execute("SELECT version()")
        version = cur.fetchone()["version"]
        print(f"   ✓ PostgreSQL: {version[:40]}...")

        # 2. Tablas existentes
        cur.execute("""
            SELECT table_name
            FROM   information_schema.tables
            WHERE  table_schema = 'public'
            ORDER  BY table_name
        """)
        tables = [r["table_name"] for r in cur.fetchall()]
        print(f"   ✓ Tablas encontradas: {tables}")

        # 3. Conteo de filas por tabla
        print("\n   📊 Filas por tabla:")
        for t in tables:
            cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
            count = cur.fetchone()["c"]
            print(f"      {t:<30} {count:>6,} filas")

        conn.close()
        print("\n   ✅ Conexión exitosa. Puedes ejecutar app.py\n")

    except EnvironmentError as e:
        print(f"\n   ❌ Error de configuración:\n   {e}\n")
    except Exception as e:
        print(f"\n   ❌ Error de conexión:\n   {e}")
        print("\n   Verifica:")
        print("     1. Que .env existe y tiene DB_HOST + DB_USER + DB_PASSWORD")
        print("     2. Que la contraseña fue rotada y .env tiene la nueva")
        print("     3. Que el host sea: aws-0-us-west-2.pooler.supabase.com")
        print("     4. Que el puerto sea: 6543  (NO 5432)\n")


if __name__ == "__main__":
    _test_connection()