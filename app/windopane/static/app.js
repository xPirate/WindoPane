"use strict";
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "html") el.innerHTML = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
    return el;
  };
  const ICON = {
    listen: '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16 8.5a5 5 0 0 1 0 7"/></svg>',
    zoom: '<svg viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>',
    reload: '<svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 1 1-2.3-5.7"/><path d="M20 4v5h-5"/></svg>',
    skip: '<svg viewBox="0 0 24 24"><path d="M5 5l9 7-9 7z"/><path d="M18 5v14"/></svg>',
    sendback: '<svg viewBox="0 0 24 24"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 0 1 0 12h-3"/></svg>',
  };
  const STATE_LABEL = {
    playing: "Live", connecting: "Connecting", resolving: "Resolving", buffering: "Buffering",
    retrying: "Retrying", error: "Error", idle: "Idle", waiting: "Waiting for live",
  };
  const RESOLUTIONS = [["auto", "Monitor default"], ["3840x2160", "3840 × 2160"], ["2560x1440", "2560 × 1440"],
    ["1920x1080", "1920 × 1080"], ["1280x720", "1280 × 720"]];

  let S = null;            // last /api/state
  let sel = 0;             // screen shown in the preview
  let screenSig = "";
  let streamsSig = "";
  let tabsSig = "";
  let devicesSig = "";
  let settingsLoaded = false;
  let volumeDragging = false;
  let view = location.pathname.replace(/\/+$/, "") === "/settings" ? "settings" : "wall";
  try { sel = Math.min(1, Math.max(0, +localStorage.getItem("wp-screen") || 0)); } catch (_) { /* storage off */ }

  // ------------------------------------------------------------------ api
  async function api(method, path, body) {
    const res = await fetch(path, {
      method, headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    let data = null;
    try { data = await res.json(); } catch (_) { /* empty */ }
    if (!res.ok) throw new Error((data && data.error) || res.statusText);
    return data;
  }
  async function act(method, path, body, okMsg) {
    try {
      const r = await api(method, path, body);
      if (okMsg) toast(okMsg);
      await refresh();
      return r;
    } catch (e) { toast(e.message, true); throw e; }
  }
  let toastTimer;
  function toast(msg, bad) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.className = "toast"), 2600);
  }

  // ------------------------------------------------------------------ helpers
  const streamById = (id) => (S.streams || []).find((s) => s.id === id);
  const layoutById = (id) => S.layouts.find((l) => l.id === id);
  const scr = (i = sel) => S.screens[i];
  const countOn = (i = sel) => layoutById(scr(i).layout).tiles.length;
  const keyNum = (_screen, slot) => slot + 1;  // tiles are numbered per screen, like the number keys
  const tileName = (screen, slot) => (S.screens.filter((s) => s.active).length > 1 ? `S${screen + 1}·` : "") + (slot + 1);
  // screens 3-4 appear once a monitor is connected or they have been set up
  const screenShown = (sc, i) => i < 2 || sc.connected || sc.slots.some(Boolean) || i === sel;
  const isAudible = (screen, slot) => !!S.audio && S.audio.screen === screen && S.audio.slot === slot;
  const monitorText = (sc) => sc.monitor ? `${sc.monitor.width} × ${sc.monitor.height}` : "";
  function setSel(i) {
    sel = i;
    try { localStorage.setItem("wp-screen", String(i)); } catch (_) { /* storage off */ }
    screenSig = streamsSig = "";
    render();
  }

  function layoutSvg(lay, w = 160, hgt = 90) {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${hgt}`);
    lay.tiles.forEach(([x, y, tw, th], i) => {
      const r = document.createElementNS(ns, "rect");
      const pad = lay.overlay.includes(i) ? 0 : 1.5;
      r.setAttribute("x", x * w + pad); r.setAttribute("y", y * hgt + pad);
      r.setAttribute("width", Math.max(1, tw * w - 2 * pad)); r.setAttribute("height", Math.max(1, th * hgt - 2 * pad));
      r.setAttribute("rx", 2);
      if (i === 0) r.setAttribute("class", "main");
      svg.append(r);
    });
    return svg;
  }

  // ------------------------------------------------------------------ render: screen tabs
  function renderTabs() {
    const sig = JSON.stringify([sel, S.focus_screen, S.screens.map((s) => [s.connected, s.enabled, s.monitor, s.layout,
      s.slots.some(Boolean)])]);
    if (sig === tabsSig) return;
    tabsSig = sig;
    const wrap = $("#screen-tabs");
    wrap.textContent = "";
    S.screens.forEach((sc, i) => {
      if (!screenShown(sc, i)) return;
      const sub = !sc.enabled ? "Turned off in settings"
        : sc.connected ? `${monitorText(sc) || "Connected"} · ${layoutById(sc.layout).name}`
        : "No monitor connected";
      wrap.append(h("button", {
        class: "stab" + (sc.active ? "" : " off"), role: "tab", "aria-selected": String(i === sel),
        title: S.focus_screen === i ? "Controller buttons act on this screen" : "",
        onclick: () => setSel(i),
      }, h("b", {}, `Screen ${i + 1}`, S.focus_screen === i && S.screens.some((x, j) => j !== i && x.active)
        ? h("span", { class: "focus", "aria-label": "focused" }) : null),
      h("small", {}, sub)));
    });
  }

  // ------------------------------------------------------------------ render: layouts
  function renderLayouts() {
    const wrap = $("#layouts");
    if (wrap.childElementCount !== S.layouts.length) {
      wrap.textContent = "";
      for (const lay of S.layouts) {
        wrap.append(h("button", {
          class: "lay", role: "radio", "data-id": lay.id, title: lay.name,
          onclick: () => act("POST", "/api/layout", { screen: sel, layout: lay.id }),
        }, layoutSvg(lay), h("span", {}, lay.name)));
      }
    }
    for (const b of wrap.children) b.setAttribute("aria-checked", String(b.dataset.id === scr().layout));
  }

  // ------------------------------------------------------------------ render: TV
  function renderScreen() {
    const sc = scr();
    const lay = layoutById(sc.layout);
    const sig = JSON.stringify([sel, sc.layout, sc.slots, S.audio, sc.zoom, sc.active,
      (S.streams || []).map((s) => [s.id, s.name]), sc.tiles.map((t) => t && t.mode), sc.takeovers]);
    const tv = $("#tv");
    const editing = tv.contains(document.activeElement) && document.activeElement.tagName === "SELECT";
    if (sig !== screenSig && !editing) {
      screenSig = sig;
      tv.textContent = "";
      lay.tiles.forEach((rect, i) => tv.append(buildTile(lay, rect, i)));
    }
    tv.classList.toggle("offline", !sc.active);
    const note = $("#tv-note");
    note.hidden = sc.active;
    note.textContent = !sc.enabled
      ? `Screen ${sel + 1} is turned off in Settings. You can still set it up here.`
      : `No monitor on HDMI port ${sel + 1} yet. Set it up here; it starts as soon as a monitor is plugged in.`;
    updateTileStatus();
  }

  function buildTile(lay, [x, y, w, hh], i) {
    const sc = scr();
    const sid = sc.slots[i];
    const takeoverId = (sc.takeovers || {})[String(i)];
    const assigned = sid ? streamById(sid) : null;
    const stream = takeoverId ? streamById(takeoverId) : assigned;
    const overlay = lay.overlay.includes(i);
    const audible = isAudible(sel, i);
    const zoomed = sc.zoom === i;
    const cls = ["tile", overlay && "overlay", !stream && "empty", audible && "audible", takeoverId && "takeover",
      sc.zoom != null && !zoomed && "hidden-by-zoom",
      (w < 0.3 || hh < 0.3) && "compact", (w < 0.26 && hh < 0.26) && "tiny"].filter(Boolean).join(" ");

    const select = h("select", {
      "aria-label": `Stream for tile ${keyNum(sel, i)}`,
      onclick: (e) => e.stopPropagation(),
      onchange: (e) => act("POST", "/api/slots", { screen: sel, slot: i, stream_id: e.target.value || null }),
    }, h("option", { value: "" }, "— empty —"),
      (S.streams || []).map((s) => h("option", { value: s.id, selected: s.id === sid }, s.name)));

    const btn = (name, title, on, fn) => h("button", {
      class: "ibtn " + name + (on ? " on" : ""), title, "aria-label": title, "aria-pressed": on ? "true" : "false",
      html: ICON[name], onclick: (e) => { e.stopPropagation(); fn(); },
    });
    const listen = () => act("POST", "/api/audio", { screen: sel, slot: audible ? null : i });

    const card = h("div", {
      class: "card", tabindex: stream ? "0" : null,
      title: stream ? (audible ? "Click to mute" : "Click to listen") : "",
      onclick: () => stream && listen(),
      onkeydown: (e) => { if (e.key === "Enter" && stream && e.target === e.currentTarget) e.currentTarget.click(); },
    },
      h("div", {},
        h("div", { class: "top" },
          h("span", { class: "slotnum", title: `Keyboard ${keyNum(sel, i)}` }, String(keyNum(sel, i))),
          h("span", { class: "tname" }, stream ? stream.name : "Empty"),
          takeoverId ? h("span", { class: "live-badge", title: "Live takeover" }, "LIVE") : null,
        ),
        h("div", { class: "tdetail", "data-detail": takeoverId ? null : "" },
          takeoverId ? `Took over from ${assigned ? assigned.name : "an empty tile"}; goes back when the live ends`
            : stream ? "" : "Pick a stream below"),
      ),
      h("div", { class: "bottom" },
        stream ? h("span", { class: "pill", "data-pill": "" }, h("span", { class: "pill-text" }, "")) : null,
        select,
        stream ? btn("listen", audible ? "Mute" : "Listen", audible, listen) : null,
        stream ? btn("zoom", zoomed ? "Back to layout" : "Fill screen", zoomed,
          () => act("POST", "/api/zoom", { screen: sel, slot: zoomed ? null : i })) : null,
        stream ? btn("reload", "Reload", false,
          () => act("POST", "/api/reload", { screen: sel, slot: i }, "Reloading tile " + keyNum(sel, i))) : null,
        stream && sc.tiles[i] && sc.tiles[i].mode === "playlist" ? btn("skip", "Next video", false,
          () => act("POST", "/api/skip", { screen: sel, slot: i })) : null,
        takeoverId ? btn("sendback", `Send back: show ${assigned ? assigned.name : "the empty tile"} again`, false,
          () => act("POST", "/api/live/send-back", { stream_id: takeoverId },
            `${stream.name} sent back until this broadcast ends`)) : null,
      ),
    );
    const tile = h("div", { class: cls, "data-slot": i }, card);
    Object.assign(tile.style, {
      left: x * 100 + "%", top: y * 100 + "%", width: w * 100 + "%", height: hh * 100 + "%",
      zIndex: overlay || zoomed ? 2 : 1,
    });
    if (zoomed) Object.assign(tile.style, { left: 0, top: 0, width: "100%", height: "100%", zIndex: 3 });
    return tile;
  }

  function updateTileStatus() {
    const sc = scr();
    for (const tile of $("#tv").children) {
      const i = +tile.dataset.slot;
      const st = sc.tiles[i];
      const pill = $("[data-pill]", tile);
      const detail = $("[data-detail]", tile);
      if (!pill) continue;
      if (!st) {
        pill.className = "pill";
        $(".pill-text", pill).textContent = sc.active ? "" : "Off";
        if (detail) detail.textContent = "";
        continue;
      }
      pill.className = "pill " + st.state;
      $(".pill-text", pill).textContent = STATE_LABEL[st.state] || st.state;
      pill.title = st.detail || "";
      if (detail) detail.textContent = st.detail || "";
    }
    $("#screen-hint").textContent = sc.zoom != null
      ? `Tile ${keyNum(sel, sc.zoom)} is filling screen ${sel + 1}. Press its zoom button again to go back.`
      : "Click a tile to listen. Pick what plays in each slot from its menu.";
  }

  // ------------------------------------------------------------------ render: streams
  function renderStreams() {
    const sig = JSON.stringify([sel, S.streams, S.screens.map((s) => [s.slots, s.layout])]);
    if (sig === streamsSig) return;
    streamsSig = sig;
    const list = $("#streams");
    list.textContent = "";
    $("#streams-empty").hidden = (S.streams || []).length > 0;
    const n = countOn();
    const slots = scr().slots;
    for (const s of S.streams) {
      const on = [];
      S.screens.forEach((sc, si) => sc.slots.forEach((id, i) => {
        if (id === s.id && i < countOn(si)) on.push(tileName(si, i));
      }));
      // a row of buttons for small layouts, a menu for big ones
      const put = n <= 6
        ? h("div", { class: "put", title: `Put on screen ${sel + 1}` },
          Array.from({ length: n }, (_, i) => h("button", {
            class: "btn small" + (slots[i] === s.id ? " active" : ""),
            "aria-label": `Put ${s.name} on tile ${keyNum(sel, i)}`,
            onclick: () => act("POST", "/api/slots", { screen: sel, slot: i, stream_id: slots[i] === s.id ? null : s.id }),
          }, String(keyNum(sel, i)))))
        : h("select", {
          class: "put-menu", "aria-label": `Put ${s.name} on a tile of screen ${sel + 1}`,
          onchange: (e) => {
            if (e.target.value !== "") act("POST", "/api/slots", { screen: sel, slot: +e.target.value, stream_id: s.id });
          },
        }, h("option", { value: "" }, `Put on screen ${sel + 1}…`),
          Array.from({ length: n }, (_, i) => {
            const cur = slots[i] ? streamById(slots[i]) : null;
            return h("option", { value: i, disabled: slots[i] === s.id },
              `Tile ${i + 1}` + (slots[i] === s.id ? " (here)" : cur ? ` (replace ${cur.name})` : ""));
          }));
      list.append(h("li", { class: "stream" },
        h("span", { class: "kind " + s.resolved_kind }, { web: "Web", adsb: "ADS-B" }[s.resolved_kind] || "Video"),
        h("div", { class: "sinfo" },
          h("div", { class: "sname" }, s.name,
            { playlist: h("span", { class: "mode-tag" }, s.shuffle ? "playlist · shuffle" : "playlist"),
              live: h("span", { class: "mode-tag" + (s.live_now && !s.sent_back ? " live-now" : "") },
                s.takeover ? (s.live_now ? (s.sent_back ? "live · sent back" : "live · on the board")
                  : `takes over S${s.takeover.screen + 1}·${s.takeover.slot + 1} when live`) : "waits for live") }[s.video_mode] || null, " ",
            s.max_height ? h("span", { class: "onslot", style: "color:var(--muted)" }, `≤${s.max_height}p`) : null),
          h("div", { class: "surl", title: s.url }, s.url + (s.location ? `  ·  from ${s.location}` : ""))),
        h("div", { class: "sactions" },
          on.length ? h("span", { class: "onslot" }, "On tile " + on.join(", ")) : null,
          put,
          h("button", { class: "btn small ghost", onclick: () => openForm(s) }, "Edit"),
          h("button", {
            class: "btn small ghost", onclick: () => {
              if (confirm(`Delete "${s.name}"?`)) act("DELETE", "/api/streams/" + s.id, null, "Deleted");
            },
          }, "Delete")),
      ));
    }
  }

  // ------------------------------------------------------------------ stream form
  const form = $("#stream-form");
  function syncKindVisibility() {
    const kind = form.kind.value;
    let resolved = kind;
    if (kind === "auto") {
      const u = form.url.value.trim().toLowerCase();
      if (/aircraft\.json(\?|$)/.test(u) || /^(tcp|sbs):\/\/[^/]+:30003/.test(u)) { form.dataset.kind = "adsb"; return; }
      const isWeb = /^https?:\/\//.test(u) &&
        !/\.(m3u8?|mpd|mp4|m4v|mkv|webm|mov|ts|flv)(\?|$)/.test(u) &&
        !/(youtube\.com|youtu\.be|twitch\.tv|kick\.com|vimeo\.com|dailymotion\.com|rumble\.com)/.test(u);
      resolved = isWeb ? "web" : "video";
    }
    form.dataset.kind = resolved;
    form.dataset.mode = videoMode(form.url.value.trim(), form.mode.value);
    if (form.takeover.value) form.dataset.takeover = "1"; else delete form.dataset.takeover;
  }
  function takeoverOptions(current) {
    const sel = form.takeover;
    sel.textContent = "";
    sel.append(h("option", { value: "" }, "Nothing (stay where assigned)"));
    S.screens.forEach((sc, si) => {
      if (!screenShown(sc, si)) return;
      const group = h("optgroup", { label: `Screen ${si + 1}` });
      const n = Math.max(countOn(si), current && current.screen === si ? current.slot + 1 : 0);
      for (let i = 0; i < n; i++) {
        const cur = sc.slots[i] ? streamById(sc.slots[i]) : null;
        group.append(h("option", { value: `${si}:${i}` }, `Screen ${si + 1}, tile ${i + 1}` + (cur ? ` (over ${cur.name})` : " (empty)")));
      }
      sel.append(group);
    });
    sel.value = current ? `${current.screen}:${current.slot}` : "";
  }
  // mirrors resolver.video_mode(): what "Auto" does with a link
  function videoMode(url, mode) {
    if (mode && mode !== "auto") return mode;
    let u;
    try { u = new URL(url); } catch (_) { return "single"; }
    const host = u.hostname.toLowerCase().replace(/^www\./, "").replace(/^m\./, "");
    const path = u.pathname.replace(/\/+$/, "");
    if (host === "youtube.com" || host.endsWith(".youtube.com")) {
      if (path === "/playlist" && u.searchParams.has("list")) return "playlist";
      if (path.endsWith("/live")) return "live";
    }
    const segs = path.split("/").filter(Boolean);
    if ((host === "twitch.tv" || host === "kick.com") && segs.length === 1 &&
        !["videos", "directory", "settings", "downloads", "search"].includes(segs[0].toLowerCase())) return "live";
    return "single";
  }
  function openForm(stream) {
    form.reset();
    form.sid.value = stream ? stream.id : "";
    $("#test-out").textContent = "";
    $("#test-out").className = "test-out";
    const slotSel = form.slot;
    slotSel.textContent = "";
    slotSel.append(h("option", { value: "" }, stream ? "Leave as is" : "Not yet"));
    let free = null;
    S.screens.forEach((sc, si) => {
      const group = h("optgroup", { label: `Screen ${si + 1}` + (sc.active ? "" : " (not connected)") });
      for (let i = 0; i < countOn(si); i++) {
        const cur = sc.slots[i] ? streamById(sc.slots[i]) : null;
        if (!cur && free == null && si === sel) free = `${si}:${i}`;
        group.append(h("option", { value: `${si}:${i}` }, `Screen ${si + 1}, tile ${i + 1}` + (cur ? ` (replace ${cur.name})` : " (empty)")));
      }
      slotSel.append(group);
    });
    if (!stream && free) slotSel.value = free;
    if (stream) {
      form.url.value = stream.url;
      form.name.value = stream.name === stream.url ? "" : stream.name;
      form.kind.value = stream.kind || "auto";
      form.max_height.value = stream.max_height || "";
      form.mode.value = stream.mode || "auto";
      form.shuffle.checked = !!stream.shuffle;
      form.check_minutes.value = stream.check_minutes || "";
      form.priority.value = String(stream.priority || 5);
      form.zoom.value = stream.zoom || "";
      form.refresh_minutes.value = stream.refresh_minutes || "";
      form.location.value = stream.location || "";
      form.units.value = stream.units || "nm";
      form.max_range.value = stream.max_range || "";
    }
    takeoverOptions(stream && stream.takeover);
    $("#save-stream").textContent = stream ? "Save changes" : "Add stream";
    syncKindVisibility();
    form.hidden = false;
    form.url.focus();
  }
  function formData() {
    return {
      url: form.url.value.trim(),
      name: form.name.value.trim(),
      kind: form.kind.value,
      max_height: form.max_height.value || null,
      mode: form.mode.value,
      shuffle: form.shuffle.checked,
      check_minutes: form.check_minutes.value || null,
      takeover: form.dataset.mode === "live" ? form.takeover.value : "",
      priority: form.takeover.value ? +form.priority.value : null,
      zoom: form.zoom.value || null,
      refresh_minutes: form.refresh_minutes.value || null,
      location: form.location.value.trim(),
      units: form.units.value,
      max_range: form.max_range.value || null,
    };
  }
  form.addEventListener("input", syncKindVisibility);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = formData();
    const target = form.slot.value ? form.slot.value.split(":").map(Number) : null;
    try {
      let saved;
      if (form.sid.value) {
        saved = await api("PUT", "/api/streams/" + form.sid.value, data);
        if (target) await api("POST", "/api/slots", { screen: target[0], slot: target[1], stream_id: saved.id });
      } else {
        saved = await api("POST", "/api/streams", target ? { ...data, screen: target[0], slot: target[1] } : data);
      }
      form.hidden = true;
      toast(`Saved "${saved.name}"`);
      await refresh();
    } catch (err) { toast(err.message, true); }
  });
  $("#add-stream").addEventListener("click", () => openForm(null));
  $("#cancel-stream").addEventListener("click", () => (form.hidden = true));
  $("#test-stream").addEventListener("click", async () => {
    const out = $("#test-out");
    const data = formData();
    if (!data.url) { out.textContent = "Enter a URL first"; out.className = "test-out bad"; return; }
    out.textContent = "Testing…"; out.className = "test-out";
    try {
      const r = await api("POST", "/api/streams/test", data);
      out.textContent = r.message + (r.title ? ` — ${r.title}` : "") + (r.live ? " (live)" : "");
      out.className = "test-out " + (r.ok ? "ok" : "bad");
    } catch (err) { out.textContent = err.message; out.className = "test-out bad"; }
  });

  // ------------------------------------------------------------------ settings
  const sform = $("#settings");
  function deviceOptions(selectEl, current) {
    selectEl.textContent = "";
    selectEl.append(h("option", { value: "auto" }, "Auto (this screen's HDMI)"),
      h("option", { value: "default" }, "System default"));
    for (const d of S.audio_devices) selectEl.append(h("option", { value: d.id }, d.name));
    if (current && ![...selectEl.options].some((o) => o.value === current)) {
      selectEl.append(h("option", { value: current }, current + " (not found)"));
    }
    selectEl.value = current || "auto";
  }
  function buildScreenRows() {
    const wrap = $("#screens-settings");
    wrap.textContent = "";
    S.screens.forEach((sc, i) => {
      if (!screenShown(sc, i)) return;
      const res = h("select", { name: `res${i}`, "aria-label": `Screen ${i + 1} resolution` },
        RESOLUTIONS.map(([v, t]) => h("option", { value: v }, t)));
      res.value = sc.resolution;
      const dev = h("select", { name: `dev${i}`, "aria-label": `Screen ${i + 1} audio output` });
      deviceOptions(dev, sc.audio_device);
      wrap.append(h("div", { class: "screen-row" },
        h("div", { class: "sname-col" }, `Screen ${i + 1}`,
          i > 0 ? h("label", { class: "check" },
            h("input", { type: "checkbox", name: `en${i}`, checked: sc.enabled }), "Use when connected") : null),
        h("label", {}, "Resolution", res),
        h("label", {}, "Video audio out", dev)));
    });
  }
  function loadSettings() {
    const devSig = JSON.stringify(S.audio_devices);
    if (settingsLoaded) {
      if (devSig !== devicesSig) {
        devicesSig = devSig;
        S.screens.forEach((_, i) => { const el = sform[`dev${i}`]; if (el) deviceOptions(el, el.value); });
      }
      return;
    }
    settingsLoaded = true;
    devicesSig = devSig;
    buildScreenRows();
    const d = S.settings.display, p = S.settings.player;
    sform.quality_cap.value = String(d.quality_cap);
    sform.gap.value = d.gap;
    sform.border.value = d.border;
    sform.osd.checked = !!d.osd;
    sform.network_caching_ms.value = p.network_caching_ms;
    sform.stall_timeout_s.value = p.stall_timeout_s;
    sform.engine.value = p.engine || "auto";
    sform.arrangement.value = d.arrangement || "auto";
    sform.password.placeholder = S.settings.web.password_set ? "set (leave blank to keep)" : "none";
  }
  sform.addEventListener("submit", async (e) => {
    e.preventDefault();
    const body = {
      display: {
        quality_cap: sform.quality_cap.value,
        gap: +sform.gap.value || 0, border: sform.border.value.trim() || "auto", osd: sform.osd.checked,
        arrangement: sform.arrangement.value,
      },
      screens: S.screens.map((_, i) => {
        if (!sform[`res${i}`]) return {};
        const row = { resolution: sform[`res${i}`].value, audio_device: sform[`dev${i}`].value };
        if (sform[`en${i}`]) row.enabled = sform[`en${i}`].checked;
        return row;
      }),
      player: { network_caching_ms: +sform.network_caching_ms.value, stall_timeout_s: +sform.stall_timeout_s.value,
        engine: sform.engine.value },
    };
    if (sform.password.value) body.web = { password: sform.password.value };
    try {
      await api("POST", "/api/settings", body);
      $("#settings-out").textContent = "Saved. Buffer changes apply to newly started streams.";
      $("#settings-out").className = "test-out ok";
      sform.password.value = "";
      settingsLoaded = false;
      await refresh();
    } catch (err) {
      $("#settings-out").textContent = err.message;
      $("#settings-out").className = "test-out bad";
    }
  });

  // ------------------------------------------------------------------ system
  function renderSystem() {
    const sys = S.system;
    const chip = $("#sys-chip");
    const temp = sys.temp_c != null ? ` · ${sys.temp_c}°C` : "";
    chip.textContent = `${sys.hostname} · ${sys.model}${temp}`;
    chip.classList.toggle("hot", (sys.temp_c || 0) >= 80 || !!(sys.throttled & 0xF));
    const mons = sys.monitors;
    const rows = [
      ["Board", sys.model + (sys.hw_h264 === false ? " (H.264 decoded in software)" : "")],
      ["Screens", mons == null ? "no display" : mons.length
        ? mons.map((m, i) => `${i + 1}: ${m.output} ${m.width}×${m.height}`).join("   ") : "none connected"],
      ["Video player", sys.engine ? (sys.engine === "mpv" ? "mpv" : "VLC") : "–"],
      ["Load", sys.load ? sys.load.join("  ") : "–"],
      ["Memory", sys.memory ? `${sys.memory.available_mb} MB free of ${sys.memory.total_mb} MB` : "–"],
      ["yt-dlp", sys.ytdlp || "not installed", !sys.ytdlp],
      ["JS runtime", sys.js_runtime ? "found" : "missing — YouTube will fail", !sys.js_runtime],
      ["Throttling", sys.throttled == null ? "n/a" : (sys.throttled & 0xF ? "ACTIVE (power/heat)" : "none"), !!(sys.throttled & 0xF)],
      ["Version", sys.version],
    ];
    const dl = $("#sys");
    dl.textContent = "";
    for (const [k, v, warn] of rows) dl.append(h("dt", {}, k), h("dd", { class: warn ? "warn" : "" }, String(v)));
    const note = $("#inputs-note");
    note.textContent = sys.inputs.length
      ? "Connected: " + sys.inputs.join(", ")
      : "No keyboard or controller detected yet — plug one in any time.";
  }

  // ------------------------------------------------------------------ audio bar
  const vol = $("#volume");
  vol.addEventListener("input", () => { volumeDragging = true; $("#volume-out").textContent = vol.value; });
  vol.addEventListener("change", async () => {
    volumeDragging = false;
    try { await api("POST", "/api/audio", { volume: +vol.value }); } catch (e) { toast(e.message, true); }
  });
  $("#mute-all").addEventListener("click", () => act("POST", "/api/audio", { slot: null }, "All tiles muted"));
  $("#reload-all").addEventListener("click", () => act("POST", "/api/reload", { screen: sel }, `Reloading screen ${sel + 1}`));

  function renderAudio() {
    if (!volumeDragging) { vol.value = S.volume; $("#volume-out").textContent = S.volume; }
    const m = $("#mute-all");
    m.classList.toggle("active", !S.audio);
    m.textContent = !S.audio ? "All muted"
      : `Listening: ${S.screens.filter((s) => s.active).length > 1 ? `screen ${S.audio.screen + 1}, ` : ""}tile ${S.audio.slot + 1} · Mute`;
  }


  // ------------------------------------------------------------------ graphics drivers (PC)
  const VENDOR_INFO = {
    amd: ["AMD", "Radeon cards and Ryzen graphics. Mesa video drivers and tear-free output."],
    intel: ["Intel", "Intel graphics. Intel media drivers."],
    nvidia: ["NVIDIA", "GeForce cards. Proprietary driver; Secure Boot may need attention."],
    none: ["None", "Remove all vendor video drivers. Video is decoded on the CPU."],
  };
  const JOB_TEXT = {
    starting: "Starting…", running: "Installing drivers… the log updates live",
    done: "Finished. Reboot to switch over.", failed: "Failed. The previous drivers were kept; see the log.",
    interrupted: "Interrupted before it finished. Run it again.",
  };
  let G = null;
  let gpuTimer = null;
  let gpuBuilt = false;
  const gform = $("#gpu-form");

  function codecs(rep) {
    if (!rep) return null;
    return rep.codecs.length ? rep.codecs.join(", ").replace("H264", "H.264") : "none";
  }
  function renderGpu() {
    const panel = $("#gpu-panel");
    panel.hidden = !G || G.platform !== "pc";
    if (panel.hidden) return;
    const card = G.gpus.find((g) => g.primary) || G.gpus[0];
    const engine = S && S.system ? S.system.engine : null;
    const rows = [
      ["GPU", G.gpus.length ? G.gpus.map((g) => g.name || `${g.vendor.toUpperCase()} ${g.device_id}`).join(", ") : "none detected"],
      ["Kernel driver", card ? (card.kernel_driver || "none loaded") : "–", card && !card.kernel_driver],
      ["Driver set", G.current ? VENDOR_INFO[G.current][0] : "not chosen yet", !G.current],
      ["Video player", engine === "mpv" ? "mpv" : "VLC"],
      ["VA-API (mpv)", codecs(G.decode.vaapi) || "not available",
        engine === "mpv" && !(G.decode.vaapi && G.decode.vaapi.codecs.length)],
      ["VDPAU (VLC)", codecs(G.decode.vdpau) || "not available",
        engine !== "mpv" && !(G.decode.vdpau && G.decode.vdpau.codecs.length)],
    ];
    const dl = $("#gpu-info");
    dl.textContent = "";
    for (const [k, v, warn] of rows) dl.append(h("dt", {}, k), h("dd", { class: warn ? "warn" : "" }, String(v)));
    dl.append(h("dt", {}, ""), h("dd", { class: "note" },
      engine === "mpv"
        ? "mpv decodes on the GPU with VA-API (AMD, Intel) or NVDEC (NVIDIA); codecs listed under VA-API play on the GPU."
        : "VLC on Debian/Ubuntu can only use VDPAU; switch the Video player to mpv in Settings for VA-API."));

    if (!gpuBuilt) {
      gpuBuilt = true;
      const wrap = $("#gpu-vendors");
      for (const v of ["amd", "intel", "nvidia", "none"]) {
        wrap.append(h("label", { class: "vendor" },
          h("input", { type: "radio", name: "vendor", value: v }),
          h("b", {}, VENDOR_INFO[v][0], h("span", { class: "tag det", "data-det": v, hidden: true }, "detected"),
            h("span", { class: "tag cur", "data-cur": v, hidden: true }, "installed")),
          h("small", {}, VENDOR_INFO[v][1])));
      }
      gform.vendor.value = G.current || G.recommended;
    }
    for (const v of ["amd", "intel", "nvidia", "none"]) {
      $(`[data-det="${v}"]`).hidden = !(G.gpus.some((g) => g.vendor === v));
      $(`[data-cur="${v}"]`).hidden = G.current !== v;
    }
    const job = G.job || {};
    const busy = job.status === "starting" || job.status === "running";
    $("#gpu-apply").disabled = busy || !gform.confirm.checked;
    const box = $("#gpu-job");
    box.hidden = !job.status;
    if (job.status) {
      const st = $("#gpu-job-status");
      const label = VENDOR_INFO[job.vendor] ? VENDOR_INFO[job.vendor][0] : job.vendor;
      st.textContent = `${label}: ${JOB_TEXT[job.status] || job.status}`;
      st.className = "job-status " + (busy ? "running" : job.status);
      $("#gpu-reboot").hidden = busy || !job.reboot;
      const log = $("#gpu-log");
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
      log.textContent = G.log || "(no output yet)";
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    clearTimeout(gpuTimer);
    gpuTimer = setTimeout(refreshGpu, busy ? 2000 : 15000);
  }
  async function refreshGpu() {
    try { G = await api("GET", "/api/gpu"); } catch (_) { gpuTimer = setTimeout(refreshGpu, 15000); return; }
    renderGpu();
  }
  gform.confirm.addEventListener("change", () => { if (G) renderGpu(); });
  gform.addEventListener("submit", async (e) => {
    e.preventDefault();
    const vendor = gform.vendor.value;
    const out = $("#gpu-out");
    if (!vendor) { out.textContent = "Pick a driver set"; out.className = "test-out bad"; return; }
    try {
      await api("POST", "/api/gpu/driver", { vendor, confirm: gform.confirm.checked });
      out.textContent = "Started. This takes a few minutes.";
      out.className = "test-out ok";
      gform.confirm.checked = false;
    } catch (err) { out.textContent = err.message; out.className = "test-out bad"; }
    refreshGpu();
  });
  $("#gpu-reboot").addEventListener("click", async () => {
    if (!confirm("Reboot this PC now? The wall will be back in about a minute.")) return;
    try {
      await api("POST", "/api/gpu/reboot", { confirm: true });
      toast("Rebooting…");
    } catch (err) { toast(err.message, true); }
  });
  refreshGpu();


  // ------------------------------------------------------------------ pages
  function setView(v, push) {
    view = v;
    $("#view-wall").hidden = v !== "wall";
    $("#view-settings").hidden = v !== "settings";
    for (const a of document.querySelectorAll(".views a")) {
      if (a.dataset.view === v) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    }
    document.title = v === "settings" ? "Settings · windoPane" : "windoPane";
    if (push) { history.pushState({}, "", v === "settings" ? "/settings" : "/"); window.scrollTo(0, 0); }
    if (S) render();
  }
  for (const a of document.querySelectorAll(".views a")) {
    a.addEventListener("click", (e) => {
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button) return;
      e.preventDefault();
      if (a.dataset.view !== view) setView(a.dataset.view, true);
    });
  }
  window.addEventListener("popstate", () =>
    setView(location.pathname.replace(/\/+$/, "") === "/settings" ? "settings" : "wall"));

  // ------------------------------------------------------------------ alerts
  const STYLE_TEXT = { popup: "pop-up", ticker: "ticker", both: "pop-up + ticker" };
  const clock = (t) => t ? new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  function ago(t) {
    if (!t) return "";
    const s = Math.max(0, Math.round((S.now || Date.now() / 1000) - t));
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.round(s / 60)} min ago`;
    if (s < 86400) return `${Math.round(s / 3600)} h ago`;
    return `${Math.round(s / 86400)} d ago`;
  }
  function secondsText(n) {
    if (!n) return "until dismissed";
    if (n < 60) return `${n} s`;
    if (n < 3600) return `${Math.round(n / 60)} min`;
    return `${Math.round(n / 3600)} h`;
  }
  const dismiss = (id) => act("POST", "/api/alerts/dismiss", id ? { id } : {});

  function alertRow(a, showState) {
    const live = showState ? a.on_screen : true;
    const left = a.expires ? Math.max(0, Math.round(a.expires - (S.now || 0))) : null;
    return h("li", { class: "alert-item" + (live ? "" : " gone") },
      h("span", { class: "alert-feed", title: a.feed }, a.feed),
      h("span", { class: "alert-title", title: a.title },
        a.link ? h("a", { href: a.link, target: "_blank", rel: "noopener noreferrer" }, a.title) : a.title),
      h("span", { class: "alert-meta" },
        a.test ? h("span", { class: "style-tag" }, "test") : null,
        h("span", { class: "style-tag" }, STYLE_TEXT[a.style] || a.style),
        h("span", {}, clock(a.created)),
        live ? h("span", {}, left == null ? "stays up" : left < 60 ? `${left}s left`
          : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left`) : h("span", {}, a.dismissed ? "dismissed" : "done"),
        live ? h("button", { class: "btn small", onclick: () => dismiss(a.id) }, "Dismiss") : null));
  }
  function renderAlertStrip() {
    const active = S.alerts.active;
    $("#alert-strip").hidden = !active.length;
    const list = $("#alert-active");
    list.textContent = "";
    for (const a of [...active].reverse()) list.append(alertRow(a, false));
  }
  $("#dismiss-all").addEventListener("click", () => dismiss(null));
  $("#dismiss-all-2").addEventListener("click", () => dismiss(null));

  function renderRecent() {
    const list = $("#alert-recent");
    list.textContent = "";
    for (const a of S.alerts.recent) list.append(alertRow(a, true));
    $("#recent-empty").hidden = S.alerts.recent.length > 0;
    $("#dismiss-all-2").hidden = !S.alerts.active.length;
  }

  let feedsSig = "";
  function feedStatus(f) {
    const st = f.status || {};
    if (!f.enabled) return ["Paused", false];
    if (!st.checked) return ["Waiting for the first check…", false];
    if (st.error) return [`${st.error} (${ago(st.checked)})`, true];
    let txt = `Checked ${ago(st.checked)} · ${st.count ?? "?"} headlines`;
    if (st.last_alert) txt += ` · last alert ${clock(st.last_alert)}`;
    else if (st.seeded) txt += " · new ones from now on will alert";
    return [txt, false];
  }
  function renderFeeds() {
    const feeds = S.alerts.feeds;
    $("#feeds-empty").hidden = feeds.length > 0;
    const list = $("#feeds");
    const sig = JSON.stringify(feeds.map((f) => ({ ...f, status: null })));
    if (sig !== feedsSig) {
      feedsSig = sig;
      list.textContent = "";
      for (const f of feeds) {
        const where = f.screens === "all" ? "" : ` · screen ${f.screens.map((i) => i + 1).join(", ")}`;
        list.append(h("li", { class: "stream", "data-fid": f.id },
          h("span", { class: "kind feed" + (f.enabled ? "" : " off") }, "RSS"),
          h("div", { class: "sinfo" },
            h("div", { class: "sname" }, f.name,
              h("span", { class: "mode-tag" }, `${STYLE_TEXT[f.style]} · ${secondsText(f.seconds)} · every ${f.every_minutes} min${where}`)),
            h("div", { class: "surl", title: f.url }, f.url + (f.match ? `  ·  only: ${f.match}` : "")),
            h("div", { class: "feed-status", "data-status": "" })),
          h("div", { class: "sactions" },
            h("button", { class: "btn small ghost", onclick: () => act("POST", `/api/alerts/feeds/${f.id}/check`, {}, "Checking…") }, "Check now"),
            h("button", { class: "btn small ghost", title: "Put the newest headline on the wall now",
              onclick: () => act("POST", `/api/alerts/feeds/${f.id}/show`, {}, "Sent to the wall") }, "Show latest"),
            h("button", { class: "btn small ghost", onclick: () => openFeedForm(f) }, "Edit"),
            h("button", { class: "btn small ghost", onclick: () => {
              if (confirm(`Delete the feed "${f.name}"?`)) act("DELETE", "/api/alerts/feeds/" + f.id, null, "Deleted");
            } }, "Delete"))));
      }
    }
    for (const f of feeds) {
      const el = $(`[data-fid="${f.id}"] [data-status]`, list);
      if (!el) continue;
      const [txt, bad] = feedStatus(f);
      el.textContent = txt;
      el.className = "feed-status" + (bad ? " bad" : "");
    }
  }

  const fform = $("#feed-form");
  function openFeedForm(feed) {
    fform.reset();
    fform.fid.value = feed ? feed.id : "";
    $("#feed-out").textContent = ""; $("#feed-out").className = "test-out";
    $("#feed-preview").hidden = true;
    const scr = fform.screens;
    scr.textContent = "";
    scr.append(h("option", { value: "all" }, "All screens"));
    S.screens.forEach((sc, i) => { if (screenShown(sc, i)) scr.append(h("option", { value: String(i) }, `Screen ${i + 1} only`)); });
    if (feed) {
      fform.url.value = feed.url;
      fform.name.value = feed.name;
      fform.style.value = feed.style;
      if (![...fform.seconds.options].some((o) => +o.value === feed.seconds)) {
        fform.seconds.append(h("option", { value: String(feed.seconds) }, secondsText(feed.seconds)));
      }
      fform.seconds.value = String(feed.seconds);
      fform.every_minutes.value = feed.every_minutes;
      fform.screens.value = feed.screens === "all" ? "all" : String(feed.screens[0]);
      fform.match.value = feed.match || "";
      fform.enabled.checked = feed.enabled;
    } else {
      fform.seconds.value = "30";
    }
    fform.seconds.dataset.touched = feed ? "1" : "";
    $("#save-feed").textContent = feed ? "Save changes" : "Add feed";
    fform.hidden = false;
    fform.url.focus();
  }
  fform.seconds.addEventListener("change", () => (fform.seconds.dataset.touched = "1"));
  fform.style.addEventListener("change", () => {
    if (!fform.seconds.dataset.touched) fform.seconds.value = fform.style.value === "ticker" ? "300" : "30";
  });
  function feedData() {
    return {
      url: fform.url.value.trim(), name: fform.name.value.trim(), style: fform.style.value,
      seconds: +fform.seconds.value, every_minutes: fform.every_minutes.value || null,
      screens: fform.screens.value === "all" ? "all" : [+fform.screens.value],
      match: fform.match.value.trim(), enabled: fform.enabled.checked,
    };
  }
  fform.addEventListener("submit", async (e) => {
    e.preventDefault();
    const data = feedData();
    try {
      const saved = fform.fid.value
        ? await api("PUT", "/api/alerts/feeds/" + fform.fid.value, data)
        : await api("POST", "/api/alerts/feeds", data);
      fform.hidden = true;
      toast(`Saved "${saved.name}". Headlines already in the feed won't alert; new ones will.`);
      await refresh();
    } catch (err) { toast(err.message, true); }
  });
  $("#add-feed").addEventListener("click", () => openFeedForm(null));
  $("#cancel-feed").addEventListener("click", () => (fform.hidden = true));
  $("#test-feed").addEventListener("click", async () => {
    const out = $("#feed-out");
    const prev = $("#feed-preview");
    const data = feedData();
    if (!data.url) { out.textContent = "Enter the feed URL first"; out.className = "test-out bad"; return; }
    out.textContent = "Reading the feed…"; out.className = "test-out"; prev.hidden = true;
    try {
      const r = await api("POST", "/api/alerts/test", { url: data.url, match: data.match });
      out.textContent = r.message; out.className = "test-out " + (r.ok ? "ok" : "bad");
      if (r.ok && !fform.name.value.trim() && r.title) fform.name.value = r.title.slice(0, 60);
      prev.textContent = "";
      for (const it of r.items || []) {
        prev.append(h("li", {}, h("time", {}, it.published ? new Date(it.published * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—"),
          h("span", { title: it.title }, it.title)));
      }
      prev.hidden = !(r.items || []).length;
    } catch (err) { out.textContent = err.message; out.className = "test-out bad"; }
  });

  const aform = $("#alert-settings");
  let alertSettingsLoaded = false;
  function loadAlertSettings() {
    if (alertSettingsLoaded) return;
    alertSettingsLoaded = true;
    const a = S.settings.alerts;
    aform.enabled.checked = !!a.enabled;
    aform.popup_position.value = a.popup_position;
    aform.max_popups.value = String(a.max_popups);
    aform.ticker_position.value = a.ticker_position;
    aform.ticker_speed.value = a.ticker_speed;
  }
  aform.addEventListener("change", async () => {
    const body = { alerts: {
      enabled: aform.enabled.checked, popup_position: aform.popup_position.value, max_popups: +aform.max_popups.value,
      ticker_position: aform.ticker_position.value, ticker_speed: aform.ticker_speed.value } };
    try { await api("POST", "/api/settings", body); toast("Alert settings saved"); await refresh(); }
    catch (err) { toast(err.message, true); }
  });

  // ------------------------------------------------------------------ power
  const POWER = {
    restart: ["Press again to restart the wall", "Restarting the wall… back in a few seconds"],
    reboot: ["Press again to reboot", "Rebooting… this page reconnects when it's back"],
    poweroff: ["Press again to shut down", "Shutting down"],
  };
  let armed = null, armTimer = null;
  function disarm() {
    if (armed) { armed.classList.remove("armed"); $("small", armed).textContent = armed.dataset.normal; }
    armed = null;
  }
  for (const b of document.querySelectorAll(".power-btn")) {
    b.addEventListener("click", async () => {
      const action = b.dataset.action;
      if (armed !== b) {
        disarm();
        armed = b;
        b.dataset.normal = $("small", b).textContent;
        b.classList.add("armed");
        $("small", b).textContent = POWER[action][0];
        clearTimeout(armTimer);
        armTimer = setTimeout(disarm, 4000);
        return;
      }
      clearTimeout(armTimer);
      disarm();
      try {
        await api("POST", "/api/system/power", { action, confirm: true });
        toast(POWER[action][1]);
        if (action === "poweroff") {
          for (const x of document.querySelectorAll(".power-btn")) x.disabled = true;
          $("#power-note").textContent = "Shutting down. " + startAgainText();
        }
      } catch (err) { toast(err.message, true); }
    });
  }
  function startAgainText() {
    const p = S && S.system ? S.system.profile : "";
    return p === "pi5" ? "To start it again, press the Pi's power button or unplug and replug the power."
      : p.startsWith("pi") ? "To start it again, unplug the power and plug it back in."
      : "To start it again, press the PC's power button.";
  }
  function renderPower() {
    const off = $("#poweroff-note");
    if (armed !== off.parentElement) off.textContent = "Turns the system off. " + startAgainText();
    const note = $("#power-note");
    if ([...document.querySelectorAll(".power-btn")].some((b) => b.disabled)) return;
    note.textContent = S.settings.web.password_set ? "Press a button twice to confirm."
      : "Press a button twice to confirm. Anyone on your network can use these: set a web UI password under Display & playback.";
    note.className = "small-note" + (S.settings.web.password_set ? "" : " warn");
  }

  // ------------------------------------------------------------------ loop
  function render() {
    renderAudio();
    renderSystem();
    if (view === "wall") {
      renderAlertStrip();
      renderTabs();
      renderLayouts();
      renderScreen();
      renderStreams();
    } else {
      loadSettings();
      loadAlertSettings();
      renderFeeds();
      renderRecent();
      renderPower();
    }
  }
  async function refresh() {
    try {
      S = await api("GET", "/api/state");
    } catch (e) {
      $("#sys-chip").textContent = "offline — retrying";
      return;
    }
    if (sel >= S.screens.length) sel = 0;
    render();
  }
  setView(view, false);
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 2000);
})();
