/* 多语言：界面源文本是简体中文；切到别的语言时，自动把页面上的文字（包括后端发来的提示）翻译掉。
   - 词典 key 是中文模板，数字/大小用 {0}{1} 占位（例如 "{0} 线程下载中"）
   - 繁体：先查整句词典（OpenCC 台湾用词），查不到就逐字转换
   - 用户自己的内容（文件名、路径、地址、分类名…）不翻译：放在 .notr / .mono / .t / td.p 里 */
(function () {
  const CJK = /[㐀-鿿＀-￯　-〿]/;
  const NUM = /\d+(?:[.,:]\d+)*(?:\s?(?:B|KB|MB|GB|TB)(?:\/s)?)?/g;
  const SKIP = '.notr, .mono, .name .t, td.p, .badge.cat, .notes, script, style, textarea, code';
  const I = window.I18N = { lang: 'zh-CN', dict: null, chars: null, cache: new Map() };

  function load(lang) {
    I.lang = lang; I.dict = null; I.chars = null; I.cache.clear();
    if (lang === 'zh-CN') return;
    try {
      const x = new XMLHttpRequest();
      x.open('GET', `/static/i18n/${lang}.json`, false); x.send();
      const j = JSON.parse(x.responseText);
      const raw = lang === 'zh-TW' ? j.phrases : j;
      if (lang === 'zh-TW') I.chars = j.chars;
      // 占位符后面跟着的单位并进占位符，和运行时「12.3 MB」整体当一个数字对齐
      const unit = s => s.replace(/\{(\d+)\}\s?(?:B|KB|MB|GB|TB)(?:\/s)?/g, '{$1}');
      I.dict = {};
      for (const k in raw) I.dict[unit(k)] = unit(raw[k]);
    } catch (e) { I.lang = 'zh-CN'; }
  }

  function look(core) {
    const D = I.dict;
    if (core in D) return D[core];
    const nums = [];
    const k = core.replace(NUM, m => { nums.push(m); return '{' + (nums.length - 1) + '}'; });
    if (k in D) return D[k].replace(/\{(\d+)\}/g, (_, i) => nums[i] ?? '');
    return null;
  }
  function conv(s) { return I.chars ? s.replace(/[㐀-鿿]/g, c => I.chars[c] || c) : s; }

  function tr(s) {
    if (I.lang === 'zh-CN' || !s || !CJK.test(s)) return s;
    if (I.cache.has(s)) return I.cache.get(s);
    let out;
    if (s.includes('\n')) out = s.split('\n').map(tr).join('\n');
    else {
      const lead = s.match(/^\s*/)[0], trail = s.match(/\s*$/)[0], core = s.trim();
      let r = look(core);
      if (r == null) {
        // 「前缀：内容」拆开分别翻（比如 "下载完成：电影名"）
        const i = core.indexOf('：');
        if (i > 0) { const a = look(core.slice(0, i)); if (a != null) r = a + (I.lang === 'en' ? ': ' : '：') + tr(core.slice(i + 1)); }
      }
      if (r == null) r = I.lang === 'zh-TW' ? conv(core) : core;
      out = lead + r + trail;
    }
    I.cache.set(s, out);
    return out;
  }
  I.t = tr;

  const done = new WeakMap();
  function trNode(n) {
    if (n.nodeType === 3) {
      const p = n.parentElement;
      if (!p || p.closest(SKIP)) return;
      if (done.get(n) === n.data) return;
      const v = tr(n.data);
      if (v !== n.data) n.data = v;
      done.set(n, n.data);
    } else if (n.nodeType === 1) {
      if (n.matches(SKIP)) return;
      for (const a of ['placeholder', 'title']) {
        const v = n.getAttribute(a);
        if (v && CJK.test(v)) { const t = tr(v); if (t !== v) n.setAttribute(a, t); }   // 值没变不能写回，否则会无限触发自己
      }
      for (const c of n.childNodes) trNode(c);
    }
  }
  I.apply = () => { if (I.lang !== 'zh-CN') { trNode(document.body); document.title = tr(document.title); } };

  if (!window.KOI_LANG) {
    try { const x = new XMLHttpRequest(); x.open('GET', '/api/lang', false); x.send(); window.KOI_LANG = JSON.parse(x.responseText).lang; } catch (e) {}
  }
  load(window.KOI_LANG || 'zh-CN');
  document.documentElement.lang = I.lang;
  const obs = new MutationObserver(ms => {
    if (I.lang === 'zh-CN') return;
    for (const m of ms) {
      if (m.type === 'characterData') trNode(m.target);
      else m.addedNodes.forEach(trNode);
      if (m.type === 'attributes') trNode(m.target);
    }
  });
  function start() {
    I.apply();
    obs.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['placeholder', 'title'] });
  }
  if (document.body) start(); else document.addEventListener('DOMContentLoaded', start);
  const A = window.alert.bind(window), C = window.confirm.bind(window);
  window.alert = m => A(tr(String(m)));
  window.confirm = m => C(tr(String(m)));
})();
