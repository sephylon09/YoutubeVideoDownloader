/* Media Downloader - front-end logic. Plain JavaScript, no build step.
 *
 * Flow: analyze a link -> pick MP4/MP3 and a quality -> start a job ->
 * poll /api/progress/<id> -> offer the finished file for download.
 * All text that comes from YouTube is inserted with textContent, never as HTML.
 */
(() => {
  "use strict";

  const POLL_INTERVAL = 700;
  const ANALYZE_TIMEOUT = 120000;
  const STORAGE_JOBS = "md.jobs";
  const STORAGE_THEME = "md.theme";
  const BASE_TITLE = document.title;
  const TERMINAL = new Set(["completed", "failed", "cancelled"]);
  // "public" when the app is hosted for others (password sign-in, files kept only temporarily).
  const PUBLIC = document.body.dataset.mode === "public";
  const WHERE = PUBLIC ? "on the server" : "on your computer";
  const YOUTUBE_HINT_RE = /^(https?:\/\/)?([a-z0-9-]+\.)*(youtube\.com|youtu\.be|youtube-nocookie\.com)(\/|$)/i;

  const ICONS = {
    check: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5"/></svg>',
    convert: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12a8 8 0 0 1 13.7-5.6L20 8.5M20 4v4.5h-4.5M20 12a8 8 0 0 1-13.7 5.6L4 15.5M4 20v-4.5h4.5"/></svg>',
    audio: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 18V5.5l11-2V16"/><circle cx="6.5" cy="18" r="2.5"/><circle cx="17.5" cy="16" r="2.5"/></svg>',
    warn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3.8 2.8 19.5h18.4z"/><path d="M12 10v4.2M12 17v.2"/></svg>',
    info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8v.2"/></svg>',
    error: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m0 0-4.5-4.5M12 15l4.5-4.5M5 19.5h14"/></svg>',
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const el = {
    themeToggle: $("#themeToggle"),
    healthBanner: $("#healthBanner"),
    form: $("#analyzeForm"),
    urlField: $("#urlField"),
    urlInput: $("#urlInput"),
    urlHint: $("#urlHint"),
    clearBtn: $("#clearBtn"),
    pasteBtn: $("#pasteBtn"),
    analyzeBtn: $("#analyzeBtn"),
    results: $("#results"),
    emptyState: $("#emptyState"),
    skeleton: $("#skeleton"),
    analyzeError: $("#analyzeError"),
    analyzeErrorTitle: $("#analyzeErrorTitle"),
    analyzeErrorMessage: $("#analyzeErrorMessage"),
    analyzeErrorDetails: $("#analyzeErrorDetails"),
    analyzeErrorDetail: $("#analyzeErrorDetail"),
    retryAnalyzeBtn: $("#retryAnalyzeBtn"),
    videoCard: $("#videoCard"),
    thumb: $("#thumb"),
    durationBadge: $("#durationBadge"),
    shortBadge: $("#shortBadge"),
    videoTitle: $("#videoTitle"),
    videoChannel: $("#videoChannel"),
    videoMeta: $("#videoMeta"),
    resolutionStrip: $("#resolutionStrip"),
    optionsCard: $("#optionsCard"),
    optionsSub: $("#optionsSub"),
    formatTabs: $("#formatTabs"),
    tabMp4: $("#tab-mp4"),
    tabMp3: $("#tab-mp3"),
    panelMp4: $("#panel-mp4"),
    panelMp3: $("#panel-mp3"),
    notices: $("#notices"),
    mp4Grid: $("#mp4Grid"),
    mp3Grid: $("#mp3Grid"),
    mp3Source: $("#mp3Source"),
    mp4Note: $("#mp4Note"),
    convertRow: $("#convertRow"),
    convertToggle: $("#convertToggle"),
    downloadSummary: $("#downloadSummary"),
    downloadBtn: $("#downloadBtn"),
    jobsSection: $("#jobsSection"),
    jobsList: $("#jobsList"),
    clearFinishedBtn: $("#clearFinishedBtn"),
    jobTemplate: $("#jobTemplate"),
    toasts: $("#toasts"),
    footerMeta: $("#footerMeta"),
  };

  const state = {
    analysis: null,
    analyzing: false,
    analyzeController: null,
    analyzeToken: 0,
    format: "mp4",
    selection: { mp4: null, mp3: null },
    convertH264: false,
    starting: false,
    health: null,
    healthTimer: null,
    jobs: new Map(),
  };

  // ------------------------------------------------------------------ utils

  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "icon") node.innerHTML = ICONS[value] || ""; // static, trusted markup only
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const store = {
    get(key, fallback) {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? fallback : JSON.parse(raw);
      } catch { return fallback; }
    },
    set(key, value) {
      try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
    },
  };

  function fmtBytes(bytes) {
    if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    const digits = unit === 0 || value >= 100 ? 0 : 1;
    return `${value.toFixed(digits)} ${units[unit]}`;
  }

  function fmtDuration(seconds) {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "";
    const s = Math.max(0, Math.round(seconds));
    const hours = Math.floor(s / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    const secs = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return hours ? `${hours}:${pad(minutes)}:${pad(secs)}` : `${minutes}:${pad(secs)}`;
  }

  function fmtEta(seconds) {
    if (seconds === null || seconds === undefined) return null;
    if (seconds < 1) return "almost done";
    return `${fmtDuration(seconds)} left`;
  }

  function fmtCount(value) {
    if (value === null || value === undefined) return "—";
    return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 }).format(value);
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const date = new Date(`${iso}T00:00:00`);
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  }

  function fmtSize(size, approximate) {
    if (size === null || size === undefined) return "Size unknown";
    return `${approximate ? "≈ " : ""}${fmtBytes(size)}`;
  }

  // -------------------------------------------------------------------- API

  class ApiError extends Error {
    constructor(info, status) {
      super(info.message || "Request failed");
      this.code = info.code;
      this.title = info.title || "Something went wrong";
      this.detail = info.detail;
      this.retryable = info.retryable;
      this.status = status;
    }
  }

  async function api(path, { method = "GET", body, signal } = {}) {
    let response;
    try {
      response = await fetch(path, {
        method,
        signal,
        cache: "no-store",
        headers: body ? { "Content-Type": "application/json" } : undefined,
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (error) {
      if (error.name === "AbortError") throw error;
      throw new ApiError({
        code: "server_unreachable",
        title: "Can't reach the app",
        message: PUBLIC
          ? "The server isn't responding. Check your connection, or wait a moment while the server starts, then try again."
          : "The Media Downloader server isn't responding. Make sure start.bat is still running.",
      }, 0);
    }
    let data = null;
    try { data = await response.json(); } catch { /* empty or non-JSON body */ }
    if (response.status === 401) {
      window.location.assign("/login"); // session expired or signed out in another tab
    }
    if (!response.ok) {
      const info = (data && data.error) || {
        code: `http_${response.status}`,
        title: "Request failed",
        message: `The server answered with status ${response.status}.`,
      };
      throw new ApiError(info, response.status);
    }
    return data;
  }

  // ----------------------------------------------------------------- toasts

  function toast({ type = "info", title, message, action, timeout = 6500 }) {
    const iconName = type === "success" ? "check" : type === "error" ? "error" : "info";
    const close = h("button", { type: "button", class: "icon-btn icon-btn-sm toast-close", "aria-label": "Dismiss", icon: "close" });
    const node = h("div", { class: `toast toast-${type}`, role: type === "error" ? "alert" : "status" },
      h("span", { class: "toast-icon", icon: iconName }),
      h("div", { class: "toast-body" },
        h("p", { class: "toast-title", text: title }),
        message ? h("p", { class: "toast-message", text: message }) : null,
        action ? h("div", { class: "toast-actions" }, action) : null),
      close);
    let timer = null;
    const dismiss = () => {
      clearTimeout(timer);
      node.classList.add("is-leaving");
      node.addEventListener("animationend", () => node.remove(), { once: true });
      setTimeout(() => node.remove(), 400);
    };
    close.addEventListener("click", dismiss);
    node.addEventListener("mouseenter", () => clearTimeout(timer));
    node.addEventListener("mouseleave", () => { if (timeout) timer = setTimeout(dismiss, 2500); });
    el.toasts.append(node);
    while (el.toasts.children.length > 4) el.toasts.firstElementChild.remove();
    if (timeout) timer = setTimeout(dismiss, timeout);
    return dismiss;
  }

  const toastError = (error) => toast({ type: "error", title: error.title || "Something went wrong", message: error.message, timeout: 9000 });

  // ------------------------------------------------------------------ theme

  function applyTheme(theme, persist) {
    document.documentElement.setAttribute("data-theme", theme);
    el.themeToggle.setAttribute("aria-label", theme === "dark" ? "Switch to light theme" : "Switch to dark theme");
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", theme === "dark" ? "#0a0b10" : "#f4f5fa");
    if (persist) {
      try { localStorage.setItem(STORAGE_THEME, theme); } catch { /* ignore */ }
    }
  }

  // ----------------------------------------------------------------- health

  async function loadHealth(refresh = false) {
    try {
      state.health = await api(`/api/health${refresh ? "?refresh=true" : ""}`);
      renderHealth();
    } catch (error) {
      el.healthBanner.hidden = false;
      el.healthBanner.className = "banner is-error";
      el.healthBanner.replaceChildren(h("span", { icon: "warn" }), h("div", {}, h("strong", { text: error.title }), " ", error.message));
    }
  }

  function renderHealth() {
    const tools = state.health.tools;
    const problems = tools.problems || [];
    const visible = problems.filter((p) => p.severity !== "info" || p.code === "yt_dlp_update");
    if (visible.length) {
      const isError = visible.some((p) => p.severity === "error");
      el.healthBanner.className = `banner${isError ? " is-error" : ""}`;
      el.healthBanner.replaceChildren(
        h("span", { icon: isError ? "error" : "warn" }),
        h("div", { class: "banner-items" }, visible.map((p) => h("p", {}, h("strong", { text: `${p.title}.` }), " ", p.message))),
      );
      el.healthBanner.hidden = false;
    } else {
      el.healthBanner.hidden = true;
    }
    const parts = [`v${state.health.version}`];
    if (tools.yt_dlp_version) parts.push(`yt-dlp ${tools.yt_dlp_version}`);
    if (tools.ffmpeg && tools.ffmpeg.version) parts.push(`FFmpeg ${tools.ffmpeg.version.split("-")[0]}`);
    if (tools.js_runtime && tools.js_runtime.available) parts.push(`${tools.js_runtime.name === "deno" ? "Deno" : "Node.js"} ${tools.js_runtime.version}`);
    el.footerMeta.textContent = parts.join(" · ");

    // Re-check periodically while something essential is missing (e.g. FFmpeg being installed).
    clearTimeout(state.healthTimer);
    if (problems.some((p) => p.severity === "error" || p.code === "js_runtime_missing")) {
      state.healthTimer = setTimeout(() => loadHealth(true), 30000);
    }
    updateDownloadBar();
  }

  // ------------------------------------------------------------- URL input

  function onUrlInput() {
    const value = el.urlInput.value.trim();
    el.clearBtn.hidden = !value;
    el.analyzeBtn.disabled = !value || state.analyzing;
    el.urlField.classList.remove("is-invalid");
    if (value && !YOUTUBE_HINT_RE.test(value) && value.length > 8) {
      setHint("This doesn't look like a YouTube link.", true);
    } else {
      setHint("Works with youtube.com, youtu.be and Shorts links.", false);
    }
  }

  function setHint(text, isError) {
    el.urlHint.textContent = text;
    el.urlHint.classList.toggle("is-error", Boolean(isError));
  }

  async function pasteFromClipboard() {
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      el.urlInput.focus();
      toast({ type: "info", title: "Paste with your keyboard", message: "Your browser doesn't allow reading the clipboard here. Press Ctrl+V instead." });
      return;
    }
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (!text) {
        toast({ type: "info", title: "Clipboard is empty", message: "Copy a YouTube link first, then press Paste." });
        return;
      }
      el.urlInput.value = text.slice(0, 2048);
      onUrlInput();
      el.analyzeBtn.focus();
    } catch {
      el.urlInput.focus();
      toast({ type: "info", title: "Clipboard access blocked", message: "Allow clipboard access for this page, or press Ctrl+V in the link box." });
    }
  }

  function clearInput() {
    el.urlInput.value = "";
    onUrlInput();
    el.urlInput.focus();
  }

  function shake(node) {
    node.classList.remove("shake");
    void node.offsetWidth; // restart the animation
    node.classList.add("shake");
  }

  // --------------------------------------------------------------- analyze

  function setAnalyzing(busy) {
    state.analyzing = busy;
    el.analyzeBtn.classList.toggle("is-busy", busy);
    el.analyzeBtn.disabled = busy || !el.urlInput.value.trim();
    el.analyzeBtn.querySelector(".btn-label").textContent = busy ? "Analyzing…" : "Analyze Video";
    el.results.setAttribute("aria-busy", String(busy));
  }

  function showView(view) {
    el.emptyState.hidden = view !== "empty";
    el.skeleton.hidden = view !== "loading";
    el.analyzeError.hidden = view !== "error";
    el.videoCard.hidden = view !== "result";
    el.optionsCard.hidden = view !== "result";
    if (view === "result") {
      for (const node of [el.videoCard, el.optionsCard]) {
        node.classList.remove("reveal");
        void node.offsetWidth;
        node.classList.add("reveal");
      }
    }
  }

  async function analyze() {
    const url = el.urlInput.value.trim();
    if (!url) {
      shake(el.urlField);
      el.urlInput.focus();
      return;
    }
    if (state.analyzeController) state.analyzeController.abort();
    const controller = new AbortController();
    const token = ++state.analyzeToken;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, ANALYZE_TIMEOUT);
    state.analyzeController = controller;
    setAnalyzing(true);
    showView("loading");

    try {
      const data = await api("/api/analyze", { method: "POST", body: { url }, signal: controller.signal });
      if (token !== state.analyzeToken) return;
      state.analysis = data;
      state.selection = { mp4: data.mp4.default, mp3: data.mp3.default };
      state.convertH264 = false;
      renderAnalysis(data);
      showView("result");
    } catch (error) {
      if (token !== state.analyzeToken) return;
      if (error.name === "AbortError") {
        if (!timedOut) return;
        showAnalyzeError(new ApiError({ title: "Taking too long", message: "YouTube didn't respond in time. Check your connection and try again." }, 0));
        return;
      }
      showAnalyzeError(error);
    } finally {
      clearTimeout(timer);
      if (token === state.analyzeToken) {
        state.analyzeController = null;
        setAnalyzing(false);
      }
    }
  }

  function showAnalyzeError(error) {
    state.analysis = null;
    el.analyzeErrorTitle.textContent = error.title || "Something went wrong";
    el.analyzeErrorMessage.textContent = error.message || "";
    el.analyzeErrorDetail.textContent = error.detail || "";
    el.analyzeErrorDetails.hidden = !error.detail;
    el.analyzeErrorDetails.open = false;
    el.retryAnalyzeBtn.hidden = error.retryable === false && error.status === 400;
    showView("error");
    if (["invalid_url", "unsupported_url", "empty_url"].includes(error.code)) {
      el.urlField.classList.add("is-invalid");
      setHint(error.message, true);
      shake(el.urlField);
    }
  }

  // ---------------------------------------------------------- video preview

  function renderAnalysis(data) {
    renderVideo(data.video);
    renderNotices(data);
    renderMp4(data.mp4);
    renderMp3(data.mp3);
    el.tabMp4.disabled = !data.mp4.available;
    el.tabMp3.disabled = !data.mp3.available;
    let format = state.format;
    if (format === "mp4" && !data.mp4.available) format = "mp3";
    if (format === "mp3" && !data.mp3.available) format = "mp4";
    setFormat(format);
  }

  function setThumbnail(img, urls) {
    const queue = urls.filter(Boolean);
    img.classList.remove("is-loaded");
    img.hidden = !queue.length;
    img.onload = () => img.classList.add("is-loaded");
    img.onerror = () => {
      const next = queue.shift();
      if (next) img.src = next; else img.hidden = true;
    };
    const first = queue.shift();
    if (first) img.src = first;
  }

  function renderVideo(video) {
    setThumbnail(el.thumb, [video.thumbnail, ...(video.thumbnail_fallbacks || [])]);
    el.durationBadge.textContent = fmtDuration(video.duration);
    el.shortBadge.hidden = !video.is_short;
    el.videoTitle.textContent = video.title;
    el.videoTitle.title = video.title;
    el.videoChannel.textContent = video.uploader;

    const meta = [
      ["Duration", video.duration ? fmtDuration(video.duration) : "—"],
      ["Views", fmtCount(video.view_count)],
      ["Likes", video.like_count !== null && video.like_count !== undefined ? fmtCount(video.like_count) : null],
      ["Uploaded", fmtDate(video.upload_date)],
      ["Best quality", video.max_quality || "—"],
    ];
    if (video.chapters) meta.push(["Chapters", String(video.chapters)]);
    el.videoMeta.replaceChildren(...meta.filter(([, value]) => value !== null).map(([label, value]) =>
      h("li", {}, h("span", { class: "meta-label", text: label }), h("span", { class: "meta-value", text: value }))));

    el.resolutionStrip.replaceChildren(...(video.resolutions || []).map((label, index) =>
      h("span", { class: `res-chip${index === 0 ? " is-top" : ""}${label.includes("HDR") ? " is-hdr" : ""}`, text: label })));
  }

  function renderNotices(data) {
    const items = [...(data.notices || [])];
    if (!data.ffmpeg_available) {
      items.unshift({ severity: "error", message: PUBLIC
        ? "FFmpeg isn't available on the server, so downloads are disabled."
        : "FFmpeg isn't installed, so downloads are disabled. Run start.bat or follow README.md to install it." });
    }
    if (data.mp3.available && data.mp3.encoder_available === false) {
      items.push({ severity: "warning", message: "Your FFmpeg build has no MP3 encoder, so MP3 downloads are unavailable." });
    }
    el.notices.replaceChildren(...items.map((item) =>
      h("div", { class: `notice notice-${item.severity === "error" ? "error" : item.severity === "info" ? "info" : "warning"}` },
        h("span", { icon: item.severity === "info" ? "info" : "warn" }), h("span", { text: item.message }))));
  }

  // ------------------------------------------------------------ MP4 options

  const PROCESSING = {
    copy: { cls: "chip-copy", icon: "check", text: "No conversion", tip: "Streams are copied into the MP4 as-is: fast and lossless." },
    audio: { cls: "chip-audio", icon: "audio", text: "Audio → AAC", tip: "Video is copied as-is; the audio is converted to AAC for MP4 compatibility (quick)." },
    video: { cls: "chip-video", icon: "convert", text: "Re-encode · slower", tip: `The video is re-encoded to H.264 ${WHERE}. This can take much longer than the download.` },
  };

  function chip({ cls, icon, text, tip }) {
    return h("span", { class: `q-chip ${cls}`, title: tip || null }, icon ? h("span", { icon }) : null, text);
  }

  function radioCard({ id, best = false, children, title }) {
    return h("button", {
      type: "button",
      role: "radio",
      class: `q-card${best ? " q-card-best" : ""}`,
      "aria-checked": "false",
      tabindex: "-1",
      "data-id": id,
      title: title || null,
    }, h("span", { class: "q-check", icon: "check" }), children);
  }

  function renderMp4(mp4) {
    el.mp4Grid.replaceChildren();
    el.convertToggle.setAttribute("aria-checked", String(state.convertH264));
    if (!mp4.available) {
      el.mp4Grid.append(h("p", { class: "panel-note", text: "No downloadable video formats were found for this video." }));
      el.convertRow.hidden = true;
      return;
    }
    const cards = [];
    if (mp4.best) {
      const b = mp4.best;
      cards.push(radioCard({
        id: "best",
        best: true,
        title: "Automatically picks the highest available resolution and frame rate.",
        children: [
          h("span", { class: "q-ribbon", text: "Recommended" }),
          h("span", { class: "q-title" }, h("span", { class: "q-best-title", text: "Best quality" })),
          h("span", { class: "q-meta", text: `${b.detail} · ${b.video_codec}` }),
          h("span", { class: "q-size", text: fmtSize(b.filesize, b.filesize_approx) }),
          h("span", { class: "q-footer" }),
        ],
      }));
    }
    for (const option of mp4.options) {
      const fps = option.fps && option.fps > 30 ? String(option.fps) : null;
      const name = option.badge || option.quality_name;
      const metaParts = [option.video_codec];
      if (option.fps) metaParts.push(`${option.fps} fps`);
      if (!option.has_audio) metaParts.push("no audio");
      const tip = [
        `${option.width || "?"}×${option.height || "?"}`,
        `Video: ${option.video_codec}${option.alternatives.length > 1 ? ` (also offered: ${option.alternatives.filter((c) => c !== option.video_codec).join(", ")})` : ""}`,
        option.audio_codec ? `Audio: ${option.audio_codec}` : "No audio track",
        option.progressive ? "Single file (video and audio together)" : "Separate video and audio streams, merged with FFmpeg",
      ].join("\n");
      cards.push(radioCard({
        id: option.id,
        title: tip,
        children: [
          h("span", { class: "q-title" },
            h("span", { class: "q-res" }, `${option.tier}p`, fps ? h("sup", { text: fps }) : null),
            name ? h("span", { class: "q-name", text: name }) : null),
          h("span", { class: "q-meta", text: metaParts.join(" · ") }),
          h("span", { class: "q-size", text: fmtSize(option.filesize, option.filesize_approx) }),
          h("span", { class: "q-footer" }),
        ],
      }));
    }
    cards.forEach((card, index) => { card.style.animationDelay = `${Math.min(index, 12) * 25}ms`; });
    el.mp4Grid.append(...cards);
    el.convertRow.hidden = !mp4.h264_conversion_available;
    updateMp4Cards();
  }

  function mp4OptionFor(id) {
    const mp4 = state.analysis && state.analysis.mp4;
    if (!mp4) return null;
    if (id === "best") {
      if (!mp4.best) return null;
      const target = mp4.options.find((o) => o.id === mp4.best.resolves_to);
      return target ? { ...target, isBest: true } : null;
    }
    return mp4.options.find((o) => o.id === id) || null;
  }

  function overSizeLimit(option) {
    const limit = state.analysis && state.analysis.limits && state.analysis.limits.max_file_bytes;
    // Re-encoded output has no predictable size, so only stream-copied options are checked here.
    const reencoded = state.convertH264 && option.video_codec_key !== "h264";
    return Boolean(limit && !reencoded && option.filesize && option.filesize > limit);
  }

  function bestConvertibleOption() {
    const mp4 = state.analysis.mp4;
    return mp4.options.find((o) => o.video_codec_key === "h264" || o.can_convert_h264) || null;
  }

  function updateMp4Cards() {
    const mp4 = state.analysis && state.analysis.mp4;
    if (!mp4 || !mp4.available) return;
    for (const card of el.mp4Grid.querySelectorAll(".q-card")) {
      let option = card.dataset.id === "best" ? mp4OptionFor("best") : mp4OptionFor(card.dataset.id);
      if (card.dataset.id === "best" && state.convertH264) option = bestConvertibleOption();
      const footer = card.querySelector(".q-footer");
      footer.replaceChildren();
      let disabled = false;
      if (!option) {
        disabled = true;
      } else if (state.convertH264 && option.video_codec_key !== "h264" && !option.can_convert_h264) {
        disabled = true;
        footer.append(chip({ cls: "chip-muted", text: option.hdr ? "HDR · can't convert" : "Too large to convert", tip: "H.264 conversion isn't offered for HDR video or resolutions above 2160p." }));
      } else if (overSizeLimit(option)) {
        disabled = true;
        footer.append(chip({ cls: "chip-muted", text: "Over size limit", tip: `Larger than this server's ${fmtBytes(state.analysis.limits.max_file_bytes)} file size limit.` }));
      } else {
        const kind = state.convertH264 ? (option.processing_h264 || option.processing) : option.processing;
        footer.append(chip(PROCESSING[kind] || PROCESSING.copy));
        if (option.hdr) footer.append(chip({ cls: "chip-hdr", text: "HDR", tip: "High dynamic range. Looks washed out on players without HDR support." }));
      }
      if (card.dataset.id === "best" && option) {
        const meta = card.querySelector(".q-meta");
        const codec = state.convertH264 ? "H.264" : option.video_codec;
        meta.textContent = `${option.label} · ${codec}`;
        card.querySelector(".q-size").textContent = state.convertH264 && option.video_codec_key !== "h264"
          ? "Size varies" : fmtSize(card.dataset.id === "best" && !state.convertH264 ? mp4.best.filesize : option.filesize,
            card.dataset.id === "best" && !state.convertH264 ? mp4.best.filesize_approx : option.filesize_approx);
      } else if (option) {
        card.querySelector(".q-size").textContent = state.convertH264 && option.video_codec_key !== "h264" && option.can_convert_h264
          ? "Size varies" : fmtSize(option.filesize, option.filesize_approx);
      }
      card.disabled = disabled;
    }
    const selected = state.selection.mp4;
    const selectedCard = el.mp4Grid.querySelector(`.q-card[data-id="${CSS.escape(selected || "")}"]`);
    if (!selectedCard || selectedCard.disabled) {
      const first = el.mp4Grid.querySelector(".q-card:not(:disabled)");
      state.selection.mp4 = first ? first.dataset.id : null;
    }
    syncRadioGroup(el.mp4Grid, state.selection.mp4);
    updateMp4Note();
  }

  function updateMp4Note() {
    const option = mp4OptionFor(state.selection.mp4);
    let note = "";
    if (state.convertH264) {
      note = `H.264 conversion re-encodes the video ${WHERE}. Expect it to take longer than the video's length for 1440p and 4K.`;
    } else if (option && ["vp9", "av1"].includes(option.video_codec_key)) {
      note = `${option.video_codec} video is kept as-is inside the MP4 (no quality loss). It plays in modern browsers, VLC and Windows Media Player (Windows may need the free ${option.video_codec} Video Extension from the Microsoft Store). Turn on H.264 conversion for older devices or video editors.`;
    } else if (option && option.processing === "audio") {
      note = "The audio track is converted to AAC so the MP4 plays everywhere. The video itself is not re-encoded.";
    }
    el.mp4Note.textContent = note;
  }

  // ------------------------------------------------------------ MP3 options

  function renderMp3(mp3) {
    el.mp3Grid.replaceChildren();
    el.mp3Source.replaceChildren();
    if (!mp3.available) {
      el.mp3Grid.append(h("p", { class: "panel-note", text: "This video has no audio track that can be converted to MP3." }));
      return;
    }
    const src = mp3.source || {};
    const items = [h("span", { class: "source-label", text: "Source audio" })];
    const addItem = (label, value) => { if (value) items.push(h("span", { class: "source-item" }, `${label} `, h("strong", { text: value }))); };
    addItem("Codec", src.codec);
    addItem("Bitrate", src.bitrate ? `${src.bitrate} kbps` : "unknown");
    addItem("Sample rate", src.sample_rate ? `${(src.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz` : null);
    addItem("Channels", src.channels ? (src.channels === 1 ? "Mono" : src.channels === 2 ? "Stereo" : `${src.channels} ch`) : null);
    el.mp3Source.replaceChildren(...items);

    const cards = mp3.options.map((option, index) => {
      const isBest = option.id === "best";
      const footer = h("span", { class: "q-footer" });
      if (option.above_source) {
        footer.append(chip({ cls: "chip-muted", text: "Above source bitrate", tip: "Makes a larger file, but can't add quality that isn't in the source audio." }));
      } else if (isBest) {
        footer.append(chip({ cls: "chip-copy", icon: "check", text: "Highest quality", tip: "LAME VBR V0, the standard high-quality MP3 setting." }));
      }
      const card = radioCard({
        id: option.id,
        best: isBest,
        title: option.detail,
        children: [
          isBest ? h("span", { class: "q-ribbon", text: "Recommended" }) : null,
          h("span", { class: "q-title" }, isBest
            ? h("span", { class: "q-best-title", text: option.label })
            : h("span", { class: "q-res" }, String(option.bitrate), h("sup", { text: "kbps" }))),
          h("span", { class: "q-meta", text: isBest ? `Variable bitrate · ~${option.approx_bitrate} kbps avg` : "Constant bitrate" }),
          h("span", { class: "q-size", text: fmtSize(option.filesize, option.filesize_approx) }),
          footer,
        ],
      });
      card.style.animationDelay = `${index * 25}ms`;
      if (mp3.encoder_available === false) card.disabled = true;
      return card;
    });
    el.mp3Grid.append(...cards);
    syncRadioGroup(el.mp3Grid, state.selection.mp3);
  }

  // ------------------------------------------------- selection & keyboard

  function syncRadioGroup(group, selectedId) {
    const cards = [...group.querySelectorAll(".q-card")];
    let focusable = null;
    for (const card of cards) {
      const checked = card.dataset.id === selectedId;
      card.setAttribute("aria-checked", String(checked));
      card.tabIndex = -1;
      if (checked) focusable = card;
    }
    const target = focusable || cards.find((c) => !c.disabled);
    if (target) target.tabIndex = 0;
  }

  function selectQuality(format, id, focus) {
    state.selection[format] = id;
    const group = format === "mp4" ? el.mp4Grid : el.mp3Grid;
    syncRadioGroup(group, id);
    if (focus) {
      const card = group.querySelector(`.q-card[data-id="${CSS.escape(id)}"]`);
      if (card) card.focus();
    }
    if (format === "mp4") updateMp4Note();
    updateDownloadBar();
  }

  function bindRadioGroup(group, format) {
    group.addEventListener("click", (event) => {
      const card = event.target.closest(".q-card");
      if (!card || card.disabled) return;
      selectQuality(format, card.dataset.id, false);
    });
    group.addEventListener("keydown", (event) => {
      const keys = ["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"];
      if (!keys.includes(event.key)) return;
      const cards = [...group.querySelectorAll(".q-card:not(:disabled)")];
      if (!cards.length) return;
      event.preventDefault();
      const current = cards.indexOf(document.activeElement);
      let next = current;
      if (event.key === "Home") next = 0;
      else if (event.key === "End") next = cards.length - 1;
      else if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (current + 1) % cards.length;
      else next = (current - 1 + cards.length) % cards.length;
      selectQuality(format, cards[next].dataset.id, true);
    });
  }

  function setFormat(format) {
    state.format = format;
    const isMp4 = format === "mp4";
    el.formatTabs.dataset.active = format;
    el.tabMp4.setAttribute("aria-selected", String(isMp4));
    el.tabMp3.setAttribute("aria-selected", String(!isMp4));
    el.tabMp4.tabIndex = isMp4 ? 0 : -1;
    el.tabMp3.tabIndex = isMp4 ? -1 : 0;
    el.panelMp4.hidden = !isMp4;
    el.panelMp3.hidden = isMp4;
    el.optionsSub.textContent = isMp4
      ? "Every resolution YouTube offers for this video."
      : "Audio only, converted to MP3.";
    updateDownloadBar();
  }

  function toggleConvert() {
    state.convertH264 = !state.convertH264;
    el.convertToggle.setAttribute("aria-checked", String(state.convertH264));
    updateMp4Cards();
    updateDownloadBar();
  }

  // ----------------------------------------------------------- download bar

  function describeSelection() {
    const data = state.analysis;
    if (!data) return null;
    if (state.format === "mp3") {
      const id = state.selection.mp3;
      const option = data.mp3.options.find((o) => o.id === id);
      if (!option || !data.mp3.available) return null;
      return {
        format: "mp3",
        quality: id,
        convert: false,
        title: `MP3 · ${option.label}`,
        details: [option.mode === "VBR" ? `VBR ~${option.approx_bitrate} kbps` : "CBR", fmtSize(option.filesize, option.filesize_approx)],
        slow: false,
      };
    }
    const id = state.selection.mp4;
    if (!id) return null;
    let option = mp4OptionFor(id);
    if (id === "best" && state.convertH264) option = bestConvertibleOption();
    if (!option) return null;
    const processing = state.convertH264 ? (option.processing_h264 || option.processing) : option.processing;
    const codec = processing === "video" ? "H.264" : option.video_codec;
    let size = fmtSize(option.filesize, option.filesize_approx);
    if (id === "best" && !state.convertH264) size = fmtSize(data.mp4.best.filesize, data.mp4.best.filesize_approx);
    if (processing === "video") size = "size varies";
    return {
      format: "mp4",
      quality: id,
      convert: state.convertH264,
      title: `MP4 · ${id === "best" ? `Best (${option.label})` : option.label}`,
      details: [codec, size],
      slow: processing === "video",
    };
  }

  function activeJobFor(selection) {
    if (!state.analysis || !selection) return null;
    const videoId = state.analysis.video.id;
    for (const entry of state.jobs.values()) {
      const d = entry.data;
      if (TERMINAL.has(d.status) || d.cancellable === false) continue;
      if (d.video_id === videoId && d.kind === selection.format && d.quality === selection.quality
        && Boolean(d.convert_h264) === Boolean(selection.format === "mp4" && selection.convert)) return entry;
    }
    return null;
  }

  function updateDownloadBar() {
    const data = state.analysis;
    if (!data) return;
    const selection = describeSelection();
    const label = el.downloadBtn.querySelector(".btn-label");
    if (!selection) {
      el.downloadSummary.replaceChildren("Choose a quality to continue.");
      el.downloadBtn.disabled = true;
      label.textContent = "Download";
      return;
    }
    const parts = [h("strong", { text: selection.title }), ` · ${selection.details.join(" · ")}`];
    if (selection.slow) parts.push(" · ", h("span", { class: "summary-warn", text: "re-encoding takes longer" }));
    el.downloadSummary.replaceChildren(...parts);

    const ffmpegOk = data.ffmpeg_available && (!state.health || state.health.tools.ffmpeg.available);
    const duplicate = activeJobFor(selection);
    el.downloadBtn.disabled = !ffmpegOk || state.starting || Boolean(duplicate);
    label.textContent = duplicate ? "Downloading…" : `Download ${selection.format.toUpperCase()}`;
    el.downloadBtn.title = !ffmpegOk ? "FFmpeg is required for downloads" : duplicate ? "This download is already in progress below" : "";
  }

  async function startDownload() {
    const selection = describeSelection();
    if (!selection || state.starting) return;
    state.starting = true;
    el.downloadBtn.classList.add("is-busy");
    updateDownloadBar();
    try {
      const response = await api("/api/download", {
        method: "POST",
        body: {
          url: state.analysis.video.url,
          format: selection.format,
          quality: selection.quality,
          convert_h264: selection.format === "mp4" && selection.convert,
        },
      });
      const entry = upsertJob(response.job);
      if (response.existing) {
        toast({ type: "info", title: "Already downloading", message: "That download is already in progress." });
      }
      highlightJob(entry);
      pollJob(entry.id, 250);
    } catch (error) {
      toastError(error);
    } finally {
      state.starting = false;
      el.downloadBtn.classList.remove("is-busy");
      updateDownloadBar();
    }
  }

  // ------------------------------------------------------------------- jobs

  function persistJobs() {
    store.set(STORAGE_JOBS, [...state.jobs.keys()].slice(0, 30));
  }

  function createJobElement() {
    const node = el.jobTemplate.content.firstElementChild.cloneNode(true);
    return node;
  }

  function upsertJob(data) {
    let entry = state.jobs.get(data.id);
    if (!entry) {
      entry = { id: data.id, data, node: createJobElement(), timer: null, failures: 0 };
      bindJobElement(entry);
      state.jobs.set(data.id, entry);
      el.jobsList.prepend(entry.node);
      persistJobs();
    }
    entry.data = data;
    renderJob(entry);
    updateJobsSection();
    return entry;
  }

  function bindJobElement(entry) {
    const node = entry.node;
    node.querySelector(".job-cancel").addEventListener("click", () => cancelJob(entry));
    node.querySelector(".job-retry").addEventListener("click", () => retryJob(entry));
    node.querySelector(".job-dismiss").addEventListener("click", () => removeJob(entry));
  }

  function highlightJob(entry) {
    el.jobsSection.hidden = false;
    entry.node.classList.add("is-highlight");
    entry.node.scrollIntoView({ behavior: "smooth", block: "nearest" });
    setTimeout(() => entry.node.classList.remove("is-highlight"), 1600);
  }

  function renderJob(entry) {
    const d = entry.data;
    const node = entry.node;
    const terminal = TERMINAL.has(d.status);
    node.dataset.status = d.status;

    node.querySelector(".job-title").textContent = d.title || `YouTube video ${d.video_id}`;
    node.querySelector(".job-title").title = d.title || "";
    node.querySelector(".job-label").textContent = d.label || `${d.kind.toUpperCase()} · ${d.quality === "best" ? "Best quality" : d.quality}`;
    node.querySelector(".job-kind").textContent = d.kind.toUpperCase();
    const img = node.querySelector(".job-thumb img");
    const thumb = d.thumbnail || `https://i.ytimg.com/vi/${encodeURIComponent(d.video_id)}/mqdefault.jpg`;
    if (img.dataset.src !== thumb) {
      img.dataset.src = thumb;
      setThumbnail(img, [thumb, `https://i.ytimg.com/vi/${encodeURIComponent(d.video_id)}/mqdefault.jpg`]);
    }

    let stageText = d.stage_label || "";
    if (d.stage_detail) stageText += ` — ${d.stage_detail}`;
    node.querySelector(".job-stage-text").textContent = stageText;

    const percent = d.status === "completed" ? 100 : d.percent;
    const showPercent = percent !== null && percent !== undefined && d.status !== "failed" && d.status !== "cancelled";
    node.querySelector(".job-percent").textContent = showPercent ? `${Math.floor(percent)}%` : "";
    const bar = node.querySelector(".progress");
    const fill = node.querySelector(".progress-fill");
    const indeterminate = !terminal && (percent === null || percent === undefined);
    bar.classList.toggle("is-indeterminate", indeterminate);
    if (!indeterminate) fill.style.width = `${Math.max(terminal && d.status !== "completed" ? (percent || 0) : 2, percent || 0)}%`;
    if (showPercent) bar.setAttribute("aria-valuenow", String(Math.floor(percent)));
    else bar.removeAttribute("aria-valuenow");
    bar.setAttribute("aria-label", stageText);

    renderJobStats(node.querySelector(".job-stats"), d);

    node.querySelector(".job-steps").replaceChildren(...(d.steps || []).map((step) =>
      h("li", { class: step.state === "pending" ? "" : `is-${step.state}`, text: step.label })));

    const save = node.querySelector(".job-save");
    save.hidden = d.status !== "completed" || !d.file;
    if (d.file) save.href = d.file.url;
    const cancelBtn = node.querySelector(".job-cancel");
    cancelBtn.hidden = terminal;
    cancelBtn.disabled = !d.cancellable;
    cancelBtn.textContent = d.cancellable ? "Cancel" : "Cancelling…";
    node.querySelector(".job-retry").hidden = !(d.status === "failed" || d.status === "cancelled") || d.lost;
    node.querySelector(".job-dismiss").hidden = !terminal;

    const errorBox = node.querySelector(".job-error");
    errorBox.hidden = d.status !== "failed" || !d.error;
    if (d.error) {
      errorBox.querySelector(".job-error-title").textContent = `${d.error.title}.`;
      errorBox.querySelector(".job-error-message").textContent = d.error.message || "";
      const details = errorBox.querySelector("details");
      details.hidden = !d.error.detail;
      details.querySelector("code").textContent = d.error.detail || "";
    }
  }

  function renderJobStats(container, d) {
    const items = [];
    const stat = (label, value) => h("span", {}, `${label} `, h("strong", { text: value }));
    if (d.status === "completed" && d.file) {
      items.push(stat("Saved as", d.file.name), stat("Size", fmtBytes(d.file.size)));
    } else if (!TERMINAL.has(d.status)) {
      const downloading = d.stage === "downloading_video" || d.stage === "downloading_audio";
      if (downloading && d.downloaded_bytes !== null && d.downloaded_bytes !== undefined) {
        const total = d.total_bytes ? `${d.total_is_estimate ? "≈ " : ""}${fmtBytes(d.total_bytes)}` : "unknown size";
        items.push(h("span", {}, h("strong", { text: fmtBytes(d.downloaded_bytes) }), ` of ${total}`));
        if (d.speed) items.push(stat("Speed", `${fmtBytes(d.speed)}/s`));
      }
      const eta = fmtEta(d.eta);
      if (eta) items.push(h("span", { text: eta }));
    }
    container.replaceChildren(...items);
  }

  function updateJobsSection() {
    const entries = [...state.jobs.values()];
    el.jobsSection.hidden = entries.length === 0;
    el.clearFinishedBtn.hidden = !entries.some((e) => TERMINAL.has(e.data.status));
    updateTitleProgress();
  }

  function updateTitleProgress() {
    const active = [...state.jobs.values()].filter((e) => !TERMINAL.has(e.data.status));
    if (!active.length) {
      document.title = BASE_TITLE;
      return;
    }
    const known = active.map((e) => e.data.percent).filter((p) => p !== null && p !== undefined);
    const avg = known.length ? Math.floor(known.reduce((a, b) => a + b, 0) / known.length) : null;
    document.title = avg === null ? `Working… · ${BASE_TITLE}` : `${avg}% · ${BASE_TITLE}`;
  }

  function pollJob(id, delay = POLL_INTERVAL) {
    const entry = state.jobs.get(id);
    if (!entry) return;
    clearTimeout(entry.timer);
    entry.timer = setTimeout(async () => {
      const previous = entry.data.status;
      try {
        const data = await api(`/api/progress/${encodeURIComponent(id)}`);
        if (!state.jobs.has(id)) return;
        entry.failures = 0;
        entry.data = data;
        renderJob(entry);
        if (TERMINAL.has(data.status)) {
          onJobFinished(entry, previous);
        } else {
          pollJob(id, POLL_INTERVAL);
        }
      } catch (error) {
        if (!state.jobs.has(id)) return;
        if (error.status === 404) {
          entry.data = {
            ...entry.data,
            status: "failed",
            stage_label: "Unavailable",
            stage_detail: null,
            cancellable: false,
            lost: true,
            steps: [],
            error: { title: "Download unavailable", message: "The app was restarted, so this download no longer exists. Start it again from the video above." },
          };
          renderJob(entry);
          updateJobsSection();
          updateDownloadBar();
          return;
        }
        entry.failures += 1;
        entry.node.querySelector(".job-stage-text").textContent = "Reconnecting to the app…";
        pollJob(id, Math.min(10000, 800 * 2 ** Math.min(entry.failures, 4)));
      }
      updateJobsSection();
      updateDownloadBar();
    }, delay);
  }

  function onJobFinished(entry, previousStatus) {
    const d = entry.data;
    updateJobsSection();
    updateDownloadBar();
    if (previousStatus === d.status || entry.restored) {
      entry.restored = false;
      return;
    }
    if (d.status === "completed" && d.file) {
      const link = h("a", { class: "btn btn-success btn-sm", href: d.file.url, download: true },
        h("span", { icon: "download" }), "Download File");
      toast({ type: "success", title: "Your file is ready", message: d.file.name, action: link, timeout: 12000 });
    } else if (d.status === "failed" && d.error) {
      toast({ type: "error", title: d.error.title, message: d.error.message, timeout: 10000 });
    } else if (d.status === "cancelled") {
      toast({ type: "info", title: "Download cancelled", message: d.title || "" });
    }
  }

  async function cancelJob(entry) {
    const button = entry.node.querySelector(".job-cancel");
    button.disabled = true;
    try {
      entry.data = await api(`/api/cancel/${encodeURIComponent(entry.id)}`, { method: "POST" });
      renderJob(entry);
      if (TERMINAL.has(entry.data.status)) onJobFinished(entry, "running");
      else pollJob(entry.id, 300);
    } catch (error) {
      button.disabled = false;
      toastError(error);
    }
  }

  async function retryJob(entry) {
    const d = entry.data;
    try {
      const response = await api("/api/download", {
        method: "POST",
        body: {
          url: `https://www.youtube.com/watch?v=${d.video_id}`,
          format: d.kind,
          quality: d.quality,
          convert_h264: Boolean(d.convert_h264),
        },
      });
      removeJob(entry, true);
      const fresh = upsertJob(response.job);
      highlightJob(fresh);
      pollJob(fresh.id, 250);
    } catch (error) {
      toastError(error);
    }
  }

  function removeJob(entry, immediate = false) {
    clearTimeout(entry.timer);
    state.jobs.delete(entry.id);
    persistJobs();
    const finish = () => { entry.node.remove(); updateJobsSection(); updateDownloadBar(); };
    if (immediate) { finish(); return; }
    entry.node.classList.add("is-leaving");
    setTimeout(finish, 250);
  }

  function clearFinished() {
    for (const entry of [...state.jobs.values()]) {
      if (TERMINAL.has(entry.data.status)) removeJob(entry);
    }
  }

  async function restoreJobs() {
    const ids = store.get(STORAGE_JOBS, []);
    if (!Array.isArray(ids)) return;
    for (const id of ids.slice().reverse()) {
      if (typeof id !== "string" || !/^[0-9a-f]{32}$/.test(id)) continue;
      try {
        const data = await api(`/api/progress/${id}`);
        const entry = upsertJob(data);
        entry.restored = TERMINAL.has(data.status);
        if (!TERMINAL.has(data.status)) pollJob(id, POLL_INTERVAL);
      } catch {
        /* job no longer exists on the server */
      }
    }
    persistJobs();
    updateJobsSection();
  }

  // ------------------------------------------------------------------- init

  function bindEvents() {
    el.themeToggle.addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      applyTheme(next, true);
    });
    el.form.addEventListener("submit", (event) => { event.preventDefault(); analyze(); });
    el.urlInput.addEventListener("input", onUrlInput);
    el.urlInput.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && el.urlInput.value) { event.preventDefault(); clearInput(); }
    });
    el.urlInput.addEventListener("paste", () => setTimeout(onUrlInput, 0));
    el.clearBtn.addEventListener("click", clearInput);
    el.pasteBtn.addEventListener("click", pasteFromClipboard);
    el.retryAnalyzeBtn.addEventListener("click", analyze);

    for (const tab of [el.tabMp4, el.tabMp3]) {
      tab.addEventListener("click", () => { if (!tab.disabled) setFormat(tab.dataset.format); });
      tab.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const other = tab === el.tabMp4 ? el.tabMp3 : el.tabMp4;
        if (other.disabled) return;
        setFormat(other.dataset.format);
        other.focus();
      });
    }
    bindRadioGroup(el.mp4Grid, "mp4");
    bindRadioGroup(el.mp3Grid, "mp3");
    el.convertToggle.addEventListener("click", toggleConvert);
    document.getElementById("convertLabel").addEventListener("click", toggleConvert);
    el.downloadBtn.addEventListener("click", startDownload);
    el.clearFinishedBtn.addEventListener("click", clearFinished);
  }

  function init() {
    applyTheme(document.documentElement.getAttribute("data-theme") || "dark", false);
    bindEvents();
    onUrlInput();
    showView("empty");
    loadHealth();
    restoreJobs();
    el.urlInput.focus();
  }

  init();
})();
