/* Aurora PDF - page thumbnails (sidebar), the organize grid, bookmarks and attachments. */
"use strict";
(function (A) {
  const S = A.S;

  /* ---------- context menu ---------- */
  A.menu = (items, x, y) => {
    const m = A.$("#menu");
    m.innerHTML = "";
    items.forEach((it) => {
      if (it === "-") { m.appendChild(A.h(`<div class="m-sep"></div>`)); return; }
      const b = A.h(`<button class="m-item${it.danger ? " danger" : ""}" role="menuitem">${A.icon(it.icon || "blank", 16)}<span></span>${it.key ? `<kbd>${A.esc(it.key)}</kbd>` : ""}</button>`);
      A.$("span", b).textContent = it.label;
      if (it.disabled) b.disabled = true;
      if (it.active) b.classList.add("on");
      b.onclick = () => { hideMenu(); it.run(); };
      m.appendChild(b);
    });
    m.classList.remove("hidden");
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = `${A.clamp(x, 6, window.innerWidth - w - 6)}px`;
    m.style.top = `${A.clamp(y, 6, window.innerHeight - h - 6)}px`;
    setTimeout(() => document.addEventListener("pointerdown", outside, true), 0);
  };
  function outside(e) { if (!e.target.closest("#menu")) hideMenu(); }
  function hideMenu() { A.$("#menu").classList.add("hidden"); document.removeEventListener("pointerdown", outside, true); }
  A.hideMenu = hideMenu;

  /* ---------- page actions shared by sidebar and grid ---------- */
  const targets = (i) => (S.selected.has(i) && S.selected.size ? [...S.selected] : [i]);
  A.pageActions = (i) => {
    const can = A.can("assemble");
    const t = targets(i);
    const n = t.length;
    return [
      { label: A.t("rotateL"), icon: "rotL", run: () => A.edit("rotate", { pages: t, degrees: -90 }), disabled: !can },
      { label: A.t("rotateR"), icon: "rotR", run: () => A.edit("rotate", { pages: t, degrees: 90 }), disabled: !can },
      "-",
      { label: A.t("insertBlankAfter"), icon: "insert", run: () => A.edit("insert_blank", { at: Math.max(...t) + 1, like: i }), disabled: !can },
      { label: A.t("insertFileAfter"), icon: "open", run: () => A.call("insert_file_dialog", { at: Math.max(...t) + 1 }), disabled: !can },
      { label: n > 1 ? A.t("duplicateN", { n }) : A.t("duplicate"), icon: "copy", run: () => A.edit("duplicate", { pages: t }), disabled: !can },
      { label: n > 1 ? A.t("extractN", { n }) : A.t("extractPage"), icon: "export", run: () => A.extractPages(t) },
      "-",
      { label: n > 1 ? A.t("deleteN", { n }) : A.t("deletePage"), icon: "trash", danger: true, run: () => A.deletePages(t), disabled: !can },
    ];
  };
  A.extractPages = async (pages, extra) => {
    await A.commitPending();
    const r = await A.call("extract_dialog", { pages, ...extra }, { handle: A.askPasswords((x) => A.extractPages(pages, x)) });
    if (r) A.savedToast(r.path);
  };
  A.deletePages = async (pages) => {
    if (!S.doc) return;
    if (pages.length >= S.doc.pages.length) return A.toast(A.t("e_CANNOT_DELETE_ALL"), "bad");
    if (pages.length > 1 && !(await A.confirm(A.t("deletePagesTitle"), A.t("deletePagesMsg", { n: pages.length }), A.t("delete"), true))) return;
    S.selected.clear();
    A.emit("selection"); // the panel would otherwise still name the deleted pages
    A.edit("delete_pages", { pages });
  };

  /* ---------- tile lists ---------- */
  class Tiles {
    constructor(host, opts) {
      this.host = host;
      this.big = !!opts.big;
      this.width = opts.big ? 320 : A.LO;
      host.addEventListener("pointerdown", (e) => this.down(e));
      host.addEventListener("contextmenu", (e) => {
        const t = e.target.closest(".tile");
        if (!t) return;
        e.preventDefault();
        const i = +t.dataset.i;
        if (!S.selected.has(i)) { S.selected.clear(); S.selected.add(i); A.emit("selection"); }
        A.menu(A.pageActions(i), e.clientX, e.clientY);
      });
    }
    /** Tiles for the open document. For the same document only what changed is touched: one edit
     *  in a 400-page file swaps one picture instead of rebuilding 400 tiles. */
    render() {
      const doc = S.doc;
      if (!doc || this.docId !== doc.id) {
        this.host.textContent = "";
        this.docId = doc ? doc.id : null;
        if (!doc) return;
      }
      const tiles = A.$$(".tile[data-i]", this.host);
      let add = A.$(".tile.add", this.host);
      doc.pages.forEach(([w, h, rev], i) => {
        let t = tiles[i];
        if (!t) {
          t = this.tile();
          this.host.insertBefore(t, add);
        }
        if (t.dataset.rev !== String(rev)) {
          t.dataset.rev = rev;
          A.$(".tframe", t).style.aspectRatio = `${w}/${h}`;
          A.$("img", t).src = A.pageUrl(rev, this.width);
        }
        t.dataset.i = i;
        A.$(".tnum", t).textContent = i + 1;
      });
      tiles.slice(doc.pages.length).forEach((t) => t.remove());
      if (this.big && !add) {
        add = A.h(`<button class="tile add" data-a="add"><span>${A.icon("plus", 26)}</span><b data-i18n="addPages"></b></button>`);
        this.host.appendChild(add);
        A.applyI18n(add);
      }
      this.mark();
    }
    tile() {
      const t = A.h(`<div class="tile"><div class="tframe"><img loading="lazy" decoding="async" draggable="false" alt=""></div><span class="tnum"></span></div>`);
      if (this.big) {
        t.appendChild(A.h(`<div class="tacts">
          <button class="icon-btn sm" data-a="rotL" data-tip="rotateL">${A.icon("rotL", 16)}</button>
          <button class="icon-btn sm" data-a="rotR" data-tip="rotateR">${A.icon("rotR", 16)}</button>
          <button class="icon-btn sm" data-a="dup" data-tip="duplicate">${A.icon("copy", 16)}</button>
          <button class="icon-btn sm danger" data-a="del" data-tip="deletePage">${A.icon("trash", 16)}</button></div>`));
        A.applyI18n(t);
      }
      return t;
    }
    mark() {
      A.$$(".tile[data-i]", this.host).forEach((t) => {
        const i = +t.dataset.i;
        t.classList.toggle("current", i === S.current);
        t.classList.toggle("selected", S.selected.has(i));
      });
    }
    scrollToCurrent() {
      const t = A.$(`.tile[data-i="${S.current}"]`, this.host);
      if (!t) return;
      const hr = this.host.getBoundingClientRect(), tr = t.getBoundingClientRect();
      if (tr.top < hr.top + 10 || tr.bottom > hr.bottom - 10) this.host.scrollTop += tr.top - hr.top - hr.height / 3;
    }
    down(e) {
      if (e.button !== 0) return;
      const act = e.target.closest("[data-a]");
      if (act) {
        e.stopPropagation();
        const tile = act.closest(".tile");
        const i = tile && tile.dataset.i != null ? +tile.dataset.i : null;
        const a = act.dataset.a;
        if (a === "add") return A.menu([
          { label: A.t("insertBlankEnd"), icon: "blank", run: () => A.edit("insert_blank", { at: S.doc.pages.length }) },
          { label: A.t("insertFileEnd"), icon: "open", run: () => A.call("insert_file_dialog", { at: S.doc.pages.length }) },
          { label: A.t("insertImagesEnd"), icon: "image", run: () => A.call("insert_images_dialog", { at: S.doc.pages.length }) },
        ], e.clientX, e.clientY);
        const t = targets(i);
        if (a === "rotL") A.edit("rotate", { pages: t, degrees: -90 });
        if (a === "rotR") A.edit("rotate", { pages: t, degrees: 90 });
        if (a === "dup") A.edit("duplicate", { pages: t });
        if (a === "del") A.deletePages(t);
        return;
      }
      const tile = e.target.closest(".tile[data-i]");
      if (!tile) return;
      const i = +tile.dataset.i;
      const start = { x: e.clientX, y: e.clientY };
      let dragging = false, ghost = null, marker = null, insertAt = null;
      const move = (ev) => {
        if (!dragging && Math.hypot(ev.clientX - start.x, ev.clientY - start.y) > 6 && A.can("assemble")) {
          dragging = true;
          if (!S.selected.has(i)) { S.selected.clear(); S.selected.add(i); this.mark(); }
          ghost = A.h(`<div class="tghost"></div>`);
          ghost.appendChild(A.$(".tframe", tile).cloneNode(true));
          if (S.selected.size > 1) ghost.appendChild(A.h(`<span class="gcount">${S.selected.size}</span>`));
          document.body.appendChild(ghost);
          marker = A.h(`<div class="tmarker ${this.big ? "v" : "h"}"></div>`);
          document.body.appendChild(marker);
        }
        if (!dragging) return;
        ghost.style.left = `${ev.clientX + 10}px`;
        ghost.style.top = `${ev.clientY + 10}px`;
        insertAt = this.insertionPoint(ev, marker);
        const hr = this.host.getBoundingClientRect();
        if (ev.clientY < hr.top + 40) this.host.scrollTop -= 14;
        if (ev.clientY > hr.bottom - 40) this.host.scrollTop += 14;
      };
      const up = (ev) => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        if (ghost) ghost.remove();
        if (marker) marker.remove();
        if (!dragging) return this.click(i, ev);
        if (insertAt == null) return;
        const moving = [...S.selected].sort((a, b) => a - b);
        const all = S.doc.pages.map((_, k) => k);
        const rest = all.filter((k) => !S.selected.has(k));
        const before = all.slice(0, insertAt).filter((k) => !S.selected.has(k)).length;
        const order = [...rest.slice(0, before), ...moving, ...rest.slice(before)];
        if (order.every((v, k) => v === k)) return;
        S.selected = new Set(moving.map((_, k) => before + k));
        A.edit("reorder", { order });
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    }
    insertionPoint(ev, marker) {
      const tiles = A.$$(".tile[data-i]", this.host);
      let best = null, bd = Infinity;
      tiles.forEach((t) => {
        const r = t.getBoundingClientRect();
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        const d = Math.hypot((ev.clientX - cx) * (this.big ? 1 : 0.2), ev.clientY - cy);
        if (d < bd) { bd = d; best = { t, r, after: this.big ? ev.clientX > cx : ev.clientY > cy }; }
      });
      if (!best) return null;
      const { t, r, after } = best;
      if (this.big) {
        marker.style.cssText = `left:${(after ? r.right : r.left) - 2}px;top:${r.top}px;height:${r.height}px`;
      } else {
        marker.style.cssText = `left:${r.left}px;top:${(after ? r.bottom + 5 : r.top - 5) - 1}px;width:${r.width}px`;
      }
      return +t.dataset.i + (after ? 1 : 0);
    }
    click(i, ev) {
      if (ev.shiftKey && S.anchorSel != null) {
        S.selected.clear();
        for (let k = Math.min(S.anchorSel, i); k <= Math.max(S.anchorSel, i); k++) S.selected.add(k);
      } else if (ev.ctrlKey || ev.metaKey) {
        S.selected.has(i) ? S.selected.delete(i) : S.selected.add(i);
        S.anchorSel = i;
      } else {
        S.selected.clear();
        S.selected.add(i);
        S.anchorSel = i;
        if (this.big) { /* stay in the grid */ } else A.gotoPage(i);
      }
      A.emit("selection");
      if (this.big && ev.detail === 2) A.setOrganize(false, i);
    }
  }

  let thumbs, grid;
  A.initPages = () => {
    thumbs = new Tiles(A.$("#thumbs"), { big: false });
    grid = new Tiles(A.$("#grid"), { big: true });
    A.$("#grid").addEventListener("dblclick", (e) => {
      const t = e.target.closest(".tile[data-i]");
      if (t) A.setOrganize(false, +t.dataset.i);
    });
  };
  const rerender = () => { thumbs.render(); if (S.organize) grid.render(); };
  A.on("doc", ({ prev, doc }) => {
    if (!doc) { S.selected.clear(); thumbs.render(); grid.render(); return; }
    if (!prev || prev.id !== doc.id) S.selected.clear();
    const changed = !prev || prev.id !== doc.id || prev.pages.length !== doc.pages.length || prev.pages.some((p, k) => p[2] !== doc.pages[k][2]);
    const had = S.selected.size;
    S.selected = new Set([...S.selected].filter((k) => k < doc.pages.length));
    if (changed) rerender();
    if (S.selected.size !== had) A.emit("selection");
    if (S.side === "outline") A.loadOutline();
    if (S.side === "attach") A.loadAttachments();
  });
  // follow the reader in the sidebar once scrolling pauses, so a fast flick doesn't load every thumbnail
  let followT = null;
  A.on("current", () => { thumbs.mark(); clearTimeout(followT); followT = setTimeout(() => thumbs.scrollToCurrent(), 180); });
  A.on("selection", () => { thumbs.mark(); if (S.organize) grid.mark(); A.emit("selectionChanged"); });

  A.setOrganize = (on, gotoIndex) => {
    const was = S.organize;
    S.organize = !!on && !!S.doc;
    A.clearTextSelection();
    A.$("#grid").classList.toggle("hidden", !S.organize);
    A.$("#pages").classList.toggle("hidden", S.organize);
    A.$("#app").classList.toggle("organizing", S.organize);
    if (S.organize) grid.render();
    else {
      if (was && S.selected.size) { S.selected.clear(); A.emit("selection"); } // the selection belongs to organizing
      A.refreshVisible();
      if (gotoIndex != null) setTimeout(() => A.gotoPage(gotoIndex, false), 30);
      if (S.panel === "organize") A.openPanel(null);
    }
  };

  /* ---------- sidebar tabs ---------- */
  const TABS = [["pages", "pages", "sidePages"], ["outline", "bookmark", "sideOutline"], ["attach", "clip", "sideAttach"]];
  A.buildSideTabs = () => {
    const host = A.$("#sideTabs");
    host.innerHTML = TABS.map(([id, ic, tip]) => `<button class="icon-btn sm${S.side === id ? " active" : ""}" data-side="${id}" data-tip="${tip}">${A.icon(ic, 17)}</button>`).join("") +
      `<span class="side-title" id="sideTitle"></span><button class="icon-btn sm" id="sideAdd" data-tip="addBookmark">${A.icon("plus", 16)}</button>`;
    A.applyI18n(host);
    host.onclick = (e) => {
      const b = e.target.closest("[data-side]");
      if (b) return A.setSide(b.dataset.side);
      if (e.target.closest("#sideAdd")) {
        if (S.side === "outline") A.addBookmark();
        else if (S.side === "attach") A.call("attachment_add_dialog", {}).then((r) => r && A.renderAttachments(r.items));
      }
    };
    A.setSide(S.side);
  };
  A.setSide = (id) => {
    S.side = id;
    A.$$("#sideTabs [data-side]").forEach((b) => b.classList.toggle("active", b.dataset.side === id));
    A.$("#thumbs").classList.toggle("hidden", id !== "pages");
    A.$("#outline").classList.toggle("hidden", id !== "outline");
    A.$("#attachList").classList.toggle("hidden", id !== "attach");
    A.$("#sideTitle").textContent = A.t({ pages: "sidePages", outline: "sideOutline", attach: "sideAttach" }[id]);
    A.$("#sideAdd").classList.toggle("hidden", id === "pages");
    A.$("#sideAdd").dataset.tipText = A.t(id === "outline" ? "addBookmark" : "addAttachment");
    if (id === "outline") A.loadOutline();
    if (id === "attach") A.loadAttachments();
  };

  /* ---------- bookmarks ---------- */
  let toc = [];
  A.loadOutline = async () => {
    if (!S.doc) { A.$("#outline").innerHTML = ""; return; }
    const r = await A.call("toc", {}, { noBusy: true, quiet: true });
    toc = (r && r.items) || [];
    renderOutline();
  };
  function renderOutline() {
    const host = A.$("#outline");
    if (!toc.length) {
      host.innerHTML = `<div class="side-empty">${A.icon("bookmark", 26)}<p>${A.esc(A.t("noBookmarks"))}</p><button class="btn sm" id="bmAdd">${A.icon("plus", 15)}${A.esc(A.t("addBookmark"))}</button></div>`;
      A.$("#bmAdd").onclick = A.addBookmark;
      return;
    }
    host.innerHTML = toc.map(([lvl, title, page], k) => `<div class="bm" data-k="${k}" style="padding-left:${8 + (lvl - 1) * 14}px">
      <button class="bm-go">${A.esc(title)}</button><span class="bm-p">${page >= 0 ? page + 1 : ""}</span>
      <button class="icon-btn xs" data-act="edit" data-tip="rename">${A.icon("text", 13)}</button><button class="icon-btn xs danger" data-act="del" data-tip="delete">${A.icon("x", 13)}</button></div>`).join("");
    A.applyI18n(host);
    host.onclick = async (e) => {
      const row = e.target.closest(".bm");
      if (!row) return;
      const k = +row.dataset.k;
      const act = e.target.closest("[data-act]");
      if (!act) { if (toc[k][2] >= 0) A.gotoPage(toc[k][2]); return; }
      if (!A.can("modify")) return A.toast(A.t("e_NOT_ALLOWED"), "bad");
      if (act.dataset.act === "del") toc.splice(k, 1);
      else {
        const t = await A.prompt(A.t("rename"), A.t("bookmarkTitle"), toc[k][1]);
        if (t == null || !t.trim()) return;
        toc[k][1] = t.trim();
      }
      saveToc();
    };
  }
  async function saveToc() {
    const r = await A.edit("toc_set", { items: toc });
    if (r) { toc = r.items; renderOutline(); }
  }
  A.addBookmark = async () => {
    if (!S.doc) return;
    if (!A.can("modify")) return A.toast(A.t("e_NOT_ALLOWED"), "bad");
    const title = await A.prompt(A.t("addBookmark"), A.t("bookmarkTitle"), `${A.t("page")} ${S.current + 1}`);
    if (title == null || !title.trim()) return;
    const at = toc.findIndex((e) => e[2] > S.current);
    const entry = [1, title.trim(), S.current];
    if (at < 0) toc.push(entry); else toc.splice(at, 0, entry);
    saveToc();
  };

  /* ---------- attachments ---------- */
  A.loadAttachments = async () => {
    if (!S.doc) { A.$("#attachList").innerHTML = ""; return; }
    const r = await A.call("attachments", {}, { noBusy: true, quiet: true });
    A.renderAttachments((r && r.items) || []);
  };
  A.renderAttachments = (items) => {
    const host = A.$("#attachList");
    if (!items.length) {
      host.innerHTML = `<div class="side-empty">${A.icon("clip", 26)}<p>${A.esc(A.t("noAttachments"))}</p></div>`;
      return;
    }
    host.innerHTML = items.map((a, k) => `<div class="att" data-k="${k}">${A.icon("clip", 15)}<span class="att-n" title="${A.esc(a.file)}">${A.esc(a.file)}</span><span class="att-s">${A.fmtBytes(a.size)}</span>
      <button class="icon-btn xs" data-act="save" data-tip="saveAttachment">${A.icon("download", 13)}</button><button class="icon-btn xs danger" data-act="del" data-tip="delete">${A.icon("x", 13)}</button></div>`).join("");
    A.applyI18n(host);
    host.onclick = async (e) => {
      const row = e.target.closest(".att");
      const act = e.target.closest("[data-act]");
      if (!row || !act) return;
      const a = items[+row.dataset.k];
      if (act.dataset.act === "save") { const r = await A.call("attachment_save_dialog", { name: a.name }); if (r) A.savedToast(r.path); }
      else if (await A.confirm(A.t("removeAttachment"), a.file, A.t("delete"), true)) {
        const r = await A.edit("attachment_delete", { name: a.name });
        if (r) A.renderAttachments(r.items);
      }
    };
  };
})(window.Aurora);
