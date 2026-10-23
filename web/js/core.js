/* Aurora PDF - shared state, backend calls and UI primitives (toast, modal, jobs). */
"use strict";
(function (A) {
  const S = (A.S = {
    lang: "sr",
    settings: {},
    doc: null,            // document summary from the backend (or {open:false})
    recent: [],
    ocrLangs: {},
    panel: null,          // open right-hand panel id
    mode: "select",       // pointer tool on the page
    side: "pages",        // left sidebar tab
    zoom: 1,
    fit: "width",         // width | page | null (manual zoom)
    night: false,
    current: 0,           // page in view
    selected: new Set(),  // selected pages (thumbnails / organize grid)
    search: { q: "", hits: [], cur: -1 },
    style: { color: "#e11d48", width: 2, hl: "#facc15", text: "#111827", size: 12 },
    selAnnot: null,       // {page, xref, a}
    organize: false,
  });

  /* ---------- DOM helpers ---------- */
  A.$ = (s, r = document) => r.querySelector(s);
  A.$$ = (s, r = document) => [...r.querySelectorAll(s)];
  A.h = (html) => {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  };
  A.esc = (v) => (v == null ? "" : String(v)).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" }[c]));
  A.clamp = (v, a, b) => Math.min(b, Math.max(a, v));

  /* ---------- events ---------- */
  const handlers = {};
  A.on = (evt, fn) => (handlers[evt] = handlers[evt] || []).push(fn);
  A.emit = (evt, data) => (handlers[evt] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(evt, e); } });

  /* ---------- i18n ---------- */
  A.t = (key, vars) => {
    const d = A.I18N[S.lang] || A.I18N.en;
    let s = d[key];
    if (s == null) s = A.I18N.en[key];
    if (s == null) s = key;
    if (vars) s = s.replace(/\{(\w+)\}/g, (_, k) => (vars[k] == null ? "" : vars[k]));
    return s;
  };
  A.applyI18n = (root = document) => {
    A.$$("[data-i18n]", root).forEach((n) => (n.textContent = A.t(n.dataset.i18n)));
    A.$$("[data-i18n-ph]", root).forEach((n) => (n.placeholder = A.t(n.dataset.i18nPh)));
    A.$$("[data-tip]", root).forEach((n) => {
      const key = n.dataset.key ? ` (${n.dataset.key})` : "";
      n.setAttribute("aria-label", A.t(n.dataset.tip));
      n.dataset.tipText = A.t(n.dataset.tip) + key;
    });
  };

  /* ---------- backend ---------- */
  const BASE = location.pathname.replace(/[^/]*$/, "");
  A.url = (p) => BASE + p;

  A.rpc = async (method, payload) => {
    const res = await fetch(A.url("api/" + method), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error("HTTP " + res.status);
    return res.json();
  };

  let busyTimer = null, busyCount = 0;
  A.busy = (on) => {
    busyCount = Math.max(0, busyCount + (on ? 1 : -1));
    clearTimeout(busyTimer);
    if (busyCount > 0) busyTimer = setTimeout(() => A.$("#busy").classList.add("show"), 280);
    else A.$("#busy").classList.remove("show");
  };

  /** Call the backend with the usual handling: spinner, error toast, document refresh.
   *  Resolves to the result, or null when it failed or was cancelled (raw:true returns errors too). */
  A.call = async (method, payload, opts = {}) => {
    if (!opts.noBusy) A.busy(true);
    let r;
    try {
      r = await A.rpc(method, payload);
    } catch (e) {
      console.error(method, e);
      A.toast(A.t("e_NETWORK"), "bad");
      return null;
    } finally {
      if (!opts.noBusy) A.busy(false);
    }
    if (r && r.ok === false) {
      if (r.error === "STALE") { A.resync(); return opts.raw ? r : null; }
      const handled = opts.handle && opts.handle(r);
      if (!handled && r.error !== "CANCELLED" && !opts.quiet) A.toastError(r);
      return opts.raw ? r : null;
    }
    if (r && r.doc) A.applyDoc(r.doc);
    return r;
  };

  // edits run one after another so each request sees the result of the previous one
  let chain = Promise.resolve();
  A.edit = (method, payload, opts) => {
    if (A.flushInk) A.flushInk(); // ink strokes wait to be sent together: they come before what was done after them
    const p = chain.then(() => A.call(method, typeof payload === "function" ? payload() : payload, opts));
    chain = p.catch(() => null);
    return p;
  };
  /** The page at index ``i`` now, for an edit sent later: an earlier edit in the queue may move the pages
   *  (a delete, an insert) before it goes. It goes to that same page, found by its object number, or comes
   *  back STALE when the page is gone. Resolves to {page, rev} when called. */
  A.pageRef = (i) => {
    const px = S.doc && S.doc.pages[i] ? S.doc.pages[i][3] : 0;
    return () => {
      const k = S.doc ? S.doc.pages.findIndex((p) => p[3] === px) : -1;
      return k < 0 ? { page: i, rev: -1 } : { page: k, rev: S.doc.pages[k][2] };
    };
  };
  /** Resolves once every queued edit has been answered (used by tests and before saving). */
  A.idle = () => chain.then(() => null);
  /** Finish what the user just did (a form field or text box still being typed in, ink waiting to be
   *  batched, queued edits) before the document is saved, printed or exported. */
  A.commitPending = async () => {
    const el = document.activeElement;
    if (el && el.closest(".pg")) el.blur(); // fields and text boxes on a page send their text when they lose focus
    if (A.flushInk) A.flushInk();
    await A.idle();
  };

  A.applyDoc = (doc) => {
    const prev = S.doc;
    S.doc = doc && doc.open ? doc : null;
    A.emit("doc", { prev, doc: S.doc });
  };

  A.resync = async () => {
    const r = await A.rpc("state").catch(() => null);
    if (r && r.ok) A.applyDoc(r.doc);
  };

  A.can = (right) => !!(S.doc && (!S.doc.rights || S.doc.rights[right] !== false));

  /* ---------- toasts ---------- */
  A.toast = (msg, kind = "good", action) => {
    const ico = kind === "bad" ? "warn" : kind === "info" ? "info" : "check";
    const node = A.h(`<div class="toast ${kind}">${A.icon(ico, 18, "dot")}<span class="msg"></span></div>`);
    A.$(".msg", node).textContent = msg;
    if (action) {
      const b = A.h(`<button class="toast-act"></button>`);
      b.textContent = action.label;
      b.onclick = () => { action.run(); node.remove(); };
      node.appendChild(b);
    }
    A.$("#toasts").appendChild(node);
    const life = action ? 7000 : kind === "bad" ? 5200 : 3400;
    setTimeout(() => { node.classList.add("out"); setTimeout(() => node.remove(), 260); }, life);
  };
  A.toastError = (r) => {
    const key = "e_" + (r.error || "ERROR");
    let msg = A.t(key);
    if (msg === key) msg = A.t("e_ERROR");
    if (r.message && (r.error === "ERROR" || r.error === "INVALID")) msg += " " + r.message;
    A.toast(msg, "bad");
  };
  A.savedToast = (path, msgKey = "savedTo") => {
    const name = (path || "").split(/[\\/]/).pop();
    A.toast(A.t(msgKey, { name }), "good", path ? { label: A.t("showFolder"), run: () => A.call("reveal", { path }, { quiet: true }) } : null);
  };

  /* ---------- modal ---------- */
  let modalStack = [];
  A.modal = ({ title, body, buttons, wide, onOpen, dismiss = true, cls = "" }) =>
    new Promise((resolve) => {
      const root = A.$("#modalRoot");
      const scrim = A.h(`<div class="scrim"><div class="modal ${wide ? "wide" : ""} ${cls}" role="dialog" aria-modal="true">
        <div class="m-head"><h3></h3>${dismiss ? `<button class="icon-btn sm m-x" aria-label="close">${A.icon("x", 18)}</button>` : ""}</div>
        <div class="m-body"></div><div class="m-foot"></div></div></div>`);
      A.$("h3", scrim).textContent = title || "";
      const bodyEl = A.$(".m-body", scrim);
      if (typeof body === "string") bodyEl.innerHTML = body;
      else if (body) bodyEl.appendChild(body);
      const foot = A.$(".m-foot", scrim);
      let done = false;
      const close = (val) => {
        if (done) return;
        done = true;
        scrim.classList.add("out");
        setTimeout(() => scrim.remove(), 160);
        modalStack = modalStack.filter((m) => m !== api);
        resolve(val);
      };
      const api = { el: scrim, body: bodyEl, close };
      (buttons || [{ label: A.t("ok"), kind: "primary", value: true }]).forEach((b) => {
        const btn = A.h(`<button class="btn ${b.kind || ""}"></button>`);
        btn.textContent = b.label;
        if (b.id) btn.id = b.id;
        btn.onclick = async () => {
          if (b.onClick) {
            btn.disabled = true;
            let keep;
            try { keep = await b.onClick(api); } finally { btn.disabled = false; }
            if (keep === false) return;
            close(keep === undefined ? b.value : keep);
          } else close(b.value);
        };
        foot.appendChild(btn);
      });
      if (dismiss) {
        A.$(".m-x", scrim).onclick = () => close(null);
        scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) close(null); });
      }
      api.dismiss = dismiss;
      root.appendChild(scrim);
      modalStack.push(api);
      A.applyI18n(scrim);
      const first = A.$("input, textarea, select", bodyEl);
      if (first) setTimeout(() => first.focus(), 30);
      A.$$("input", bodyEl).forEach((inp) => inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && inp.type !== "checkbox") { const p = A.$(".m-foot .btn.primary", scrim); if (p) p.click(); }
      }));
      if (onOpen) onOpen(api);
    });
  A.topModal = () => modalStack[modalStack.length - 1];
  A.closeTopModal = () => { const m = A.topModal(); if (m && m.dismiss) { m.close(null); return true; } return false; };

  A.confirm = (title, msg, okLabel, danger) =>
    A.modal({
      title,
      body: `<p>${A.esc(msg)}</p>`,
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: false },
        { label: okLabel || A.t("ok"), kind: danger ? "danger solid" : "primary", value: true },
      ],
    }).then((v) => !!v);

  A.prompt = (title, label, value = "", opts = {}) =>
    A.modal({
      title,
      body: `<div class="field"><label>${A.esc(label)}</label>${opts.multiline
        ? `<textarea class="input" id="pIn" rows="4">${A.esc(value)}</textarea>`
        : `<input class="input" id="pIn" type="${opts.password ? "password" : "text"}" value="${A.esc(value)}">`}</div>${opts.hint ? `<p class="hint">${A.esc(opts.hint)}</p>` : ""}`,
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        { label: opts.ok || A.t("ok"), kind: "primary", onClick: (m) => A.$("#pIn", m.el).value },
      ],
    });

  /* ---------- jobs with progress ---------- */
  A.runJob = (jobId, title, total) =>
    new Promise((resolve) => {
      const body = A.h(`<div class="job"><div class="bar"><i></i></div><div class="job-txt"></div></div>`);
      const bar = A.$("i", body), txt = A.$(".job-txt", body);
      let dlg = null;
      A.modal({
        title,
        body,
        dismiss: false,
        onOpen: (m) => (dlg = m),
        buttons: [{ label: A.t("cancel"), kind: "ghost", onClick: () => { A.rpc("job_cancel", { id: jobId }); txt.textContent = A.t("cancelling"); return false; } }],
      });
      const tick = async () => {
        const r = await A.rpc("job_status", { id: jobId }).catch(() => null);
        const j = r && r.job;
        if (j && j.state === "running") {
          const tot = j.total || total || 0;
          bar.style.width = tot ? `${Math.round((j.done / tot) * 100)}%` : "12%";
          bar.parentElement.classList.toggle("indet", !tot);
          txt.textContent = tot ? A.t("progressOf", { done: j.done, total: tot }) : A.t("working");
          setTimeout(tick, 280);
          return;
        }
        dlg.close(null); // its own: another dialog may have opened on top meanwhile
        if (j && j.doc) A.applyDoc(j.doc);
        resolve(j || { state: "error", error: "?" });
      };
      setTimeout(tick, 150);
    });

  /* ---------- formatting ---------- */
  const LOCALE = { sr: "sr-Latn-RS", en: "en-GB", no: "nb-NO", pt: "pt-PT" }; // the rest are their own locale tags
  A.fmtNum = (n, digits = 0) => n.toLocaleString(LOCALE[S.lang] || S.lang, { maximumFractionDigits: digits, minimumFractionDigits: digits });
  A.fmtBytes = (n) => {
    if (!n && n !== 0) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return `${A.fmtNum(n, i ? 1 : 0)} ${u[i]}`;
  };
  A.fmtPdfDate = (d) => {
    const m = /D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?/.exec(d || "");
    if (!m) return "";
    return A.t("dateFmt", { d: +m[3], m: A.t("month" + +m[2]), y: m[1] }) + (m[4] ? ` ${m[4]}:${m[5] || "00"}` : "");
  };
  A.fmtTime = (t) => {
    if (!t) return "";
    const d = new Date(t * 1000);
    return A.t("dateFmt", { d: d.getDate(), m: A.t("month" + (d.getMonth() + 1)), y: d.getFullYear() });
  };
  A.fmtIsoDate = (s) => {
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?/.exec(s || "");
    if (!m) return s || "";
    return A.t("dateFmt", { d: +m[3], m: A.t("month" + +m[2]), y: m[1] }) + (m[4] ? ` ${m[4]}:${m[5]}` : "");
  };
  /** Numeric date (and time) the way the interface language writes it: 13.10.2026, 13. 10. 2026., 13/10/2026. */
  A.fmtShort = (d = new Date(), time = false) => d.toLocaleString(LOCALE[S.lang] || S.lang,
    Object.assign({ day: "numeric", month: "numeric", year: "numeric" }, time ? { hour: "2-digit", minute: "2-digit" } : {}));
  A.dirname = (p) => (p || "").replace(/[\\/][^\\/]*$/, "");
  A.basename = (p) => (p || "").split(/[\\/]/).pop();

  /** Page list text ("1-3, 5") from a set of 0-based indices. */
  A.rangeText = (pages) => {
    const a = [...pages].sort((x, y) => x - y);
    const out = [];
    for (let i = 0; i < a.length; i++) {
      let j = i;
      while (j + 1 < a.length && a[j + 1] === a[j] + 1) j++;
      out.push(i === j ? `${a[i] + 1}` : `${a[i] + 1}-${a[j] + 1}`);
      i = j;
    }
    return out.join(", ");
  };
})(window.Aurora);
