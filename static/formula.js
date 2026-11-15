/* Spreadsheet formula engine. No eval(): formulas are parsed and evaluated safely. */
(function (global) {
  'use strict';

  class Err { constructor(code) { this.code = code; } toString() { return this.code; } }

  function colToNum(s) { let n = 0; for (const ch of s.toUpperCase()) n = n * 26 + (ch.charCodeAt(0) - 64); return n; }
  function numToCol(n) { let s = ''; while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); } return s; }
  function norm(ref) { return ref.replace(/\$/g, '').toUpperCase(); }
  function parseRef(key) { const m = /^([A-Z]{1,2})(\d+)$/.exec(key); return m ? { c: colToNum(m[1]), r: parseInt(m[2], 10) } : null; }

  const CELL = '\\$?[A-Za-z]{1,2}\\$?\\d+';
  const RANGE_RE = new RegExp('^' + CELL + '(?::' + CELL + ')?(?![A-Za-z0-9_(])');
  const FN_RE = /^[A-Za-z_][A-Za-z0-9_.]*(?=\()/;
  const NUM_RE = /^(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?/;

  function tokenize(src) {
    const toks = []; let i = 0;
    while (i < src.length) {
      const ch = src[i];
      if (/\s/.test(ch)) { i++; continue; }
      const rest = src.slice(i); let m;
      if (ch === '"') {
        let j = i + 1, s = '';
        for (;;) {
          if (j >= src.length) throw new Err('#ERROR!');
          if (src[j] === '"') { if (src[j + 1] === '"') { s += '"'; j += 2; continue; } break; }
          s += src[j++];
        }
        toks.push({ k: 'str', v: s }); i = j + 1; continue;
      }
      if ((m = NUM_RE.exec(rest))) { toks.push({ k: 'num', v: parseFloat(m[0]) }); i += m[0].length; continue; }
      if ((m = FN_RE.exec(rest))) { toks.push({ k: 'fn', v: m[0] }); i += m[0].length; continue; }
      if ((m = RANGE_RE.exec(rest))) {
        const parts = m[0].split(':');
        toks.push(parts.length === 2 ? { k: 'range', a: norm(parts[0]), b: norm(parts[1]) } : { k: 'ref', v: norm(parts[0]) });
        i += m[0].length; continue;
      }
      if ((m = /^(TRUE|FALSE)(?![A-Za-z0-9_])/i.exec(rest))) { toks.push({ k: 'bool', v: m[1].toUpperCase() === 'TRUE' }); i += m[0].length; continue; }
      if ((m = /^(<>|<=|>=)/.exec(rest))) { toks.push({ k: 'op', v: m[0] }); i += 2; continue; }
      if ('+-*/^&=<>(),%'.includes(ch)) { toks.push({ k: 'op', v: ch }); i++; continue; }
      throw new Err(/[A-Za-z]/.test(ch) ? '#NAME?' : '#ERROR!');
    }
    return toks;
  }

  function parse(src) {
    const toks = tokenize(src); let p = 0;
    const next = () => toks[p++];
    const isOp = (o) => toks[p] && toks[p].k === 'op' && toks[p].v === o;
    const expect = (o) => { if (!isOp(o)) throw new Err('#ERROR!'); p++; };
    function cmp() {
      let l = concat();
      while (toks[p] && toks[p].k === 'op' && ['=', '<>', '<', '>', '<=', '>='].includes(toks[p].v)) { const op = next().v; l = { t: 'bin', op, l, r: concat() }; }
      return l;
    }
    function concat() { let l = add(); while (isOp('&')) { next(); l = { t: 'bin', op: '&', l, r: add() }; } return l; }
    function add() { let l = mul(); while (isOp('+') || isOp('-')) { const op = next().v; l = { t: 'bin', op, l, r: mul() }; } return l; }
    function mul() { let l = pow(); while (isOp('*') || isOp('/')) { const op = next().v; l = { t: 'bin', op, l, r: pow() }; } return l; }
    function pow() { let l = unary(); while (isOp('^')) { next(); l = { t: 'bin', op: '^', l, r: unary() }; } return l; }
    function unary() {
      if (isOp('-')) { next(); return { t: 'neg', e: unary() }; }
      if (isOp('+')) { next(); return unary(); }
      return postfix();
    }
    function postfix() { let e = primary(); while (isOp('%')) { next(); e = { t: 'pct', e }; } return e; }
    function primary() {
      const tk = next();
      if (!tk) throw new Err('#ERROR!');
      switch (tk.k) {
        case 'num': return { t: 'lit', v: tk.v };
        case 'str': return { t: 'lit', v: tk.v };
        case 'bool': return { t: 'lit', v: tk.v };
        case 'ref': return { t: 'ref', key: tk.v };
        case 'range': return { t: 'range', a: tk.a, b: tk.b };
        case 'fn': {
          expect('('); const args = [];
          if (!isOp(')')) { do { args.push(cmp()); } while (isOp(',') && next()); }
          expect(')');
          return { t: 'fn', name: tk.v.toUpperCase(), args };
        }
        case 'op': if (tk.v === '(') { const e = cmp(); expect(')'); return e; }
      }
      throw new Err('#ERROR!');
    }
    const ast = cmp();
    if (p < toks.length) throw new Err('#ERROR!');
    return ast;
  }

  function rangeKeys(a, b) {
    const A = parseRef(a), B = parseRef(b);
    if (!A || !B) throw new Err('#REF!');
    const c1 = Math.min(A.c, B.c), c2 = Math.max(A.c, B.c), r1 = Math.min(A.r, B.r), r2 = Math.max(A.r, B.r);
    if ((c2 - c1 + 1) * (r2 - r1 + 1) > 100000) throw new Err('#VALUE!');
    const keys = [];
    for (let r = r1; r <= r2; r++) for (let c = c1; c <= c2; c++) keys.push(numToCol(c) + r);
    return keys;
  }

  function fmtNumber(v) {
    if (Number.isInteger(v)) return String(v);
    return String(parseFloat(v.toPrecision(12)));
  }
  function format(v) {
    if (v instanceof Err) return v.code;
    if (typeof v === 'number') return Number.isFinite(v) ? fmtNumber(v) : '#NUM!';
    if (typeof v === 'boolean') return v ? 'TRUE' : 'FALSE';
    return v === null || v === undefined ? '' : String(v);
  }

  class Sheet {
    constructor(cells) { this.cells = cells || {}; this.cache = new Map(); this.stack = new Set(); }
    raw(key) { const v = this.cells[key]; return v === undefined ? '' : String(v); }
    get(key) {
      if (this.cache.has(key)) return this.cache.get(key);
      if (this.stack.has(key)) return new Err('#CIRC!');
      this.stack.add(key);
      let v; const raw = this.raw(key);
      try {
        if (raw.startsWith('=')) v = this.node(parse(raw.slice(1)));
        else if (raw.trim() !== '' && Number.isFinite(Number(raw))) v = Number(raw);
        else v = raw;
      } catch (e) { v = e instanceof Err ? e : new Err('#ERROR!'); }
      this.stack.delete(key);
      this.cache.set(key, v);
      return v;
    }
    num(v) {
      if (v instanceof Err) return v;
      if (v === '' || v === null) return 0;
      if (typeof v === 'boolean') return v ? 1 : 0;
      if (typeof v === 'number') return v;
      const x = Number(v);
      return String(v).trim() !== '' && Number.isFinite(x) ? x : new Err('#VALUE!');
    }
    text(v) { return v instanceof Err ? v : format(v); }
    node(n) {
      switch (n.t) {
        case 'lit': return n.v;
        case 'ref': return this.get(n.key);
        case 'range': return new Err('#VALUE!');
        case 'neg': { const v = this.num(this.node(n.e)); return v instanceof Err ? v : -v; }
        case 'pct': { const v = this.num(this.node(n.e)); return v instanceof Err ? v : v / 100; }
        case 'bin': return this.bin(n);
        case 'fn': return this.fn(n);
      }
      return new Err('#ERROR!');
    }
    bin(n) {
      const l = this.node(n.l), r = this.node(n.r);
      if (l instanceof Err) return l;
      if (r instanceof Err) return r;
      if (n.op === '&') return this.text(l) + this.text(r);
      if (['=', '<>', '<', '>', '<=', '>='].includes(n.op)) {
        let a = l, b = r;
        if (!(typeof a === 'number' && typeof b === 'number')) { a = format(l).toLowerCase(); b = format(r).toLowerCase(); }
        switch (n.op) { case '=': return a === b; case '<>': return a !== b; case '<': return a < b; case '>': return a > b; case '<=': return a <= b; default: return a >= b; }
      }
      const a = this.num(l), b = this.num(r);
      if (a instanceof Err) return a;
      if (b instanceof Err) return b;
      switch (n.op) {
        case '+': return a + b;
        case '-': return a - b;
        case '*': return a * b;
        case '/': return b === 0 ? new Err('#DIV/0!') : a / b;
        case '^': { const x = Math.pow(a, b); return Number.isNaN(x) ? new Err('#NUM!') : x; }
      }
      return new Err('#ERROR!');
    }
    flat(args) {
      const out = [];
      for (const a of args) {
        if (a.t === 'range') for (const k of rangeKeys(a.a, a.b)) out.push({ v: this.get(k), range: true });
        else out.push({ v: this.node(a), range: false });
      }
      return out;
    }
    numbers(args) {
      const out = [];
      for (const { v, range } of this.flat(args)) {
        if (v instanceof Err) return v;
        if (range) { if (typeof v === 'number') out.push(v); }
        else { const x = this.num(v); if (x instanceof Err) return x; out.push(x); }
      }
      return out;
    }
    fn(n) {
      const a = n.args;
      switch (n.name) {
        case 'SUM': { const x = this.numbers(a); return x instanceof Err ? x : x.reduce((s, v) => s + v, 0); }
        case 'AVERAGE': { const x = this.numbers(a); if (x instanceof Err) return x; return x.length ? x.reduce((s, v) => s + v, 0) / x.length : new Err('#DIV/0!'); }
        case 'MIN': { const x = this.numbers(a); return x instanceof Err ? x : (x.length ? Math.min(...x) : 0); }
        case 'MAX': { const x = this.numbers(a); return x instanceof Err ? x : (x.length ? Math.max(...x) : 0); }
        case 'COUNT': return this.flat(a).filter(({ v, range }) => (range ? typeof v === 'number' : !(v instanceof Err) && !(this.num(v) instanceof Err))).length;
        case 'IF': {
          if (a.length < 2) return new Err('#ERROR!');
          const c = this.node(a[0]);
          if (c instanceof Err) return c;
          const truth = typeof c === 'boolean' ? c : (typeof c === 'number' ? c !== 0 : null);
          if (truth === null) return new Err('#VALUE!');
          return truth ? this.node(a[1]) : (a[2] ? this.node(a[2]) : false);
        }
        case 'ROUND': {
          const x = this.num(this.node(a[0])), d = a[1] ? this.num(this.node(a[1])) : 0;
          if (x instanceof Err) return x; if (d instanceof Err) return d;
          const f = Math.pow(10, Math.trunc(d));
          return Math.sign(x) * Math.round(Math.abs(x) * f + 1e-9) / f;
        }
        case 'ABS': { const x = this.num(this.node(a[0])); return x instanceof Err ? x : Math.abs(x); }
        case 'SQRT': { const x = this.num(this.node(a[0])); if (x instanceof Err) return x; return x < 0 ? new Err('#NUM!') : Math.sqrt(x); }
        case 'CONCAT': {
          let s = '';
          for (const { v } of this.flat(a)) { if (v instanceof Err) return v; s += format(v); }
          return s;
        }
      }
      return new Err('#NAME?');
    }
  }

  const api = { Sheet, Err, colToNum, numToCol, parseRef, format };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else global.TenantmanFormula = api;
})(typeof window !== 'undefined' ? window : globalThis);
