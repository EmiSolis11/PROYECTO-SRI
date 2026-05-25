/**
 * CineMatch — app.js
 * Lógica principal del SPA.
 * Maneja: sesión, cold-start, carruseles, modal, interacciones.
 */

const API = ''   // Flask corre en el mismo origen

// ─── Estado global ────────────────────────────────────────────
const State = {
  session:      null,   // objeto devuelto por /api/session
  currentMovie: null,   // película abierta en el modal
  starRating:   null,   // instancia de StarRating activa
  search:       null,   // instancia de MovieSearch
  allMovies:    [],     // caché plano para el diccionario de búsqueda
}

// ─── Helpers ──────────────────────────────────────────────────
const $ = id => document.getElementById(id)
const genres = m => {
  try {
    return Array.isArray(m.genres) ? m.genres
         : JSON.parse(m.genres || '[]')
  } catch { return [] }
}

function showToast(msg, ms = 2500) {
  const t = $('toast')
  t.textContent = msg
  t.classList.add('show')
  setTimeout(() => t.classList.remove('show'), ms)
}

function posterUrl(url, title = '') {
  return url || `https://placehold.co/150x225/16161f/7a7888?text=${encodeURIComponent(title.slice(0,10))}`
}

// ─── SESIÓN ───────────────────────────────────────────────────
async function initSession() {
  try {
    const res  = await fetch(`${API}/api/session`, { method: 'POST', credentials: 'include' })
    const data = await res.json()
    State.session = data

    if (data.is_new_user || !data.cold_start_done) {
      await showColdStart()
    } else {
      await loadHome()
    }
  } catch (e) {
    console.error('Error iniciando sesión:', e)
    await loadHome()
  }
}

// ─── COLD START ───────────────────────────────────────────────
async function showColdStart() {
  const overlay = $('cold-start-overlay')
  overlay.style.display = 'flex'

  // Cargar géneros disponibles
  const moviesRes = await fetch(`${API}/api/movies/trending?limit=50`).catch(() => null)
  const moviesData = moviesRes ? await moviesRes.json() : { movies: [] }
  const movies = moviesData.movies || []
  State.allMovies = movies

  // Extraer géneros únicos
  const genreSet = new Set()
  movies.forEach(m => genres(m).forEach(g => genreSet.add(g)))
  const allGenres = [...genreSet].sort()

  // Render chips de géneros
  const genreGrid = $('genre-grid')
  genreGrid.innerHTML = allGenres.map(g =>
    `<div class="genre-chip" data-genre="${g}">${g}</div>`
  ).join('')

  genreGrid.querySelectorAll('.genre-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      chip.classList.toggle('selected')
      validateColdStart()
    })
  })

  // Render películas populares para calificar (seed)
  const seedGrid = $('seed-movies-grid')
  const topMovies = movies.slice(0, 10)
  seedGrid.innerHTML = topMovies.map(m => `
    <div class="seed-movie-card" data-id="${m.movie_id}">
      <img src="${posterUrl(m.poster_url, m.title)}"
           alt="${m.title}"
           onerror="this.src='https://placehold.co/100x150/16161f/7a7888?text=?'">
      <div class="seed-stars">
        <div class="mini-stars" data-id="${m.movie_id}">
          ${[1,2,3,4,5].map(n =>
            `<span style="font-size:14px;color:var(--muted);cursor:pointer"
                   data-val="${n}">★</span>`
          ).join('')}
        </div>
        <span style="font-size:10px;color:#fff;margin-top:2px">${m.title.slice(0,14)}</span>
      </div>
    </div>
  `).join('')

  // Bind mini estrellas del cold-start
  const seedRatings = {}
  seedGrid.querySelectorAll('.mini-stars').forEach(block => {
    const movieId = +block.dataset.id
    block.querySelectorAll('span').forEach(star => {
      star.addEventListener('click', (e) => {
        e.stopPropagation()
        const val = +star.dataset.val
        seedRatings[movieId] = val
        block.querySelectorAll('span').forEach(s => {
          s.style.color = +s.dataset.val <= val ? 'var(--accent)' : 'var(--muted)'
        })
      })
    })
  })

  // Mostrar/ocultar las estrellas al hover sobre la tarjeta
  seedGrid.querySelectorAll('.seed-movie-card').forEach(card => {
    card.querySelector('.seed-stars').style.display = 'flex'
  })

  validateColdStart()

  // Submit
  $('cold-start-submit').addEventListener('click', async () => {
    const selectedGenres = [...genreGrid.querySelectorAll('.genre-chip.selected')]
      .map(c => c.dataset.genre)

    const seedRatingsArr = Object.entries(seedRatings).map(([id, score]) => ({
      movie_id: +id, score
    }))

    const btn = $('cold-start-submit')
    btn.disabled = true
    btn.textContent = 'Guardando...'

    try {
      const res = await fetch(`${API}/api/cold-start`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ genres: selectedGenres, seed_ratings: seedRatingsArr })
      })
      const data = await res.json()
      if (data.success) {
        overlay.style.display = 'none'
        await loadHome()
        showToast('¡Perfil creado! Aquí van tus recomendaciones.')
      }
    } catch {
      btn.disabled = false
      btn.textContent = 'Empezar'
      showToast('Error guardando preferencias. Intenta de nuevo.')
    }
  })
}

function validateColdStart() {
  const selected = document.querySelectorAll('.genre-chip.selected').length
  $('cold-start-submit').disabled = selected < 1
}

// ─── HOME ─────────────────────────────────────────────────────
async function loadHome() {
  $('section-home').classList.add('active')

  // Cargar en paralelo
  const [trendingRes, recommendRes] = await Promise.allSettled([
    fetch(`${API}/api/movies/trending?limit=24`, { credentials: 'include' }),
    fetch(`${API}/api/recommend?limit=16`,        { credentials: 'include' }),
  ])

  const trending   = trendingRes.status   === 'fulfilled' ? await trendingRes.value.json()   : null
  const recommend  = recommendRes.status  === 'fulfilled' ? await recommendRes.value.json()  : null

  const container = $('carousels-container')
  container.innerHTML = ''

  const country = State.session?.country_name || State.session?.country_code || 'tu país'

  // Carrusel 1 — Lo más visto en tu país
  if (trending?.movies?.length) {
    State.allMovies = [...State.allMovies, ...trending.movies]
    renderCarousel(container, {
      title:       `Lo más visto en ${country}`,
      explanation: 'Tendencias en tu región',
      movies:      trending.movies,
      icon:        'ti-flame',
    })
  }

  // Carrusel 2 — Recomendado para ti
  if (recommend?.movies?.length) {
    State.allMovies = [...State.allMovies, ...recommend.movies]
    renderCarousel(container, {
      title:       'Recomendado para ti',
      explanation: recommend.explanation || '',
      movies:      recommend.movies,
      icon:        'ti-sparkles',
    })
  }

  // Carrusel 3 — Por género (top 2 géneros del usuario)
  if (recommend?.movies?.length) {
    const genreMap = {}
    recommend.movies.forEach(m => {
      genres(m).forEach(g => { genreMap[g] = (genreMap[g]||0) + 1 })
    })
    const topGenre = Object.entries(genreMap).sort((a,b)=>b[1]-a[1])[0]?.[0]
    if (topGenre) {
      const filtered = (trending?.movies || []).filter(m =>
        genres(m).includes(topGenre)
      )
      if (filtered.length >= 3) {
        renderCarousel(container, {
          title:       `Porque te gusta ${topGenre}`,
          explanation: `Películas de ${topGenre} que otros disfrutan`,
          movies:      filtered,
          icon:        'ti-tag',
        })
      }
    }
  }

  // Si no hay nada aún, mostrar solo populares
  if (!container.children.length) {
    renderCarousel(container, {
      title:       'Películas populares',
      explanation: 'Las favoritas de la comunidad',
      movies:      trending?.movies || [],
      icon:        'ti-star',
    })
  }

  // Inicializar búsqueda con el catálogo cargado
  initSearch()
}

// ─── CARRUSEL ─────────────────────────────────────────────────
function renderCarousel(container, { title, explanation, movies, icon }) {
  if (!movies.length) return

  const section = document.createElement('div')
  section.className = 'carousel-section'
  section.innerHTML = `
    <div class="carousel-header">
      <i class="ti ${icon}" style="color:var(--accent);font-size:18px" aria-hidden="true"></i>
      <span class="carousel-title">${title}</span>
      ${explanation ? `<span class="carousel-explanation">— ${explanation}</span>` : ''}
    </div>
    <div class="carousel-track-wrap">
      <div class="carousel-track">
        ${movies.map(m => movieCardHTML(m)).join('')}
      </div>
    </div>
  `
  container.appendChild(section)

  // Bind clicks + scroll tracking
  section.querySelectorAll('.movie-card').forEach((card, idx) => {
    card.addEventListener('click', () => {
      const id = +card.dataset.id
      const movie = movies.find(m => m.movie_id === id)
      openModal(movie)
      logInteraction('click', id, { carousel: title, position: idx })
    })
  })

  // Scroll tracking (qué tanto recorrió el carrusel)
  const track = section.querySelector('.carousel-track')
  let scrollTimer = null
  track.addEventListener('scroll', () => {
    clearTimeout(scrollTimer)
    scrollTimer = setTimeout(() => {
      const pct = Math.round((track.scrollLeft / (track.scrollWidth - track.clientWidth)) * 100)
      if (pct > 20) logInteraction('scroll', null, { carousel: title, scroll_pct: pct })
    }, 500)
  })
}

function movieCardHTML(m) {
  const g = genres(m).slice(0,2).join(' · ')
  const rating = m.avg_rating ? (+m.avg_rating).toFixed(1) : ''
  return `
    <div class="movie-card" data-id="${m.movie_id}" title="${m.title}">
      <img src="${posterUrl(m.poster_url, m.title)}"
           alt="${m.title}"
           loading="lazy"
           onerror="this.src='https://placehold.co/150x225/16161f/7a7888?text=?'">
      <div class="movie-card-info">
        <div class="movie-card-title">${m.title}</div>
        <div class="movie-card-meta">${m.year || ''} ${g ? '· '+g : ''}</div>
        ${rating ? `<div class="movie-card-rating">★ ${rating}</div>` : ''}
      </div>
    </div>`
}

// ─── MODAL ────────────────────────────────────────────────────
async function openModal(moviePreview) {
  const modal = $('movie-modal')
  modal.classList.add('open')
  document.body.style.overflow = 'hidden'

  // Mostrar datos básicos inmediatamente
  renderModalBasic(moviePreview)

  // Cargar detalle completo (con user_rating)
  try {
    const res  = await fetch(`${API}/api/movies/${moviePreview.movie_id}`, { credentials: 'include' })
    const data = await res.json()
    State.currentMovie = data
    renderModalFull(data)
    logInteraction('detail_view', data.movie_id, { title: data.title })
  } catch {
    State.currentMovie = moviePreview
  }
}

function renderModalBasic(m) {
  const g = genres(m)
  $('modal-backdrop').src   = m.backdrop_url || posterUrl(m.poster_url, m.title)
  $('modal-poster-img').src = posterUrl(m.poster_url, m.title)
  $('modal-title').textContent   = m.title
  $('modal-meta').textContent    = [m.year, m.runtime_min ? m.runtime_min+'min' : '', m.director].filter(Boolean).join(' · ')
  $('modal-genres').innerHTML    = g.map(g => `<span class="modal-genre-tag">${g}</span>`).join('')
  $('modal-synopsis').textContent = m.synopsis || 'Sin descripción disponible.'
  $('modal-explanation').textContent = m.explanation || ''
  $('modal-explanation').style.display = m.explanation ? 'block' : 'none'
  $('modal-star-container').innerHTML = '<div class="loader"><span></span><span></span><span></span></div>'
}

function renderModalFull(m) {
  // Actualizar campos que pueden haber llegado del backend
  $('modal-synopsis').textContent  = m.synopsis || 'Sin descripción disponible.'
  $('modal-meta').textContent = [m.year, m.runtime_min ? m.runtime_min+'min' : '', m.director].filter(Boolean).join(' · ')

  // Inicializar estrellas
  const starContainer = $('modal-star-container')
  starContainer.innerHTML = ''

  const label = document.createElement('p')
  label.style.cssText = 'font-size:13px;color:var(--muted);margin-bottom:8px'
  label.textContent = 'Tu calificación:'
  starContainer.appendChild(label)

  State.starRating = new StarRating(starContainer, {
    initialScore: m.user_rating || 0,
    onRate: async (score) => {
      try {
        await fetch(`${API}/api/rate`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ movie_id: m.movie_id, score })
        })
        showToast(`Calificaste "${m.title}" con ${score} ★`)
      } catch {
        showToast('Error al guardar la calificación.')
      }
    }
  })
}

function closeModal() {
  $('movie-modal').classList.remove('open')
  document.body.style.overflow = ''
  State.currentMovie = null
  State.starRating   = null
}

// ─── INTERACCIONES ────────────────────────────────────────────
function logInteraction(eventType, movieId, payload = {}) {
  fetch(`${API}/api/interact`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_type: eventType, movie_id: movieId, payload })
  }).catch(() => {})
}

// ─── BÚSQUEDA ─────────────────────────────────────────────────
function initSearch() {
  State.search = new MovieSearch({
    inputEl:      $('search-input'),
    resultsEl:    $('search-results-dropdown'),
    correctionEl: $('search-correction'),
    onSelect: (movie) => {
      if (movie) openModal(movie)
    }
  })
  // Alimentar diccionario con películas ya cargadas
  const unique = [...new Map(State.allMovies.map(m => [m.movie_id, m])).values()]
  State.search.buildDictionary(unique)
}

// ─── INIT ─────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  // Cerrar modal
  $('modal-close-btn').addEventListener('click', closeModal)
  $('movie-modal').addEventListener('click', (e) => {
    if (e.target === $('movie-modal')) closeModal()
  })
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeModal()
  })

  await initSession()
})