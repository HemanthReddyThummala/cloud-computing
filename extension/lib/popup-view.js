// Pure view models for the popup (testable without a DOM).

export const STEPS = [
  ["QUEUED", "Queued"],
  ["TRANSCRIBING", "Transcribing"],
  ["SUMMARIZING", "Summarizing"],
  ["EXTRACTING_FRAMES", "Extracting Frames"],
  ["PUBLISHING", "Publishing"],
  ["DONE", "Done"],
];

const ERRORS = {
  VIDEO_TOO_LONG: "The video is longer than 20 minutes.",
  VIDEO_DURATION_UNKNOWN: "The video's length could not be determined (for example a live stream).",
  YOUTUBE_VIDEO_UNAVAILABLE: "The video is private, deleted or unavailable.",
  YOUTUBE_AGE_RESTRICTED: "Age-restricted videos are not supported.",
  YOUTUBE_VIDEO_UNPLAYABLE: "The video cannot be played.",
  YOUTUBE_TRANSCRIPT_UNAVAILABLE: "The video has no usable transcript (captions are missing or disabled).",
  NOTION_PARENT_MISSING: "No Notion destination page is configured.",
  NOTION_UNAUTHORIZED: "Notion rejected the connection. Disconnect and connect Notion again.",
  RETRIES_EXHAUSTED: "Processing kept failing and was stopped after several attempts.",
  DEAD_LETTERED_BY_BROKER: "Processing crashed repeatedly and was stopped.",
};

const PROVIDERS = { NOTION: "Notion", GEMINI: "the AI model", YOUTUBE: "YouTube" };

/**
 * Readable text for a retry/last-error reason code such as GEMINI_RATE_LIMITED or NOTION_UNREACHABLE.
 * ``providerCode`` is the provider's sanitized status (e.g. Google's UNAVAILABLE), when the gateway has it.
 */
export function reasonText(code, providerCode = null) {
  if (typeof code !== "string") return "a temporary problem";
  const [prefix] = code.split("_");
  const who = PROVIDERS[prefix];
  if (!who) return code;
  // ADR-21: provider HTTP 5xx (GEMINI_HTTP_503 = Google UNAVAILABLE, 504 = DEADLINE_EXCEEDED)
  if (/_HTTP_503$/.test(code) || providerCode === "UNAVAILABLE") return `${who} is temporarily unavailable (overloaded)`;
  if (/_HTTP_504$/.test(code) || providerCode === "DEADLINE_EXCEEDED") return `${who} took too long to respond`;
  if (/_HTTP_5\d\d$/.test(code)) return `${who} had a temporary server problem`;
  if (/_(DAILY_LIMIT_REACHED|QUOTA_EXHAUSTED)$/.test(code)) return `${who}'s daily quota is used up`;
  if (/_(RATE_LIMITED|LOCAL_RATE_LIMIT|COOLDOWN_ACTIVE|SERVICE_OVERLOAD|BLOCKED)$/.test(code)) return `${who} is limiting requests`;
  if (/_(UNREACHABLE|TLS_ERROR|TLS_CONNECTION_DROPPED)$/.test(code)) return `${who} could not be reached`;
  if (/_(UNAVAILABLE|UNEXPECTED_HTTP_STATUS|UNEXPECTED_RESPONSE|TEMPORARY)$/.test(code)) return `${who} had a temporary problem`;
  return code;
}

const NOTION_HOST = /(^|\.)notion\.(so|site|com)$/;

/** The Notion page URL if it is a safe https link to a Notion host, else null. */
export function safeNotionUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && NOTION_HOST.test(url.hostname) ? url.href : null;
  } catch {
    return null;
  }
}

function clock(sec) {
  if (typeof sec !== "number" || !Number.isFinite(sec) || sec < 0) return null;
  const s = Math.floor(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function warningText(w) {
  const at = clock(w.timestampSec);
  if (w.code === "FRAME_EXTRACTION_FAILED") return at ? `The frame at ${at} could not be captured.` : "A frame could not be captured.";
  if (w.code === "FRAME_UPLOAD_FAILED") return at ? `The frame at ${at} could not be uploaded to Notion.` : "A frame could not be uploaded to Notion.";
  return `Warning: ${w.code ?? "unknown"}`;
}

function errorText(error) {
  if (!error) return "Unknown error.";
  const base = ERRORS[error.code] ?? error.message ?? `Error ${error.code ?? "unknown"}.`;
  if (error.code === "RETRIES_EXHAUSTED" && error.lastErrorCode) {
    const text = `${base} Last cause: ${reasonText(error.lastErrorCode, error.lastProviderCode)}.`;
    // A provider outage is not the video's fault: say that a later retry can succeed.
    return /_HTTP_5\d\d$/.test(error.lastErrorCode) ? `${text} Please try again later.` : text;
  }
  return base;
}

/** View model for one tracked job (chrome.storage.local["jobs"] entry). */
export function describeJob(job) {
  const title = job.title || `YouTube video ${job.videoId}`;
  const status = job.status;
  const done = status === "COMPLETED" || status === "COMPLETED_WITH_WARNINGS";
  const failed = status === "FAILED" || status === "DEAD_LETTERED" || status === "NOT_FOUND";
  const currentIndex = done ? STEPS.length - 1 : STEPS.findIndex(([s]) => s === (failed ? job.lastStage : status));
  const steps = STEPS.map(([, label], i) => {
    let state = "todo";
    if (done || i < currentIndex) state = "done";
    else if (i === currentIndex) state = failed ? "failed" : "current";
    return { label, state };
  });
  const view = { title, steps, tone: "busy", headline: "", details: [], link: null, finished: done || failed };

  if (status === "COMPLETED") {
    view.tone = "ok";
    view.headline = "Done";
  } else if (status === "COMPLETED_WITH_WARNINGS") {
    view.tone = "warn";
    view.headline = "Done, with warnings";
    const r = job.result ?? {};
    if (r.framesRequested !== null && r.framesRequested !== undefined) {
      view.details.push(`${r.framesPublished ?? 0} of ${r.framesRequested} frames were added.`);
    }
    view.details.push(...(job.warnings ?? []).map(warningText));
  } else if (status === "FAILED") {
    view.tone = "error";
    view.headline = "Failed";
    view.details.push(errorText(job.error));
  } else if (status === "DEAD_LETTERED") {
    view.tone = "error";
    view.headline = "Failed after retries";
    view.details.push(errorText(job.error));
  } else if (status === "NOT_FOUND") {
    view.tone = "error";
    view.headline = "Job not found";
    view.details.push("The gateway no longer knows this job.");
  } else {
    const label = STEPS[Math.max(0, currentIndex)][1];
    view.headline = typeof job.progress === "number" && status !== "QUEUED" ? `${label}… (${job.progress}%)` : `${label}…`;
    if (job.retry) {
      view.tone = "wait";
      const reason = reasonText(job.retry.reason);
      const attempt = job.retry.attempt && job.retry.maxAttempts ? ` (attempt ${job.retry.attempt} of ${job.retry.maxAttempts})` : "";
      view.details.push(`Waiting to retry: ${reason}${attempt}. Still processing.`);
    }
  }
  if (done) {
    const url = safeNotionUrl(job.result?.notionPageUrl);
    if (url) view.link = { href: url, label: "View in Notion" };
  }
  if (!view.finished && job.connection === "unreachable") view.details.push("Cannot reach the gateway; still trying.");
  else if (!view.finished && job.connection === "error") view.details.push("Status check failed; still trying.");
  return view;
}

export function describeConnection(status) {
  if (!status || status.ok === false) {
    return {
      text: "Notion status unavailable.",
      detail: status?.error?.message ?? null,
      button: null,
      tone: "error",
    };
  }
  if (status.signingIn) {
    return { text: "Waiting for Notion sign-in…", detail: "Finish the steps in the Notion window.", button: null, tone: "busy" };
  }
  if (status.connected) {
    const where = status.workspaceName ? `Connected to Notion: ${status.workspaceName}` : "Connected to Notion";
    return { text: where, detail: null, button: { action: "auth:signOut", label: "Disconnect" }, tone: "ok" };
  }
  return {
    text: "Not connected to Notion.",
    detail: status.lastError?.message ?? null,
    button: { action: "auth:signIn", label: "Connect Notion" },
    tone: status.lastError ? "error" : "idle",
  };
}
