# Video Notes Agent — Chrome extension

Current state: a minimal Manifest V3 extension used only to read the exact Notion OAuth redirect URI
(see `docs/decisions.md` ADR-07/ADR-08). It does not start OAuth and makes no network requests. The
"Take Notes" button, OAuth flow and popup are not implemented yet.

## Read the Notion redirect URI

1. Open `chrome://extensions` in Chrome.
2. Switch on **Developer mode** (top right).
3. Click **Load unpacked** and select this `extension/` folder.
4. Check the ID shown on the extension's card. It must be **`kkkeigfecmfhmainhbkcpdhjnloifbdf`**; the
   `key` in `manifest.json` fixes it. If it differs, stop and report it.
5. On the card, click the **service worker** link (next to "Inspect views"). This opens DevTools for
   the background service worker.
6. In the **Console** tab:
   - Two lines `[Video Notes Agent] …` are printed when the extension is installed or reloaded. If
     they are not visible, click the reload icon on the card and open the service-worker console
     again.
   - Or type `showNotionRedirectUri()` and press Enter.
7. Copy the URI **exactly** as printed (including or excluding any trailing slash as shown). Expected,
   but not yet verified: `https://kkkeigfecmfhmainhbkcpdhjnloifbdf.chromiumapp.org/notion`.

Then register exactly that string as a redirect URI in the Notion integration and put it only into
your local `.env` as `NOTION_REDIRECT_URI`. Whether Notion accepts a `chromiumapp.org` redirect is
confirmed only by a later real authorization round trip.
