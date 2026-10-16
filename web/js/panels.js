/* Aurora PDF - tool rail and the right-hand panels. */
"use strict";
(function (A) {
  const S = A.S;
  const RAIL = [
    ["open", "open"], "-",
    ["annotate", "annotate"], ["fillsign", "fillsign"], ["protect", "protect"], ["redact", "redact"],
    ["watermark", "watermark"], ["organize", "organize"], ["ocr", "ocr"], ["export", "export"],
    "spacer", ["combine", "combine"], ["info", "info"],
  ];
  const NEEDS_DOC = new Set(["annotate", "fillsign", "protect", "redact", "watermark", "organize", "ocr", "export", "info"]);

  /* ---------- rail ---------- */
  A.buildRail = () => {
    const rail = A.$("#rail");
    rail.innerHTML = "";
    RAIL.forEach((it) => {
      if (it === "-") return rail.appendChild(A.h(`<div class="rail-sep"></div>`));
      if (it === "spacer") return rail.appendChild(A.h(`<div class="spacer"></div>`));
      const [id, ic] = it;
      const b = A.h(`<button class="tool" data-tool="${id}" aria-label="">${A.icon(ic, 21)}<span class="tip"></span></button>`);
      A.$(".tip", b).textContent = A.t("t_" + id);
      b.setAttribute("aria-label", A.t("t_" + id));
      b.onclick = () => (id === "open" ? A.openFile() : A.openPanel(S.panel === id ? null : id));
      rail.appendChild(b);
    });
    markRail();
  };
  function markRail() {
    A.$$("#rail .tool").forEach((b) => {
      b.classList.toggle("active", b.dataset.tool === S.panel);
      b.disabled = NEEDS_DOC.has(b.dataset.tool) && !S.doc;
    });
  }

  /* ---------- panel shell ---------- */
  A.openPanel = (id) => {
    const prev = S.panel;
    if (id && NEEDS_DOC.has(id) && !S.doc) id = null;
    S.panel = id; // set first: leaving organize calls back into openPanel
    if (prev === "organize" && id !== "organize") A.setOrganize(false);
    if (prev === "annotate" && id !== "annotate") { A.showAnnotToolbar(false); if (S.mode !== "select") A.setMode("select"); }
    A.$("#app").classList.toggle("panel-open", !!id);
    A.$("#app").dataset.panel = id || "";
    markRail();
    if (!id) { A.$("#panelInner").innerHTML = ""; return; }
    if (id === "annotate") { A.showAnnotToolbar(true); A.buildAnnotToolbar(); }
    if (id === "organize") A.setOrganize(true);
    render();
  };
  function render() {
    const id = S.panel;
    if (!id || !PANELS[id]) return;
    const inner = A.$("#panelInner");
    const keepScroll = A.$("#panel").scrollTop;
    inner.innerHTML = "";
    PANELS[id](inner);
    A.applyI18n(inner);
    A.$("#panel").scrollTop = keepScroll;
  }
  A.refreshPanel = render;

  const head = (icon, title, sub) =>
    `<div class="panel-head"><div class="pc-ico">${A.icon(icon, 18)}</div><h2>${A.esc(A.t(title))}</h2>
     <button class="icon-btn sm p-x" data-close aria-label="close">${A.icon("x", 17)}</button></div>${sub ? `<p class="sub">${A.esc(A.t(sub))}</p>` : ""}`;
  const field = (label, inner, hint) => `<div class="field"><label>${A.esc(A.t(label))}</label>${inner}${hint ? `<p class="hint">${A.esc(A.t(hint))}</p>` : ""}</div>`;
  const check = (id, label, sub, on) =>
    `<label class="check"><input type="checkbox" id="${id}"${on ? " checked" : ""}><span class="box">${A.icon("check", 13)}</span><span class="lbl">${A.esc(A.t(label))}${sub ? `<small>${A.esc(A.t(sub))}</small>` : ""}</span></label>`;
  const seg = (id, opts, cur) => `<div class="seg" id="${id}">${opts.map(([v, l]) => `<button data-v="${v}"${v === cur ? ' class="active"' : ""}>${A.esc(A.t(l))}</button>`).join("")}</div>`;
  const range = (id, label, min, max, val, unit, step = 1) =>
    `<div class="field"><label>${A.esc(A.t(label))}<span class="rangeval" id="${id}v">${val}${unit || ""}</span></label><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}"></div>`;
  const section = (title, inner, cls = "") => `<section class="psec ${cls}"><h4>${A.esc(A.t(title))}</h4>${inner}</section>`;
  const btn = (id, label, icon, kind = "") => `<button class="btn ${kind}" id="${id}">${icon ? A.icon(icon, 16) : ""}<span>${A.esc(A.t(label))}</span></button>`;
  const pagesField = (id, val = "") => field("pagesRange", `<input class="input" id="${id}" value="${A.esc(val)}" placeholder="${A.esc(A.t("pagesPh"))}">`);

  function wire(root) {
    A.$$("[data-close]", root).forEach((b) => (b.onclick = () => A.openPanel(null)));
    A.$$(".seg", root).forEach((sg) => sg.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      A.$$("button", sg).forEach((x) => x.classList.toggle("active", x === b));
      sg.dispatchEvent(new CustomEvent("change", { detail: b.dataset.v }));
    }));
    A.$$("input[type=range]", root).forEach((r) => {
      const out = A.$(`#${r.id}v`, root);
      const unit = out ? out.textContent.replace(/^[\d.]+/, "") : "";
      r.addEventListener("input", () => { if (out) out.textContent = r.value + unit; });
    });
  }
  // a protected, unprotected or compressed copy is a full rewrite, which breaks every signature in it
  const signedOk = async () => !S.doc.signed || A.confirm(A.t("signedSaveTitle"), A.t("signedCopyMsg"), A.t("saveAnyway"));
  const segVal = (root, id) => { const b = A.$(`#${id} .active`, root); return b ? b.dataset.v : null; };
  const val = (root, id) => A.$(`#${id}`, root).value;
  const on = (root, id) => A.$(`#${id}`, root).checked;
  const noDoc = (root) => { root.innerHTML = `<div class="pempty">${A.icon("open", 26)}<p>${A.esc(A.t("noDoc"))}</p>${btn("pOpen", "openPdf", "open", "primary")}</div>`; A.$("#pOpen", root).onclick = A.openFile; };

  const PANELS = {};

  /* ---------- comments ---------- */
  const TYPE_ICON = { Ink: "pen", Square: "rect", Circle: "ellipse", Line: "line", FreeText: "text", Text: "note", Highlight: "highlight", Underline: "underline", StrikeOut: "strikeout", Squiggly: "underline", Stamp: "fillsign", Redact: "redact", Polygon: "rect", PolyLine: "line" };
  PANELS.annotate = (root) => {
    root.innerHTML = head("annotate", "annotTitle", "annotSub") + `<div class="clist" id="clist"><div class="pload">${A.esc(A.t("loading"))}</div></div>`;
    wire(root);
    loadComments(root);
  };
  async function loadComments(root) {
    const r = await A.call("annots_list", {}, { noBusy: true, quiet: true });
    const host = A.$("#clist", root);
    if (!host) return;
    const items = (r && r.items) || [];
    if (!items.length) {
      host.innerHTML = `<div class="pempty small">${A.icon("annotate", 24)}<p>${A.esc(A.t("noComments"))}</p></div>`;
      return;
    }
    let lastPage = -1;
    host.innerHTML = items.map((a) => {
      const pageHead = a.p !== lastPage ? `<div class="cpage">${A.esc(A.t("page"))} ${a.p + 1}</div>` : "";
      lastPage = a.p;
      const label = A.t("an_" + a.t) === "an_" + a.t ? a.t : A.t("an_" + a.t);
      return `${pageHead}<div class="citem" data-p="${a.p}" data-x="${a.x}"><span class="cdot" style="--c:${a.c || "#94a3b8"}">${A.icon(TYPE_ICON[a.t] || "note", 15)}</span>
        <div class="cbody"><div class="ctype">${A.esc(label)}</div>${a.txt ? `<div class="ctext">${A.esc(a.txt.slice(0, 220))}</div>` : ""}
        <div class="cmeta">${A.esc([a.by, A.fmtPdfDate(a.dt)].filter(Boolean).join(" · "))}</div></div>
        <button class="icon-btn xs danger" data-del aria-label="delete">${A.icon("trash", 14)}</button></div>`;
    }).join("");
    host.onclick = (e) => {
      const it = e.target.closest(".citem");
      if (!it) return;
      const p = +it.dataset.p, x = +it.dataset.x;
      if (e.target.closest("[data-del]")) return A.deleteAnnot(p, x);
      const a = items.find((q) => q.x === x && q.p === p);
      A.setMode("select");
      if (a) A.gotoRect(p, a.r);
      setTimeout(() => A.selectAnnot({ page: p, xref: x }), 350);
    };
  }

  /* ---------- fill & sign ---------- */
  PANELS.fillsign = (root) => {
    const doc = S.doc;
    root.innerHTML = head("fillsign", "fsTitle", "fsSub") +
      (doc.forms ? section("formFields", `<p class="hint">${A.esc(A.t("formHint"))}</p><div class="btnrow">${btn("fReset", "resetFields", "undo", "sm")}${btn("fFlat", "flattenFields", "layers", "sm")}</div>`) : "") +
      section("handSig", `<div class="siggrid" id="sigGrid"></div><div class="btnrow">${btn("sigNew", "sigCreate", "plus", "primary sm")}${btn("addText", "addText", "text", "sm")}</div>`) +
      section("digitalSig", `<p class="hint">${A.esc(A.t("digitalHint"))}</p>${btn("dsGo", "signWithCert", "shield", "block")}
        <div class="idmini" id="idMini"></div>`) +
      (doc.signed ? section("sigsInDoc", `<div id="sigVal"><div class="pload">${A.esc(A.t("checking"))}</div></div>`) : "");
    wire(root);
    if (doc.forms) {
      A.$("#fReset", root).onclick = async () => { if (await A.confirm(A.t("resetFields"), A.t("resetFieldsMsg"), A.t("reset"))) A.edit("fields_reset", {}); };
      A.$("#fFlat", root).onclick = async () => { if (await A.confirm(A.t("flattenFields"), A.t("flattenMsg"), A.t("flatten"))) A.edit("flatten", {}); };
    }
    A.$("#sigNew", root).onclick = async () => { const id = await A.createSignature(); if (id) { loadSigs(root); A.placeSignature(id); } };
    A.$("#addText", root).onclick = () => { A.openPanel("annotate"); A.setMode("text"); };
    A.$("#dsGo", root).onclick = () => A.startDigitalSign();
    loadSigs(root);
    loadIds(root);
    if (doc.signed) validate(root);
  };
  async function loadSigs(root) {
    const r = await A.call("sig_list", {}, { noBusy: true, quiet: true });
    const host = A.$("#sigGrid", root);
    if (!host) return;
    const items = (r && r.items) || [];
    host.innerHTML = items.length
      ? items.map((s) => `<div class="sigtile" data-id="${s.id}"><img src="${A.url(`sig/${s.id}.png`)}" alt=""><button class="icon-btn xs" data-rm aria-label="remove">${A.icon("x", 12)}</button></div>`).join("")
      : `<p class="hint">${A.esc(A.t("noSignatures"))}</p>`;
    host.onclick = async (e) => {
      const t = e.target.closest(".sigtile");
      if (!t) return;
      if (e.target.closest("[data-rm]")) {
        if (await A.confirm(A.t("removeSignature"), A.t("removeSignatureMsg"), A.t("remove"))) {
          await A.call("sig_remove", { id: t.dataset.id });
          loadSigs(root);
        }
        return;
      }
      A.placeSignature(t.dataset.id);
    };
  }
  async function loadIds(root) {
    const r = await A.call("ids_list", {}, { noBusy: true, quiet: true });
    const host = A.$("#idMini", root);
    if (!host) return;
    const items = (r && r.items) || [];
    host.innerHTML = `<div class="idhead">${A.esc(A.t("myIds"))}</div>` + (items.length
      ? items.map((d) => `<div class="idrow" data-id="${d.id}">${A.icon("idcard", 16)}<span><b>${A.esc(d.name)}</b><small>${A.esc(A.t("validUntil", { date: A.fmtIsoDate(d.not_after) }))}</small></span><button class="icon-btn xs" data-rm aria-label="remove">${A.icon("x", 12)}</button></div>`).join("")
      : `<p class="hint">${A.esc(A.t("noIds"))}</p>`) +
      `<div class="linkrow"><button class="lnk" id="idNew">${A.icon("plus", 14)}${A.esc(A.t("idCreate"))}</button><button class="lnk" id="idImp">${A.icon("download", 14)}${A.esc(A.t("idImport"))}</button></div>`;
    A.$("#idNew", host).onclick = async () => { if (await A.createDigitalId()) loadIds(root); };
    A.$("#idImp", host).onclick = async () => { if (await A.importDigitalId()) loadIds(root); };
    host.onclick = async (e) => {
      const rm = e.target.closest("[data-rm]");
      if (!rm) return;
      const id = rm.closest(".idrow").dataset.id;
      if (await A.confirm(A.t("removeId"), A.t("removeIdMsg"), A.t("remove"), true)) { await A.call("id_remove", { id }); loadIds(root); }
    };
  }
  A.on("ids", () => { if (S.panel === "fillsign") loadIds(A.$("#panelInner")); });
  A.on("signatures", () => { if (S.panel === "fillsign") loadSigs(A.$("#panelInner")); });
  async function validate(root) {
    const r = await A.call("signatures_validate", {}, { noBusy: true, quiet: true });
    const host = A.$("#sigVal", root);
    if (!host) return;
    const items = (r && r.items) || [];
    if (!items.length) { host.innerHTML = `<p class="hint">${A.esc(A.t("noSigsFound"))}</p>`; return; }
    host.innerHTML = items.map((s) => `<div class="sigstat ${s.status}">${A.icon(A.STATUS_ICON[s.status] || "warn", 18)}
      <div><b>${A.esc(A.t(s.kind === "timestamp" ? "ts_" + s.status : "sig_" + s.status))}</b><div>${A.esc(s.signer || "?")}${s.email ? ` &lt;${A.esc(s.email)}&gt;` : ""}</div>
      <small>${A.esc([A.fmtIsoDate(s.time), s.reason, s.location].filter(Boolean).join(" · "))}</small>
      <small>${A.esc(A.t("issuedBy", { who: s.issuer || "?" }))}</small></div></div>`).join("") +
      (r.dirty ? `<p class="hint warn">${A.esc(A.t("sigCheckSaved"))}</p>` : "");
  }

  /* ---------- protect ---------- */
  PANELS.protect = (root) => {
    const doc = S.doc;
    const status = doc.protected
      ? `<div class="status ok">${A.icon("protect", 18)}<div><b>${A.esc(A.t("isProtected"))}</b><small>${A.esc((doc.algorithm || "").toUpperCase().replace("AES", "AES-"))}${doc.owner ? "" : " · " + A.esc(A.t("limitedRights"))}</small></div></div>`
      : "";
    const ownerBox = doc.protected && !doc.owner
      ? section("ownerUnlock", `<p class="hint">${A.esc(A.t("ownerUnlockHint"))}</p><div class="pwrow"><input class="input" id="ownPw" type="password" placeholder="${A.esc(A.t("ownerPw"))}">${btn("ownGo", "unlock", "key", "sm")}</div>`)
      : "";
    const unprot = doc.protected && doc.owner ? `<div class="btnrow">${btn("unprot", "saveUnprotected", "export", "ghost sm")}</div>` : "";
    root.innerHTML = head("protect", "lockTitle", "lockSub") + status + ownerBox + unprot +
      section("newProtection",
        field("openPw", `<div class="pwrow"><input class="input" id="userPw" type="password" autocomplete="new-password"><button class="icon-btn sm" data-eye="userPw" aria-label="show">${A.icon("eye", 16)}</button></div><div class="meter" id="pwMeter"><i></i></div>`, "openPwHint") +
        field("ownerPw", `<input class="input" id="ownerPw" type="password" autocomplete="new-password">`, "ownerPwHint") +
        field("algorithm", seg("algo", [["aes256", "aes256"], ["aes128", "aes128"]], "aes256"), "algoHint") +
        `<div class="field"><label>${A.esc(A.t("perms"))}</label>${check("p_print", "p_print", "", true)}${check("p_copy", "p_copy", "", true)}${check("p_modify", "p_modify", "", false)}${check("p_annotate", "p_annotate", "", true)}${check("p_assemble", "p_assemble", "", false)}</div>` +
        `<div class="actions">${btn("doLock", "saveLocked", "protect", "primary block")}</div>`);
    wire(root);
    A.$$("[data-eye]", root).forEach((b) => (b.onclick = () => { const i = A.$("#" + b.dataset.eye, root); i.type = i.type === "password" ? "text" : "password"; }));
    const meter = A.$("#pwMeter i", root);
    A.$("#userPw", root).addEventListener("input", (e) => {
      const v = e.target.value;
      const score = Math.min(4, (v.length >= 8) + (v.length >= 12) + (/[A-Z]/.test(v) && /[a-z]/.test(v)) + /\d/.test(v) + /[^\w]/.test(v));
      meter.style.width = `${(score / 4) * 100}%`;
      meter.dataset.s = score;
    });
    if (A.$("#ownGo", root)) A.$("#ownGo", root).onclick = async () => {
      const r = await A.call("unlock_owner", { password: val(root, "ownPw") }, { handle: (e) => e.error === "WRONG_PASSWORD" && (A.toast(A.t("e_WRONG_PASSWORD"), "bad"), true) });
      if (r) A.toast(A.t("ownerUnlocked"));
    };
    if (A.$("#unprot", root)) A.$("#unprot", root).onclick = async () => {
      await A.commitPending();
      if (!(await signedOk())) return;
      const r = await A.call("save_unprotected", {});
      if (r) A.savedToast(r.path);
    };
    A.$("#doLock", root).onclick = async () => {
      const user = val(root, "userPw"), owner = val(root, "ownerPw");
      if (!user && !owner) return A.toast(A.t("needAPassword"), "bad");
      await A.commitPending();
      if (!(await signedOk())) return;
      const allowed = [];
      if (on(root, "p_print")) allowed.push("print", "print_hq");
      if (on(root, "p_copy")) allowed.push("copy", "accessibility");
      if (on(root, "p_modify")) allowed.push("modify");
      if (on(root, "p_annotate")) allowed.push("annotate", "form");
      if (on(root, "p_assemble")) allowed.push("assemble");
      const r = await A.call("save_protected", { user_pw: user, owner_pw: owner || user, allowed, algorithm: segVal(root, "algo") });
      if (r) A.savedToast(r.path, "protectedTo");
    };
  };

  /* ---------- redact ---------- */
  PANELS.redact = (root) => {
    root.innerHTML = head("redact", "redactTitle", "redactSub") +
      section("redactMark", `<div class="btnrow">${btn("rArea", "redactArea", "rect", "sm")}</div>
        <div class="pwrow mt">${`<input class="input" id="rQ" placeholder="${A.esc(A.t("redactFindPh"))}">`}${btn("rFind", "markAll", "search", "sm")}</div>`) +
      section("redactApplyT", `<p class="hint" id="rCount"></p>${btn("rApply", "redactApply", "check", "danger solid block")}`) +
      section("scrubT", `<p class="hint">${A.esc(A.t("scrubHint"))}</p>${btn("rScrub", "scrub", "eraser", "sm")}`);
    wire(root);
    const count = redactCount;
    count();
    A.$("#rArea", root).onclick = () => { A.setMode("redact"); A.showBanner(A.t("redactDrawHint"), () => A.setMode("select")); };
    A.$("#rFind", root).onclick = async () => {
      const q = val(root, "rQ").trim();
      if (!q) return;
      const r = await A.edit("redact_search", { query: q }, { handle: (e) => e.error === "NOTHING_FOUND" && (A.toast(A.t("nothingFound"), "info"), true) });
      if (r) { A.toast(A.t("redactFound", { n: r.count })); count(); }
    };
    A.$("#rApply", root).onclick = async () => {
      if (!(await A.confirm(A.t("redactApply"), A.t("redactApplyMsg"), A.t("redactApplyOk"), true))) return;
      const r = await A.edit("redact_apply", {});
      if (r) { A.toast(A.t("redactDone", { n: r.count })); count(); }
    };
    A.$("#rScrub", root).onclick = async () => {
      if (!(await A.confirm(A.t("scrub"), A.t("scrubMsg"), A.t("scrub")))) return;
      if (await A.edit("scrub", {})) A.toast(A.t("scrubDone"));
    };
  };
  async function redactCount() {
    const r = await A.call("redact_count", {}, { noBusy: true, quiet: true });
    const n = (r && r.count) || 0;
    const el = A.$("#rCount");
    if (el) el.textContent = A.t("redactCount", { n });
    const b = A.$("#rApply");
    if (b) b.disabled = !n;
  }
  A.on("doc", () => { if (S.panel === "redact") redactCount(); });

  /* ---------- watermark & header/footer ---------- */
  let wmTab = "wm";
  PANELS.watermark = (root) => {
    root.innerHTML = head("watermark", "wmTitle", "wmSub") + seg("wmTabs", [["wm", "wmTab"], ["hf", "hfTab"]], wmTab) + `<div id="wmBody"></div>`;
    wire(root);
    A.$("#wmTabs", root).addEventListener("change", (e) => { wmTab = e.detail; drawWm(root); });
    drawWm(root);
  };
  function drawWm(root) {
    const host = A.$("#wmBody", root);
    if (wmTab === "wm") {
      host.innerHTML = field("type", seg("wmType", [["text", "text"], ["image", "image"]], "text")) +
        `<div id="wmT">${field("wmText", `<input class="input" id="wmt" value="${A.esc(A.t("wmDefault"))}">`)}
        <div class="row">${field("color", `<input class="input color" id="wmc" type="color" value="#8892a6">`)}${field("angle", `<input class="input" id="wma" type="number" value="45" min="-180" max="180">`)}</div>
        ${range("wmo", "opacity", 5, 100, 18, "%")}${range("wms", "size", 12, 160, 54, " pt")}${check("wmtile", "tile", "tileHint", false)}</div>
        <div id="wmI" class="hidden">${range("wmio", "opacity", 5, 100, 25, "%")}${range("wmsc", "scale", 10, 100, 45, "%")}</div>` +
        check("wmBehind", "behind", "behindHint", false) + pagesField("wmpg") +
        `<div class="actions">${btn("wmApply", "apply", "check", "primary block")}</div>`;
    } else {
      host.innerHTML = field("hfText", `<input class="input" id="hft" value="${A.esc(A.t("hfDefault"))}">`, "hfHint") +
        `<div class="chips" id="hfChips">${["{n}", "{total}", "{date}", "{file}"].map((c) => `<button class="chip" data-c="${c}">${c}</button>`).join("")}</div>` +
        field("position", `<div class="posgrid" id="hfPos">${["tl", "tc", "tr", "bl", "bc", "br"].map((p) => `<button data-p="${p}"${p === "bc" ? ' class="active"' : ""} aria-label="${p}"><i></i></button>`).join("")}</div>`) +
        `<div class="row">${field("size", `<input class="input" id="hfs" type="number" value="10" min="5" max="48">`)}${field("startAt", `<input class="input" id="hfn" type="number" value="1" min="0">`)}</div>` +
        `<div class="row">${field("color", `<input class="input color" id="hfc" type="color" value="#444444">`)}${field("margin", `<input class="input" id="hfm" type="number" value="28" min="4" max="200">`)}</div>` +
        pagesField("hfpg") + `<div class="actions">${btn("hfApply", "apply", "check", "primary block")}</div>`;
    }
    wire(host);
    if (wmTab === "wm") {
      let type = "text";
      A.$("#wmType", host).addEventListener("change", (e) => {
        type = e.detail;
        A.$("#wmT", host).classList.toggle("hidden", type !== "text");
        A.$("#wmI", host).classList.toggle("hidden", type === "text");
        A.$("#wmApply span", host).textContent = A.t(type === "text" ? "apply" : "chooseImageApply");
      });
      A.$("#wmApply", host).onclick = async () => {
        const common = { pages: val(host, "wmpg"), behind: on(host, "wmBehind") };
        const r = type === "text"
          ? await A.edit("watermark_text", Object.assign(common, { text: val(host, "wmt"), color: val(host, "wmc"), angle: +val(host, "wma"), opacity: +val(host, "wmo") / 100, size: +val(host, "wms"), tile: on(host, "wmtile") }))
          : await A.call("watermark_image_dialog", Object.assign(common, { opacity: +val(host, "wmio") / 100, scale: +val(host, "wmsc") / 100 }));
        if (r) A.toast(A.t("applied"));
      };
    } else {
      const input = A.$("#hft", host);
      A.$("#hfChips", host).onclick = (e) => {
        const c = e.target.closest("[data-c]");
        if (!c) return;
        const s = input.selectionStart ?? input.value.length;
        input.value = input.value.slice(0, s) + c.dataset.c + input.value.slice(input.selectionEnd ?? s);
        input.focus();
      };
      let pos = "bc";
      A.$("#hfPos", host).onclick = (e) => {
        const b = e.target.closest("[data-p]");
        if (!b) return;
        pos = b.dataset.p;
        A.$$("#hfPos button", host).forEach((x) => x.classList.toggle("active", x === b));
      };
      A.$("#hfApply", host).onclick = async () => {
        const r = await A.edit("header_footer", { text: input.value, position: pos, size: +val(host, "hfs"), start: +val(host, "hfn"), color: val(host, "hfc"), margin: +val(host, "hfm"), pages: val(host, "hfpg"), date: A.fmtShort() });
        if (r) A.toast(A.t("applied"));
      };
    }
  }

  /* ---------- organize ---------- */
  PANELS.organize = (root) => {
    const sel = [...S.selected].sort((a, b) => a - b);
    const n = sel.length;
    root.innerHTML = head("organize", "orgTitle", "orgSub") +
      `<div class="selinfo">${n ? A.esc(A.t("selectedPages", { n, list: A.rangeText(sel) })) : A.esc(A.t("noneSelected"))}</div>` +
      section("orgSel", `<div class="grid2">${btn("oL", "rotateL", "rotL", "sm")}${btn("oR", "rotateR", "rotR", "sm")}${btn("oDup", "duplicate", "copy", "sm")}${btn("oExt", "extract", "export", "sm")}</div>
        ${btn("oDel", "deleteSel", "trash", "danger sm block")}`) +
      section("orgInsert", `<div class="grid2">${btn("oBlank", "insertBlank", "blank", "sm")}${btn("oFile", "insertFile", "open", "sm")}${btn("oImg", "insertImages", "image", "sm")}${btn("oSplit", "splitDoc", "split", "sm")}</div>`) +
      section("orgCrop", `<p class="hint">${A.esc(A.t("cropHint"))}</p><div class="btnrow">${btn("oCrop", "cropStart", "crop", "sm")}${btn("oUncrop", "cropReset", "undo", "ghost sm")}</div>`) +
      `<div class="actions">${btn("oDone", "backToReading", "chevL", "ghost block")}</div>`;
    wire(root);
    const pick = () => (S.selected.size ? [...S.selected] : [S.current]);
    const after = () => (S.selected.size ? Math.max(...S.selected) + 1 : S.doc.pages.length);
    A.$("#oL", root).onclick = () => A.edit("rotate", { pages: pick(), degrees: -90 });
    A.$("#oR", root).onclick = () => A.edit("rotate", { pages: pick(), degrees: 90 });
    A.$("#oDup", root).onclick = () => A.edit("duplicate", { pages: pick() });
    A.$("#oExt", root).onclick = () => A.extractPages(pick());
    A.$("#oDel", root).onclick = () => A.deletePages(pick());
    A.$("#oBlank", root).onclick = () => A.edit("insert_blank", { at: after(), like: Math.max(0, after() - 1) });
    A.$("#oFile", root).onclick = () => A.insertFile(after());
    A.$("#oImg", root).onclick = () => A.call("insert_images_dialog", { at: after() });
    A.$("#oSplit", root).onclick = async () => {
      const n = await A.prompt(A.t("splitDoc"), A.t("splitEvery"), "1");
      if (n == null) return;
      const split = async (extra) => {
        await A.commitPending();
        const r = await A.call("split_dialog", { every: Math.max(1, parseInt(n, 10) || 1), ...extra }, { handle: A.askPasswords(split) });
        if (r) A.savedToast(r.path, "splitDone");
      };
      split();
    };
    A.$("#oCrop", root).onclick = () => {
      const target = S.selected.size ? Math.min(...S.selected) : S.current;
      A.setOrganize(false, target);
      A.setMode("crop");
      A.showBanner(A.t("cropDraw"), () => A.setMode("select"));
    };
    A.$("#oUncrop", root).onclick = () => A.edit("uncrop", { pages: pick() });
    A.$("#oDone", root).onclick = () => A.openPanel(null);
  };
  A.on("selectionChanged", () => { if (S.panel === "organize") A.refreshPanel(); });
  A.cropDrawn = async (i, r, pg) => {
    A.setMode("select");
    A.hideBanner();
    const frac = [r[0] / pg.w, r[1] / pg.h, r[2] / pg.w, r[3] / pg.h];
    const choice = await A.modal({
      title: A.t("cropTitle"),
      body: `<p>${A.esc(A.t("cropAsk"))}</p>`,
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        { label: A.t("cropAll"), kind: "", value: "all" },
        { label: A.t("cropThis"), kind: "primary", value: "this" },
      ],
    });
    if (!choice) return;
    A.edit("crop", { pages: choice === "all" ? "all" : [i], frac });
  };

  /* ---------- OCR ---------- */
  PANELS.ocr = (root) => {
    const doc = S.doc;
    const langs = S.ocrLangs || {};
    const pref = (S.settings.ocr_langs || { sr: "srp_latn+eng", no: "nor+eng", en: "eng" }[S.lang] || "eng").split("+");
    root.innerHTML = head("ocr", "ocrTitle", "ocrSub") +
      `<div class="status ${doc.scanned ? "warn" : "ok"}">${A.icon(doc.scanned ? "ocr" : "check", 18)}<div><b>${A.esc(A.t(doc.scanned ? "looksScanned" : "hasText"))}</b><small>${A.esc(A.t(doc.scanned ? "looksScannedHint" : "hasTextHint"))}</small></div></div>` +
      `<div class="field"><label>${A.esc(A.t("ocrLangs"))}</label>${Object.entries(langs).map(([code, name]) => check("ol_" + code, name, "", pref.includes(code))).join("")}</div>` +
      field("quality", seg("ocrQ", [["200", "fast"], ["300", "accurate"]], "300")) +
      pagesField("ocrPg") + check("ocrForce", "ocrForce", "ocrForceHint", false) +
      `<div class="actions">${btn("ocrGo", "ocrRun", "ocr", "primary block")}</div><p class="hint">${A.esc(A.t("ocrNote"))}</p>`;
    wire(root);
    A.$("#ocrGo", root).onclick = async () => {
      const chosen = Object.keys(langs).filter((c) => on(root, "ol_" + c));
      if (!chosen.length) return A.toast(A.t("ocrPickLang"), "bad");
      const r = await A.call("ocr_start", { langs: chosen.join("+"), pages: val(root, "ocrPg"), force: on(root, "ocrForce"), dpi: +segVal(root, "ocrQ") },
        { handle: (e) => e.error === "NOTHING_TO_OCR" && (A.toast(A.t("e_NOTHING_TO_OCR"), "info"), true) });
      if (!r) return;
      S.settings.ocr_langs = chosen.join("+"); // the backend keeps them for next time; the panel shows them now
      const j = await A.runJob(r.job, A.t("ocrRunning"), r.total);
      if (j.state === "done") A.toast(A.t("ocrDone", { pages: j.result.pages, words: j.result.words }));
      else if (j.state === "cancelled") A.toast(A.t("cancelled"), "info");
      else A.toast(A.t("e_ERROR") + " " + (j.error || ""), "bad");
      A.refreshPanel();
    };
  };

  /* ---------- export ---------- */
  PANELS.export = (root) => {
    const cards = [
      ["docx", "word", "exDocx", "exDocxHint"],
      ["txt", "txt", "exTxt", "exTxtHint"],
      ["images", "image", "exImages", "exImagesHint"],
      ["embedded", "layers", "exEmbedded", "exEmbeddedHint"],
      ["print", "print", "exPrint", "exPrintHint"],
    ];
    root.innerHTML = head("export", "expTitle", "expSub") + pagesField("exPg") +
      `<div class="excards">${cards.map(([k, ic, t, h]) => `<button class="excard" data-k="${k}">${A.icon(ic, 22)}<span><b>${A.esc(A.t(t))}</b><small>${A.esc(A.t(h))}</small></span></button>`).join("")}</div>` +
      section("imgOpts", `${field("format", seg("exF", [["png", "PNG"], ["jpg", "JPG"]], "png"))}${range("exD", "resolution", 72, 600, 200, " dpi", 4)}`);
    wire(root);
    A.$(".excards", root).onclick = async (e) => {
      const c = e.target.closest(".excard");
      if (!c) return;
      const kind = c.dataset.k, pages = val(root, "exPg");
      if (kind === "print") return A.print(pages);
      const payload = { kind, pages };
      if (kind === "images") Object.assign(payload, { format: segVal(root, "exF"), dpi: +val(root, "exD") });
      await A.commitPending();
      const r = await A.call("export_start", payload);
      if (!r) return;
      const j = await A.runJob(r.job, A.t("exporting"));
      if (j.state === "done") A.savedToast(j.result.path, kind === "images" || kind === "embedded" ? "exportedN" : "savedTo");
      else if (j.state === "cancelled") A.toast(A.t("cancelled"), "info");
      else A.toast(A.t("e_ERROR") + " " + (j.error || ""), "bad");
    };
  };

  /* ---------- combine files ---------- */
  let mergeList = [];
  PANELS.combine = (root) => {
    root.innerHTML = head("combine", "combTitle", "combSub") +
      section("mergeT", `<p class="hint">${A.esc(A.t("mergeHint"))}</p><div class="mlist" id="mList"></div>
        <div class="btnrow">${btn("mPick", "chooseFiles", "plus", "sm")}${S.doc && S.doc.path ? btn("mCur", "addCurrent", "pages", "ghost sm") : ""}</div>
        <div class="actions">${btn("mGo", "mergeRun", "combine", "primary block")}</div>`) +
      section("imgPdfT", `<p class="hint">${A.esc(A.t("imgPdfHint"))}</p>${btn("iGo", "chooseImages", "image", "block")}`);
    wire(root);
    const draw = () => {
      const host = A.$("#mList", root);
      host.innerHTML = mergeList.length
        ? mergeList.map((p, k) => `<div class="mrow" data-k="${k}">${A.icon("blank", 15)}<span title="${A.esc(p)}">${A.esc(A.basename(p))}</span>
            <button class="icon-btn xs" data-a="up" aria-label="up">${A.icon("chevU", 13)}</button><button class="icon-btn xs" data-a="down" aria-label="down">${A.icon("chevD", 13)}</button><button class="icon-btn xs danger" data-a="rm" aria-label="remove">${A.icon("x", 13)}</button></div>`).join("")
        : `<p class="hint dim">${A.esc(A.t("noFilesYet"))}</p>`;
      A.$("#mGo", root).disabled = mergeList.length < 2;
    };
    draw();
    A.$("#mList", root).onclick = (e) => {
      const b = e.target.closest("[data-a]");
      if (!b) return;
      const k = +b.closest(".mrow").dataset.k;
      if (b.dataset.a === "rm") mergeList.splice(k, 1);
      if (b.dataset.a === "up" && k > 0) [mergeList[k - 1], mergeList[k]] = [mergeList[k], mergeList[k - 1]];
      if (b.dataset.a === "down" && k < mergeList.length - 1) [mergeList[k + 1], mergeList[k]] = [mergeList[k], mergeList[k + 1]];
      draw();
    };
    A.$("#mPick", root).onclick = async () => {
      const r = await A.call("merge_dialog", {});
      ((r && r.paths) || []).forEach((p) => { if (!mergeList.includes(p)) mergeList.push(p); });
      draw();
    };
    if (A.$("#mCur", root)) A.$("#mCur", root).onclick = () => { if (!mergeList.includes(S.doc.path)) mergeList.unshift(S.doc.path); draw(); };
    A.$("#mGo", root).onclick = async () => {
      const r = await A.withSourcePasswords((passwords, opts) => A.call("merge_run", { paths: mergeList, passwords }, opts));
      if (!r) return;
      mergeList = [];
      draw();
      A.toast(A.t("mergedTo", { name: A.basename(r.path), n: r.pages }), "good", { label: A.t("openIt"), run: () => A.openPath(r.path) });
    };
    A.$("#iGo", root).onclick = async () => {
      const r = await A.call("images_to_pdf_dialog", {});
      if (r) A.toast(A.t("imagesTo", { name: A.basename(r.path), n: r.pages }), "good", { label: A.t("openIt"), run: () => A.openPath(r.path) });
    };
  };

  /* ---------- document info ---------- */
  PANELS.info = (root) => {
    root.innerHTML = head("info", "infoTitle", "infoSub") + `<div id="infoBody"><div class="pload">${A.esc(A.t("loading"))}</div></div>`;
    wire(root);
    A.call("doc_info", {}, { noBusy: true }).then((r) => {
      const host = A.$("#infoBody", root);
      if (!r || !host) return;
      const m = r.meta || {};
      const kv = (k, v) => (v || v === 0 ? `<div class="kv"><span class="k">${A.esc(A.t(k))}</span><span class="v">${A.esc(v)}</span></div>` : "");
      const mm = (pt) => `${Math.round((pt / 72) * 25.4)}`;
      host.innerHTML = section("metaT",
        field("title", `<input class="input" id="mTitle" value="${A.esc(m.title)}">`) +
        field("author", `<input class="input" id="mAuthor" value="${A.esc(m.author)}">`) +
        field("subject", `<input class="input" id="mSubject" value="${A.esc(m.subject)}">`) +
        field("keywords", `<input class="input" id="mKeywords" value="${A.esc(m.keywords)}">`) +
        `<div class="btnrow">${btn("mApply", "applyMeta", "check", "sm")}</div>`) +
        section("fileT",
          kv("st_pages", r.pages) + kv("fileSize", r.size ? A.fmtBytes(r.size) : A.t("notSavedYet")) + kv("pdfVersion", r.format) +
          kv("pageSize", `${mm(r.pageSize[0])} × ${mm(r.pageSize[1])} mm`) + kv("st_enc", S.doc.protected ? A.t("st_yes") : A.t("st_no")) +
          kv("producer", m.producer) + kv("creator", m.creator) + kv("created", A.fmtPdfDate(m.creationDate)) + kv("modified", A.fmtPdfDate(m.modDate)) +
          (r.path ? `<button class="lnk" id="iReveal">${A.icon("folder", 14)}${A.esc(A.t("showFolder"))}</button>` : "")) +
        section("compressT", `<p class="hint">${A.esc(A.t("compressHint"))}</p>${field("imgQuality", seg("cq", [["150", "qGood"], ["110", "qSmaller"], ["72", "qSmallest"]], "150"))}${btn("cGo", "compressBtn", "compress", "block")}`) +
        section("fontsT", r.fonts.length ? `<div class="fonts">${r.fonts.map((f) => `<div class="font"><span>${A.esc(f.name)}</span><small>${A.esc(f.type)}${f.embedded ? "" : " · " + A.esc(A.t("notEmbedded"))}</small></div>`).join("")}</div>` : `<p class="hint">${A.esc(A.t("noFonts"))}</p>`, "collapsible");
      wire(host);
      A.$("#mApply", host).onclick = async () => {
        const r2 = await A.edit("set_metadata", { title: val(host, "mTitle"), author: val(host, "mAuthor"), subject: val(host, "mSubject"), keywords: val(host, "mKeywords") });
        if (r2) A.toast(A.t("applied"));
      };
      if (A.$("#iReveal", host)) A.$("#iReveal", host).onclick = () => A.call("reveal", { path: r.path }, { quiet: true });
      const compress = async (asked, extra) => {
        const dpi = +segVal(host, "cq");
        await A.commitPending();
        if (!asked && !(await signedOk())) return;
        const st = await A.call("export_start", { kind: "compress", dpi, quality: dpi >= 150 ? 80 : dpi >= 110 ? 70 : 60, ...extra },
          { handle: A.askPasswords((x) => compress(true, x)) });
        if (!st) return;
        const j = await A.runJob(st.job, A.t("compressing"));
        if (j.state !== "done") return j.state === "cancelled" ? A.toast(A.t("cancelled"), "info") : A.toast(A.t("e_ERROR"), "bad");
        const pct = j.result.before ? Math.round((1 - j.result.after / j.result.before) * 100) : 0;
        A.toast(A.t("compressed", { size: A.fmtBytes(j.result.after), pct: Math.max(0, pct) }), "good", { label: A.t("showFolder"), run: () => A.call("reveal", { path: j.result.path }, { quiet: true }) });
      };
      A.$("#cGo", host).onclick = () => compress();
    });
  };

  /* ---------- settings ---------- */
  PANELS.settings = (root) => {
    const st = S.settings || {};
    root.innerHTML = head("settings", "settingsTitle", "") +
      field("language", seg("sLang", [["sr", "Srpski"], ["en", "English"], ["no", "Norsk"]], S.lang)) +
      field("theme", seg("sTheme", [["system", "themeSystem"], ["light", "themeLight"], ["dark", "themeDark"]], st.theme || "system"), "themeHint") +
      field("authorName", `<input class="input" id="sAuthor" value="${A.esc(st.author || "")}">`, "authorHint") +
      check("sNight", "nightMode", "nightHint", S.night) +
      field("defaultFit", seg("sFit", [["width", "fitWidth"], ["page", "fitPage"]], st.fit || "width")) +
      section("aboutT", `<p class="hint">${A.esc(A.t("aboutText"))}</p><div class="kv"><span class="k">${A.esc(A.t("version"))}</span><span class="v">2.0.0</span></div>
        <button class="lnk" id="sKeys">${A.icon("keyboard", 14)}${A.esc(A.t("shortcuts"))}</button>`);
    wire(root);
    A.$("#sLang", root).addEventListener("change", (e) => A.setLang(e.detail));
    A.$("#sTheme", root).addEventListener("change", (e) => A.setTheme(e.detail));
    A.$("#sAuthor", root).addEventListener("change", (e) => A.saveSettings({ author: e.target.value.trim() }));
    A.$("#sNight", root).addEventListener("change", (e) => A.setNight(e.target.checked));
    A.$("#sFit", root).addEventListener("change", (e) => { A.saveSettings({ fit: e.detail }); if (S.doc) A.fit(e.detail); });
    A.$("#sKeys", root).onclick = A.showShortcuts;
  };

  A.on("doc", ({ prev, doc }) => {
    markRail();
    if (!S.panel) return;
    if (!doc && NEEDS_DOC.has(S.panel)) return A.openPanel(null);
    const reopened = !prev || !doc || prev.id !== doc.id;
    const structural = prev && doc && (prev.protected !== doc.protected || prev.owner !== doc.owner || prev.forms !== doc.forms || prev.signed !== doc.signed || prev.scanned !== doc.scanned);
    if (reopened || structural || S.panel === "annotate") {
      clearTimeout(A._panelT); // the comments list alone is read again, so it keeps its place
      A._panelT = setTimeout(reopened || structural ? render : () => loadComments(A.$("#panelInner")), S.panel === "annotate" ? 250 : 0);
    }
  });
})(window.Aurora);
