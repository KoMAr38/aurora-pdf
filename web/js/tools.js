/* Aurora PDF - drawing and markup on pages.
 *
 * Every page has an SVG overlay in PDF points (viewBox = page size on screen), so shapes
 * drawn here line up with the rendered page at any zoom. Finished drawings become real
 * PDF annotations in the backend; previews stay on screen until the new page image is in.
 */
"use strict";
(function (A) {
  const S = A.S, V = A.V;
  const NS = "http://www.w3.org/2000/svg";

  const DRAW_MODES = new Set(["highlight", "underline", "strikeout", "pen", "rect", "ellipse", "line", "arrow", "text", "note", "eraser", "redact", "crop", "sigbox", "place"]);
  const MARKUP = new Set(["highlight", "underline", "strikeout"]);
  const COLORS = ["#e11d48", "#f97316", "#facc15", "#16a34a", "#2563eb", "#7c3aed", "#111827"];
  const HL_COLORS = ["#facc15", "#86efac", "#f9a8d4", "#7dd3fc", "#fdba74"];
  const WIDTHS = [1, 2, 4];
  const TOOLS = [
    ["select", "select", "V"], ["hand", "hand", "H"], null,
    ["highlight", "highlight", "I"], ["underline", "underline", "U"], ["strikeout", "strikeout", "K"], null,
    ["pen", "pen", "P"], ["rect", "rect", "R"], ["ellipse", "ellipse", "E"], ["line", "line", "L"], ["arrow", "arrow", "A"], null,
    ["text", "text", "T"], ["note", "note", "N"], ["eraser", "eraser", "X"],
  ];
  A.TOOL_KEYS = Object.fromEntries(TOOLS.filter(Boolean).map(([m, , k]) => [k.toLowerCase(), m]));

  const svgEl = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  };

  /* ---------- geometry ---------- */
  function pageOf(target) {
    const el = target && target.closest ? target.closest(".pg") : null;
    return el && el._pg ? el._pg : null;
  }
  function toPt(pg, e) {
    const r = pg.el.getBoundingClientRect();
    return { x: ((e.clientX - r.left) / r.width) * pg.w, y: ((e.clientY - r.top) / r.height) * pg.h };
  }
  const idx = (pg) => +pg.el.dataset.i;
  const norm = (a, b) => [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.max(a.x, b.x), Math.max(a.y, b.y)];

  /** Word box (display space) from a text-layer word entry. */
  function wordBox(w) {
    const [x, y, len, th, ang] = w;
    if (ang === 90) return [x - th, y, x, y + len];
    if (ang === 180) return [x - len, y - th, x, y];
    if (ang === 270) return [x, y - len, x + th, y];
    return [x, y, x + len, y + th];
  }
  function wordAt(pg, p, slack = 18) {
    const words = (pg.info && pg.info.words) || [];
    let best = -1, bd = Infinity;
    words.forEach((w, k) => {
      const [x0, y0, x1, y1] = wordBox(w);
      const dx = p.x < x0 ? x0 - p.x : p.x > x1 ? p.x - x1 : 0;
      const dy = p.y < y0 ? y0 - p.y : p.y > y1 ? p.y - y1 : 0;
      const d = dx * 2 + dy * 4;
      if (d < bd) { bd = d; best = k; }
    });
    return bd <= slack ? best : -1;
  }

  /* ---------- overlay rendering ---------- */
  A.renderOverlay = (pg) => {
    if (!pg || !pg.ov || !pg.info) return;
    const ov = pg.ov, info = pg.info;
    ov.textContent = "";
    const gA = svgEl("g", { class: "g-ann" });
    (info.annots || []).forEach((a) => {
      const [x0, y0, x1, y1] = a.r;
      const pad = 2;
      const r = svgEl("rect", { x: x0 - pad, y: y0 - pad, width: Math.max(4, x1 - x0 + pad * 2), height: Math.max(4, y1 - y0 + pad * 2), class: "ah", rx: 2 });
      r.dataset.x = a.x;
      r._a = a;
      if (a.t === "Redact") r.classList.add("redact");
      gA.appendChild(r);
    });
    ov.appendChild(gA);
    // search hits
    const gH = svgEl("g", { class: "g-hit" });
    const i = idx(pg);
    S.search.hits.forEach((h, k) => {
      if (h.p !== i) return;
      const [x0, y0, x1, y1] = h.r;
      gH.appendChild(svgEl("rect", { x: x0 - 1, y: y0 - 1, width: x1 - x0 + 2, height: y1 - y0 + 2, rx: 2, class: k === S.search.cur ? "hit cur" : "hit" }));
    });
    ov.appendChild(gH);
    // selection
    const sel = S.selAnnot;
    if (sel && sel.page === i) {
      const a = (info.annots || []).find((x) => x.x === sel.xref);
      if (a) {
        sel.a = a;
        drawSelection(pg, a.r, a.rs);
      }
    }
    const gT = svgEl("g", { class: "g-tmp" });
    ov.appendChild(gT);
    (pg.previews || []).forEach((pv) => gT.appendChild(pv.node));
    A.positionAnnbar();
  };

  function drawSelection(pg, r, resizable, dx = 0, dy = 0) {
    const old = pg.ov.querySelector(".g-sel");
    if (old) old.remove();
    const g = svgEl("g", { class: "g-sel" });
    const [x0, y0, x1, y1] = r;
    g.appendChild(svgEl("rect", { x: x0 - 3 + dx, y: y0 - 3 + dy, width: x1 - x0 + 6, height: y1 - y0 + 6, class: "selbox", rx: 3 }));
    if (resizable) {
      const s = 7 / A.scale();
      const hd = svgEl("rect", { x: x1 + 3 + dx - s / 2, y: y1 + 3 + dy - s / 2, width: s, height: s, class: "handle", rx: s / 4 });
      g.appendChild(hd);
    }
    const tmp = pg.ov.querySelector(".g-tmp");
    pg.ov.insertBefore(g, tmp);
    return g;
  }

  A.on("pageImage", (pg) => {
    if (!pg.previews || !pg.previews.length) return;
    const keep = pg.previews.filter((pv) => !(pv.until && pg.rev >= pv.until));
    if (keep.length !== pg.previews.length) {
      pg.previews.filter((pv) => !keep.includes(pv)).forEach((pv) => pv.node.remove());
      pg.previews = keep;
    }
  });

  function addPreview(pg, node) {
    pg.previews = pg.previews || [];
    const pv = { node, until: 0 };
    pg.previews.push(pv);
    const g = pg.ov && pg.ov.querySelector(".g-tmp");
    if (g) g.appendChild(node);
    return pv;
  }
  /** After an edit landed, keep previews until the page image with the new revision is shown. */
  function settlePreviews(pageIndex, pvs, ok) {
    const pg = V.pages[pageIndex];
    if (!pg) return;
    if (!ok) {
      pvs.forEach((pv) => pv.node.remove());
      pg.previews = (pg.previews || []).filter((x) => !pvs.includes(x));
      return;
    }
    pvs.forEach((pv) => (pv.until = pg.rev));
    if (pg.hi && pg.hi.classList.contains("ready") && pg.hi.src.includes(`/page/${pg.rev}/`)) A.emit("pageImage", pg);
  }

  /* ---------- modes ---------- */
  A.setMode = (mode, opts = {}) => {
    if (S.mode === "pen" && mode !== "pen") flushInk();
    S.mode = mode;
    S.placing = mode === "place" ? opts.placing : null;
    S.sigboxDone = mode === "sigbox" ? opts.done : S.sigboxDone;
    const host = A.$("#pages");
    host.classList.toggle("drawing", DRAW_MODES.has(mode));
    host.classList.toggle("handmode", mode === "hand");
    host.dataset.mode = mode;
    if (mode !== "select") A.selectAnnot(null);
    A.$$("#atb [data-mode]").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    renderStyleRow();
    hideSelbar();
    A.emit("mode", mode);
  };

  /* ---------- pointer handling ---------- */
  let drag = null;

  function onDown(e) {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    const pg = pageOf(e.target);
    if (S.mode === "hand" || (spaceDown && !e.target.closest(".fx"))) return startPan(e);
    if (!pg) return; // page info may be reloading after an edit; only text markup needs it
    const p = toPt(pg, e);
    const mode = S.mode;
    if (mode === "select") return selectDown(e, pg, p);
    if (!DRAW_MODES.has(mode)) return;
    if (mode === "eraser") {
      const hit = e.target.closest(".ah");
      if (hit) A.deleteAnnot(idx(pg), +hit.dataset.x);
      return;
    }
    if (mode === "note") return addNote(pg, p);
    if (mode === "text") return startText(pg, p);
    e.preventDefault();
    pg.el.setPointerCapture(e.pointerId);
    if (mode === "pen") {
      if (ink && ink.page !== idx(pg)) flushInk();
      const path = svgEl("path", { class: "pre-ink", stroke: S.style.color, "stroke-width": S.style.width, fill: "none" });
      const pv = addPreview(pg, path);
      drag = { mode, pg, pts: [p], path, pv };
      clearTimeout(inkTimer);
      return;
    }
    if (MARKUP.has(mode)) {
      if (!pg.info) return;
      const w = wordAt(pg, p, 30);
      if (w < 0) { A.toast(A.t("noTextHere"), "info"); return; }
      const g = svgEl("g", { class: "pre-mark " + mode });
      const pv = addPreview(pg, g);
      drag = { mode, pg, a: w, b: w, g, pv };
      drawMarkup(drag);
      return;
    }
    const node = mode === "line" || mode === "arrow" ? svgEl("line", { class: "pre-shape" }) : svgEl(mode === "ellipse" ? "ellipse" : "rect", { class: `pre-shape ${mode}` });
    node.setAttribute("stroke-width", mode === "redact" || mode === "crop" || mode === "sigbox" || mode === "place" ? 1.2 / A.scale() * 1.5 : S.style.width);
    if (["rect", "ellipse", "line", "arrow"].includes(mode)) node.setAttribute("stroke", S.style.color);
    const pv = addPreview(pg, node);
    drag = { mode, pg, start: p, cur: p, node, pv };
    if (mode === "place") drawPlacement(drag);
  }

  function onMove(e) {
    if (pan) return panMove(e);
    if (!drag) { if (S.mode === "eraser") eraserHover(e); if (S.mode === "place") ghostMove(e); return; }
    const pg = drag.pg;
    if (drag.mode === "pen") {
      const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      evs.forEach((ev) => drag.pts.push(toPt(pg, ev)));
      drag.path.setAttribute("d", smoothPath(drag.pts));
      return;
    }
    const p = toPt(pg, e);
    if (MARKUP.has(drag.mode)) {
      const w = wordAt(pg, p, 40);
      if (w >= 0 && w !== drag.b) { drag.b = w; drawMarkup(drag); }
      return;
    }
    if (drag.mode === "move" || drag.mode === "resize") return annotDragMove(e, p);
    drag.cur = p;
    if (drag.mode === "place") return drawPlacement(drag);
    const [x0, y0, x1, y1] = norm(drag.start, p);
    if (drag.node.tagName === "line") {
      Object.entries({ x1: drag.start.x, y1: drag.start.y, x2: p.x, y2: p.y }).forEach(([k, v]) => drag.node.setAttribute(k, v));
    } else if (drag.node.tagName === "ellipse") {
      Object.entries({ cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, rx: (x1 - x0) / 2, ry: (y1 - y0) / 2 }).forEach(([k, v]) => drag.node.setAttribute(k, v));
    } else {
      Object.entries({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 }).forEach(([k, v]) => drag.node.setAttribute(k, v));
    }
  }

  function onUp(e) {
    if (pan) return panEnd();
    if (!drag) return;
    const d = drag;
    drag = null;
    const pg = d.pg, i = idx(pg);
    if (d.mode === "move" || d.mode === "resize") return finishAnnotDrag(d, i);
    if (d.mode === "pen") {
      const pts = simplify(d.pts, 0.35);
      inkAdd(i, pts.map((q) => [round(q.x), round(q.y)]), d.pv);
      return;
    }
    if (MARKUP.has(d.mode)) {
      const words = range(d.a, d.b);
      commit(i, [d.pv], { kind: d.mode, words, color: d.mode === "highlight" ? S.style.hl : S.style.color });
      return;
    }
    const p = d.cur;
    const r = norm(d.start, p).map(round);
    const small = r[2] - r[0] < 4 && r[3] - r[1] < 4;
    if (d.mode === "place") {
      const rect = placementRect(d);
      d.pv.node.remove();
      pg.previews = pg.previews.filter((x) => x !== d.pv);
      return placeImage(i, rect);
    }
    if (small && d.mode !== "line" && d.mode !== "arrow") { dropPreview(pg, d.pv); return; }
    if (d.mode === "line" || d.mode === "arrow") {
      if (Math.hypot(p.x - d.start.x, p.y - d.start.y) < 4) { dropPreview(pg, d.pv); return; }
      return commit(i, [d.pv], { kind: d.mode, coords: [d.start.x, d.start.y, p.x, p.y].map(round), color: S.style.color, width: S.style.width });
    }
    if (d.mode === "rect" || d.mode === "ellipse") return commit(i, [d.pv], { kind: d.mode, coords: r, color: S.style.color, width: S.style.width });
    if (d.mode === "redact") return commit(i, [d.pv], { kind: "redact", rect: r });
    dropPreview(pg, d.pv);
    if (d.mode === "crop") return A.cropDrawn(i, r, pg);
    if (d.mode === "sigbox" && S.sigboxDone) {
      const cb = S.sigboxDone;
      A.setMode("select");
      return cb(i, r);
    }
  }

  function dropPreview(pg, pv) {
    pv.node.remove();
    pg.previews = (pg.previews || []).filter((x) => x !== pv);
  }
  const round = (v) => Math.round(v * 100) / 100;
  const range = (a, b) => { const out = []; for (let k = Math.min(a, b); k <= Math.max(a, b); k++) out.push(k); return out; };

  function commit(i, pvs, payload) {
    return A.edit("annot_add", () => Object.assign({ page: i, rev: V.pages[i].rev }, payload), { noBusy: true })
      .then((r) => { settlePreviews(i, pvs, !!r); return r; });
  }

  /* ---------- markup ---------- */
  function drawMarkup(d) {
    const words = d.pg.info.words;
    d.g.textContent = "";
    range(d.a, d.b).forEach((k) => {
      const [x0, y0, x1, y1] = wordBox(words[k]);
      if (d.mode === "highlight") d.g.appendChild(svgEl("rect", { x: x0, y: y0, width: x1 - x0, height: y1 - y0, fill: S.style.hl }));
      else {
        const y = d.mode === "underline" ? y1 - (y1 - y0) * 0.08 : (y0 + y1) / 2;
        d.g.appendChild(svgEl("line", { x1: x0, y1: y, x2: x1, y2: y, stroke: S.style.color, "stroke-width": Math.max(0.8, (y1 - y0) * 0.07) }));
      }
    });
  }

  /* ---------- ink ---------- */
  let ink = null, inkTimer = null;
  function inkAdd(i, pts, pv) {
    if (ink && ink.page !== i) flushInk();
    if (!ink) ink = { page: i, strokes: [], pvs: [], color: S.style.color, width: S.style.width };
    ink.strokes.push(pts);
    ink.pvs.push(pv);
    clearTimeout(inkTimer);
    inkTimer = setTimeout(flushInk, 900);
  }
  function flushInk() {
    clearTimeout(inkTimer);
    if (!ink) return;
    const job = ink;
    ink = null;
    commit(job.page, job.pvs, { kind: "ink", strokes: job.strokes, color: job.color, width: job.width });
  }
  A.flushInk = flushInk;

  function smoothPath(pts) {
    if (pts.length < 3) return `M${pts[0].x} ${pts[0].y}` + pts.slice(1).map((p) => ` L${p.x} ${p.y}`).join("") + (pts.length === 1 ? ` l0.01 0.01` : "");
    let d = `M${pts[0].x} ${pts[0].y}`;
    for (let k = 1; k < pts.length - 1; k++) {
      const mx = (pts[k].x + pts[k + 1].x) / 2, my = (pts[k].y + pts[k + 1].y) / 2;
      d += ` Q${pts[k].x} ${pts[k].y} ${mx} ${my}`;
    }
    const last = pts[pts.length - 1];
    return d + ` L${last.x} ${last.y}`;
  }
  function simplify(pts, eps) {
    if (pts.length < 3) return pts;
    const keep = new Uint8Array(pts.length);
    keep[0] = keep[pts.length - 1] = 1;
    const stack = [[0, pts.length - 1]];
    while (stack.length) {
      const [a, b] = stack.pop();
      let md = 0, mi = -1;
      const ax = pts[a].x, ay = pts[a].y, bx = pts[b].x, by = pts[b].y;
      const len = Math.hypot(bx - ax, by - ay) || 1e-9;
      for (let k = a + 1; k < b; k++) {
        const dd = Math.abs((by - ay) * pts[k].x - (bx - ax) * pts[k].y + bx * ay - by * ax) / len;
        if (dd > md) { md = dd; mi = k; }
      }
      if (md > eps && mi > 0) { keep[mi] = 1; stack.push([a, mi], [mi, b]); }
    }
    return pts.filter((_, k) => keep[k]);
  }

  /* ---------- text box and notes ---------- */
  function startText(pg, p) {
    const i = idx(pg);
    const box = A.h(`<textarea class="ft-edit" rows="1"></textarea>`); // no spellcheck: WebView2 checks against the Windows language, not the text's
    const s = A.scale();
    box.style.left = `${(p.x / pg.w) * 100}%`;
    box.style.top = `${(p.y / pg.h) * 100}%`;
    box.style.fontSize = `${S.style.size * s}px`;
    box.style.color = S.style.text;
    pg.el.appendChild(box);
    setTimeout(() => box.focus(), 0);
    const grow = () => { box.style.height = "auto"; box.style.height = `${box.scrollHeight}px`; box.style.width = "auto"; box.style.width = `${Math.max(80, box.scrollWidth + 4)}px`; };
    box.addEventListener("input", grow);
    grow();
    let done = false;
    const finish = (save) => {
      if (done) return;
      done = true;
      const text = box.value.replace(/\s+$/, "");
      const r = box.getBoundingClientRect(), pr = pg.el.getBoundingClientRect();
      box.remove();
      if (!save || !text) return;
      const x0 = ((r.left - pr.left) / pr.width) * pg.w, y0 = ((r.top - pr.top) / pr.height) * pg.h;
      const x1 = ((r.right - pr.left) / pr.width) * pg.w + 6, y1 = ((r.bottom - pr.top) / pr.height) * pg.h + 3;
      A.edit("annot_add", () => ({ page: i, rev: V.pages[i].rev, kind: "text", rect: [x0, y0, x1, y1].map(round), text, size: S.style.size, color: S.style.text }));
    };
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.stopPropagation(); finish(false); }
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) finish(true);
    });
    box.addEventListener("blur", () => finish(true));
  }

  async function addNote(pg, p) {
    const i = idx(pg);
    const text = await A.prompt(A.t("noteTitle"), A.t("noteText"), "", { multiline: true, ok: A.t("add") });
    if (text == null) return;
    A.edit("annot_add", () => ({ page: i, rev: V.pages[i].rev, kind: "note", point: [round(p.x), round(p.y)], text, color: S.style.color }));
  }

  /* ---------- placing images (signatures) ---------- */
  let ghost = null;
  function ghostMove(e) {
    const pg = pageOf(e.target);
    if (!S.placing) return;
    if (!ghost) {
      ghost = A.h(`<img class="sig-ghost" alt="">`);
      ghost.src = S.placing.url;
      document.body.appendChild(ghost);
    }
    const w = 150 * A.scale();
    ghost.style.width = `${w}px`;
    ghost.style.left = `${e.clientX - w / 2}px`;
    ghost.style.top = `${e.clientY - (w * S.placing.aspect) / 2}px`;
    ghost.style.display = pg ? "block" : "none";
  }
  function clearGhost() { if (ghost) { ghost.remove(); ghost = null; } }
  A.on("mode", (m) => { if (m !== "place") clearGhost(); });

  function placementRect(d) {
    const a = S.placing.aspect;
    const dx = Math.abs(d.cur.x - d.start.x);
    if (dx < 6) {
      const w = 150, h = w * a;
      return [d.start.x - w / 2, d.start.y - h / 2, d.start.x + w / 2, d.start.y + h / 2].map(round);
    }
    const w = dx, h = w * a;
    const x0 = Math.min(d.start.x, d.cur.x);
    const y0 = d.cur.y < d.start.y ? d.start.y - h : d.start.y;
    return [x0, y0, x0 + w, y0 + h].map(round);
  }
  function drawPlacement(d) {
    const [x0, y0, x1, y1] = placementRect(d);
    Object.entries({ x: x0, y: y0, width: x1 - x0, height: y1 - y0 }).forEach(([k, v]) => d.node.setAttribute(k, v));
  }
  function placeImage(i, rect) {
    const pl = S.placing;
    A.setMode("select");
    clearGhost();
    A.edit("annot_add", () => ({ page: i, rev: V.pages[i].rev, kind: "image", rect, sig: pl.id, label: A.t("signature") }))
      .then((r) => { if (r) { A.toast(A.t("sigPlaced")); A.selectAnnot({ page: i, xref: r.xref }); } });
  }

  /* ---------- eraser ---------- */
  function eraserHover(e) {
    A.$$(".ah.erase").forEach((n) => n.classList.remove("erase"));
    const hit = e.target.closest && e.target.closest(".ah");
    if (hit) hit.classList.add("erase");
  }

  /* ---------- selecting / moving annotations ---------- */
  function selectDown(e, pg, p) {
    const handle = e.target.closest(".handle");
    const hit = e.target.closest(".ah");
    if (handle && S.selAnnot) {
      e.preventDefault();
      pg.el.setPointerCapture(e.pointerId);
      drag = { mode: "resize", pg, start: p, a: S.selAnnot.a };
      return;
    }
    if (!hit) { if (S.selAnnot) A.selectAnnot(null); return; }
    e.preventDefault();
    const a = hit._a;
    A.selectAnnot({ page: idx(pg), xref: a.x });
    if (!a.mv || !A.can("annotate")) return;
    pg.el.setPointerCapture(e.pointerId);
    drag = { mode: "move", pg, start: p, a, moved: false };
  }
  function finishAnnotDrag(d, i) {
    if (d.mode === "move" && d.moved) {
      A.edit("annot_move", () => ({ page: i, rev: V.pages[i].rev, xref: d.a.x, dx: round(d.dx), dy: round(d.dy) }), { noBusy: true });
    } else if (d.mode === "resize" && d.rect) {
      A.edit("annot_resize", () => ({ page: i, rev: V.pages[i].rev, xref: d.a.x, rect: d.rect.map(round) }), { noBusy: true });
    } else A.renderOverlay(d.pg);
  }
  function annotDragMove(e, p) {
    const d = drag, a = d.a;
    const dx = p.x - d.start.x, dy = p.y - d.start.y;
    if (d.mode === "move") {
      if (!d.moved && Math.hypot(dx, dy) < 2) return;
      d.moved = true;
      d.dx = dx; d.dy = dy;
      drawSelection(d.pg, a.r, false, dx, dy);
      A.$("#annbar").classList.add("hidden");
    } else {
      const [x0, y0, x1, y1] = a.r;
      const ratio = a.img ? (y1 - y0) / (x1 - x0) : null;
      let nx1 = Math.max(x0 + 8, x1 + dx), ny1 = ratio ? y0 + (nx1 - x0) * ratio : Math.max(y0 + 8, y1 + dy);
      d.rect = [x0, y0, nx1, ny1];
      drawSelection(d.pg, d.rect, true);
    }
  }
  A.selectAnnot = (sel) => {
    const prev = S.selAnnot;
    S.selAnnot = sel;
    if (prev && V.pages[prev.page]) A.renderOverlay(V.pages[prev.page]);
    if (sel && V.pages[sel.page] && (!prev || prev.page !== sel.page)) A.renderOverlay(V.pages[sel.page]);
    A.positionAnnbar();
    A.emit("annotSelected", sel);
  };

  A.deleteAnnot = (page, xref) => {
    if (S.selAnnot && S.selAnnot.xref === xref) S.selAnnot = null;
    return A.edit("annot_delete", () => ({ page, rev: V.pages[page].rev, xref }), { noBusy: true });
  };

  /* annotation mini toolbar */
  A.positionAnnbar = () => {
    const bar = A.$("#annbar");
    const sel = S.selAnnot;
    const pg = sel && V.pages[sel.page];
    if (!sel || !pg || !pg.mat || !sel.a || S.mode !== "select") { bar.classList.add("hidden"); return; }
    const a = sel.a;
    const colorable = !a.img && a.t !== "Redact";
    const editable = a.t === "FreeText" || a.t === "Text";
    const sw = colorable ? (a.t === "Highlight" ? HL_COLORS : COLORS).map((c) => `<button class="sw" data-c="${c}" style="--c:${c}" aria-label="${c}"></button>`).join("") : "";
    bar.innerHTML = `${sw}${editable ? `<button class="icon-btn sm" data-act="edit" data-tip="editText">${A.icon("text", 16)}</button>` : ""}` +
      `<button class="icon-btn sm danger" data-act="del" data-tip="delete">${A.icon("trash", 16)}</button>` +
      (a.by ? `<span class="by">${A.esc(a.by)}</span>` : "");
    A.applyI18n(bar);
    bar.classList.remove("hidden");
    const pr = pg.el.getBoundingClientRect();
    const s = pr.width / pg.w;
    const x = pr.left + ((a.r[0] + a.r[2]) / 2) * s, y = pr.top + a.r[1] * s;
    const bw = bar.offsetWidth;
    bar.style.left = `${A.clamp(x - bw / 2, 8, window.innerWidth - bw - 8)}px`;
    bar.style.top = `${Math.max(64, y - bar.offsetHeight - 12)}px`;
    bar.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      const i = sel.page;
      if (b.dataset.c) A.edit("annot_edit", () => ({ page: i, rev: V.pages[i].rev, xref: a.x, color: b.dataset.c }), { noBusy: true });
      else if (b.dataset.act === "del") A.deleteAnnot(i, a.x);
      else if (b.dataset.act === "edit") A.editAnnotText(i, a);
    };
  };
  A.editAnnotText = async (i, a) => {
    const text = await A.prompt(A.t("editText"), A.t("noteText"), a.txt || "", { multiline: true });
    if (text == null) return;
    A.edit("annot_edit", () => ({ page: i, rev: V.pages[i].rev, xref: a.x, content: text }));
  };

  /* ---------- text selection -> quick actions ---------- */
  function selectedWords() {
    const sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const byPage = new Map();
    V.pages.forEach((pg) => {
      if (!pg.mat || !pg.tl || !range.intersectsNode(pg.tl)) return;
      const ws = [];
      pg.tl.querySelectorAll("span").forEach((sp) => { if (range.intersectsNode(sp)) ws.push(+sp.dataset.w); });
      if (ws.length) byPage.set(idx(pg), ws);
    });
    return byPage.size ? { byPage, text: sel.toString() } : null;
  }
  function hideSelbar() { A.$("#selbar").classList.add("hidden"); }
  A.clearTextSelection = () => { const s = window.getSelection(); if (s && !s.isCollapsed) s.removeAllRanges(); hideSelbar(); };
  function showSelbar() {
    if (S.mode !== "select") return;
    const sw = selectedWords();
    const bar = A.$("#selbar");
    if (!sw) { hideSelbar(); return; }
    const r = window.getSelection().getRangeAt(0).getBoundingClientRect();
    if (!r.width && !r.height) { hideSelbar(); return; }
    const canAnn = A.can("annotate"), canCopy = A.can("copy");
    bar.innerHTML =
      (canCopy ? `<button class="icon-btn sm" data-act="copy" data-tip="copy">${A.icon("copy", 16)}</button>` : "") +
      (canAnn ? `<button class="icon-btn sm" data-act="highlight" data-tip="t_highlight">${A.icon("highlight", 16)}</button>` +
        `<button class="icon-btn sm" data-act="underline" data-tip="t_underline">${A.icon("underline", 16)}</button>` +
        `<button class="icon-btn sm" data-act="strikeout" data-tip="t_strikeout">${A.icon("strikeout", 16)}</button>` : "") +
      (A.can("modify") ? `<button class="icon-btn sm" data-act="redact" data-tip="t_redactSel">${A.icon("redact", 16)}</button>` : "") +
      `<button class="icon-btn sm" data-act="speak" data-tip="readAloud">${A.icon("speaker", 16)}</button>`;
    A.applyI18n(bar);
    bar.classList.remove("hidden");
    bar.style.left = `${A.clamp(r.left + r.width / 2 - bar.offsetWidth / 2, 8, window.innerWidth - bar.offsetWidth - 8)}px`;
    bar.style.top = `${Math.max(64, r.top - bar.offsetHeight - 10)}px`;
    bar.onclick = async (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      const act = b.dataset.act;
      if (act === "copy") { copyText(sw.text); }
      else if (act === "speak") { A.speak(sw.text); }
      else if (act === "redact") {
        for (const [i, ws] of sw.byPage) {
          const words = V.pages[i].info.words;
          A.edit("annot_add", () => ({ page: i, rev: V.pages[i].rev, kind: "redact", rects: ws.map((k) => wordBox(words[k]).map(round)) }), { noBusy: true });
        }
        A.toast(A.t("redactMarked"), "info");
      } else {
        for (const [i, ws] of sw.byPage) A.edit("annot_add", () => ({ page: i, rev: V.pages[i].rev, kind: act, words: ws, color: act === "highlight" ? S.style.hl : S.style.color }), { noBusy: true });
      }
      window.getSelection().removeAllRanges();
      hideSelbar();
    };
  }
  function copyText(text) {
    const done = () => A.toast(A.t("copied"));
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, () => { legacyCopy(text); done(); });
    else { legacyCopy(text); done(); }
  }
  function legacyCopy(text) {
    const ta = A.h(`<textarea style="position:fixed;left:-9999px"></textarea>`);
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  A.copyText = copyText;

  /* ---------- panning ---------- */
  let pan = null, spaceDown = false;
  function startPan(e) {
    e.preventDefault();
    pan = { x: e.clientX, y: e.clientY, sl: V.viewer.scrollLeft, st: V.viewer.scrollTop };
    A.$("#pages").classList.add("panning");
  }
  function panMove(e) {
    V.viewer.scrollLeft = pan.sl - (e.clientX - pan.x);
    V.viewer.scrollTop = pan.st - (e.clientY - pan.y);
  }
  function panEnd() { pan = null; A.$("#pages").classList.remove("panning"); }

  /* ---------- annotation toolbar ---------- */
  function renderStyleRow() {
    const row = A.$("#atb .style-row");
    if (!row) return;
    const m = S.mode;
    const showColor = ["pen", "rect", "ellipse", "line", "arrow", "underline", "strikeout", "note", "highlight", "text"].includes(m);
    const palette = m === "highlight" ? HL_COLORS : COLORS;
    const cur = m === "highlight" ? S.style.hl : m === "text" ? S.style.text : S.style.color;
    const showW = ["pen", "rect", "ellipse", "line", "arrow"].includes(m);
    row.innerHTML = showColor
      ? palette.map((c) => `<button class="sw${c === cur ? " on" : ""}" data-c="${c}" style="--c:${c}" aria-label="${c}"></button>`).join("") +
        (showW ? `<span class="sep"></span>` + WIDTHS.map((w) => `<button class="wd${w === S.style.width ? " on" : ""}" data-w="${w}" aria-label="${w}"><i style="height:${w + 1}px"></i></button>`).join("") : "") +
        (m === "text" ? `<span class="sep"></span>` + [9, 12, 16, 22].map((s) => `<button class="fsz${s === S.style.size ? " on" : ""}" data-s="${s}">${s}</button>`).join("") : "")
      : `<span class="hint-txt">${A.esc(A.t("mode_" + m) || "")}</span>`;
    row.onclick = (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.dataset.c) {
        if (m === "highlight") S.style.hl = b.dataset.c;
        else if (m === "text") S.style.text = b.dataset.c;
        else S.style.color = b.dataset.c;
      }
      if (b.dataset.w) S.style.width = +b.dataset.w;
      if (b.dataset.s) S.style.size = +b.dataset.s;
      renderStyleRow();
    };
  }

  A.buildAnnotToolbar = () => {
    const atb = A.$("#atb");
    atb.innerHTML = `<div class="tool-row">${TOOLS.map((t) => (t
      ? `<button class="icon-btn" data-mode="${t[0]}" data-tip="t_${t[0]}" data-key="${t[2]}">${A.icon(t[1], 18)}</button>`
      : `<span class="sep"></span>`)).join("")}</div><div class="style-row"></div>`;
    A.applyI18n(atb);
    atb.onclick = (e) => {
      const b = e.target.closest("[data-mode]");
      if (!b) return;
      if (!A.can("annotate") && !["select", "hand"].includes(b.dataset.mode)) return A.toast(A.t("e_NOT_ALLOWED"), "bad");
      A.setMode(b.dataset.mode);
    };
    A.$$("[data-mode]", atb).forEach((b) => b.classList.toggle("active", b.dataset.mode === S.mode));
    renderStyleRow();
  };
  A.showAnnotToolbar = (on) => A.$("#atb").classList.toggle("hidden", !on);

  /* ---------- init ---------- */
  A.initTools = () => {
    const host = A.$("#pages");
    host.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    host.addEventListener("dblclick", (e) => {
      if (S.mode !== "select") return;
      const hit = e.target.closest(".ah");
      const pg = pageOf(e.target);
      if (hit && pg && (hit._a.t === "FreeText" || hit._a.t === "Text")) A.editAnnotText(idx(pg), hit._a);
    });
    document.addEventListener("mouseup", () => setTimeout(showSelbar, 0));
    document.addEventListener("selectionchange", () => { if (window.getSelection().isCollapsed) hideSelbar(); });
    A.$("#viewer").addEventListener("scroll", () => { hideSelbar(); if (S.selAnnot) A.positionAnnbar(); }, { passive: true });
    window.addEventListener("keydown", (e) => { if (e.code === "Space" && !e.repeat && !isTyping(e)) { spaceDown = true; host.classList.add("handmode"); } });
    window.addEventListener("keyup", (e) => { if (e.code === "Space") { spaceDown = false; host.classList.toggle("handmode", S.mode === "hand"); } });
    A.on("zoom", () => { if (S.selAnnot) A.positionAnnbar(); });
  };
  const isTyping = (e) => /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  A.isTyping = isTyping;
  A.wordBox = wordBox;
})(window.Aurora);
