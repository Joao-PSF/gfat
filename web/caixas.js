// Editor de caixas sobre uma foto, usado na Revisão de imagens e no Editor de dataset.
// Coordenadas sempre em pixels da imagem original (a mesma convenção da API e do YOLO).
class EditorCaixas {
  constructor(vista, { aoSelecionar = () => {}, aoMudar = () => {}, aoCriar = () => {}, minimo = 8 } = {}) {
    this.vista = vista;
    this.palco = vista.querySelector('.palco');
    this.palco.innerHTML = '<img alt="" draggable="false"><div class="camada"></div>';
    this.img = this.palco.querySelector('img');
    this.camada = this.palco.querySelector('.camada');
    Object.assign(this, { aoSelecionar, aoMudar, aoCriar, minimo });
    this.caixas = []; this.sel = null; this.modo = 'selecionar'; this.zoom = 1; this.rotulos = true; this.L = 1; this.A = 1;
    this.palco.addEventListener('pointerdown', e => this._inicio(e));
    new ResizeObserver(() => requestAnimationFrame(() => this.ajustar())).observe(vista);
  }

  carregar(src, largura, altura, alt) {
    this.L = largura; this.A = altura;
    this.img.alt = alt || '';
    this.img.src = src;
    this.ajustar();
  }

  definir(caixas, sel = this.sel) { this.caixas = caixas; this.sel = sel; this.render(); }

  definirModo(modo) { this.modo = modo; this.palco.classList.toggle('desenhar', modo === 'desenhar'); }

  ajustar() {
    const r = this.vista.getBoundingClientRect();
    const base = Math.max(260, Math.min(r.width - 64, (r.height - 84) * this.L / this.A));
    const w = Math.round(base * this.zoom);
    this.palco.style.width = w + 'px';
    this.palco.style.height = Math.round(w * this.A / this.L) + 'px';
  }

  _estilo(b) {
    const p = (v, t) => (v / t * 100).toFixed(3) + '%';
    return `left:${p(b[0], this.L)};top:${p(b[1], this.A)};width:${p(b[2] - b[0], this.L)};height:${p(b[3] - b[1], this.A)}`;
  }

  render() {
    const esc = G.esc;
    this.camada.innerHTML = this.caixas.map(c => {
      const sel = c.id === this.sel;
      return `<div class="caixa ${sel ? 'sel' : ''} ${c.tracejada ? 'tracejada' : ''} ${c.fixa ? 'fixa' : ''}" data-id="${esc(c.id)}"
        style="${this._estilo(c.bbox)};--cor:${esc(c.cor)};z-index:${sel ? 6 : c.fundo ? 1 : 2}" title="${esc(c.titulo || c.rotulo || '')}">
        ${(this.rotulos || sel) && c.rotulo ? `<span class="rotulo">${esc(c.rotulo)}</span>` : ''}
        ${sel && !c.fixa ? '<i data-canto="nw"></i><i data-canto="ne"></i><i data-canto="sw"></i><i data-canto="se"></i>' : ''}</div>`;
    }).join('') + (this.temp ? `<div class="nova" style="${this._estilo(this.temp)}"></div>` : '');
  }

  _px(e) {
    const r = this.palco.getBoundingClientRect();
    const lim = (v, m) => Math.min(Math.max(v, 0), m);
    return [lim((e.clientX - r.left) / r.width * this.L, this.L), lim((e.clientY - r.top) / r.height * this.A, this.A)];
  }

  _inicio(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    const p0 = this._px(e), alvo = e.target.closest('.caixa'), canto = e.target.dataset.canto;
    let op = null;
    if (this.modo === 'desenhar') {
      op = { tipo: 'criar' };
    } else if (alvo) {
      const c = this.caixas.find(x => String(x.id) === alvo.dataset.id);
      this.sel = c.id; this.render(); this.aoSelecionar(c.id);
      if (!c.fixa) op = { tipo: canto || 'mover', caixa: c, original: c.bbox.slice() };
    } else {
      this.sel = null; this.render(); this.aoSelecionar(null);
    }
    if (!op) return;
    try { this.palco.setPointerCapture(e.pointerId); } catch { /* ponteiro já solto: segue sem captura */ }
    const mover = ev => {
      const [x, y] = this._px(ev), dx = x - p0[0], dy = y - p0[1];
      if (op.tipo === 'criar') {
        this.temp = [Math.min(p0[0], x), Math.min(p0[1], y), Math.max(p0[0], x), Math.max(p0[1], y)];
      } else if (op.tipo === 'mover') {
        const [x1, y1, x2, y2] = op.original, w = x2 - x1, h = y2 - y1;
        const nx = Math.min(Math.max(x1 + dx, 0), this.L - w), ny = Math.min(Math.max(y1 + dy, 0), this.A - h);
        op.caixa.bbox = [nx, ny, nx + w, ny + h];
      } else {
        let [x1, y1, x2, y2] = op.original;
        if (op.tipo.includes('w')) x1 = Math.min(x, x2 - this.minimo); else x2 = Math.max(x, x1 + this.minimo);
        if (op.tipo.includes('n')) y1 = Math.min(y, y2 - this.minimo); else y2 = Math.max(y, y1 + this.minimo);
        op.caixa.bbox = [x1, y1, x2, y2];
      }
      op.mudou = true;
      this.render();
    };
    const fim = () => {
      this.palco.removeEventListener('pointermove', mover);
      this.palco.removeEventListener('pointerup', fim);
      this.palco.removeEventListener('pointercancel', fim);
      if (op.tipo === 'criar') {
        const b = this.temp; this.temp = null; this.render();
        if (b && b[2] - b[0] >= this.minimo && b[3] - b[1] >= this.minimo) this.aoCriar(b.map(Math.round));
      } else if (op.mudou) {
        op.caixa.bbox = op.caixa.bbox.map(Math.round);
        this.aoMudar(op.caixa.id, op.caixa.bbox, op.original);
      }
    };
    this.palco.addEventListener('pointermove', mover);
    this.palco.addEventListener('pointerup', fim);
    this.palco.addEventListener('pointercancel', fim);
  }
}
