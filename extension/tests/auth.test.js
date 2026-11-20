// Extension Notion sign-in (ADR-18) against a local fake gateway with real fetch. Run: npm test
import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test } from "node:test";

import { AUTH_KEY, LAST_ERROR_KEY, createAuthService, createMessageHandler, parseRedirect } from "../lib/auth.js";
import { EXTENSION_ID, REDIRECT_URI, SEALED, fakeChrome, recordingLogger, startFakeGateway } from "./support.js";

let gw;
let logger;

function service(chrome, overrides = {}) {
  return createAuthService({ chrome, fetch, baseUrl: gw.baseUrl, redirectPath: "notion", timeoutMs: 2000, logger,
    ...overrides });
}

async function rejectsWith(promise, code) {
  await assert.rejects(promise, (err) => {
    assert.equal(err.code, code);
    return true;
  });
}

const stored = (chrome) => chrome.storage.local.data.get(AUTH_KEY);
const lastError = (chrome) => chrome.storage.session.data.get(LAST_ERROR_KEY);

beforeEach(async () => {
  gw = await startFakeGateway();
  logger = recordingLogger();
});
afterEach(async () => {
  await gw.close();
});

describe("successful sign-in", () => {
  test("authorize -> launchWebAuthFlow -> callback -> only the sealed token is stored", async () => {
    const { chrome, calls } = fakeChrome();
    const status = await service(chrome).signIn();

    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize", "/oauth/notion/callback"]);
    const [details] = calls.launch;
    assert.equal(details.interactive, true);
    const state = new URL(details.url).searchParams.get("state");
    assert.deepEqual(gw.requests[1].body, { state, code: "good-code" });
    assert.equal(gw.requests[1].headers["content-type"], "application/json");

    const auth = stored(chrome);
    assert.deepEqual(Object.keys(auth).sort(),
      ["botId", "connectedAt", "sealedToken", "tokenFormat", "workspaceId", "workspaceName"]);
    assert.equal(auth.sealedToken, SEALED);
    assert.equal(auth.workspaceName, "Test Workspace");
    const everything = JSON.stringify([...chrome.storage.local.data, ...chrome.storage.session.data]);
    assert.ok(!everything.includes("good-code") && !everything.includes(state), "code/state never stored");

    assert.equal(status.connected, true);
    assert.equal(status.workspaceName, "Test Workspace");
    assert.ok(!JSON.stringify(status).includes(SEALED), "status never exposes the sealed token");
    assert.equal(lastError(chrome), undefined);
  });

  test("logs contain no code, state or token", async () => {
    const { chrome } = fakeChrome();
    await service(chrome).signIn();
    const text = logger.lines.join("\n");
    assert.ok(!text.includes("good-code") && !text.includes(SEALED) && !text.includes("state-1"));
    assert.match(text, /Notion connected/);
  });
});

describe("cancellation and OAuth errors", () => {
  test("closing the Notion window -> SIGN_IN_CANCELLED, no callback, nothing stored", async () => {
    const { chrome } = fakeChrome({ launch: async () => { throw new Error("The user did not approve access."); } });
    await rejectsWith(service(chrome).signIn(), "SIGN_IN_CANCELLED");
    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize"]);
    assert.equal(stored(chrome), undefined);
    assert.equal(lastError(chrome).code, "SIGN_IN_CANCELLED");
  });

  test("other launchWebAuthFlow failures -> SIGN_IN_FAILED", async () => {
    const { chrome } = fakeChrome({ launch: async () => { throw new Error("Authorization page could not be loaded."); } });
    await rejectsWith(service(chrome).signIn(), "SIGN_IN_FAILED");
  });

  test("Notion denial is forwarded as {state, error} and reported", async () => {
    const { chrome } = fakeChrome({
      launch: async ({ url }) => `${REDIRECT_URI}?error=access_denied&state=${new URL(url).searchParams.get("state")}`,
    });
    await rejectsWith(service(chrome).signIn(), "OAUTH_ACCESS_DENIED");
    assert.deepEqual(Object.keys(gw.requests[1].body).sort(), ["error", "state"]);
    assert.equal(stored(chrome), undefined);
    assert.equal(lastError(chrome).message, "Notion access was not granted.");
  });

  test("a redirect with another state is never forwarded", async () => {
    const { chrome } = fakeChrome({ launch: async () => `${REDIRECT_URI}?code=stolen-code&state=someone-else` });
    await rejectsWith(service(chrome).signIn(), "STATE_MISMATCH");
    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize"]);
  });

  test("a denial without our state is reported locally", async () => {
    const { chrome } = fakeChrome({ launch: async () => `${REDIRECT_URI}?error=access_denied` });
    await rejectsWith(service(chrome).signIn(), "OAUTH_ACCESS_DENIED");
    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize"]);
  });

  test("redirect without code -> gateway OAUTH_CODE_MISSING", async () => {
    const { chrome } = fakeChrome({ launch: async ({ url }) => `${REDIRECT_URI}?state=${new URL(url).searchParams.get("state")}` });
    await rejectsWith(service(chrome).signIn(), "OAUTH_CODE_MISSING");
  });

  test("expired / used state -> OAUTH_STATE_INVALID with a reconnect message", async () => {
    gw.close();
    gw = await startFakeGateway({ callback: () => ({ status: 400, body: { error: { code: "OAUTH_STATE_INVALID", message: "x" } } }) });
    const { chrome } = fakeChrome();
    await rejectsWith(service(chrome).signIn(), "OAUTH_STATE_INVALID");
    assert.match(lastError(chrome).message, /expired or was already used/);
  });

  test("exchange failure and Notion rate limit (Retry-After kept)", async () => {
    for (const [code, expected] of [["expired", "OAUTH_EXCHANGE_FAILED"], ["ratelimit", "NOTION_RATE_LIMITED"]]) {
      const { chrome } = fakeChrome({
        launch: async ({ url }) => `${REDIRECT_URI}?code=${code}&state=${new URL(url).searchParams.get("state")}`,
      });
      await assert.rejects(service(chrome).signIn(), (err) => {
        assert.equal(err.code, expected);
        if (expected === "NOTION_RATE_LIMITED") assert.equal(err.retryAfterSec, 7);
        return true;
      });
      assert.equal(stored(chrome), undefined);
    }
  });

  test("gateway redirect URI that differs from this extension -> REDIRECT_MISMATCH before any window", async () => {
    gw.close();
    gw = await startFakeGateway({
      authorize: () => ({ body: { authorization_url: "https://notion.example.test/x", state: "s",
        redirect_uri: "https://other.chromiumapp.org/notion", expires_in: 600 } }),
    });
    const { chrome, calls } = fakeChrome();
    await rejectsWith(service(chrome).signIn(), "REDIRECT_MISMATCH");
    assert.equal(calls.launch.length, 0);
  });

  test("a returned URL that is not our redirect URI -> REDIRECT_MISMATCH", async () => {
    const { chrome } = fakeChrome({ launch: async () => "https://evil.example/notion?code=x&state=y" });
    await rejectsWith(service(chrome).signIn(), "REDIRECT_MISMATCH");
    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize"]);
  });
});

describe("gateway connection errors and invalid responses", () => {
  test("gateway down -> GATEWAY_UNREACHABLE", async () => {
    const { baseUrl } = gw;
    await gw.close();
    const { chrome } = fakeChrome();
    await rejectsWith(service(chrome, { baseUrl }).signIn(), "GATEWAY_UNREACHABLE");
    assert.equal(lastError(chrome).code, "GATEWAY_UNREACHABLE");
    gw = await startFakeGateway(); // for afterEach
  });

  test("gateway not answering -> GATEWAY_TIMEOUT", async () => {
    gw.close();
    gw = await startFakeGateway({ authorize: () => ({ hang: true }) });
    const { chrome } = fakeChrome();
    await rejectsWith(service(chrome, { timeoutMs: 300 }).signIn(), "GATEWAY_TIMEOUT");
  });

  const invalid = {
    "authorize HTML": { authorize: () => ({ raw: "<html>proxy</html>", headers: { "Content-Type": "text/html" } }) },
    "authorize without state": { authorize: () => ({ body: { authorization_url: "https://n.test/x", redirect_uri: REDIRECT_URI } }) },
    "authorize non-https URL": { authorize: () => ({ body: { authorization_url: "javascript:alert(1)", state: "s", redirect_uri: REDIRECT_URI } }) },
    "callback raw (unsealed) token": { callback: () => ({ body: { notion_token: "ntn_raw_token", token_format: "raw" } }) },
    "callback array": { callback: () => ({ body: [] }) },
    "500 without error shape": { callback: () => ({ status: 500, raw: "Internal Server Error" }) },
  };
  for (const [name, behaviour] of Object.entries(invalid)) {
    test(`invalid response: ${name} -> GATEWAY_INVALID_RESPONSE, nothing stored`, async () => {
      gw.close();
      gw = await startFakeGateway(behaviour);
      const { chrome, calls } = fakeChrome();
      await rejectsWith(service(chrome).signIn(), "GATEWAY_INVALID_RESPONSE");
      assert.equal(stored(chrome), undefined);
      if (name.startsWith("authorize")) assert.equal(calls.launch.length, 0);
    });
  }

  test("gateway OAUTH_NOT_CONFIGURED is reported as such", async () => {
    gw.close();
    gw = await startFakeGateway({ authorize: () => ({ status: 503, body: { error: { code: "OAUTH_NOT_CONFIGURED", message: "x" } } }) });
    const { chrome } = fakeChrome();
    await rejectsWith(service(chrome).signIn(), "OAUTH_NOT_CONFIGURED");
  });
});

describe("repeated sign-in and sign-out", () => {
  test("concurrent sign-in clicks share one attempt (one authorize, one window)", async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const { chrome, calls } = fakeChrome({
      launch: async ({ url }) => {
        await gate;
        return `${REDIRECT_URI}?code=good-code&state=${new URL(url).searchParams.get("state")}`;
      },
    });
    const auth = service(chrome);
    const first = auth.signIn();
    const second = auth.signIn();
    await new Promise((r) => setTimeout(r, 50));
    assert.equal((await auth.status()).signingIn, true);
    release();
    const [a, b] = await Promise.all([first, second]);
    assert.equal(a.connected && b.connected, true);
    assert.equal(calls.launch.length, 1);
    assert.deepEqual(gw.paths(), ["/oauth/notion/authorize", "/oauth/notion/callback"]);
    assert.equal((await auth.status()).signingIn, false);
  });

  test("a new attempt after a failure starts fresh and clears the old error", async () => {
    let cancel = true;
    const { chrome } = fakeChrome({
      launch: async ({ url }) => {
        if (cancel) throw new Error("The user did not approve access.");
        return `${REDIRECT_URI}?code=good-code&state=${new URL(url).searchParams.get("state")}`;
      },
    });
    const auth = service(chrome);
    await rejectsWith(auth.signIn(), "SIGN_IN_CANCELLED");
    cancel = false;
    const status = await auth.signIn();
    assert.equal(status.connected, true);
    assert.equal(status.lastError, null);
    assert.equal(gw.paths().filter((p) => p === "/oauth/notion/authorize").length, 2);
  });

  test("a failed re-sign-in keeps the existing connection", async () => {
    let fail = false;
    const { chrome } = fakeChrome({
      launch: async ({ url }) => {
        if (fail) throw new Error("The user did not approve access.");
        return `${REDIRECT_URI}?code=good-code&state=${new URL(url).searchParams.get("state")}`;
      },
    });
    const auth = service(chrome);
    await auth.signIn();
    fail = true;
    await rejectsWith(auth.signIn(), "SIGN_IN_CANCELLED");
    const status = await auth.status();
    assert.equal(status.connected, true);
    assert.equal(status.lastError.code, "SIGN_IN_CANCELLED");
  });

  test("sign-out removes the connection; a sign-in finishing after sign-out is discarded", async () => {
    const { chrome } = fakeChrome();
    const auth = service(chrome);
    await auth.signIn();
    assert.equal((await auth.signOut()).connected, false);
    assert.equal(stored(chrome), undefined);

    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const { chrome: chrome2 } = fakeChrome({
      launch: async ({ url }) => {
        await gate;
        return `${REDIRECT_URI}?code=good-code&state=${new URL(url).searchParams.get("state")}`;
      },
    });
    const auth2 = service(chrome2);
    const pending = auth2.signIn();
    await new Promise((r) => setTimeout(r, 50));
    await auth2.signOut();
    release();
    await rejectsWith(pending, "SIGN_IN_CANCELLED");
    assert.equal(stored(chrome2), undefined);
  });
});

describe("job submission uses the sealed token", () => {
  test("POST /jobs carries the stored sealed token; the result has no token", async () => {
    const { chrome } = fakeChrome();
    const auth = service(chrome);
    await auth.signIn();
    const job = await auth.submitJob({ youtubeUrl: "https://www.youtube.com/watch?v=dQw4w9WgXcQ" });
    const req = gw.requests.at(-1);
    assert.equal(req.path, "/jobs");
    assert.deepEqual(req.body, { youtube_url: "https://www.youtube.com/watch?v=dQw4w9WgXcQ", notion_token: SEALED });
    assert.deepEqual(job, { jobId: "11111111-2222-3333-4444-555555555555", status: "QUEUED", videoId: "dQw4w9WgXcQ" });
  });

  test("optional notion_parent_id is passed through", async () => {
    const { chrome } = fakeChrome();
    const auth = service(chrome);
    await auth.signIn();
    await auth.submitJob({ youtubeUrl: "https://youtu.be/dQw4w9WgXcQ", notionParentId: "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d" });
    assert.equal(gw.requests.at(-1).body.notion_parent_id, "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d");
  });

  test("not signed in -> NOT_SIGNED_IN without any request", async () => {
    const { chrome } = fakeChrome();
    await rejectsWith(service(chrome).submitJob({ youtubeUrl: "https://youtu.be/dQw4w9WgXcQ" }), "NOT_SIGNED_IN");
    assert.equal(gw.requests.length, 0);
  });

  test("gateway rejects the sealed token -> connection cleared, NOTION_RECONNECT_REQUIRED", async () => {
    gw.close();
    gw = await startFakeGateway({ jobs: () => ({ status: 400, body: { error: { code: "INVALID_NOTION_TOKEN", message: "x" } } }) });
    const { chrome } = fakeChrome();
    const auth = service(chrome);
    await auth.signIn();
    await rejectsWith(auth.submitJob({ youtubeUrl: "https://youtu.be/dQw4w9WgXcQ" }), "NOTION_RECONNECT_REQUIRED");
    assert.equal(stored(chrome), undefined);
  });

  test("other gateway errors are passed through and keep the connection", async () => {
    gw.close();
    gw = await startFakeGateway({ jobs: () => ({ status: 400, body: { error: { code: "INVALID_YOUTUBE_URL", message: "x" } } }) });
    const { chrome } = fakeChrome();
    const auth = service(chrome);
    await auth.signIn();
    await rejectsWith(auth.submitJob({ youtubeUrl: "nope" }), "INVALID_YOUTUBE_URL");
    assert.ok(stored(chrome));
  });

  test("a tampered stored value (not sealed) is treated as not signed in", async () => {
    const { chrome } = fakeChrome();
    await chrome.storage.local.set({ [AUTH_KEY]: { sealedToken: "ntn_raw_token" } });
    const auth = service(chrome);
    assert.equal((await auth.status()).connected, false);
    await rejectsWith(auth.submitJob({ youtubeUrl: "https://youtu.be/dQw4w9WgXcQ" }), "NOT_SIGNED_IN");
  });
});

describe("message handler (background <-> popup)", () => {
  test("own messages are handled; responses never contain the sealed token", async () => {
    const { chrome } = fakeChrome();
    const handle = createMessageHandler(service(chrome), EXTENSION_ID);
    const me = { id: EXTENSION_ID };
    const signedIn = await handle({ type: "auth:signIn" }, me);
    assert.equal(signedIn.ok, true);
    assert.equal(signedIn.workspaceName, "Test Workspace");
    const status = await handle({ type: "auth:status" }, me);
    const job = await handle({ type: "job:submit", youtubeUrl: "https://youtu.be/dQw4w9WgXcQ" }, me);
    assert.equal(job.ok, true);
    for (const r of [signedIn, status, job]) assert.ok(!JSON.stringify(r).includes(SEALED));
    assert.equal((await handle({ type: "auth:signOut" }, me)).connected, false);
  });

  test("errors become {ok:false, error:{code,message}}", async () => {
    const { chrome } = fakeChrome({ launch: async () => { throw new Error("The user did not approve access."); } });
    const handle = createMessageHandler(service(chrome), EXTENSION_ID);
    assert.deepEqual(await handle({ type: "auth:signIn" }, { id: EXTENSION_ID }),
      { ok: false, error: { code: "SIGN_IN_CANCELLED", message: "Notion sign-in was cancelled." } });
  });

  test("foreign senders and unknown types are ignored", () => {
    const { chrome } = fakeChrome();
    const handle = createMessageHandler(service(chrome), EXTENSION_ID);
    assert.equal(handle({ type: "auth:status" }, { id: "another-extension" }), null);
    assert.equal(handle({ type: "auth:steal" }, { id: EXTENSION_ID }), null);
    assert.equal(handle(null, { id: EXTENSION_ID }), null);
  });
});

test("parseRedirect reads code/state/error from our redirect URI only", () => {
  assert.deepEqual(parseRedirect(`${REDIRECT_URI}?code=c&state=s`, REDIRECT_URI), { code: "c", state: "s", error: null });
  assert.throws(() => parseRedirect("not a url", REDIRECT_URI), { code: "SIGN_IN_FAILED" });
  assert.throws(() => parseRedirect(`${REDIRECT_URI}/extra?code=c`, REDIRECT_URI), { code: "REDIRECT_MISMATCH" });
});
