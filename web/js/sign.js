/* Aurora PDF - signatures: hand-drawn/typed/image signatures to place on pages, and
 * certificate-based digital signing with the user's Digital IDs. */
"use strict";
(function (A) {
  const S = A.S;
  const FONTS = [
    { id: "Great Vibes", size: 92 },
    { id: "Caveat", size: 96 },
    { id: "Marck Script", size: 84 },
  ];
  const INKS = ["#0f172a", "#1d3fbf", "#0b6b3a"];

  /* ---------- canvas helpers ---------- */
  function trimCanvas(src, pad = 10) {
    const ctx = src.getContext("2d");
    const { width: w, height: h } = src;
    const data = ctx.getImageData(0, 0, w, h).data;
    let x0 = w, y0 = h, x1 = -1, y1 = -1;
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        if (data[(y * w + x) * 4 + 3] > 8) {
          if (x < x0) x0 = x;
          if (x > x1) x1 = x;
          if (y < y0) y0 = y;
          if (y > y1) y1 = y;
        }
      }
    }
    if (x1 < 0) return null;
    const out = document.createElement("canvas");
    out.width = x1 - x0 + 1 + pad * 2;
    out.height = y1 - y0 + 1 + pad * 2;
    out.getContext("2d").drawImage(src, x0, y0, x1 - x0 + 1, y1 - y0 + 1, pad, pad, x1 - x0 + 1, y1 - y0 + 1);
    return out;
  }

  function drawPad(canvas, getColor) {
    const ctx = canvas.getContext("2d");
    const ratio = window.devicePixelRatio || 1;
    const fit = () => {
      const r = canvas.getBoundingClientRect();
      canvas.width = Math.round(r.width * ratio * 2);
      canvas.height = Math.round(r.height * ratio * 2);
      ctx.setTransform(ratio * 2, 0, 0, ratio * 2, 0, 0);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
    };
    fit();
    let pts = null, last = null, drawn = false;
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, t: e.timeStamp }; };
    canvas.addEventListener("pointerdown", (e) => {
      canvas.setPointerCapture(e.pointerId);
      pts = [pos(e)];
      last = 2.6;
      ctx.fillStyle = getColor();
      ctx.beginPath();
      ctx.arc(pts[0].x, pts[0].y, 1.3, 0, Math.PI * 2);
      ctx.fill();
      drawn = true;
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!pts) return;
      const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
      evs.forEach((ev) => {
        const p = pos(ev);
        const q = pts[pts.length - 1];
        const dist = Math.hypot(p.x - q.x, p.y - q.y);
        if (dist < 0.8) return;
        const speed = dist / Math.max(1, p.t - q.t);
        const width = A.clamp(3.4 - speed * 1.1, 1.3, 3.4);
        last = last * 0.7 + width * 0.3;
        pts.push(p);
        if (pts.length >= 3) {
          const a = pts[pts.length - 3], b = pts[pts.length - 2], c = pts[pts.length - 1];
          ctx.strokeStyle = getColor();
          ctx.lineWidth = last;
          ctx.beginPath();
          ctx.moveTo((a.x + b.x) / 2, (a.y + b.y) / 2);
          ctx.quadraticCurveTo(b.x, b.y, (b.x + c.x) / 2, (b.y + c.y) / 2);
          ctx.stroke();
        }
      });
    });
    const end = () => { pts = null; };
    canvas.addEventListener("pointerup", end);
    canvas.addEventListener("pointercancel", end);
    return {
      clear: () => { ctx.clearRect(0, 0, canvas.width, canvas.height); drawn = false; },
      isEmpty: () => !drawn,
    };
  }

  async function typedCanvas(text, font, color) {
    await document.fonts.load(`${font.size}px '${font.id}'`);
    const c = document.createElement("canvas");
    const ctx = c.getContext("2d");
    ctx.font = `${font.size * 2}px '${font.id}'`;
    const m = ctx.measureText(text);
    c.width = Math.ceil(m.width + font.size * 1.2);
    c.height = Math.ceil(font.size * 3.2);
    ctx.font = `${font.size * 2}px '${font.id}'`;
    ctx.fillStyle = color;
    ctx.textBaseline = "middle";
    ctx.fillText(text, font.size * 0.6, c.height / 2);
    return c;
  }

  /* ---------- create a signature ---------- */
  A.createSignature = () => {
    let tab = "draw", color = INKS[0], font = FONTS[0];
    const body = A.h(`<div class="sigmaker">
      <div class="seg" id="sgTabs"><button data-v="draw" class="active">${A.t("sigDraw")}</button><button data-v="type">${A.t("sigType")}</button><button data-v="image">${A.t("sigImage")}</button></div>
      <div class="sg-pane" data-p="draw"><div class="pad-wrap"><canvas class="pad"></canvas><div class="pad-line"></div><span class="pad-hint">${A.esc(A.t("sigDrawHint"))}</span></div>
        <div class="sg-row"><div class="inks">${INKS.map((c, k) => `<button class="sw${k ? "" : " on"}" data-c="${c}" style="--c:${c}" aria-label="${c}"></button>`).join("")}</div><button class="btn ghost sm" id="sgClear">${A.icon("eraser", 15)}${A.esc(A.t("clear"))}</button></div></div>
      <div class="sg-pane hidden" data-p="type"><input class="input" id="sgName" maxlength="60">
        <div class="fontpick">${FONTS.map((f, k) => `<button class="fp${k ? "" : " on"}" data-f="${k}" style="font-family:'${f.id}'"></button>`).join("")}</div></div>
      <div class="sg-pane hidden" data-p="image"><p class="hint">${A.esc(A.t("sigImageHint"))}</p><button class="btn block" id="sgPick">${A.icon("image", 16)}${A.esc(A.t("chooseImage"))}</button></div>
    </div>`);
    const name = A.$("#sgName", body);
    name.value = (S.settings && S.settings.author) || "";
    const previews = () => A.$$(".fp", body).forEach((b) => (b.textContent = name.value || A.t("sigTypeSample")));
    name.addEventListener("input", previews);
    previews();
    let pad;
    return A.modal({
      title: A.t("sigCreate"),
      body,
      wide: true,
      onOpen: (m) => {
        pad = drawPad(A.$(".pad", body), () => color);
        A.$("#sgTabs", body).onclick = (e) => {
          const b = e.target.closest("button");
          if (!b) return;
          tab = b.dataset.v;
          A.$$("#sgTabs button", body).forEach((x) => x.classList.toggle("active", x === b));
          A.$$(".sg-pane", body).forEach((p) => p.classList.toggle("hidden", p.dataset.p !== tab));
          A.$("#sgSave", m.el).classList.toggle("hidden", tab === "image");
        };
        A.$(".inks", body).onclick = (e) => {
          const b = e.target.closest("[data-c]");
          if (!b) return;
          color = b.dataset.c;
          A.$$(".inks .sw", body).forEach((x) => x.classList.toggle("on", x === b));
          A.$$(".fp", body).forEach((x) => (x.style.color = color));
        };
        A.$(".fontpick", body).onclick = (e) => {
          const b = e.target.closest("[data-f]");
          if (!b) return;
          font = FONTS[+b.dataset.f];
          A.$$(".fp", body).forEach((x) => x.classList.toggle("on", x === b));
        };
        A.$("#sgClear", body).onclick = () => pad.clear();
        A.$("#sgPick", body).onclick = async () => {
          const r = await A.call("sig_image_dialog", {});
          if (r && r.id) { m.close(r.id); A.emit("signatures", r.items); }
        };
      },
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        {
          label: A.t("saveSignature"), kind: "primary", id: "sgSave",
          onClick: async () => {
            let canvas;
            if (tab === "draw") {
              if (pad.isEmpty()) { A.toast(A.t("sigEmpty"), "bad"); return false; }
              canvas = trimCanvas(A.$(".pad", body), 16);
            } else {
              const txt = name.value.trim();
              if (!txt) { A.toast(A.t("sigEmpty"), "bad"); return false; }
              canvas = trimCanvas(await typedCanvas(txt, font, color), 16);
            }
            if (!canvas) { A.toast(A.t("sigEmpty"), "bad"); return false; }
            const r = await A.call("sig_save", { png: canvas.toDataURL("image/png") });
            if (!r) return false;
            A.emit("signatures", r.items);
            return r.id;
          },
        },
      ],
    });
  };

  /** Start placing a saved signature: the next click on a page puts it there. */
  A.placeSignature = (id) => {
    if (!A.can("annotate")) return A.toast(A.t("e_NOT_ALLOWED"), "bad");
    const url = A.url(`sig/${id}.png`);
    const img = new Image();
    img.onload = () => {
      A.setMode("place", { placing: { id, url, aspect: img.naturalHeight / img.naturalWidth } });
      A.toast(A.t("sigPlaceHint"), "info");
    };
    img.src = url;
  };

  /* ---------- Digital IDs ---------- */
  A.createDigitalId = () =>
    A.modal({
      title: A.t("idCreate"),
      body: `<p class="hint">${A.esc(A.t("idCreateHint"))}</p>
        <div class="field"><label>${A.esc(A.t("idName"))}</label><input class="input" id="idN" maxlength="80"></div>
        <div class="row"><div class="field"><label>${A.esc(A.t("idEmail"))}</label><input class="input" id="idE" maxlength="120"></div>
        <div class="field"><label>${A.esc(A.t("idOrg"))}</label><input class="input" id="idO" maxlength="120"></div></div>
        <div class="row"><div class="field"><label>${A.esc(A.t("password"))}</label><input class="input" id="idP" type="password"></div>
        <div class="field"><label>${A.esc(A.t("passwordAgain"))}</label><input class="input" id="idP2" type="password"></div></div>`,
      onOpen: (m) => { A.$("#idN", m.el).value = (S.settings && S.settings.author) || ""; },
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        {
          label: A.t("create"), kind: "primary",
          onClick: async (m) => {
            const v = (id) => A.$(id, m.el).value.trim();
            if (!v("#idN")) { A.toast(A.t("idNeedName"), "bad"); return false; }
            if (v("#idP").length < 4) { A.toast(A.t("pwTooShort"), "bad"); return false; }
            if (v("#idP") !== v("#idP2")) { A.toast(A.t("pwMismatch"), "bad"); return false; }
            const r = await A.call("id_create", { name: v("#idN"), email: v("#idE"), org: v("#idO"), password: v("#idP") });
            if (!r) return false;
            A.toast(A.t("idCreated"));
            A.emit("ids", r.items);
            return r.id;
          },
        },
      ],
    });

  A.importDigitalId = async () => {
    const pw = await A.prompt(A.t("idImport"), A.t("idImportPw"), "", { password: true, ok: A.t("chooseFile") });
    if (pw == null) return null;
    const r = await A.call("id_import_dialog", { password: pw });
    if (r) { A.toast(A.t("idImported")); A.emit("ids", r.items); }
    return r && r.id;
  };

  /* ---------- digital signing ---------- */
  A.startDigitalSign = async (opts = {}) => {
    if (!S.doc) return;
    let ids = ((await A.call("ids_list", {}, { noBusy: true })) || {}).items || [];
    if (!ids.length) {
      const made = await A.createDigitalId();
      if (!made) return;
      ids = ((await A.call("ids_list", {}, { noBusy: true })) || {}).items || [];
    }
    const sigs = ((await A.call("sig_list", {}, { noBusy: true })) || {}).items || [];
    const st = S.settings || {};
    const body = A.h(`<div class="dsign">
      <div class="field"><label>${A.esc(A.t("idChoose"))}</label><div class="idlist">${ids.map((d, k) => `
        <label class="idcard"><input type="radio" name="dsid" value="${d.id}" ${k ? "" : "checked"}>
        <span class="ic-body">${A.icon("idcard", 20)}<span><b>${A.esc(d.name)}</b><small>${A.esc([d.email, d.org].filter(Boolean).join(" · "))}</small>
        <small>${A.esc(A.t("validUntil", { date: A.fmtIsoDate(d.not_after) }))}${d.self_signed ? " · " + A.esc(A.t("selfSigned")) : ""}</small></span></span></label>`).join("")}</div>
        <div class="linkrow"><button class="lnk" id="dsNew">${A.icon("plus", 14)}${A.esc(A.t("idCreate"))}</button><button class="lnk" id="dsImp">${A.icon("download", 14)}${A.esc(A.t("idImport"))}</button></div></div>
      <div class="field"><label>${A.esc(A.t("idPassword"))}</label><input class="input" id="dsPw" type="password" autocomplete="off"></div>
      <div class="row"><div class="field"><label>${A.esc(A.t("reason"))}</label><input class="input" id="dsR" value="${A.esc(st.sig_reason || "")}"></div>
      <div class="field"><label>${A.esc(A.t("location"))}</label><input class="input" id="dsL" value="${A.esc(st.sig_location || "")}"></div></div>
      ${opts.rect ? "" : `<label class="check"><input type="checkbox" id="dsVis" checked><span class="box">${A.icon("check", 13)}</span><span class="lbl">${A.esc(A.t("dsVisible"))}<small>${A.esc(A.t("dsVisibleHint"))}</small></span></label>`}
      ${sigs.length ? `<div class="field"><label>${A.esc(A.t("dsWithImage"))}</label><div class="sigpick"><button class="sp on" data-id="">${A.esc(A.t("none"))}</button>${sigs.map((s) => `<button class="sp" data-id="${s.id}"><img src="${A.url(`sig/${s.id}.png`)}" alt=""></button>`).join("")}</div></div>` : ""}
      <p class="hint">${A.esc(A.t("dsNote"))}</p></div>`);
    let sigId = "";
    const res = await A.modal({
      title: A.t("dsTitle"),
      body,
      wide: true,
      onOpen: (m) => {
        A.$("#dsNew", m.el).onclick = async () => { const id = await A.createDigitalId(); if (id) { m.close(null); A.startDigitalSign(opts); } };
        A.$("#dsImp", m.el).onclick = async () => { const id = await A.importDigitalId(); if (id) { m.close(null); A.startDigitalSign(opts); } };
        const pick = A.$(".sigpick", m.el);
        if (pick) pick.onclick = (e) => {
          const b = e.target.closest(".sp");
          if (!b) return;
          sigId = b.dataset.id;
          A.$$(".sp", pick).forEach((x) => x.classList.toggle("on", x === b));
        };
      },
      buttons: [
        { label: A.t("cancel"), kind: "ghost", value: null },
        {
          label: A.t("continue"), kind: "primary",
          onClick: (m) => {
            const chosen = A.$("input[name=dsid]:checked", m.el);
            const pw = A.$("#dsPw", m.el).value;
            if (!chosen) return false;
            if (!pw) { A.toast(A.t("idNeedPassword"), "bad"); return false; }
            const vis = opts.rect ? true : A.$("#dsVis", m.el).checked;
            return { id: chosen.value, password: pw, reason: A.$("#dsR", m.el).value.trim(), location: A.$("#dsL", m.el).value.trim(), visible: vis };
          },
        },
      ],
    });
    if (!res) return;
    const go = async (page, rect) => {
      const payload = Object.assign({}, res, {
        page, rect, sig: sigId || undefined, field: opts.field || undefined,
        labels: { signedBy: A.t("apSignedBy"), date: A.t("apDate"), reason: A.t("reason"), location: A.t("location"), when: A.fmtShort(new Date(), true) },
      });
      delete payload.visible;
      if (S.doc.signed && S.doc.dirty && !(await A.confirm(A.t("signedSaveTitle"), A.t("signedSaveMsg"), A.t("saveAnyway")))) return;
      if (S.doc.dirty) A.toast(A.t("dsUsesCurrent"), "info");
      const r = await A.call("sign_digital", payload, { handle: (e) => { if (e.error === "WRONG_PASSWORD") { A.toast(A.t("idWrongPassword"), "bad"); return true; } } });
      if (r) A.savedToast(r.path, "signedTo");
      A.emit("signed");
    };
    if (opts.rect) return go(opts.page, opts.rect);
    if (!res.visible) return go(0, null);
    A.setMode("sigbox", { done: (page, rect) => go(page, rect) });
    A.showBanner(A.t("dsDrawBox"), () => A.setMode("select"));
  };

  A.STATUS_ICON = { valid: "shield", untrusted: "shield", modified: "warn", invalid: "warn" };
})(window.Aurora);
