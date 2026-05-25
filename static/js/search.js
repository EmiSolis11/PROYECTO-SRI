/**
 * CineMatch — search.js
 * Buscador con autocorrección ortográfica.
 *
 * La corrección usa distancia de Levenshtein sobre un diccionario
 * construido desde los títulos de las películas del catálogo.
 * Si detecta un posible error tipográfico, muestra una sugerencia
 * "¿Quisiste decir X?" antes de enviar la búsqueda al backend.
 */

class MovieSearch {
  constructor({ inputEl, resultsEl, correctionEl, onSelect }) {
    this.input       = inputEl
    this.resultsEl   = resultsEl
    this.correctionEl= correctionEl
    this.onSelect    = onSelect || (() => {})
    this.dictionary  = []   // palabras del catálogo
    this.debounceTimer = null
    this.MIN_QUERY   = 2
    this.DEBOUNCE_MS = 350
    this._bind()
  }

  // Alimenta el diccionario con los títulos del catálogo
  buildDictionary(movies) {
    const words = new Set()
    movies.forEach(m => {
      if (!m.title) return
      m.title.toLowerCase()
        .replace(/[^a-záéíóúüñ0-9\s]/gi, '')
        .split(/\s+/)
        .filter(w => w.length > 3)
        .forEach(w => words.add(w))
    })
    this.dictionary = [...words]
  }

  // Distancia de Levenshtein entre dos strings
  _levenshtein(a, b) {
    const m = a.length, n = b.length
    const dp = Array.from({length: m+1}, (_, i) => [i])
    for (let j = 0; j <= n; j++) dp[0][j] = j
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        dp[i][j] = a[i-1] === b[j-1]
          ? dp[i-1][j-1]
          : 1 + Math.min(dp[i-1][j], dp[i][j-1], dp[i-1][j-1])
      }
    }
    return dp[m][n]
  }

  // Intenta corregir una palabra buscando la más cercana en el diccionario
  _correctWord(word) {
    if (word.length < 4) return word
    let best = word, bestDist = Infinity
    for (const dictWord of this.dictionary) {
      const dist = this._levenshtein(word.toLowerCase(), dictWord)
      if (dist < bestDist && dist <= 2) {
        bestDist = dist
        best = dictWord
      }
    }
    return best
  }

  // Corrige todas las palabras de la query
  correctQuery(raw) {
    const words     = raw.trim().split(/\s+/)
    const corrected = words.map(w => this._correctWord(w))
    const result    = corrected.join(' ')
    return {
      raw,
      corrected: result,
      changed: result.toLowerCase() !== raw.toLowerCase()
    }
  }

  async _search(rawQuery) {
    if (rawQuery.length < this.MIN_QUERY) {
      this._hideResults()
      this._hideCorrection()
      return
    }

    const { raw, corrected, changed } = this.correctQuery(rawQuery)

    // Mostrar sugerencia de corrección
    if (changed) {
      this._showCorrection(corrected, raw)
    } else {
      this._hideCorrection()
    }

    // Llamar al backend con query corregido
    try {
      const params = new URLSearchParams({ q: corrected, raw, limit: 8 })
      const res    = await fetch(`/api/movies/search?${params}`)
      const data   = await res.json()
      this._renderResults(data.movies || [])
    } catch {
      this._hideResults()
    }
  }

  _renderResults(movies) {
    if (!movies.length) {
      this.resultsEl.innerHTML = `
        <div style="padding:16px;text-align:center;color:var(--muted);font-size:13px">
          Sin resultados
        </div>`
      this.resultsEl.style.display = 'block'
      return
    }

    this.resultsEl.innerHTML = movies.map(m => {
      const genres = Array.isArray(m.genres)
        ? m.genres.slice(0,2).join(' · ')
        : (typeof m.genres === 'string'
            ? JSON.parse(m.genres || '[]').slice(0,2).join(' · ')
            : '')
      return `
        <div class="search-result-item" data-id="${m.movie_id}">
          <img src="${m.poster_url || ''}"
               onerror="this.style.background='var(--bg3)';this.removeAttribute('src')"
               alt="${m.title}">
          <div class="search-result-info">
            <div class="search-result-title">${m.title}</div>
            <div class="search-result-meta">${m.year || ''} ${genres ? '· ' + genres : ''}</div>
          </div>
          <div style="font-size:12px;color:var(--accent)">
            ${m.avg_rating ? '★ ' + (+m.avg_rating).toFixed(1) : ''}
          </div>
        </div>`
    }).join('')

    this.resultsEl.style.display = 'block'

    // Click en resultado
    this.resultsEl.querySelectorAll('.search-result-item').forEach(el => {
      el.addEventListener('click', () => {
        const id = +el.dataset.id
        const movie = movies.find(m => m.movie_id === id)
        this._hideResults()
        this._hideCorrection()
        this.input.value = ''
        this.onSelect(movie)
        // Registrar click de búsqueda
        fetch('/api/interact', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ event_type: 'click', movie_id: id,
                                 payload: { source: 'search' } })
        }).catch(() => {})
      })
    })
  }

  _showCorrection(corrected, original) {
    this.correctionEl.innerHTML =
      `¿Quisiste decir <span data-q="${corrected}">"${corrected}"</span>?`
    this.correctionEl.style.display = 'block'
    this.correctionEl.querySelector('span').addEventListener('click', () => {
      this.input.value = corrected
      this._hideCorrection()
      this._search(corrected)
    })
  }

  _hideCorrection() { this.correctionEl.style.display = 'none' }
  _hideResults()    { this.resultsEl.style.display    = 'none'  }

  _bind() {
    this.input.addEventListener('input', (e) => {
      clearTimeout(this.debounceTimer)
      const val = e.target.value.trim()
      if (!val) { this._hideResults(); this._hideCorrection(); return }
      this.debounceTimer = setTimeout(() => this._search(val), this.DEBOUNCE_MS)
    })

    this.input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        this._hideResults()
        this._hideCorrection()
        this.input.blur()
      }
    })

    // Cerrar al hacer click fuera
    document.addEventListener('click', (e) => {
      if (!this.input.closest('.search-wrap').contains(e.target)) {
        this._hideResults()
        this._hideCorrection()
      }
    })
  }
}