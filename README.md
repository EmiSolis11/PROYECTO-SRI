# 🎬 CineMatch — Sistema de Recomendación de Películas

> Proyecto desarrollado para la materia optativa **Sistemas de Recomendación de la Información**
>
> **Equipo:**
> - Cimé Morales Esteban
> - Salazar Bastarrachea Gael Francisco
> - Solís Ek Emiliano

<img src="https://github.com/user-attachments/assets/dbcc9c00-6b58-421d-a711-d51b95874c4e" alt="Descripción" width="250" />
<img src="https://github.com/user-attachments/assets/d8eb052b-91bc-4d37-bcb6-006e3e45bba9" alt="Descripción" width="250" />
<img src="https://github.com/user-attachments/assets/2a268968-4761-4bdc-a92f-c9db1b91b99c" alt="Descripción" width="250" />

Aplicación en producción: https://proyecto-sri-production.up.railway.app/

---

## Tabla de Contenidos

1. [Descripción General](#descripción-general)
2. [Stack Tecnológico](#stack-tecnológico)
3. [Arquitectura del Sistema](#arquitectura-del-sistema)
4. [Funcionalidades de la Página](#funcionalidades-de-la-página)
5. [Algoritmos de Recomendación](#algoritmos-de-recomendación)
6. [API REST — Endpoints](#api-rest--endpoints)
7. [Base de Datos](#base-de-datos)
8. [Acceso a la Aplicación](#acceso-a-la-aplicación)

---

## Descripción General

**CineMatch** es una aplicación web de recomendación de películas que aplica técnicas de recuperación y filtrado de información para sugerir contenido personalizado a cada usuario. El sistema aprende de las preferencias declaradas (géneros favoritos) y del comportamiento implícito (calificaciones, clics, búsquedas) para construir un perfil de gustos en tiempo real y ofrecer recomendaciones relevantes.

El proyecto implementa un **motor híbrido** que combina tres enfoques complementarios:

- Filtrado colaborativo basado en clustering de usuarios (K-Means)
- Filtrado basado en contenido (TF-IDF + codificación de géneros)
- Boosting por frescura, popularidad regional y rating

---

## Stack Tecnológico

| Capa | Tecnología |
|---|---|
| Backend | Python 3.12 + Flask 3.1 |
| Base de datos relacional | PostgreSQL (Supabase) via psycopg2 |
| Base de datos de eventos | MongoDB Atlas via pymongo |
| Machine Learning | scikit-learn 1.6, NumPy 2.2, pandas 2.2 |
| Frontend | HTML5 + CSS3 + JavaScript vanilla (SPA) |
| Geolocalización | ip-api.com (sin API key) |
| Servidor de producción | Gunicorn 23 |
| Plataforma de deploy | Railway / Render |

---

## Arquitectura del Sistema

```
┌─────────────────────────────────────────────────────────┐
│                    CLIENTE (SPA)                         │
│   index.html + app.js + search.js + stars.js + CSS       │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP / JSON
┌────────────────────▼────────────────────────────────────┐
│                   FLASK (app.py)                         │
│  Sesiones · Cold-Start · Búsqueda · Detalles · Ratings  │
│  Interacciones · Recomendaciones                        │
└──────┬──────────────────────────┬───────────────────────┘
       │                          │
┌──────▼──────────┐     ┌─────────▼──────────┐
│  PostgreSQL     │     │   MongoDB Atlas     │
│  (Supabase)     │     │                     │
│  movies         │     │  interactions       │
│  cookie_sessions│     │  session_profiles   │
│  ratings        │     │  (eventos en tiempo │
│  clusters       │     │   real)             │
│  session_cluster│     └─────────────────────┘
└─────────────────┘
       │
┌──────▼──────────────────────────────────────────────────┐
│             MOTOR DE RECOMENDACIÓN                       │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ Clustering  │  │  Contenido   │  │   HybridEngine │  │
│  │ (K-Means)   │  │ (TF-IDF +    │  │  α·collab +    │  │
│  │             │  │  One-Hot)    │  │  β·content +   │  │
│  └─────────────┘  └──────────────┘  │  γ·boost       │  │
│                                     └────────────────┘  │
└─────────────────────────────────────────────────────────┘
```

---

## Funcionalidades de la Página

### 1. Gestión de Sesión sin Registro

La aplicación no requiere cuenta de usuario. Al entrar por primera vez se genera automáticamente un **UUID de sesión** almacenado en una cookie HTTP-only (`cm_session`) con vigencia de un año. La sesión persiste entre visitas y permite acumular el historial de preferencias del usuario.

Además, el sistema realiza **geolocalización por IP** usando `ip-api.com` para detectar el país y ciudad del usuario sin necesidad de pedírselos, lo que permite personalizar el carrusel de tendencias regionales.

### 2. Onboarding por Cold-Start

Cuando un usuario llega por primera vez (o no ha completado el proceso de configuración inicial), se muestra un **overlay de bienvenida** que incluye:

- **Selección de géneros favoritos**: chips interactivos con todos los géneros presentes en el catálogo (Action, Drama, Sci-Fi, etc.). El usuario debe seleccionar al menos uno para continuar.
- **Calificación de películas populares**: una cuadrícula de hasta 10 películas tendencia donde el usuario puede dar de 1 a 5 estrellas. Estas calificaciones iniciales se denominan *seed ratings*.

Con esta información se construye el **`taste_vector`** inicial del usuario (ver sección de algoritmos) y se le asigna al cluster más cercano.

### 3. Página Principal con Carruseles

La pantalla de inicio muestra dos carruseles horizontales con scroll:

- **"Lo más visto en [país]"**: películas populares en las últimas 24 horas en el país del usuario, obtenidas desde MongoDB. Si no hay datos recientes, utiliza las más populares globalmente filtradas por país.
- **"Recomendado para ti"**: resultado del motor de recomendación híbrido personalizado para la sesión activa.

Cada tarjeta de película muestra el póster, título, año, géneros, calificación promedio y —en el carrusel de recomendaciones— una **explicación en lenguaje natural** del porqué fue recomendada (ej. *"Coincide con tus géneros favoritos: Sci-Fi y Thriller"*).

### 4. Búsqueda con Autocorrección Ortográfica

La barra de búsqueda implementa un sistema completo de búsqueda con las siguientes características:

- **Debounce de 350 ms**: evita llamadas al servidor en cada pulsación de teclado.
- **Corrección ortográfica client-side** usando **distancia de Levenshtein**: antes de enviar la consulta al servidor, el motor de búsqueda del frontend compara cada palabra con un diccionario construido desde los títulos del catálogo. Si detecta una posible errata (distancia ≤ 2 con alguna palabra del diccionario), muestra el aviso *"¿Quisiste decir X?"* y permite al usuario confirmar la corrección.
- **Full-Text Search en PostgreSQL**: el backend usa `to_tsvector` + `plainto_tsquery` en inglés para búsqueda semántica sobre título y sinopsis, complementado con `ILIKE` como fallback para coincidencias parciales. Los resultados se ordenan por relevancia (`ts_rank`) y calificación promedio.
- Los eventos de búsqueda se guardan tanto en PostgreSQL (`search_log`) como en MongoDB para análisis de comportamiento.

### 5. Modal de Detalle de Película

Al hacer clic en cualquier póster se abre un modal con:

- Información completa: título, año, director, géneros, sinopsis, calificación promedio y número de valoraciones.
- **Sistema de calificación con estrellas** (1–5, medias estrellas habilitadas), implementado en `stars.js`.
- Actualización en tiempo real del `taste_vector` basada en el rating dado (ver sección de algoritmos).
- Registro automático del evento `detail_view` en MongoDB.

### 6. Registro de Interacciones

Todos los comportamientos del usuario se registran de forma transparente en MongoDB para retroalimentar el sistema de recomendación:

| Evento | Cuándo se registra |
|---|---|
| `click` | Clic en una tarjeta de película |
| `detail_view` | Apertura del modal de detalle |
| `search` | Ejecución de una búsqueda |
| `rating` | Calificación con estrellas |
| `scroll` | Scroll en un carrusel |

Los clics en películas también incrementan el contador de vistas por país en PostgreSQL mediante la función SQL `increment_movie_view(movie_id, country_code)`.

### 7. Carga de Datos con MovieLens

El script `load_movielens.py` descarga automáticamente el dataset **MovieLens ml-latest-small** desde GroupLens y lo carga en Supabase. Funcionalidades:

- Descarga y descompresión automática del ZIP.
- Mapeo de IDs de IMDb a posters de TMDB.
- Inserción masiva optimizada (hasta 2000 ratings por commit).
- Sistema de reanudación: detecta registros ya existentes y no los duplica.
- Creación de sesiones de usuario demo para poblar el sistema de clustering desde el inicio.

---

## Algoritmos de Recomendación

### A. Construcción del Perfil de Usuario (`taste_vector`)

El perfil del usuario se representa como un diccionario `{género: peso}` donde cada peso es un valor entre 0.0 y 1.0. Este vector evoluciona en tres momentos:

**1. Construcción en el cold-start** (preferencias declaradas)

```
taste_vector[género_i] = 1.0 - (i / (total_géneros × 2))
```

El primer género seleccionado recibe peso 1.0, y los siguientes reciben pesos decrecientes hasta aproximadamente 0.5. Esto refleja que el orden de selección indica preferencia relativa.

**2. Actualización por rating** (ingeniería inversa de comportamiento)

Cuando el usuario califica una película con `score` estrellas, los géneros de esa película ajustan su peso en el `taste_vector`:

```
delta = (score - 3.0) / 10.0       # rango: -0.2 (score=1) a +0.2 (score=5)
nuevo_peso = clamp(peso_actual + delta, 0.0, 1.0)
```

Una calificación de 5 estrellas aumenta el peso de esos géneros; una de 1 estrella lo reduce. Si el delta supera ±0.15, la sesión se reasigna automáticamente al cluster más cercano.

**3. Reconstrucción masiva en el reentrenamiento**

Durante el reentrenamiento del modelo K-Means, el sistema reconstruye el `taste_vector` de sesiones que solo tienen ratings (sin cold-start explícito):

```
taste_vector[género] = Σ(score × pertenencia_al_género) / Σ(scores_totales)
```

Solo se guardan pesos mayores al 1% para mantener el vector disperso.

---

### B. Algoritmo de Clustering K-Means (Filtrado Colaborativo)

**Objetivo**: agrupar usuarios con gustos similares para recomendar películas que les han gustado a usuarios del mismo grupo.

**Pipeline completo:**

1. **Vectorización**: cada `taste_vector` se convierte en un array NumPy de 21 dimensiones, una por cada género conocido (Action, Adventure, Animation, Biography, Comedy, Crime, Documentary, Drama, Family, Fantasy, Foreign, History, Horror, Music, Mystery, Romance, Sci-Fi, Sport, Thriller, War, Western).

2. **Normalización L2**: antes del clustering, todos los vectores se normalizan a norma unitaria, lo que convierte la distancia euclidiana en una aproximación a la similitud coseno. Esto hace que el algoritmo agrupe por *dirección* de preferencias (perfiles de gusto similares), no por intensidad absoluta.

3. **Inicialización K-Means++**: se usa `init="k-means++"` para una inicialización inteligente de los centroides que acelera la convergencia y evita mínimos locales.

4. **Valor de K dinámico**: durante el reentrenamiento administrativo, K = 5% del número total de sesiones con perfil válido, con un rango de [2, 50]. Esto escala automáticamente con el tamaño de la base de usuarios.

5. **Versionado de clusters**: cada reentrenamiento incrementa el número de versión y desactiva los clusters anteriores. Las sesiones se reasignan en batch de 500 en 500 mediante `UPSERT` en la tabla `session_cluster`.

6. **Asignación en tiempo real**: cuando un usuario completa el cold-start o actualiza su `taste_vector` significativamente, se llama a la función SQL `assign_session_to_cluster()` que calcula la distancia al centroide más cercano sin necesidad de recargar el modelo completo en memoria.

**Uso en las recomendaciones:**

Una vez asignada la sesión a un cluster, el motor consulta la vista materializada `v_movie_cluster_affinity` que precalcula para cada par (película, cluster) el promedio de calificaciones de los miembros del cluster. Solo se toman películas con calificación promedio ≥ 3.0 en el cluster.

---

### C. Algoritmo de Contenido (TF-IDF + One-Hot de Géneros)

**Objetivo**: recomendar películas con características similares al perfil de gustos del usuario, independientemente de quién las haya visto.

**Construcción del vector híbrido por película:**

Cada película se representa con un vector que combina dos componentes (peso 50%/50%):

**Componente TF-IDF (sinopsis + título + director)**

```python
TfidfVectorizer(
    max_features=5000,
    stop_words="english",
    ngram_range=(1, 2),   # unigramas y bigramas
    min_df=1
)
```

Se construye un corpus donde cada documento es la concatenación del título, sinopsis y director de la película. El vectorizador extrae hasta 5000 características de unigramas y bigramas con pesos TF-IDF.

**Componente One-Hot de géneros**

Usando `MultiLabelBinarizer` de scikit-learn, cada película se codifica como un vector binario de 21 dimensiones (una por género). Si la película pertenece a los géneros [Action, Sci-Fi], las posiciones correspondientes valen 1 y el resto 0.

**Normalización y ensamblado:**

Ambas matrices se normalizan por filas (L2) por separado y luego se concatenan horizontalmente, generando un único vector `hybrid_matrix` por película.

**Cálculo de similitud:**

La similitud entre dos películas (o entre el taste_vector del usuario y una película) se calcula con **similitud coseno** sobre el `hybrid_matrix`. Para recomendar por perfil de usuario, se proyecta el `taste_vector` al espacio de géneros y se compara solo contra la parte One-Hot de la matriz híbrida.

---

### D. Motor Híbrido (`HybridEngine`)

**Objetivo**: combinar los tres enfoques anteriores en una única lista rankeada con explicaciones.

**Configuración de pesos:**

| Componente | Variable | Peso por defecto |
|---|---|---|
| Filtrado colaborativo (cluster) | `ALPHA` | 0.50 |
| Filtrado por contenido (TF-IDF) | `BETA` | 0.35 |
| Boosting | `GAMMA` | 0.15 |

**Fórmula del score final:**

```
final_score = ALPHA × collab_score + BETA × content_score + GAMMA × boost_score
```

Donde cada score individual está normalizado en [0, 1] antes de combinarse.

**Componentes del boost_score:**

```
boost_score = min(1.0, freshness_bonus + country_popularity × 0.5 + rating_bonus × 0.3)
```

- **Frescura**: bonus fijo por año de producción (2026: +0.25, 2025: +0.20, 2024: +0.12, 2023: +0.08, 2022: +0.05).
- **Popularidad en el país**: vistas en el país del usuario normalizadas respecto a la película más vista.
- **Bonus de rating**: `max(0, (avg_rating - 4.0) / 5.0)` para películas muy bien calificadas.

**Flujo de ejecución del motor:**

1. Lee el `taste_vector` y `cluster_id` de la sesión desde PostgreSQL.
2. Obtiene candidatos colaborativos (películas bien calificadas en el mismo cluster, ×2 del límite pedido).
3. Obtiene candidatos por contenido (películas más similares al taste_vector, ×2 del límite pedido).
4. Hace merge de ambas listas eliminando duplicados y películas ya vistas por el usuario.
5. Aplica el boost a todos los candidatos.
6. Ordena por `final_score` descendente.
7. Genera la explicación en lenguaje natural para cada película.
8. Devuelve los primeros `limit` resultados.

**Fallback**: si la sesión no tiene perfil construido (usuario sin cold-start), devuelve las películas más populares en el país del usuario ordenadas por vistas locales y rating global.

---

### E. Buscador con Corrección Ortográfica (Levenshtein)

Implementado en el frontend (`search.js`) sin dependencias externas:

**Construcción del diccionario**: al cargar la página, se extraen todas las palabras (longitud > 3) de los títulos del catálogo cargado y se almacenan en un `Set`. Este diccionario vive en memoria del navegador.

**Algoritmo de distancia de Levenshtein**:

```
dp[i][j] = min(
    dp[i-1][j] + 1,          // eliminación
    dp[i][j-1] + 1,          // inserción
    dp[i-1][j-1] + (a[i] ≠ b[j])  // sustitución
)
```

Se implementa con programación dinámica en O(m×n). Una palabra se considera candidata a corrección si su distancia ≤ 2 con alguna palabra del diccionario.

**Corrección de queries multi-palabra**: cada palabra de la query se corrige independientemente y se reconstruye la frase. El query corregido se envía al backend en el parámetro `q`, mientras que el original va en `raw` para trazabilidad.

---

## API REST — Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Sirve la SPA (index.html) |
| `GET` | `/api/ping` | Health check: verifica PostgreSQL y MongoDB |
| `POST` | `/api/session` | Crea o recupera sesión por cookie |
| `POST` | `/api/cold-start` | Guarda preferencias del onboarding |
| `GET` | `/api/movies/trending` | Películas más vistas en el país del usuario |
| `GET` | `/api/movies/search?q=` | Búsqueda full-text con corrección |
| `GET` | `/api/movies/<id>` | Detalle completo de una película |
| `POST` | `/api/rate` | Calificar una película (1–5 estrellas) |
| `POST` | `/api/interact` | Registrar evento de interacción |
| `GET` | `/api/recommend` | Recomendaciones híbridas personalizadas |
| `POST` | `/api/admin/fit-model` | Entrena el modelo de contenido (admin) |
| `POST` | `/api/admin/retrain` | Reentrena K-Means con datos actuales (admin) |

---

## Base de Datos

### PostgreSQL (Supabase) — Datos estructurales

**`movies`**: catálogo de películas con `movie_id`, `title`, `year`, `genres` (JSONB), `synopsis`, `director`, `poster_url`, `avg_rating`, `rating_count`, `views_total`, `views_by_country` (JSONB `{country_code: count}`).

**`cookie_sessions`**: sesiones de usuario con `session_id` (UUID), `country_code`, `taste_vector` (JSONB), `cold_start_done`, `is_new_user`, `created_at`, `last_seen`.

**`ratings`**: calificaciones explícitas con `session_id`, `movie_id`, `score` (1.0–5.0), `source` ('explicit'), `rated_at`. Restricción `UNIQUE(session_id, movie_id)`.

**`clusters`**: centroides K-Means activos con `cluster_id`, `cluster_number`, `centroid_vector` (JSONB), `member_count`, `version`, `is_active`.

**`session_cluster`**: asignación de cada sesión a su cluster con `session_id`, `cluster_id`, `distance_to_centroid`, `cluster_version`, `assigned_at`.

**`search_log`**: historial de búsquedas con `session_id`, `raw_query`, `corrected_query`, `results_count`, `result_movie_ids` (JSONB).

**`v_movie_cluster_affinity`** (vista): precalcula el promedio de calificaciones por par (película, cluster) para acelerar las consultas del motor colaborativo.

### MongoDB Atlas — Eventos de comportamiento

**`interactions`**: todos los eventos (`click`, `detail_view`, `search`, `rating`, `scroll`) con `session_id`, `event_type`, `movie_id`, `payload`, `country_code`, `created_at`.

**`session_profiles`**: perfil acumulado por sesión con contadores de clics, términos buscados, películas calificadas y scrolls por carrusel. Se actualiza en tiempo real con `$inc` y `$addToSet`.

---

## Acceso a la Aplicación

La aplicación está desplegada y disponible en producción. No requiere instalación ni configuración:

🔗 **[https://proyecto-sri-production.up.railway.app/](https://proyecto-sri-production.up.railway.app/)**

Al ingresar por primera vez, el sistema creará automáticamente una sesión anónima, detectará el país desde la IP y mostrará el formulario de cold-start para configurar las preferencias iniciales.
