/**
 * CineMatch — stars.js
 * Componente reutilizable de calificación con estrellas (1-5).
 *
 * Uso:
 *   const sr = new StarRating(containerEl, { initialScore: 3.5, onRate: (score) => {} })
 *   sr.setScore(4)
 *   sr.getScore()  → 4
 */

class StarRating {
  constructor(container, options = {}) {
    this.container   = container
    this.score       = options.initialScore || 0
    this.onRate      = options.onRate || (() => {})
    this.labels      = ['Pésima', 'Mala', 'Regular', 'Buena', 'Excelente']
    this.hovered     = 0
    this._render()
  }

  _render() {
    this.container.innerHTML = `
      <div class="star-rating" role="group" aria-label="Calificación">
        ${[1,2,3,4,5].map(n => `
          <span class="star ${n <= this.score ? 'active' : ''}"
                data-val="${n}"
                role="button"
                aria-label="${n} estrella${n>1?'s':''}"
                tabindex="0">★</span>
        `).join('')}
      </div>
      <div class="star-label">${this._label()}</div>
      <div class="rating-saved ${this.score ? 'show' : ''}">
        ✓ Calificación guardada
      </div>
    `
    this._bind()
  }

  _label() {
    const s = this.hovered || this.score
    return s ? this.labels[s - 1] : 'Toca para calificar'
  }

  _bind() {
    const stars   = this.container.querySelectorAll('.star')
    const label   = this.container.querySelector('.star-label')
    const saved   = this.container.querySelector('.rating-saved')

    stars.forEach(star => {
      const val = +star.dataset.val

      star.addEventListener('mouseenter', () => {
        this.hovered = val
        this._highlight(stars, val)
        label.textContent = this.labels[val - 1]
      })

      star.addEventListener('mouseleave', () => {
        this.hovered = 0
        this._highlight(stars, this.score)
        label.textContent = this._label()
      })

      star.addEventListener('click', () => {
        this.score = val
        this._highlight(stars, val)
        label.textContent = this.labels[val - 1]
        saved.classList.add('show')
        this.onRate(val)
      })

      // Accesibilidad — teclado
      star.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          star.click()
        }
      })
    })
  }

  _highlight(stars, upTo) {
    stars.forEach(s => {
      s.classList.toggle('active', +s.dataset.val <= upTo)
    })
  }

  setScore(score) {
    this.score = score
    this._render()
  }

  getScore() { return this.score }
}