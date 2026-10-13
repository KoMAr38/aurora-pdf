/* Aurora PDF - the page viewer.
 *
 * Continuous scrolling with virtualisation: every page has a sized placeholder, and only
 * pages near the viewport get images and overlays. Images are requested at the exact
 * device-pixel width (bucketed for caching) and swapped in when loaded, so zooming never
 * flashes. Everything per page is keyed by its revision from the backend.
 */
"use strict";
(function (A) {
  const S = A.S;
  const PX = 96 / 72;                 // CSS px per point at 100 %
  const GAP = 18, PAD_TOP = 26, PAD_BOTTOM = 90, PAD_X = 36;
  const BUCKETS = [200, 320, 480, 640, 800, 960, 1120, 1280, 1440, 1600, 1920, 2240, 2560, 2880, 3200, 3600, 4000, 4800, 5600];
  const LO = 200;                     // thumbnail / placeholder width
  const KEEP = 2.5;                   // screens of pages kept materialised around the view

  const V = (A.V = { pages: [], infos: new Map(), viewer: null, host: null });
  const infoWaiters = new Map();

  A.bucket = (w) => BUCKETS.find((b) => b >= w) || BUCKETS[BUCKETS.length - 1];
  A.pageUrl = (rev, w) => A.url(`page/${rev}/${w}.jpg`);

  /* ---------- overlay data ---------- */
  A.pageInfo = (rev) => {
    if (V.infos.has(rev)) return Promise.resolve(V.infos.get(rev));
    if (infoWaiters.has(rev)) return infoWaiters.get(rev);
    const p = fetch(A.url(`info/${rev}.json`))
      .then((r) => (r.ok ? r.json() : null))
      .then((info) => {
        infoWaiters.delete(rev);
        if (info) {
          V.infos.set(rev, info);
          if (V.infos.size > 400) V.infos.delete(V.infos.keys().next().value);
        }
        return info;
      })
      .catch(() => { infoWaiters.delete(rev); return null; });
    infoWaiters.set(rev, p);
    return p;
  };

  /* ---------- geometry ---------- */
  const scale = () => S.zoom * PX;            // CSS px per point
  const dpr = () => window.devicePixelRatio || 1;

  function sizeEl(pg) {
    const s = scale();
    pg.el.style.width = `${Math.round(pg.w * s)}px`;
    pg.el.style.height = `${Math.round(pg.h * s)}px`;
    pg.el.style.setProperty("--s", s.toFixed(5));
  }

  A.fitZoom = (mode) => {
    if (!V.pages.length) return S.zoom;
    const vw = V.viewer.clientWidth - PAD_X * 2;
    const vh = V.viewer.clientHeight - PAD_TOP - 30;
    const maxW = Math.max(...V.pages.map((p) => p.w));
    if (mode === "width") return A.clamp(vw / (maxW * PX), 0.1, 8);
    const cur = V.pages[S.current] || V.pages[0];
    return A.clamp(Math.min(vw / (cur.w * PX), vh / (cur.h * PX)), 0.1, 8);
  };

  /** Point under the viewport centre (or a given client point) as page + fraction. */
  function anchorAt(clientX, clientY) {
    const vr = V.viewer.getBoundingClientRect();
    const x = clientX == null ? vr.left + vr.width / 2 : clientX;
    const y = clientY == null ? vr.top + vr.height / 2 : clientY;
    const i = pageIndexAtY(V.viewer.scrollTop + (y - vr.top));
    const pg = V.pages[i];
    if (!pg) return null;
    const r = pg.el.getBoundingClientRect();
    return { i, fx: (x - r.left) / r.width, fy: (y - r.top) / r.height, x, y };
  }
  function restoreAnchor(an) {
    if (!an) return;
    const pg = V.pages[an.i];
    if (!pg) return;
    const vr = V.viewer.getBoundingClientRect();
    const top = pg.el.offsetTop + an.fy * pg.el.offsetHeight;
    const left = pg.el.offsetLeft + an.fx * pg.el.offsetWidth;
    V.viewer.scrollTop = top - (an.y - vr.top);
    V.viewer.scrollLeft = left - (an.x - vr.left);
  }

  A.setZoom = (z, opts = {}) => {
    if (!V.pages.length) return;
    const an = opts.keep === false || S.organize ? null : anchorAt(opts.x, opts.y);
    S.zoom = A.clamp(z, 0.1, 8);
    if (!opts.fit) S.fit = null;
    V.pages.forEach(sizeEl);
    restoreAnchor(an);
    updateZoomLabel();
    scheduleUpdate(true);
    A.emit("zoom");
  };
  A.zoomBy = (factor, x, y) => A.setZoom(S.zoom * factor, { x, y });
  A.fit = (mode) => {
    S.fit = mode;
    A.setZoom(A.fitZoom(mode), { fit: true });
    S.fit = mode;
    if (mode === "page") A.gotoPage(S.current, false);
    A.call("settings_set", { fit: mode }, { quiet: true, noBusy: true });
  };
  function updateZoomLabel() {
    const z = A.$("#zoomVal");
    if (z) z.textContent = `${Math.round(S.zoom * 100)}%`;
    A.$$("#vbar [data-fit]").forEach((b) => b.classList.toggle("active", S.fit === b.dataset.fit));
  }

  /* ---------- building the page list ---------- */
  function newPage(w, h, rev) {
    const el = document.createElement("div");
    el.className = "pg";
    const pg = { w, h, rev, el, mat: false, info: null, hiW: 0 };
    el._pg = pg;
    return pg;
  }

  /** Bring the page list in line with the backend summary, reusing whatever did not change. */
  function syncPages(doc) {
    const list = doc ? doc.pages : [];
    const sameShape = list.length === V.pages.length && list.every((p, i) => p[0] === V.pages[i].w && p[1] === V.pages[i].h);
    if (sameShape) {
      list.forEach((p, i) => {
        if (V.pages[i].rev !== p[2]) changeRev(V.pages[i], p[2]);
        V.pages[i].el.dataset.i = i;
      });
      return false;
    }
    const an = V.pages.length ? anchorAt() : null;
    const byRev = new Map(V.pages.map((pg) => [pg.rev, pg]));
    const next = list.map(([w, h, rev]) => {
      const old = byRev.get(rev);
      if (old && old.w === w && old.h === h) { byRev.delete(rev); return old; }
      return newPage(w, h, rev);
    });
    byRev.forEach((pg) => pg.el.remove());
    V.pages = next;
    const frag = document.createDocumentFragment();
    V.pages.forEach((pg, i) => { pg.el.dataset.i = i; sizeEl(pg); frag.appendChild(pg.el); });
    V.host.appendChild(frag);
    if (an && an.i < V.pages.length) restoreAnchor(an);
    return true;
  }

  function changeRev(pg, rev) {
    pg.rev = rev; // the old overlay info stays until the new one arrives, so the page never goes dead
    if (!pg.mat) { pg.info = null; return; }
    loadHi(pg, true);
    loadInfo(pg);
  }

  /* ---------- materialising pages ---------- */
  function materialize(pg) {
    if (pg.mat) return;
    pg.mat = true;
    const el = pg.el;
    el.innerHTML =
      `<img class="lo" alt="" draggable="false"><img class="hi" alt="" draggable="false">` +
      `<div class="tl"></div><svg class="ov" preserveAspectRatio="none"></svg><div class="lk"></div><div class="fx"></div>`;
    pg.lo = el.children[0];
    pg.hi = el.children[1];
    pg.tl = el.children[2];
    pg.ov = el.children[3];
    pg.lk = el.children[4];
    pg.fx = el.children[5];
    pg.lo.src = A.pageUrl(pg.rev, LO);
    pg.hiW = 0;
    loadHi(pg);
    loadInfo(pg);
  }

  function dematerialize(pg) {
    if (!pg.mat) return;
    if (pg.fx && pg.fx.contains(document.activeElement)) return; // keep a field being edited
    pg.mat = false;
    cancelHi(pg);
    pg.lo.src = pg.hi.src = ""; // abort loads nobody will see, so they don't queue up ahead of visible pages
    pg.el.innerHTML = "";
    pg.lo = pg.hi = pg.tl = pg.ov = pg.lk = pg.fx = null;
    pg.hiW = 0;
  }

  /** Stop a sharp image that is no longer wanted, so it doesn't hold up the ones that are. */
  function cancelHi(pg) {
    if (!pg.loader) return;
    pg.loader.onload = null;
    pg.loader.src = "";
    pg.loader = null;
  }

  function loadHi(pg, force) {
    const want = A.bucket(Math.round(pg.w * scale() * dpr()));
    if (!force && want <= pg.hiW) return;           // a sharper image is already there or on its way
    cancelHi(pg);
    const rev = pg.rev;
    const img = new Image();
    img.decoding = "async";
    pg.loader = img;
    img.onload = () => {
      if (pg.loader === img) pg.loader = null;
      if (!pg.mat || pg.rev !== rev) return;
      pg.hi.src = img.src;
      pg.hi.classList.add("ready");
      if (force) pg.lo.src = A.pageUrl(rev, LO);
      A.emit("pageImage", pg);
    };
    pg.hiW = want;
    img.src = A.pageUrl(rev, want);
  }

  function loadInfo(pg) {
    const rev = pg.rev;
    A.pageInfo(rev).then((info) => {
      if (!info || pg.rev !== rev || !pg.mat) return;
      pg.info = info;
      pg.ov.setAttribute("viewBox", `0 0 ${info.w} ${info.h}`);
      renderTextLayer(pg);
      renderLinks(pg);
      A.renderFields(pg);
      A.renderOverlay(pg);
    });
  }

  /* ---------- visible range ---------- */
  function pageIndexAtY(y) {
    let lo = 0, hi = V.pages.length - 1;
    while (lo < hi) {
      const mid = (lo + hi + 1) >> 1;
      if (V.pages[mid].el.offsetTop <= y) lo = mid;
      else hi = mid - 1;
    }
    return Math.max(0, lo);
  }

  let rafPending = false, resChange = false, settleTimer = null, lastTop = 0;
  function scheduleUpdate(resolution) {
    resChange = resChange || !!resolution;
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      updateVisible();
    });
  }

  function updateVisible() {
    if (!V.pages.length || S.organize) return; // the organize grid scrolls the same viewer; pages are hidden then
    const top = V.viewer.scrollTop, h = V.viewer.clientHeight;
    const first = pageIndexAtY(top - h * KEEP);
    const last = pageIndexAtY(top + h + h * KEEP);
    const vFirst = pageIndexAtY(top), vLast = pageIndexAtY(top + h);
    // flying through the document (scrollbar drag, long jump): leave pages blank until it stops
    const fast = Math.abs(top - lastTop) > h * 0.9;
    lastTop = top;
    for (let i = 0; i < V.pages.length; i++) {
      const pg = V.pages[i];
      if (i >= first && i <= last) {
        if (!fast && i >= vFirst - 1 && i <= vLast + 1) materialize(pg);
      } else dematerialize(pg);
    }
    // sharper images once scrolling/zooming settles
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => {
      for (let i = vFirst; i <= vLast; i++) if (V.pages[i] && V.pages[i].mat) loadHi(V.pages[i]);
      for (let i = Math.max(first, vFirst - 2); i <= Math.min(last, vLast + 2); i++) {
        const pg = V.pages[i];
        if (pg && !pg.mat) materialize(pg);
      }
    }, resChange ? 140 : 90);
    resChange = false;
    const mid = pageIndexAtY(top + h * 0.45);
    if (mid !== S.current) {
      S.current = mid;
      A.emit("current", mid);
    }
  }

  A.gotoPage = (i, smooth = true, yFrac = 0) => {
    const pg = V.pages[A.clamp(i, 0, V.pages.length - 1)];
    if (!pg) return;
    const top = pg.el.offsetTop - 14 + yFrac * pg.el.offsetHeight;
    V.viewer.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
    if (!smooth) { S.current = +pg.el.dataset.i; A.emit("current", S.current); }
  };
  A.gotoRect = (i, r, smooth = true) => {
    const pg = V.pages[i];
    if (!pg) return;
    const s = scale();
    const y = pg.el.offsetTop + r[1] * s - V.viewer.clientHeight * 0.35;
    const x = pg.el.offsetLeft + r[0] * s - V.viewer.clientWidth * 0.3;
    V.viewer.scrollTo({ top: y, left: Math.max(0, x), behavior: smooth ? "smooth" : "auto" });
  };

  /* ---------- text layer ---------- */
  const measureCtx = document.createElement("canvas").getContext("2d");
  const widthCache = new Map();
  function unitWidth(text) {
    let w = widthCache.get(text);
    if (w == null) {
      measureCtx.font = "100px Arial, 'Segoe UI', sans-serif";
      w = measureCtx.measureText(text).width / 100 || 0.5;
      if (widthCache.size > 20000) widthCache.clear();
      widthCache.set(text, w);
    }
    return w;
  }

  function renderTextLayer(pg) {
    const tl = pg.tl, info = pg.info;
    tl.textContent = "";
    if (!info || !info.words.length) return;
    const W = info.w, H = info.h;
    const frag = document.createDocumentFragment();
    info.words.forEach((wd, k) => {
      const [x, y, len, th, ang, txt, end] = wd;
      const fs = th * 0.8;
      const sx = len / Math.max(0.01, unitWidth(txt) * fs);
      const span = document.createElement("span");
      span.textContent = end ? txt : txt + " ";
      span.dataset.w = k;
      span.style.cssText = `left:${(x / W) * 100}%;top:${(y / H) * 100}%;font-size:calc(var(--s) * ${fs.toFixed(2)}px);transform:${ang ? `rotate(${ang}deg) ` : ""}scaleX(${sx.toFixed(4)})`;
      frag.appendChild(span);
      if (end) frag.appendChild(document.createElement("br"));
    });
    tl.appendChild(frag);
  }

  /* ---------- links ---------- */
  // a link names its target page by xref; where that page is now comes from the current page list
  const targetOf = (l) => (S.doc ? S.doc.pages.findIndex((p) => p[3] === l.px) : -1);
  function renderLinks(pg) {
    const lk = pg.lk, info = pg.info;
    lk.textContent = "";
    (info.links || []).forEach((l) => {
      const a = document.createElement("a");
      a.className = "link";
      a.style.cssText = pct(l.r, info);
      if (l.u) a.title = l.u;
      else a.onpointerenter = () => { const i = targetOf(l); a.title = i < 0 ? "" : `${A.t("page")} ${i + 1}`; };
      a.onclick = (e) => {
        e.preventDefault();
        if (S.mode !== "select") return;
        if (l.u) return openExternal(l.u);
        const i = targetOf(l);
        if (i >= 0) A.gotoPage(i);
      };
      lk.appendChild(a);
    });
  }
  async function openExternal(url) {
    if (await A.confirm(A.t("openLinkTitle"), A.t("openLinkMsg", { url }), A.t("openLink"))) A.call("open_url", { url }, { noBusy: true });
  }
  const pct = (r, info) =>
    `left:${(r[0] / info.w) * 100}%;top:${(r[1] / info.h) * 100}%;width:${((r[2] - r[0]) / info.w) * 100}%;height:${((r[3] - r[1]) / info.h) * 100}%`;
  A.pct = pct;

  /* ---------- form fields ---------- */
  A.renderFields = (pg) => {
    const fx = pg.fx, info = pg.info;
    if (!fx || !info) return;
    if (fx.contains(document.activeElement)) { pg.fieldsStale = true; return; }
    pg.fieldsStale = false;
    fx.textContent = "";
    const editable = A.can("form");
    (info.fields || []).forEach((f) => {
      if (f.k === "button") return;
      let el;
      const rect = f.r;
      const fsz = f.fs && f.fs > 0 ? f.fs : Math.min(12, (rect[3] - rect[1]) * 0.62);
      if (f.k === "text") {
        el = document.createElement(f.ml ? "textarea" : "input");
        if (!f.ml) el.type = f.pw ? "password" : "text";
        el.value = f.v || "";
        if (f.max) el.maxLength = f.max;
        el.addEventListener("change", () => setField(pg, f, el.value));
      } else if (f.k === "check" || f.k === "radio") {
        el = document.createElement("button");
        el.className = `chk${f.k === "radio" ? " radio" : ""}${f.v ? " on" : ""}`; // a bare "check" class would pick up the panel checkbox styles
        el.innerHTML = A.icon("check", 14);
        el.onclick = () => {
          if (f.ro || !editable) return;
          const v = f.k === "radio" ? true : !f.v;
          setField(pg, f, v);
          // the layer is not rebuilt while one of its fields has focus, and this button keeps
          // the focus after the click, so show the new state here; the next rebuild uses the file
          if (f.k === "radio") fx.querySelectorAll(".chk.radio").forEach((b) => { if (b.title === f.n) b.classList.remove("on"); });
          f.v = v;
          el.classList.toggle("on", v);
        };
      } else if (f.k === "combo" || f.k === "list") {
        el = document.createElement("select");
        if (f.k === "list") el.size = Math.max(2, Math.min(6, (f.opts || []).length));
        if (f.multi) el.multiple = true;
        const chosen = Array.isArray(f.v) ? f.v : [f.v];
        (f.opts || []).forEach(([value, label]) => { // the file keeps the value, the list shows the label
          const op = document.createElement("option");
          op.value = value;
          op.textContent = label;
          if (chosen.includes(value)) op.selected = true;
          el.appendChild(op);
        });
        el.addEventListener("change", () => setField(pg, f, el.multiple ? [...el.selectedOptions].map((o) => o.value) : el.value));
      } else if (f.k === "signature") {
        if (f.signed) return; // the page image shows the signature itself
        el = document.createElement("button");
        el.className = "sigfield";
        el.innerHTML = `${A.icon("signbox", 16)}<span>${A.esc(A.t("signHere"))}</span>`;
        el.onclick = () => A.startDigitalSign({ page: +pg.el.dataset.i, rect: f.r, field: f.n });
      } else return;
      el.classList.add("fld");
      el.title = f.n;
      el.style.cssText = pct(rect, info) + `;font-size:calc(var(--s) * ${fsz.toFixed(2)}px)`;
      if ((f.ro || !editable) && el.tagName !== "BUTTON") el.disabled = true;
      el.addEventListener("blur", () => { if (pg.fieldsStale) setTimeout(() => A.renderFields(pg), 0); });
      fx.appendChild(el);
    });
  };
  function setField(pg, f, value) {
    const i = +pg.el.dataset.i;
    A.edit("field_set", () => ({ page: i, rev: V.pages[i].rev, xref: f.x, value }), { noBusy: true });
  }

  /* ---------- reacting to the backend ---------- */
  A.on("doc", ({ prev, doc }) => {
    if (!doc || !prev || prev.id !== doc.id) {   // closed, or a different document
      V.pages.forEach((pg) => pg.el.remove());
      V.pages = [];
      V.infos.clear();
      if (!doc) return;
      syncPages(doc);
      S.current = 0;
      S.zoom = A.fitZoom(S.fit || "width");
      if (!S.fit) S.fit = "width";
      V.pages.forEach(sizeEl);
      V.viewer.scrollTop = 0;
      V.viewer.scrollLeft = 0;
      updateZoomLabel();
      scheduleUpdate(true);
      return;
    }
    if (syncPages(doc)) scheduleUpdate(true);
  });

  /* ---------- init ---------- */
  A.initViewer = () => {
    V.viewer = A.$("#viewer");
    V.host = A.$("#pages");
    V.viewer.addEventListener("scroll", () => scheduleUpdate(false), { passive: true });
    V.viewer.addEventListener("wheel", (e) => {
      if (!(e.ctrlKey || e.metaKey) || !V.pages.length) return;
      e.preventDefault();
      A.zoomBy(Math.exp(-e.deltaY * 0.0022), e.clientX, e.clientY);
    }, { passive: false });
    // refit when the viewer changes size: window resize, side panels opening or closing
    let rz, lastW = 0, lastH = 0;
    new ResizeObserver(() => {
      const w = V.viewer.clientWidth, h = V.viewer.clientHeight;
      if (w === lastW && h === lastH) return;
      lastW = w; lastH = h;
      clearTimeout(rz);
      rz = setTimeout(() => { if (S.fit && V.pages.length) A.setZoom(A.fitZoom(S.fit), { fit: true }); else scheduleUpdate(true); }, 120);
    }).observe(V.viewer);
  };
  A.refreshVisible = () => scheduleUpdate(true);
  A.pageEl = (i) => (V.pages[i] ? V.pages[i].el : null);
  A.PX = PX;
  A.scale = scale;
  A.LO = LO;
})(window.Aurora);
