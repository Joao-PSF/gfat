// Partes comuns das páginas do GeoFAT: menu lateral, chamadas à API, formatação e acompanhamento de tarefas.
const G = (() => {
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = (sel, raiz = document) => raiz.querySelector(sel);
  const $$ = (sel, raiz = document) => [...raiz.querySelectorAll(sel)];
  const nf = (n, d = 0) => n == null ? '—' : Number(n).toLocaleString('pt-BR', { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (a, b) => b ? Math.round(a / b * 100) : 0;
  const dataHora = v => v ? new Date(v).toLocaleString('pt-BR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';
  const duracao = s => s == null ? '' : s < 60 ? `${Math.round(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.floor(s / 3600)} h ${Math.round(s % 3600 / 60)} min`;
  const bytes = b => b == null ? '—' : b < 1e6 ? `${nf(b / 1e3)} kB` : b < 1e9 ? `${nf(b / 1e6, 1)} MB` : `${nf(b / 1e9, 2)} GB`;

  const CATEGORIAS = ['residencial', 'comercial', 'misto', 'industrial', 'institucional', 'terreno_vazio', 'inconclusivo'];
  const CAT_NOME = { residencial: 'Residencial', comercial: 'Comercial', misto: 'Misto', industrial: 'Industrial', institucional: 'Institucional',
                     terreno_vazio: 'Terreno vazio', inconclusivo: 'Inconclusivo', sem_categoria: 'Sem classificação' };
  const CAT_COR = { residencial: '#4C7FD0', comercial: '#E8912D', misto: '#A26BD1', industrial: '#6F7B89', institucional: '#2FA36B',
                    terreno_vazio: '#A07A4B', inconclusivo: '#A9B0B9', sem_categoria: '#D6DAE0' };
  const SITUACAO = {
    divergencia_confirmada: ['Divergência confirmada', 'erro'], possivel_divergencia: ['Possível divergência', 'laranja'],
    sem_matricula_confirmada: ['Sem matrícula (confirmado)', 'roxo'], sem_correspondencia: ['Sem matrícula correspondente', 'roxo'],
    associacao_inconclusiva: ['Associação inconclusiva', ''], classificacao_inconclusiva: ['Classificação inconclusiva', ''],
    ok: ['Sem divergência', 'ok'], nao_classificado: ['Não classificado', ''], descartado: ['Descartado', ''],
  };
  const LOCAL = { alta: 'alta (confirmada)', media: 'média (sugerida)', baixa: 'baixa (pendente de revisão)' };
  const FONTE_LOCAL = { camera: 'sem edificação na linha de visada, fica na posição da câmera', direcao: 'direção da câmera + edificação',
                        lado_via: 'lado da via + sentido do deslocamento (direção estimada, não medida)', manual: 'definida pelo revisor' };
  const FONTE_EDIF = { osm: 'OpenStreetMap', open_buildings: 'Open Buildings' };
  // Evidências geográficas de uma edificação candidata, em texto
  const evidencia = e => [e.distancia_m != null && `${nf(e.distancia_m, 0)} m da câmera`, e.no_raio ? 'na linha de visada' : `${nf(e.desvio_graus, 0)}° fora da linha de visada`,
    e.primeira_no_raio && 'primeira atingida', e.lado && `lado ${e.lado} da via`,
    e.poligono_pouco_confiavel != null && `polígono pouco confiável (score ${nf(e.poligono_pouco_confiavel, 2)})`].filter(Boolean).join(' · ');
  const pc = (v, t) => (v / t * 100) + '%';
  const miniatura = (o, w = 240) => `<span class="foto-caixa"><img src="/api/fotos/${o.foto_id}/imagem?w=${w}" alt="Foto da observação #${o.id}" loading="lazy">${o.bbox
    ? `<span class="caixa-fixa" style="left:${pc(o.bbox[0], o.largura)};top:${pc(o.bbox[1], o.altura)};width:${pc(o.bbox[2] - o.bbox[0], o.largura)};height:${pc(o.bbox[3] - o.bbox[1], o.altura)}"></span>` : ''}</span>`;

  async function api(url, corpo, metodo) {
    const op = corpo === undefined && !metodo ? {} : { method: metodo || 'POST', headers: { 'Content-Type': 'application/json' }, body: corpo === undefined ? undefined : JSON.stringify(corpo) };
    const r = await fetch(url, op);
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      const d = Array.isArray(e.detail) ? e.detail.map(x => x.msg).join('; ') : e.detail;
      throw new Error(d || `${r.status} ${r.statusText}`);
    }
    return r.json();
  }
  // Arquivo como corpo cru (o servidor não precisa de multipart)
  async function enviar(url, arquivo) {
    const r = await fetch(url, { method: 'POST', body: arquivo, headers: { 'Content-Type': arquivo.type || 'application/octet-stream' } });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    return r.json();
  }

  const ICONES = {
    inicio: '<path d="M4 11.5 12 5l8 6.5V20h-5v-5h-6v5H4z"/>',
    mapa: '<path d="M9 4.5 3.5 6.5v13l5.5-2 6 2 5.5-2v-13l-5.5 2-6-2z"/><path d="M9 4.5v13M15 6.5v13"/>',
    imagens: '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/><circle cx="9" cy="10" r="1.8"/><path d="m20.5 16-4.5-4.5-8 8"/>',
    comercial: '<path d="M8 7h11M8 12h11M8 17h11"/><path d="m3.5 7 1 1 2-2M3.5 12l1 1 2-2M3.5 17l1 1 2-2"/>',
    modelos: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2.5V6M15 2.5V6M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
    treino: '<path d="M3.5 3.5v17h17"/><path d="m7.5 15 3.5-4.5 3 2.5 5-6.5"/>',
    tarefas: '<path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5"/><path d="M12 7.5V12l3 2"/>',
    det: '<path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M8 9h8v6H8z"/>', baixar: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    subir: '<path d="M12 20V9M7 14l5-5 5 5M5 4h14"/>', texto: '<path d="M5 6h14M12 6v13M8 19h8"/>',
    cerebro: '<path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 0V4zM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 0"/>',
    nuvem: '<path d="M7 18a4 4 0 0 1-.5-8 5.5 5.5 0 0 1 10.6 1.5A3.3 3.3 0 0 1 17 18z"/>', sync: '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 5v6h-6"/>',
    alvo: '<circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/>', fechar: '<path d="M6 6l12 12M18 6 6 18"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>', esq: '<path d="m15 6-6 6 6 6"/>', dir: '<path d="m9 6 6 6-6 6"/>', mais: '<path d="M12 5v14M5 12h14"/>',
    menos: '<path d="M5 12h14"/>', cursor: '<path d="M5 3l13 7.5-5.5 1.5L10 17.5z"/>', caixa: '<path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4M12 9v6M9 12h6"/>',
    lixo: '<path d="M5 7h14M10 7V4.5h4V7M7 7l1 13h8l1-13"/>', seta: '<path d="m6 9 6 6 6-6"/>',
    brilho: '<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.5 2.5M15.5 15.5 18 18M6 18l2.5-2.5M15.5 8.5 18 6"/>',
  };
  const icone = (nome, t = 18, extra = '') => `<svg width="${t}" height="${t}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" ${extra}>${ICONES[nome] || ''}</svg>`;

  const PAGINAS = [
    ['Operação'], ['index.html', 'Visão geral', 'inicio'], ['mapa.html', 'Mapa', 'mapa'], ['revisao.html', 'Revisão de imagens', 'imagens', 'pendentes'],
    ['comercial.html', 'Análise comercial', 'comercial'], ['Inteligência artificial'], ['modelos.html', 'Modelos', 'modelos'],
    ['treinamento.html', 'Treinamento', 'treino'], ['processamentos.html', 'Processamentos', 'tarefas', 'tarefas'],
  ];

  function menu(atual) {
    const nav = document.createElement('nav');
    nav.className = 'menu';
    nav.setAttribute('aria-label', 'Navegação principal');
    nav.innerHTML = `
      <a class="marca" href="index.html"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 8V3h5M16 3h5v5M21 16v5h-5M8 21H3v-5" stroke="#0B7A75"/><path d="M7.5 17.5v-5.5L12 8.5l4.5 3.5v5.5z" stroke="#161A1F"/></svg><span>GeoFAT</span></a>
      <div class="projeto"><span class="sigla">CA</span><span><b>CAEMA</b><small id="menu-resumo">carregando…</small></span></div>
      ${PAGINAS.map((p, i) => p.length === 1 ? `<span class="secao ${i ? 'ia' : ''}">${p[0]}</span>` :
        `<a class="item" href="${p[0]}" ${p[0] === atual ? 'aria-current="page"' : ''}>${icone(p[2])}${p[1]}${p[3] ? `<span class="contador ${p[3] === 'tarefas' ? 'ativo' : 'pendente'}" data-contador="${p[3]}" hidden></span>` : ''}</a>`).join('')}
      <span class="espaco"></span>
      <span class="nota">Sem login: use só na <strong>rede interna</strong>.</span>
      <div class="rodape">IA local · GSAN só para consulta</div>`;
    document.querySelector('.app').prepend(nav);
    atualizarMenu();
    setInterval(atualizarMenu, 10000);
  }
  async function atualizarMenu() {
    try {
      const r = await api('/api/resumo');
      $('#menu-resumo').textContent = `${nf(r.fotos)} fotos · ${nf(r.imoveis)} imóveis`;
      const c = { pendentes: r.pendentes_revisao, tarefas: r.tarefas_executando + r.tarefas_fila };
      $$('[data-contador]').forEach(el => { const v = c[el.dataset.contador]; el.hidden = !v; el.textContent = nf(v); });
      return r;
    } catch { $('#menu-resumo').textContent = 'servidor fora do ar'; }
  }

  // Progresso real de uma tarefa: com total conhecido mostra %, sem total a barra fica indeterminada
  function progresso(t) {
    const conhecido = t.total != null && t.total > 0 && t.atual != null;
    const p = conhecido ? Math.min(100, t.atual / t.total * 100) : null;
    const unid = t.unidade === 'bytes' ? null : t.unidade;
    let nota = conhecido ? (t.unidade === 'bytes' ? `${bytes(t.atual)} de ${bytes(t.total)}` : `${nf(t.atual)} de ${nf(t.total)} ${unid || ''}`) : '';
    if (t.estado === 'executando' && t.iniciada_em) {
      const decorrido = (Date.now() - new Date(t.iniciada_em)) / 1000;
      if (conhecido && t.atual > 0 && p < 100) nota += ` · cerca de ${duracao(decorrido / t.atual * (t.total - t.atual))} restantes`;
      else nota += `${nota ? ' · ' : ''}${duracao(decorrido)} decorridos`;
    }
    return { pct: p, rotulo: p == null ? '' : `${Math.floor(p)}%`, nota, html: `<div class="trilho ${p == null ? 'indet' : ''}"><span style="width:${p ?? 0}%"></span></div>` };
  }

  const ESTADO = { fila: ['Na fila', ''], executando: ['Executando', 'pri'], concluida: ['Concluída', 'ok'], falhou: ['Com falha', 'erro'], cancelada: ['Cancelada', ''] };

  // Aviso flutuante que acompanha uma tarefa até o fim (consulta o servidor a cada segundo)
  function acompanhar(id, { aoTerminar } = {}) {
    let el = $('#toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; el.className = 'toast'; el.setAttribute('role', 'status'); document.body.append(el); }
    el.hidden = false;
    let parar = false;
    const ciclo = async () => {
      if (parar) return;
      let t;
      try { t = await api(`/api/tarefas/${id}`); } catch { setTimeout(ciclo, 2000); return; }
      const p = progresso(t), fim = ['concluida', 'falhou', 'cancelada'].includes(t.estado);
      el.innerHTML = `<div class="topo"><span>${esc(fim ? ESTADO[t.estado][0] : t.titulo)}</span><span>${t.estado === 'fila' ? 'na fila' : p.rotulo}</span></div>
        ${fim ? '' : p.html}<small>${esc(fim ? (t.mensagem || '') : (t.mensagem ? t.mensagem + (p.nota ? ' · ' : '') : '') + p.nota)}</small>
        ${fim && t.arquivo ? `<a href="/api/tarefas/${t.id}/arquivo">Baixar ${esc(t.arquivo.split('/').pop())}</a>` : ''}
        <button type="button">${fim ? 'Fechar' : 'Ocultar'}</button>`;
      el.querySelector('button').onclick = () => { el.hidden = true; parar = true; };
      if (fim) { aoTerminar && aoTerminar(t); return; }
      setTimeout(ciclo, 1000);
    };
    ciclo();
  }

  // Envia o CSV do GSAN e acompanha a importação
  function importarGsan(aoTerminar) {
    const inp = document.createElement('input');
    inp.type = 'file'; inp.accept = '.csv,text/csv';
    inp.onchange = async () => {
      const f = inp.files[0]; if (!f) return;
      try { const r = await enviar(`/api/gsan/importar?nome=${encodeURIComponent(f.name)}`, f); acompanhar(r.id, { aoTerminar }); }
      catch (e) { alert('Não foi possível enviar o arquivo: ' + e.message); }
    };
    inp.click();
  }

  return { esc, $, $$, nf, pct, dataHora, duracao, bytes, api, enviar, icone, menu, atualizarMenu, progresso, acompanhar, importarGsan,
           CATEGORIAS, CAT_NOME, CAT_COR, SITUACAO, LOCAL, FONTE_LOCAL, FONTE_EDIF, ESTADO, evidencia, miniatura };
})();
