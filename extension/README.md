Muse2API Cookie Import Extension — Installation Guide
====================================================

This extension does one thing: it syncs your browser's muse.ai login cookies
to your muse2api service. No Python, no terminal.

[Chinese version](README.zh-CN.md)


1. Unzip
-------
Extract muse2api-extension.zip to a directory you will not delete, for example:

    Windows : D:\muse2api-extension
    macOS   : ~/Documents/muse2api-extension

After extracting, you should see manifest.json, popup.html, and popup.js.


2. Load in Chrome / Edge
------------------------
Chrome:
  1. Type  chrome://extensions  in the address bar and press Enter
  2. Turn on the "Developer mode" switch in the top-right corner
  3. Click "Load unpacked" in the top-left corner
  4. Select the folder extracted in step 1 (not an individual file inside it)
  5. A puzzle-piece icon appears in the toolbar; pin it for convenience (optional)

Edge:
  1. Type  edge://extensions  in the address bar and press Enter
  2. Turn on "Developer mode" in the bottom-left corner
  3. Click "Load unpacked"
  4. The rest is the same as above

Other Chromium-based browsers (Brave / Vivaldi / 360 Chrome, etc.) follow similar steps.


3. Usage
--------
1. First open https://muse.ai/ in this browser and log in,
   until the chat screen is visible.
2. On the muse2api admin page, at the top of the "Account pool" tab,
   copy the BASE URL and API Key.
3. Click the extension icon in the browser toolbar and enter both values
   (only once; they are remembered).
4. Click "Read and import". When you see "✓ Import successful", you are done.


4. FAQ
------
Q: It says "No muse.ai cookies found"
A: This browser is not logged in to muse.ai. Open https://muse.ai/ and log in first.

Q: It says "Missing core items such as hatch_sess"
A: Same as above — the login is not complete. Make sure the chat screen is visible, then retry.

Q: It says "Invalid API Key (server returned 401)"
A: Copy the API Key from the admin page again. The key starts with m2a_.

Q: It reports a CORS / network error
A: Check that the service URL uses https and that the service itself is reachable
   (try opening the service URL /admin directly in the browser).

Q: Will my cookies be sent anywhere else?
A: No. The extension only has the cookies and storage permissions; the code is in popup.js,
   and you can inspect it: it sends a single POST to the service URL you entered, nothing else.

Q: Why does it ask for permission to access my service URL?
A: Only `muse.ai` is granted at install time. The service URL is arbitrary (your own
   server), so its origin is requested at runtime the first time you import. Approving it
   grants access to that one origin, not to every website.

Q: Where is the API Key stored?
A: In `chrome.storage.local` for this extension profile, unencrypted — the same place the
   browser keeps extension settings. Anyone with access to this browser profile can read
   it. If that matters, set `MUSE2API_MAINTAINER_KEY` on the server so the stored key can
   still chat and manage accounts but cannot push code or trigger an upgrade.


5. Uninstall
------------
chrome://extensions → find "Muse2API Cookie Import" → Remove.
