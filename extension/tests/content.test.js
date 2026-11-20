// YouTube content script (content.js) in a fake DOM: button creation/removal, SPA navigation,
// duplicate prevention, submission messages and their outcomes.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { beforeEach, test } from "node:test";
import vm from "node:vm";

import { FakeDocument, FakeWindow, buildPlayer } from "./fake-dom.js";

// Load the real content script as a classic script. Without a `chrome` global it does not start itself.
const source = readFileSync(new URL("../content.js", import.meta.url), "utf8");
const context = vm.createContext({ URL, console });
vm.runInContext(source, context, { filename: "content.js" });
const { createTakeNotes } = context;

const A = "https://www.youtube.com/watch?v=aaaaaaaaaaa";
const B = "https://www.youtube.com/watch?v=bbbbbbbbbbb&list=PL123&t=42s";

let doc;
let win;
let sent;
let respond;
let intervals;
let ui;

function setup(href, { player = true } = {}) {
  doc = new FakeDocument();
  win = new FakeWindow(href);
  sent = [];
  respond = async () => ({ ok: true, jobId: "j1", status: "QUEUED", videoId: "x", alreadyTracking: false });
  intervals = [];
  if (player) buildPlayer(doc);
  ui = createTakeNotes({
    doc, win,
    sendMessage: async (msg) => { sent.push(msg); return respond(msg); },
    setIntervalFn: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length; },
    clearIntervalFn: () => {},
  });
  ui.start();
}

const buttons = () => doc.querySelectorAll("#vna-take-notes");
const button = () => doc.getElementById("vna-take-notes");
function navigate(href, event = "yt-navigate-finish") {
  win.location.href = href;
  if (event === "popstate") win.dispatch("popstate");
  else if (event) doc.dispatch(event);
}
const tickWatchdog = () => intervals.forEach(({ fn }) => fn());

beforeEach(() => setup(A));

test("watch page: one Take Notes button, first in the player toolbar", () => {
  assert.equal(buttons().length, 1);
  const b = button();
  assert.equal(b.textContent, "Take Notes");
  assert.equal(b.disabled, false);
  assert.equal(b.dataset.videoId, "aaaaaaaaaaa");
  assert.ok(b.parentNode.matches(".ytp-right-controls"));
  assert.equal(b.parentNode.children[0], b);
  assert.match(b.className, /vna-take-notes/);
  assert.equal(intervals[0].ms, 1000);
});

test("repeated navigation events and watchdog ticks never duplicate the button", () => {
  for (let i = 0; i < 5; i += 1) {
    doc.dispatch("yt-navigate-finish");
    win.dispatch("popstate");
    tickWatchdog();
  }
  assert.equal(buttons().length, 1);
});

test("SPA navigation to another video updates the same single button", () => {
  const first = button();
  navigate(B);
  assert.equal(buttons().length, 1);
  assert.equal(button(), first);
  assert.equal(button().dataset.videoId, "bbbbbbbbbbb");
});

test("navigating away from a video removes the button; coming back restores it", () => {
  for (const href of ["https://www.youtube.com/", "https://www.youtube.com/results?search_query=x",
    "https://www.youtube.com/shorts/aaaaaaaaaaa", "https://www.youtube.com/watch?v=short"]) {
    navigate(href);
    assert.equal(buttons().length, 0, href);
  }
  navigate(A, "popstate");
  assert.equal(buttons().length, 1);
});

test("navigation missed by events is caught by the watchdog", () => {
  navigate("https://www.youtube.com/feed/subscriptions", null);
  assert.equal(buttons().length, 1);
  tickWatchdog();
  assert.equal(buttons().length, 0);
});

test("player built after the script started: the button appears once the toolbar exists", () => {
  setup(A, { player: false });
  assert.equal(buttons().length, 0);
  tickWatchdog();
  assert.equal(buttons().length, 0);
  buildPlayer(doc);
  tickWatchdog();
  assert.equal(buttons().length, 1);
});

test("player rebuilt with a new toolbar: the button moves, never duplicates", () => {
  doc.getElementById("movie_player").remove();
  const { toolbar } = buildPlayer(doc);
  tickWatchdog();
  assert.equal(buttons().length, 1);
  assert.equal(button().parentNode, toolbar);
});

test("click submits the canonical URL of the current video once", async () => {
  navigate(B);
  await button().click();
  // JSON copy: objects created inside the vm sandbox have that realm's prototypes.
  assert.deepEqual(JSON.parse(JSON.stringify(sent)),
    [{ type: "job:submit", youtubeUrl: "https://www.youtube.com/watch?v=bbbbbbbbbbb" }]);
  assert.equal(button().textContent, "Notes requested ✓");
  assert.equal(button().disabled, true);
  assert.match(button().title, /popup/);
  await button().click();
  await ui.onClick({ currentTarget: button() }); // even a forced second click does nothing
  assert.equal(sent.length, 1);
});

test("double click while submitting sends one message", async () => {
  let release;
  respond = () => new Promise((resolve) => { release = () => resolve({ ok: true, alreadyTracking: false }); });
  const first = ui.onClick({ currentTarget: button() });
  assert.equal(button().textContent, "Submitting…");
  assert.equal(button().disabled, true);
  const second = ui.onClick({ currentTarget: button() });
  release();
  await Promise.all([first, second]);
  assert.equal(sent.length, 1);
});

test("Notion not connected: clear message, button stays usable", async () => {
  respond = async () => ({ ok: false, error: { code: "NOT_SIGNED_IN", message: "Connect Notion first." } });
  await button().click();
  assert.equal(button().dataset.state, "error");
  assert.equal(button().disabled, false);
  assert.match(button().title, /Connect Notion/);
  respond = async () => ({ ok: true, alreadyTracking: false });
  await button().click();
  assert.equal(sent.length, 2);
  assert.equal(button().textContent, "Notes requested ✓");
});

test("gateway errors are shown from the background's message", async () => {
  respond = async () => ({ ok: false, error: { code: "GATEWAY_UNREACHABLE", message: "Cannot reach the Video Notes gateway." } });
  await button().click();
  assert.equal(button().textContent, "Take Notes (retry)");
  assert.equal(button().title, "Cannot reach the Video Notes gateway.");
});

test("extension reloaded (sendMessage throws): readable error, no crash", async () => {
  respond = async () => { throw new Error("Extension context invalidated."); };
  await button().click();
  assert.equal(button().dataset.state, "error");
  assert.match(button().title, /Reload the page/);
});

test("job already running for this video -> 'Notes in progress'", async () => {
  respond = async () => ({ ok: true, alreadyTracking: true });
  await button().click();
  assert.equal(button().textContent, "Notes in progress");
  assert.equal(button().disabled, true);
});

test("per-video state survives SPA navigation; a response for the old video does not touch the new one", async () => {
  let release;
  respond = () => new Promise((resolve) => { release = () => resolve({ ok: true, alreadyTracking: false }); });
  const pending = ui.onClick({ currentTarget: button() });
  navigate(B);
  assert.equal(button().textContent, "Take Notes");
  release();
  await pending;
  assert.equal(button().textContent, "Take Notes"); // still video B, idle
  navigate(A, "popstate");
  assert.equal(button().textContent, "Notes requested ✓");
});

test("stop() removes the button", () => {
  ui.stop();
  assert.equal(buttons().length, 0);
});
