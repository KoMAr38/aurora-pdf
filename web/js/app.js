/* Aurora PDF - application shell: start screen, top bar, view bar, files, keyboard. */
"use strict";
(function (A) {
  const S = A.S;
  const $ = A.$;

  /* ---------- settings ---------- */
  A.saveSettings = (obj) => {
    Object.assign(S.settings, obj);
    return A.call("settings_set", obj, { quiet: true, noBusy: true });
  };
  A.setLang = (lang) => {
    if (!A.I18N[lang]) return;
    S.lang = lang;
    document.documentElement.lang = lang;
    A.saveSettings({ lang });
    rebuildChrome();
  };
  A.setNight = (on) => {
    S.night = !!on;
    $("#app").classList.toggle("night", S.night);
    A.saveSettings({ night: S.night });
    renderVbar();
  };

  /* theme: "system" follows the Windows app colour (seen here as prefers-color-scheme), or "dark" / "light" */
  const lightOS = window.matchMedia("(prefers-color-scheme: light)");
  A.isLight = () => document.documentElement.dataset.theme === "light";
  function applyTheme() {
    const t = S.settings.theme || "system";
    const light = t === "light" || (t === "system" && lightOS.matches);
    document.documentElement.dataset.theme = light ? "light" : "dark";
    A.rpc("window_theme", { dark: !light }).catch(() => {}); // only the title bar; nothing to report
  }
  A.setTheme = (theme) => {
    A.saveSettings({ theme });
    applyTheme();
    if (S.panel === "settings") A.refreshPanel();
  };
  lightOS.addEventListener("change", () => { if ((S.settings.theme || "system") === "system") applyTheme(); });

  function rebuildChrome() {
    A.applyI18n();
    A.buildRail();
    A.buildSideTabs();
    if (S.panel === "annotate") A.buildAnnotToolbar();
    A.refreshPanel();
    renderStart();
    renderFileInfo();
    renderVbar();
    A.emit("lang");
  }

  /* ---------- opening files ---------- */
  async function guardUnsaved() {
    await A.commitPending();
    if (!S.doc || !S.doc.dirty) return true;
    const v = await A.modal({
      title: A.t("unsavedTitle"),
      body: `<p>${A.esc(A.t("unsavedMsg", { name: S.doc.name }))}</p>`,
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        { label: A.t("dontSave"), kind: "", value: "discard" },
        { label: A.t("save"), kind: "primary", value: "save" },
      ],
    });
    if (v === "discard") return true;
    if (v === "save") return !!(await A.save());
    return false;
  }
  A.guardUnsaved = guardUnsaved;

  async function handleOpen(r) {
    if (!r) return null;
    if (r.ok) { afterOpen(); return r; }
    if (r.error === "NEED_PASSWORD" || r.error === "WRONG_PASSWORD") return askPassword(r.path, r.name, r.error === "WRONG_PASSWORD");
    if (r.error !== "CANCELLED") A.toastError(r);
    return null;
  }
  A.openFile = async () => {
    if (!(await guardUnsaved())) return;
    handleOpen(await A.call("open_dialog", {}, { raw: true, quiet: true }));
  };
  A.openPath = async (path, opts = {}) => {
    if (!opts.skipGuard && !(await guardUnsaved())) return;
    handleOpen(await A.call("open_path", { path }, { raw: true, quiet: true }));
  };
  async function askPassword(path, name, wrong) {
    const pw = await A.modal({
      title: A.t("needPw"),
      body: `<p>${A.esc(A.t("needPwSub", { name }))}</p><div class="field"><label>${A.esc(A.t("password"))}</label><input class="input" id="pwIn" type="password" autocomplete="off"></div>${wrong ? `<p class="hint warn">${A.esc(A.t("wrongPw"))}</p>` : ""}`,
      buttons: [{ label: A.t("cancel"), kind: "ghost", value: null }, { label: A.t("unlock"), kind: "primary", onClick: (m) => $("#pwIn", m.el).value }],
    });
    if (pw == null) return null;
    return handleOpen(await A.call("open_path", { path, password: pw }, { raw: true, quiet: true }));
  }
  function afterOpen() {
    A.setMode("select");
    A.clearTextSelection();
    S.search = { q: "", hits: [], cur: -1 };
    $("#search").value = "";
    renderSearchCount();
    if (S.doc && S.doc.scanned) {
      A.showBanner(A.t("scannedBanner"), null, { label: A.t("ocrRun"), run: () => { A.hideBanner(); A.openPanel("ocr"); } });
    } else A.hideBanner();
    if (S.doc && S.doc.protected && !S.doc.owner && !S.doc.rights.modify) A.toast(A.t("restrictedToast"), "info");
  }
  A.closeDoc = async () => {
    if (!(await guardUnsaved())) return;
    await A.call("close_doc", {});
    A.hideBanner();
    A.openPanel(null);
    refreshRecent();
  };

  /* ---------- saving ---------- */
  // both resolve to the backend's answer once the file is written (also by way of Save As or the
  // password question), else to null: "save, then close or open another" relies on it
  A.save = async () => {
    if (!S.doc) return null;
    await A.commitPending();
    if (S.doc.signed && S.doc.dirty && !(await A.confirm(A.t("signedSaveTitle"), A.t("signedSaveMsg"), A.t("saveAnyway")))) return null;
    const r = await A.call("save", {}, { raw: true, handle: (e) => ["NEED_PATH", "SAVE_AS_COPY", "NEED_PASSWORDS"].includes(e.error) });
    if (!r || r.ok === false) {
      if (r && r.error === "NEED_PATH") return A.saveAs();
      if (r && r.error === "SAVE_AS_COPY") { A.toast(A.t("e_SAVE_AS_COPY"), "info"); return A.saveAs(); }
      if (r && r.error === "NEED_PASSWORDS") return needPasswords(A.save);
      return null;
    }
    A.toast(A.t("savedTo", { name: A.basename(r.path) }));
    return r;
  };
  A.saveAs = async (extra) => {
    if (!S.doc) return null;
    await A.commitPending();
    if (S.doc.signed && S.doc.dirty && !(await A.confirm(A.t("signedSaveTitle"), A.t("signedSaveMsg"), A.t("saveAnyway")))) return null;
    const r = await A.call("save_as", { ...extra }, { raw: true, handle: (e) => e.error === "NEED_PASSWORDS" });
    if (!r || r.ok === false) return r && r.error === "NEED_PASSWORDS" ? needPasswords(A.saveAs, true) : null;
    A.savedToast(r.path);
    return r;
  };
  /** The open password is unknown: drop the protection or give that password, then save the same way again.
   *  For a copy (Save As, extract, split, compress) only the copy goes without it: `retry({ unprotected: true })`,
   *  so a cancelled file dialog leaves the document protected. */
  async function needPasswords(retry, copy) {
    const r = await A.modal({
      title: A.t("needPasswordsTitle"),
      body: `<p>${A.esc(A.t(copy ? "needPasswordsCopyMsg" : "needPasswordsMsg"))}</p><div class="field"><label>${A.esc(A.t("openPw"))}</label><input class="input" id="npU" type="password"></div>`,
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        { label: A.t("removeProtection"), kind: "", value: "remove" },
        { label: A.t("keepProtection"), kind: "primary", onClick: (m) => ({ user: $("#npU", m.el).value }) },
      ],
    });
    if (!r) return null;
    if (r === "remove" && copy) return retry({ unprotected: true });
    const ok = r === "remove"
      ? await A.call("set_protection", { remove: true })
      : await A.call("set_protection", { user_pw: r.user });
    return ok ? retry() : null;
  }
  /** `handle` for copies that keep the protection (extract, split, compress): the same question, then `again(extra)`. */
  A.askPasswords = (again) => (e) => e.error === "NEED_PASSWORDS" && (needPasswords(again, true), true);

  /* ---------- printing ---------- */
  A.print = async (spec) => {
    if (!S.doc) return;
    if (!A.can("print")) return A.toast(A.t("e_NOT_ALLOWED"), "bad");
    let pages;
    if (spec == null) {
      const v = await A.modal({
        title: A.t("print"),
        body: `<div class="field"><label>${A.esc(A.t("pagesRange"))}</label><div class="seg" id="prSeg"><button class="active" data-v="all">${A.esc(A.t("allPages"))}</button><button data-v="cur">${A.esc(A.t("currentPage"))}</button><button data-v="range">${A.esc(A.t("range"))}</button></div></div>
          <input class="input" id="prRange" placeholder="${A.esc(A.t("pagesPh"))}" style="display:none">`,
        onOpen: (m) => {
          $("#prSeg", m.el).onclick = (e) => {
            const b = e.target.closest("button");
            if (!b) return;
            A.$$("#prSeg button", m.el).forEach((x) => x.classList.toggle("active", x === b));
            $("#prRange", m.el).style.display = b.dataset.v === "range" ? "" : "none";
          };
        },
        buttons: [{ label: A.t("cancel"), kind: "ghost", value: null }, { label: A.t("printGo"), kind: "primary", onClick: (m) => { const b = $("#prSeg .active", m.el).dataset.v; return b === "range" ? $("#prRange", m.el).value || "all" : b; } }],
      });
      if (!v) return;
      spec = v === "cur" ? String(S.current + 1) : v;
    }
    try { pages = parseRange(spec, S.doc.pages.length); } catch (e) { return A.toast(A.t("e_BAD_PAGES"), "bad"); }
    await A.commitPending();
    const revs = pages.map((i) => S.doc.pages[i][2]);
    const maxW = Math.max(...pages.map((i) => S.doc.pages[i][0]));
    const w = A.bucket(Math.min(3000, Math.round((maxW / 72) * 200)));
    const frame = $("#printFrame");
    A.busy(true);
    const onMsg = (e) => {
      if (!e.data || !e.data.auroraPrintReady) return;
      window.removeEventListener("message", onMsg);
      A.busy(false);
      try { frame.contentWindow.focus(); frame.contentWindow.print(); } catch (err) { A.toast(A.t("e_ERROR"), "bad"); }
    };
    window.addEventListener("message", onMsg);
    frame.src = A.url(`print?revs=${revs.join(",")}&w=${w}`);
  };
  function parseRange(spec, total) {
    spec = String(spec || "").trim().toLowerCase();
    if (!spec || spec === "all") return [...Array(total).keys()];
    const out = new Set();
    spec.split(/[,;]/).forEach((part) => {
      part = part.trim();
      if (!part) return;
      const m = /^(\d*)\s*-\s*(\d*)$/.exec(part);
      const [a, b] = m ? [m[1] ? +m[1] : 1, m[2] ? +m[2] : total] : [+part, +part];
      if (isNaN(a) || isNaN(b)) throw new Error("bad");
      for (let k = a; k <= b; k++) if (k >= 1 && k <= total) out.add(k - 1);
    });
    if (!out.size) throw new Error("empty");
    return [...out].sort((x, y) => x - y);
  }

  /* ---------- read aloud ---------- */
  A.speak = (text) => {
    const synth = window.speechSynthesis;
    if (!synth) return A.toast(A.t("noSpeech"), "bad");
    synth.cancel();
    if (!text || !text.trim()) return A.toast(A.t("nothingToRead"), "info");
    const u = new SpeechSynthesisUtterance(text);
    const want = { sr: ["sr", "hr", "bs"], no: ["nb", "no", "nn"], en: ["en"] }[S.lang] || ["en"];
    const voices = synth.getVoices();
    const v = voices.find((x) => want.some((w) => x.lang.toLowerCase().startsWith(w)));
    if (v) { u.voice = v; u.lang = v.lang; }
    u.onend = u.onerror = () => { S.speaking = false; renderVbar(); };
    S.speaking = true;
    renderVbar();
    synth.speak(u);
  };
  A.readPage = async () => {
    if (S.speaking) { window.speechSynthesis.cancel(); S.speaking = false; renderVbar(); return; }
    const r = await A.call("page_text", { page: S.current }, { noBusy: true });
    if (r) A.speak(r.text);
  };

  /* ---------- banner ---------- */
  A.showBanner = (text, onCancel, action) => {
    const b = $("#banner");
    b.innerHTML = `<span class="bt"></span>${action ? `<button class="btn sm primary" id="bnAct"></button>` : ""}${onCancel ? `<button class="btn sm ghost" id="bnX">${A.esc(A.t(action ? "dismiss" : "cancel"))}</button>` : `<button class="icon-btn sm" id="bnX">${A.icon("x", 16)}</button>`}`;
    $(".bt", b).textContent = text;
    if (action) { $("#bnAct", b).textContent = action.label; $("#bnAct", b).onclick = action.run; }
    $("#bnX", b).onclick = () => { A.hideBanner(); if (onCancel) onCancel(); };
    // a hint for a drawing mode goes away by itself when that mode ends
    if (["sigbox", "crop", "redact", "place"].includes(S.mode)) b.dataset.mode = S.mode;
    else delete b.dataset.mode;
    b.classList.remove("hidden");
  };
  A.hideBanner = () => { const b = $("#banner"); b.classList.add("hidden"); delete b.dataset.mode; };
  A.on("mode", (m) => { const b = $("#banner"); if (b.dataset.mode && b.dataset.mode !== m) A.hideBanner(); });

  /* ---------- top bar ---------- */
  function renderFileInfo() {
    const fi = $("#fileInfo");
    const d = S.doc;
    if (!d) { fi.innerHTML = ""; A.call("set_title", { title: "Aurora PDF" }, { quiet: true, noBusy: true }); return; }
    const badges = [
      d.protected ? `<span class="badge warn" title="${A.esc(A.t("isProtected"))}">${A.icon("protect", 12)}${A.esc(A.t("badgeProtected"))}</span>` : "",
      d.signed ? `<span class="badge ok">${A.icon("shield", 12)}${A.esc(A.t("badgeSigned"))}</span>` : "",
      d.forms ? `<span class="badge">${A.icon("form", 12)}${A.esc(A.t("badgeForm"))}</span>` : "",
    ].join("");
    fi.innerHTML = `<span class="dirty${d.dirty ? " on" : ""}" title="${A.esc(A.t("unsaved"))}"></span><span class="name" title="${A.esc(d.path || d.name)}">${A.esc(d.name)}</span><span class="pc">${d.pages.length} ${A.esc(A.plural(d.pages.length, "page"))}</span>${badges}`;
    A.call("set_title", { title: `${d.dirty ? "• " : ""}${d.name} — Aurora PDF` }, { quiet: true, noBusy: true });
  }
  function renderTopButtons() {
    const d = S.doc;
    $("#undoBtn").disabled = !d || !d.undo;
    $("#redoBtn").disabled = !d || !d.redo;
    $("#undoBtn").dataset.tipText = A.t("undo") + (d && d.undo ? `: ${A.t("op_" + d.undo)}` : "") + " (Ctrl+Z)";
    $("#redoBtn").dataset.tipText = A.t("redo") + (d && d.redo ? `: ${A.t("op_" + d.redo)}` : "") + " (Ctrl+Y)";
    $("#saveBtn").disabled = !d;
    $("#saveAsBtn").disabled = !d;
    $("#printBtn").disabled = !d || !A.can("print");
    $("#saveBtn").classList.toggle("pulse", !!(d && d.dirty));
    $("#searchBox").classList.toggle("disabled", !d);
    $("#search").disabled = !d;
  }
  function buildTopIcons() {
    $("#undoBtn").innerHTML = A.icon("undo", 18);
    $("#redoBtn").innerHTML = A.icon("redo", 18);
    $("#searchIco").innerHTML = A.icon("search", 15);
    $("#searchPrev").innerHTML = A.icon("chevU", 15);
    $("#searchNext").innerHTML = A.icon("chevD", 15);
    $("#printBtn").innerHTML = A.icon("print", 18);
    $("#menuBtn").innerHTML = A.icon("more", 18);
    $(".save-ico").innerHTML = A.icon("save", 16);
    $("#undoBtn").onclick = () => A.edit("undo", {});
    $("#redoBtn").onclick = () => A.edit("redo", {});
    $("#printBtn").onclick = () => A.print();
    $("#saveBtn").onclick = () => A.save();
    $("#saveAsBtn").onclick = () => A.saveAs();
    $("#menuBtn").onclick = (e) => {
      const r = e.currentTarget.getBoundingClientRect();
      A.menu(mainMenu(), r.right - 230, r.bottom + 6);
    };
  }
  function mainMenu() {
    const d = !!S.doc;
    return [
      { label: A.t("openPdf"), icon: "open", key: "Ctrl+O", run: A.openFile },
      { label: A.t("saveAs"), icon: "save", key: "Ctrl+Shift+S", run: A.saveAs, disabled: !d },
      { label: A.t("print"), icon: "print", key: "Ctrl+P", run: () => A.print(), disabled: !d },
      { label: A.t("closeDoc"), icon: "x", run: A.closeDoc, disabled: !d },
      "-",
      { label: A.t("nightMode"), icon: "moon", run: () => A.setNight(!S.night), active: S.night },
      { label: A.t("lightTheme"), icon: "sun", run: () => A.setTheme(A.isLight() ? "dark" : "light"), active: A.isLight() },
      { label: A.t("fullscreen"), icon: "fullscreen", key: "F11", run: () => A.call("toggle_fullscreen", {}, { quiet: true, noBusy: true }) },
      { label: A.t("readAloud"), icon: "speaker", run: A.readPage, disabled: !d },
      "-",
      { label: A.t("settingsTitle"), icon: "settings", run: () => A.openPanel("settings") },
      { label: A.t("shortcuts"), icon: "keyboard", key: "?", run: A.showShortcuts },
    ];
  }

  /* ---------- search ---------- */
  let searchTimer = null, searchSeq = 0;
  function renderSearchCount() {
    const s = S.search;
    $("#searchCount").textContent = s.q ? (s.hits.length ? `${s.cur + 1}/${s.hits.length}` : "0") : "";
    $("#searchBox").classList.toggle("has", !!s.q);
  }
  async function runSearch(q, keepPos) {
    const seq = ++searchSeq;
    if (!q || !S.doc) {
      S.search = { q: "", hits: [], cur: -1 };
      redrawOverlays();
      renderSearchCount();
      return;
    }
    const r = await A.call("search", { query: q }, { noBusy: true, quiet: true });
    if (seq !== searchSeq) return;
    const hits = (r && r.hits) || [];
    let cur = hits.length ? 0 : -1;
    if (keepPos && S.search.cur >= 0) cur = Math.min(S.search.cur, hits.length - 1);
    else if (hits.length) {
      const k = hits.findIndex((h) => h.p >= S.current);
      cur = k < 0 ? 0 : k;
    }
    S.search = { q, hits, cur };
    redrawOverlays();
    renderSearchCount();
    if (!keepPos && cur >= 0) A.gotoRect(hits[cur].p, hits[cur].r);
  }
  function stepHit(dir) {
    const s = S.search;
    if (!s.hits.length) return;
    s.cur = (s.cur + dir + s.hits.length) % s.hits.length;
    redrawOverlays();
    renderSearchCount();
    A.gotoRect(s.hits[s.cur].p, s.hits[s.cur].r);
  }
  function redrawOverlays() { A.V.pages.forEach((pg) => { if (pg.mat && pg.info) A.renderOverlay(pg); }); }
  function wireSearch() {
    const inp = $("#search");
    inp.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch(inp.value.trim()), 320); });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); if (S.search.q !== inp.value.trim()) runSearch(inp.value.trim()); else stepHit(e.shiftKey ? -1 : 1); }
      if (e.key === "Escape") { inp.value = ""; runSearch(""); inp.blur(); }
    });
    $("#searchNext").onclick = () => stepHit(1);
    $("#searchPrev").onclick = () => stepHit(-1);
  }

  /* ---------- view bar ---------- */
  function renderVbar() {
    const bar = $("#vbar");
    if (!S.doc) { bar.classList.add("hidden"); return; }
    bar.classList.remove("hidden");
    const n = S.doc.pages.length;
    bar.innerHTML = `
      <button class="icon-btn sm" data-v="prev" data-tip="prevPage">${A.icon("chevU", 16)}</button>
      <span class="pnum"><input id="pageIn" value="${S.current + 1}" aria-label="page"><span>/ ${n}</span></span>
      <button class="icon-btn sm" data-v="next" data-tip="nextPage">${A.icon("chevD", 16)}</button>
      <span class="vsep"></span>
      <button class="icon-btn sm" data-v="out" data-tip="zoomOut">${A.icon("minus", 16)}</button>
      <button class="zv" id="zoomVal" data-v="zoom">${Math.round(S.zoom * 100)}%</button>
      <button class="icon-btn sm" data-v="in" data-tip="zoomIn">${A.icon("plus", 16)}</button>
      <button class="icon-btn sm${S.fit === "width" ? " active" : ""}" data-v="fitw" data-fit="width" data-tip="fitWidth">${A.icon("fitW", 16)}</button>
      <button class="icon-btn sm${S.fit === "page" ? " active" : ""}" data-v="fitp" data-fit="page" data-tip="fitPage">${A.icon("fitP", 16)}</button>
      <span class="vsep"></span>
      <button class="icon-btn sm${S.night ? " active" : ""}" data-v="night" data-tip="nightMode">${A.icon("moon", 16)}</button>
      <button class="icon-btn sm${S.speaking ? " active" : ""}" data-v="speak" data-tip="${S.speaking ? "stopReading" : "readAloud"}">${A.icon(S.speaking ? "stop" : "speaker", 16)}</button>
      <button class="icon-btn sm" data-v="full" data-tip="fullscreen">${A.icon("fullscreen", 16)}</button>`;
    A.applyI18n(bar);
    const inp = $("#pageIn", bar);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { const p = parseInt(inp.value, 10); if (p >= 1 && p <= n) A.gotoPage(p - 1, false); inp.blur(); }
      if (e.key === "Escape") inp.blur();
    });
    inp.addEventListener("focus", () => inp.select());
  }
  function wireVbar() {
    $("#vbar").addEventListener("click", (e) => {
      const b = e.target.closest("[data-v]");
      if (!b) return;
      const v = b.dataset.v;
      if (v === "prev") A.gotoPage(S.current - 1);
      if (v === "next") A.gotoPage(S.current + 1);
      if (v === "in") A.zoomBy(1.2);
      if (v === "out") A.zoomBy(1 / 1.2);
      if (v === "fitw") A.fit("width");
      if (v === "fitp") A.fit("page");
      if (v === "night") A.setNight(!S.night);
      if (v === "speak") A.readPage();
      if (v === "full") A.call("toggle_fullscreen", {}, { quiet: true, noBusy: true });
      if (v === "zoom") {
        const r = b.getBoundingClientRect();
        A.menu([50, 75, 100, 125, 150, 200, 300, 400].map((z) => ({ label: `${z}%`, icon: "blank", run: () => A.setZoom(z / 100), active: Math.round(S.zoom * 100) === z })), r.left, r.top - 330);
      }
    });
  }
  A.on("current", (i) => { const inp = $("#pageIn"); if (inp && document.activeElement !== inp) inp.value = i + 1; });
  A.on("zoom", () => { const z = $("#zoomVal"); if (z) z.textContent = `${Math.round(S.zoom * 100)}%`; A.$$("#vbar [data-fit]").forEach((b) => b.classList.toggle("active", S.fit === b.dataset.fit)); });

  /* ---------- start screen ---------- */
  function renderStart() {
    const st = $("#start");
    const recent = S.recent || [];
    st.innerHTML = `<div class="halo"></div><div class="start-inner">
      <img class="bigmark" src="assets/logo.svg" alt="">
      <h1>${A.esc(A.t("startTitle"))}</h1><p class="lead">${A.esc(A.t("startText"))}</p>
      <div class="dropzone" id="dropzone"><button class="btn primary lg" id="openBtn">${A.icon("open", 18)}<span>${A.esc(A.t("openPdf"))}</span></button><div class="hint">${A.esc(A.t("dropHint"))}</div></div>
      <div class="quick">
        <button class="qa" data-q="combine">${A.icon("combine", 20)}<span>${A.esc(A.t("qMerge"))}</span></button>
        <button class="qa" data-q="images">${A.icon("image", 20)}<span>${A.esc(A.t("qImages"))}</span></button>
        <button class="qa" data-q="id">${A.icon("idcard", 20)}<span>${A.esc(A.t("qId"))}</span></button>
      </div>
      ${recent.length ? `<div class="recent"><h4>${A.esc(A.t("recentFiles"))}</h4><div class="rlist">${recent.map((r, k) => `
        <div class="ritem${r.exists ? "" : " missing"}" data-k="${k}" title="${A.esc(r.path)}">${A.icon("blank", 18)}<span class="rn">${A.esc(r.name)}</span>
        <span class="rd">${A.esc(A.dirname(r.path))}</span><span class="rt">${A.esc(A.fmtTime(r.t))}</span>
        <button class="icon-btn xs" data-rm aria-label="remove">${A.icon("x", 13)}</button></div>`).join("")}</div></div>` : ""}
    </div>`;
    $("#openBtn", st).onclick = A.openFile;
    $(".quick", st).onclick = (e) => {
      const q = e.target.closest("[data-q]");
      if (!q) return;
      if (q.dataset.q === "combine") A.openPanel("combine");
      if (q.dataset.q === "images") A.call("images_to_pdf_dialog", {}).then((r) => r && A.toast(A.t("imagesTo", { name: A.basename(r.path), n: r.pages }), "good", { label: A.t("openIt"), run: () => A.openPath(r.path) }));
      if (q.dataset.q === "id") A.createDigitalId();
    };
    const rl = $(".rlist", st);
    if (rl) rl.onclick = async (e) => {
      const it = e.target.closest(".ritem");
      if (!it) return;
      const r = recent[+it.dataset.k];
      if (e.target.closest("[data-rm]")) {
        const res = await A.call("recent_forget", { path: r.path }, { noBusy: true });
        if (res) { S.recent = res.recent; renderStart(); }
        return;
      }
      if (!r.exists) return A.toast(A.t("e_FILE_NOT_FOUND"), "bad");
      A.openPath(r.path);
    };
  }
  async function refreshRecent() {
    const r = await A.rpc("state").catch(() => null);
    if (r && r.ok) { S.recent = r.recent; renderStart(); }
  }

  /* ---------- drag and drop ---------- */
  function wireDrop() {
    let depth = 0;
    const veil = $("#dropveil");
    window.addEventListener("dragenter", (e) => { if ([...(e.dataTransfer.types || [])].includes("Files")) { depth++; veil.classList.remove("hidden"); } });
    window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) veil.classList.add("hidden"); });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => { e.preventDefault(); depth = 0; veil.classList.add("hidden"); });
  }
  /** Called from Python with the real paths of files dropped on the window. */
  A.dropped = async (paths) => {
    const pdfs = paths.filter((p) => /\.(pdf|png|jpe?g|tiff?|bmp|gif|webp|xps|epub)$/i.test(p));
    if (!pdfs.length) return A.toast(A.t("e_NOT_SUPPORTED"), "bad");
    if (S.doc && A.can("assemble")) {
      const v = await A.modal({
        title: A.t("dropTitle"),
        body: `<p>${A.esc(pdfs.map(A.basename).join(", "))}</p>`,
        buttons: [{ label: A.t("cancel"), kind: "ghost", value: null }, { label: A.t("dropInsert"), kind: "", value: "insert" }, { label: A.t("dropOpen"), kind: "primary", value: "open" }],
      });
      if (v === "insert") {
        for (const p of pdfs) await A.call("insert_file_dialog", { at: S.doc.pages.length, paths: [p] });
        return;
      }
      if (v !== "open") return;
    }
    A.openPath(pdfs[0]);
  };

  /** Called from Python whenever the window is closed: commit what is typed, then ask only if there are changes. */
  A.confirmClose = () => {
    (async () => {
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); // a typed field commits on blur
      await A.commitPending();
      if (!S.doc || !S.doc.dirty) return A.rpc("quit");
      const v = await A.modal({
        title: A.t("unsavedTitle"),
        body: `<p>${A.esc(A.t("unsavedCloseMsg", { name: S.doc ? S.doc.name : "" }))}</p>`,
        buttons: [
          { label: A.t("cancel"), kind: "ghost", value: null },
          { label: A.t("dontSave"), kind: "", value: "discard" },
          { label: A.t("save"), kind: "primary", value: "save" },
        ],
      });
      if (v === "discard") A.rpc("quit");
      if (v === "save" && (await A.save())) A.rpc("quit");
    })();
    return true;
  };

  /* ---------- keyboard ---------- */
  A.showShortcuts = () => {
    const rows = [
      ["Ctrl+O", "openPdf"], ["Ctrl+S", "save"], ["Ctrl+Shift+S", "saveAs"], ["Ctrl+P", "print"], ["Ctrl+F", "searchPh"],
      ["Ctrl+Z / Ctrl+Y", "undoRedo"], ["Ctrl + / Ctrl −", "zoomInOut"], ["Ctrl+0", "fitWidth"], ["PgUp / PgDn", "prevNextPage"],
      ["V · H", "selectHand"], ["P · R · E · L · A", "drawTools"], ["I · U · K", "markTools"], ["T · N · X", "textNoteEraser"],
      ["Space", "holdToPan"], ["Del", "deleteSelected"], ["Esc", "cancelTool"], ["F11", "fullscreen"],
    ];
    A.modal({
      title: A.t("shortcuts"),
      body: `<div class="keys">${rows.map(([k, l]) => `<div class="krow"><kbd>${A.esc(k)}</kbd><span>${A.esc(A.t(l))}</span></div>`).join("")}</div>`,
      buttons: [{ label: A.t("close"), kind: "primary", value: true }],
    });
  };

  function onKey(e) {
    const ctrl = e.ctrlKey || e.metaKey;
    const typing = A.isTyping(e);
    if (e.key === "Escape") {
      if (A.closeTopModal()) return e.preventDefault();
      if (!$("#menu").classList.contains("hidden")) return A.hideMenu();
      if (S.mode !== "select") { A.setMode("select"); A.hideBanner(); return; }
      if (S.selAnnot) return A.selectAnnot(null);
      if (S.organize) return A.openPanel(null);
      return;
    }
    if (A.topModal()) return;
    if (ctrl) {
      const k = e.key.toLowerCase();
      const map = {
        o: () => A.openFile(), s: () => (e.shiftKey ? A.saveAs() : A.save()), p: () => A.print(),
        f: () => { $("#search").focus(); $("#search").select(); }, z: () => (e.shiftKey ? A.edit("redo", {}) : A.edit("undo", {})),
        y: () => A.edit("redo", {}), "=": () => A.zoomBy(1.2), "+": () => A.zoomBy(1.2), "-": () => A.zoomBy(1 / 1.2), 0: () => A.fit("width"),
        a: () => { if (S.organize && S.doc) { S.selected = new Set(S.doc.pages.map((_, i) => i)); A.emit("selection"); } else return false; },
      };
      if (typing && !["s", "o", "p"].includes(k)) return;
      if (map[k] && (k !== "z" && k !== "y" ? true : !!S.doc)) {
        if (map[k]() === false) return;
        e.preventDefault();
      }
      return;
    }
    if (e.key === "F11") { e.preventDefault(); A.call("toggle_fullscreen", {}, { quiet: true, noBusy: true }); return; }
    if (typing || !S.doc) return;
    if (e.key === "Delete" || e.key === "Backspace") {
      if (S.selAnnot) { e.preventDefault(); A.deleteAnnot(S.selAnnot.page, S.selAnnot.xref); return; }
      if (S.organize && S.selected.size) { e.preventDefault(); A.deletePages([...S.selected]); return; }
    }
    if (e.key === "PageDown" || (e.key === "ArrowRight" && !S.organize)) { e.preventDefault(); A.gotoPage(S.current + 1); return; }
    if (e.key === "PageUp" || (e.key === "ArrowLeft" && !S.organize)) { e.preventDefault(); A.gotoPage(S.current - 1); return; }
    if (e.key === "Home") { e.preventDefault(); A.gotoPage(0); return; }
    if (e.key === "End") { e.preventDefault(); A.gotoPage(S.doc.pages.length - 1); return; }
    if (e.key === "?") { A.showShortcuts(); return; }
    const mode = A.TOOL_KEYS[e.key.toLowerCase()];
    if (mode && !e.altKey) {
      if (!["select", "hand"].includes(mode) && !A.can("annotate")) return;
      if (S.panel !== "annotate") A.openPanel("annotate");
      A.setMode(mode);
    }
  }

  /* ---------- reacting to the document ---------- */
  A.on("doc", ({ prev, doc }) => {
    $("#app").classList.toggle("has-doc", !!doc);
    renderFileInfo();
    renderTopButtons();
    if (!prev || !doc || prev.id !== doc.id || prev.pages.length !== doc.pages.length) renderVbar();
    if (!doc) { renderStart(); S.search = { q: "", hits: [], cur: -1 }; renderSearchCount(); return; }
    if (S.search.q && prev && prev.id === doc.id && prev.pages.some((p, k) => !doc.pages[k] || p[2] !== doc.pages[k][2])) {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => runSearch(S.search.q, true), 400);
    }
  });

  /* ---------- boot ---------- */
  async function init() {
    A.initViewer();
    A.initTools();
    A.initPages();
    buildTopIcons();
    wireSearch();
    wireVbar();
    wireDrop();
    window.addEventListener("keydown", onKey);
    document.addEventListener("contextmenu", (e) => { if (!e.target.closest("input, textarea, .tl")) e.preventDefault(); });
    document.addEventListener("pointerover", tipOver);
    let st = null;
    try { st = await A.rpc("state"); } catch (e) { /* server not reachable: shown below */ }
    if (!st || !st.ok) {
      document.body.innerHTML = `<div class="fatal">${A.icon("warn", 28)}<p>Aurora PDF backend is not reachable.</p></div>`;
      return;
    }
    S.settings = st.settings || {};
    applyTheme();
    S.lang = A.I18N[S.settings.lang] ? S.settings.lang : "sr";
    document.documentElement.lang = S.lang;
    S.night = !!S.settings.night;
    S.fit = S.settings.fit || "width";
    S.recent = st.recent || [];
    S.ocrLangs = st.ocrLangs || {};
    $("#app").classList.toggle("night", S.night);
    rebuildChrome();
    A.setMode("select");
    if (st.doc && st.doc.open) { A.applyDoc(st.doc); afterOpen(); }
    else {
      A.applyDoc(null);
      const b = await A.rpc("boot").catch(() => null);
      if (b && b.ok) { A.applyDoc(b.doc); afterOpen(); }
      else if (b && (b.error === "NEED_PASSWORD")) askPassword(b.path, b.name, false);
    }
    document.body.classList.add("ready");
  }

  /* tooltips for icon buttons (one shared bubble) */
  let tipEl = null, tipTimer = null;
  function tipOver(e) {
    const t = e.target.closest("[data-tip-text]");
    clearTimeout(tipTimer);
    if (tipEl) { tipEl.remove(); tipEl = null; }
    if (!t || t.closest("#rail")) return;
    tipTimer = setTimeout(() => {
      if (!document.body.contains(t)) return;
      tipEl = A.h(`<div class="tooltip"></div>`);
      tipEl.textContent = t.dataset.tipText;
      document.body.appendChild(tipEl);
      const r = t.getBoundingClientRect();
      const below = r.top < 90;
      tipEl.style.left = `${A.clamp(r.left + r.width / 2 - tipEl.offsetWidth / 2, 6, window.innerWidth - tipEl.offsetWidth - 6)}px`;
      tipEl.style.top = `${below ? r.bottom + 8 : r.top - tipEl.offsetHeight - 8}px`;
      t.addEventListener("pointerleave", () => { if (tipEl) { tipEl.remove(); tipEl = null; } }, { once: true });
    }, 420);
  }

  document.addEventListener("DOMContentLoaded", init);
  window.Aurora = A;
})(window.Aurora);
