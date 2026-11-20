// Popup: Notion connection (Connect/Disconnect) + progress of the tracked jobs. All work happens in the
// background service worker; the popup never sees the sealed token. It reads the display-only job list
// from chrome.storage.local["jobs"] and re-renders whenever storage changes (the background updates it
// while polling). Opening the popup also wakes the background, which resumes polling unfinished jobs.
import { describeConnection, describeJob } from "./lib/popup-view.js";

const section = document.getElementById("notion");
const text = document.getElementById("notion-text");
const detail = document.getElementById("notion-detail");
const button = document.getElementById("notion-button");
const jobsList = document.getElementById("jobs");
const jobsEmpty = document.getElementById("jobs-empty");

async function send(type) {
  try {
    return await chrome.runtime.sendMessage({ type });
  } catch {
    return { ok: false, error: { code: "EXTENSION_ERROR", message: "The extension background is not responding." } };
  }
}

function render(view) {
  section.dataset.tone = view.tone;
  text.textContent = view.text;
  detail.textContent = view.detail ?? "";
  detail.hidden = !view.detail;
  if (view.button) {
    button.textContent = view.button.label;
    button.dataset.action = view.button.action;
    button.hidden = false;
    button.disabled = false;
  } else {
    button.hidden = true;
  }
}

function el(tag, className, textContent) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (textContent !== undefined) node.textContent = textContent;
  return node;
}

function renderJob(job) {
  const view = describeJob(job);
  const item = el("li", "card job");
  item.dataset.tone = view.tone;
  item.append(el("p", "job-title", view.title));
  const steps = el("ol", "steps");
  for (const step of view.steps) {
    const li = el("li", `step step-${step.state}`, step.label);
    if (step.state === "current") li.setAttribute("aria-current", "step");
    steps.append(li);
  }
  item.append(steps, el("p", "job-headline", view.headline));
  for (const line of view.details) item.append(el("p", "detail", line));
  if (view.link) {
    const a = el("a", "notion-link", view.link.label);
    a.href = view.link.href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    item.append(a);
  }
  return item;
}

async function renderJobs() {
  let jobs = [];
  try {
    jobs = (await chrome.storage.local.get("jobs")).jobs ?? [];
  } catch {
    jobs = [];
  }
  jobsList.replaceChildren(...(Array.isArray(jobs) ? jobs : []).map(renderJob));
  jobsEmpty.hidden = jobsList.childElementCount > 0;
}

async function refresh() {
  render(describeConnection(await send("auth:status")));
  await renderJobs();
}

button.addEventListener("click", async () => {
  const action = button.dataset.action;
  button.disabled = true;
  if (action === "auth:signIn") render(describeConnection({ ok: true, signingIn: true }));
  const result = await send(action);
  if (result?.ok === false && action === "auth:signIn") {
    render(describeConnection({ ok: true, connected: false, lastError: result.error }));
    return;
  }
  await refresh();
});

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && Object.keys(changes).every((k) => k === "jobs" || k === "pendingSubmissions")) {
    renderJobs();
  } else {
    refresh();
  }
});

refresh();
