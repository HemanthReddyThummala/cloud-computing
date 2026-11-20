// YouTube content script (classic script; runs on www.youtube.com, ADR-20).
// Injects ONE "Take Notes" button into the player's toolbar on watch pages, keeps it in sync with
// YouTube's single-page navigation and asks the background to submit the current video.
// It never sees the Notion token: it only sends {type: "job:submit", youtubeUrl}.

/* exported createTakeNotes */
function createTakeNotes({ doc, win, sendMessage, setIntervalFn = setInterval, clearIntervalFn = clearInterval }) {
  const BUTTON_ID = "vna-take-notes";
  const TOOLBAR = "#movie_player .ytp-right-controls";
  const VIDEO_ID = /^[A-Za-z0-9_-]{11}$/;
  const LABELS = {
    idle: "Take Notes",
    submitting: "Submitting…",
    queued: "Notes requested ✓",
    tracking: "Notes in progress",
    error: "Take Notes (retry)",
  };
  const states = new Map(); // videoId -> {state, message}: survives SPA navigation within the tab
  let watchdog = null;

  function currentVideoId() {
    let url;
    try {
      url = new URL(win.location.href);
    } catch {
      return null;
    }
    if (url.pathname !== "/watch") return null;
    const id = url.searchParams.get("v");
    return id && VIDEO_ID.test(id) ? id : null;
  }

  function render(button, videoId) {
    const { state, message } = states.get(videoId) ?? { state: "idle", message: null };
    button.dataset.videoId = videoId;
    button.dataset.state = state;
    button.textContent = LABELS[state];
    button.disabled = state === "submitting" || state === "queued" || state === "tracking";
    const hint = message ?? (state === "idle" ? "Create Notion notes for this video" : "Open the Video Notes Agent popup to follow progress");
    button.title = hint;
    button.setAttribute("aria-label", `${LABELS[state]}. ${hint}`);
  }

  async function onClick(event) {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    const button = event?.currentTarget ?? doc.getElementById(BUTTON_ID);
    const videoId = currentVideoId();
    if (!videoId) return;
    const current = states.get(videoId)?.state;
    if (current === "submitting" || current === "queued" || current === "tracking") return; // no double submit
    states.set(videoId, { state: "submitting", message: null });
    if (button) render(button, videoId);
    let response;
    try {
      response = await sendMessage({ type: "job:submit", youtubeUrl: `https://www.youtube.com/watch?v=${videoId}` });
    } catch {
      response = { ok: false, error: { code: "EXTENSION_ERROR", message: "The extension is not responding. Reload the page and try again." } };
    }
    if (response?.ok) {
      states.set(videoId, { state: response.alreadyTracking ? "tracking" : "queued", message: null });
    } else {
      const error = response?.error ?? { code: "EXTENSION_ERROR", message: "The extension is not responding." };
      const message = error.code === "NOT_SIGNED_IN"
        ? "Connect Notion first: click the Video Notes Agent icon, then Connect Notion."
        : error.message;
      states.set(videoId, { state: "error", message });
    }
    const live = doc.getElementById(BUTTON_ID);
    if (live && live.dataset.videoId === videoId) render(live, videoId);
  }

  /** Make the DOM match the current URL: one button on watch pages, none elsewhere. */
  function sync() {
    const videoId = currentVideoId();
    let button = doc.getElementById(BUTTON_ID);
    if (!videoId) {
      button?.remove();
      return;
    }
    const toolbar = doc.querySelector(TOOLBAR);
    if (!toolbar) return; // player not built yet; the watchdog tries again
    if (button && button.parentNode !== toolbar) {
      button.remove();
      button = null;
    }
    if (!button) {
      button = doc.createElement("button");
      button.id = BUTTON_ID;
      button.type = "button";
      button.className = "ytp-button vna-take-notes";
      button.addEventListener("click", onClick);
      toolbar.prepend(button);
    }
    render(button, videoId);
  }

  function start() {
    doc.addEventListener("yt-navigate-finish", sync); // YouTube SPA navigation finished
    win.addEventListener("popstate", sync);
    watchdog = setIntervalFn(sync, 1000); // late player build / missed events
    sync();
  }

  function stop() {
    doc.removeEventListener?.("yt-navigate-finish", sync);
    win.removeEventListener?.("popstate", sync);
    if (watchdog !== null) clearIntervalFn(watchdog);
    doc.getElementById(BUTTON_ID)?.remove();
  }

  return { start, stop, sync, onClick, currentVideoId };
}

if (typeof chrome !== "undefined" && chrome.runtime?.id && typeof document !== "undefined") {
  createTakeNotes({ doc: document, win: window, sendMessage: (msg) => chrome.runtime.sendMessage(msg) }).start();
}
