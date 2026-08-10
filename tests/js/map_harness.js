/* Executa o script de map.html com um DOM minimo e simula o desenho.
   Verifica o que o usuario faz: ligar a ferramenta, clicar, fechar a area. */
const fs = require('fs');
const vm = require('vm');

const LARGURA = 800, ALTURA = 600;

function elemento(id) {
  const ouvintes = {};
  return {
    id, style: {}, innerHTML: '', textContent: '', children: [],
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
      toggle(c, on) { on === undefined ? (this._s.has(c) ? this._s.delete(c) : this._s.add(c)) : (on ? this._s.add(c) : this._s.delete(c)); },
      contains(c) { return this._s.has(c); },
    },
    addEventListener(tipo, fn) { (ouvintes[tipo] = ouvintes[tipo] || []).push(fn); },
    _disparar(tipo, ev) { (ouvintes[tipo] || []).forEach(fn => fn(ev)); },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: LARGURA, height: ALTURA }),
    appendChild(n) { this.children.push(n); return n; },
    removeChild(n) { this.children = this.children.filter(c => c !== n); return n; },
    get firstChild() { return this.children[0] || null; },
    querySelectorAll: () => [],
    querySelector: () => null,
    remove() {},
    blur() {}, focus() {},
  };
}

const elementos = {};
for (const id of ['map', 'tiles', 'box', 'shape', 'status', 'credit',
                  'btn-select', 'btn-draw', 'btn-layer', 'btn-clear',
                  'btn-in', 'btn-out', 'search']) {
  elementos[id] = elemento(id);
}

const janelaOuvintes = {};
const contexto = {
  console,
  document: {
    getElementById: id => elementos[id],
    createElementNS: (ns, tipo) => ({
      ns, tipo, atributos: {}, textContent: '', children: [],
      setAttribute(k, v) { this.atributos[k] = v; },
      appendChild(n) { this.children.push(n); return n; },
    }),
    addEventListener() {},
    createElement: tipo => ({
      tipo, style: {}, dataset: {}, atributos: {},
      setAttribute(k, v) { this.atributos[k] = v; },
      addEventListener() {}, appendChild(n) { return n; },
    }),
    createDocumentFragment: () => ({
      children: [], appendChild(n) { this.children.push(n); return n; },
    }),
  },
  window: {
    addEventListener(tipo, fn) { (janelaOuvintes[tipo] = janelaOuvintes[tipo] || []).push(fn); },
  },
  Math, JSON, Object, Array, String, Number, Boolean, Date, isNaN, parseFloat, parseInt,
  setTimeout, Image: function () { return { style: {}, addEventListener() {} }; },
  QWebChannel: function (transporte, cb) {
    cb({ objects: { bridge: registro.bridge } });
  },
  qt: { webChannelTransport: {} },
};
contexto.window.mapforge = undefined;
contexto.globalThis = contexto;

const registro = { poligonos: [], selecoes: [] };
registro.bridge = {
  setPolygon(plano) { registro.poligonos.push(plano); },
  setSelection(...a) { registro.selecoes.push(a); },
  search() {}, ready() {},
};

const js = fs.readFileSync(process.argv[2], 'utf8');
vm.createContext(contexto);
try {
  vm.runInContext(js, contexto);
} catch (e) {
  console.log('FALHOU ao carregar o script:', e.message);
  process.exit(1);
}

const mapa = elementos['map'];

/* Reproduz a sequencia que o navegador emite num toque: mousedown no elemento,
   mouseup na janela e, por fim, o `click` sintetico. Passar os tres garante que
   o teste vale tanto para uma implementacao baseada em `click` quanto para uma
   baseada em `mouseup`. */
const toque = (x, y, detail = 1) => {
  mapa._disparar('mousedown', { clientX: x, clientY: y, preventDefault() {}, shiftKey: false });
  (janelaOuvintes['mouseup'] || []).forEach(fn => fn({ clientX: x, clientY: y }));
  mapa._disparar('click', { clientX: x, clientY: y, detail, preventDefault() {} });
};

// 1. Liga a ferramenta.
elementos['btn-draw'].onclick({ currentTarget: elementos['btn-draw'] });
const ligou = elementos['btn-draw'].classList.contains('active');
const cruz = mapa.classList.contains('selecting');

// 2. Tres toques em lugares distintos.
toque(100, 100); toque(300, 120); toque(220, 320);
const nosDesenhados = elementos['shape'].children.length;

// 3. Fecha repetindo o toque no mesmo lugar (duplo toque).
toque(220, 320, 2);
mapa._disparar('dblclick', { preventDefault() {} });

console.log('botao ficou ativo........:', ligou);
console.log('cursor virou cruz........:', cruz);
console.log('nos SVG apos 3 cliques...:', nosDesenhados, '(esperado >= 4: 1 linha + 3 circulos)');
console.log('poligonos enviados.......:', registro.poligonos.length);
if (registro.poligonos.length) {
  console.log('vertices no poligono.....:', registro.poligonos[0].length / 2, '(esperado 3)');
}
const ok = ligou && cruz && nosDesenhados >= 4
  && registro.poligonos.length === 1 && registro.poligonos[0].length === 6;
console.log(ok ? '\nRESULTADO: ok' : '\nRESULTADO: FALHOU');
process.exit(ok ? 0 : 1);
