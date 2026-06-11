// Cursor Theater - native extension (no server, no port).
// The extension host reads ~/.cursor/projects + the global state.vscdb directly,
// watches them for changes, and PUSHES payloads into webviews that render the
// shared UI (ui/theater.html). Surfaces that share the exact same UI + data:
//   * a dockable side View (Activity Bar / Secondary Side Bar / Panel),
//   * a full editor-tab Panel opened on demand via the command, and
//   * a bottom-right status-bar item showing the live "working" count.
// Each window is independent: closing one never affects another, because nothing
// is shared between windows.
import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import * as https from "https";
import { scan, PROJECTS_DIR } from "./scan";
import { cursorStateDb } from "./composer";
import { Payload } from "./types";

const SCAN_INTERVAL_MS = 6000; // background cadence (the file watcher drives faster updates when a view is open)
const RELEASES_REPO = "udah1/cursor-theater"; // where Check-for-updates looks; releases are tagged cursor-vX.Y.Z

let extensionPath = "";
let extensionVersion = "0.0.0";
let panel: vscode.WebviewPanel | undefined;
let view: vscode.WebviewView | undefined;
let statusItem: vscode.StatusBarItem | undefined;

let watchers: fs.FSWatcher[] = [];
let interval: NodeJS.Timeout | undefined;
let debounce: NodeJS.Timeout | undefined;
let scanning = false;
let queued = false;

function nonce(): string {
  let s = "";
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 32; i++) {
    s += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return s;
}

function resolveUiHtml(): string | undefined {
  const candidates = [
    path.join(extensionPath, "media", "theater.html"),
    path.join(extensionPath, "..", "ui", "theater.html"),
  ];
  for (const c of candidates) {
    try {
      if (fs.existsSync(c)) {
        return fs.readFileSync(c, "utf8");
      }
    } catch {
      /* try next */
    }
  }
  return undefined;
}

function buildWebviewHtml(pageHtml: string, webview: vscode.Webview): string {
  const n = nonce();
  const csp =
    "default-src 'none'; " +
    `img-src ${webview.cspSource} data:; ` +
    `style-src ${webview.cspSource} 'unsafe-inline'; ` +
    `font-src ${webview.cspSource}; ` +
    `script-src 'nonce-${n}';`;
  const meta = `<meta http-equiv="Content-Security-Policy" content="${csp}">`;
  // The UI's inline scripts need the nonce to run under the CSP above.
  let html = pageHtml.replace(/<script>/g, `<script nonce="${n}">`);
  if (/<head[^>]*>/i.test(html)) {
    html = html.replace(/<head[^>]*>/i, (m) => m + meta);
  } else {
    html = meta + html;
  }
  return html;
}

function liveWebviews(): vscode.Webview[] {
  const out: vscode.Webview[] = [];
  if (panel) {
    out.push(panel.webview);
  }
  if (view) {
    out.push(view.webview);
  }
  return out;
}

function updateStatusItem(payload: Payload) {
  if (!statusItem) {
    return;
  }
  let running = 0;
  let idle = 0;
  let aborted = 0;
  let done = 0;
  for (const a of payload.agents) {
    if (a.status === "running") {
      running += 1;
    } else if (a.status === "stale") {
      idle += 1;
    } else if (a.status === "aborted") {
      aborted += 1;
    } else if (a.status === "done") {
      done += 1;
    }
  }
  statusItem.text = running > 0 ? `$(broadcast) Agents Theater ${running}` : "$(play-circle) Agents Theater";
  statusItem.tooltip =
    `Cursor Theater - ${running} working, ${idle} idle, ${aborted} stopped, ${done} finished\n` +
    "Click to open the theater";
}

async function runScan() {
  if (scanning) {
    queued = true;
    return;
  }
  scanning = true;
  try {
    const payload = await scan();
    updateStatusItem(payload);
    for (const w of liveWebviews()) {
      w.postMessage({ type: "agents", payload });
    }
  } catch (e) {
    console.error("Cursor Theater scan failed:", e);
  } finally {
    scanning = false;
    if (queued) {
      queued = false;
      void runScan();
    }
  }
}

function scheduleScan() {
  if (debounce) {
    clearTimeout(debounce);
  }
  debounce = setTimeout(() => {
    debounce = undefined;
    void runScan();
  }, 300);
}

function startWatching() {
  stopWatching();
  // Watch the projects tree (recursive where supported: macOS/Windows) and the
  // globalStorage dir (for state.vscdb title/status changes).
  const dirs = [PROJECTS_DIR, path.dirname(cursorStateDb())];
  for (const dir of dirs) {
    try {
      const w = fs.watch(dir, { recursive: true }, () => scheduleScan());
      watchers.push(w);
    } catch {
      try {
        // Linux: recursive not supported - watch the top level only; the safety
        // interval below covers nested changes.
        const w = fs.watch(dir, () => scheduleScan());
        watchers.push(w);
      } catch {
        /* unwatchable - rely on the interval */
      }
    }
  }
  // Background cadence: keeps the status-bar count live even with no view open,
  // and is the safety net on platforms with flaky/no recursive watch.
  interval = setInterval(() => void runScan(), SCAN_INTERVAL_MS);
}

function stopWatching() {
  for (const w of watchers) {
    try {
      w.close();
    } catch {
      /* ignore */
    }
  }
  watchers = [];
  if (interval) {
    clearInterval(interval);
    interval = undefined;
  }
  if (debounce) {
    clearTimeout(debounce);
    debounce = undefined;
  }
}

function wireWebview(webview: vscode.Webview, pageHtml: string) {
  webview.options = { enableScripts: true };
  webview.html = buildWebviewHtml(pageHtml, webview);
  webview.onDidReceiveMessage((msg) => {
    if (msg && msg.type === "ready") {
      void runScan();
    }
  });
}

// ---- side view (dockable: Activity Bar / Secondary Side Bar / Panel) ------

class TheaterViewProvider implements vscode.WebviewViewProvider {
  resolveWebviewView(webviewView: vscode.WebviewView): void {
    view = webviewView;
    const pageHtml = resolveUiHtml();
    if (!pageHtml) {
      webviewView.webview.html =
        "<body style='font-family:sans-serif;padding:1rem'>Cursor Theater: ui/theater.html not found. Run <code>npm run copy-ui</code> and reload.</body>";
      return;
    }
    wireWebview(webviewView.webview, pageHtml);
    webviewView.onDidDispose(() => {
      view = undefined;
    });
    void runScan();
  }
}

// ---- editor-tab panel (on demand via the command) ------------------------

function openTheater() {
  if (panel) {
    panel.reveal(vscode.ViewColumn.Active);
    return;
  }
  const pageHtml = resolveUiHtml();
  if (!pageHtml) {
    vscode.window.showErrorMessage(
      "Cursor Theater: could not find ui/theater.html. Run the build (npm run copy-ui) and reload."
    );
    return;
  }
  panel = vscode.window.createWebviewPanel("cursorTheater", "Cursor Theater", vscode.ViewColumn.Active, {
    enableScripts: true,
    retainContextWhenHidden: true,
  });
  wireWebview(panel.webview, pageHtml);
  panel.onDidDispose(() => {
    panel = undefined;
  });
  void runScan();
}

function revealSideView() {
  // Reveal the docked side view wherever the user parked it; fall back to the
  // editor tab if focusing the view isn't available.
  vscode.commands.executeCommand("cursorTheater.view.focus").then(undefined, () => openTheater());
}

function readVersion(): string {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(extensionPath, "package.json"), "utf8"));
    return typeof pkg.version === "string" ? pkg.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

// Compare dotted versions; tolerates a leading "v"/"cursor-v" and missing parts.
function compareVersions(a: string, b: string): number {
  const norm = (v: string) => v.replace(/^cursor-v/i, "").replace(/^v/i, "").split(".").map((n) => parseInt(n, 10) || 0);
  const x = norm(a);
  const y = norm(b);
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    const d = (x[i] || 0) - (y[i] || 0);
    if (d !== 0) {
      return d < 0 ? -1 : 1;
    }
  }
  return 0;
}

interface LatestRelease {
  tag: string;
  htmlUrl: string;
  vsixUrl?: string;
}

function fetchLatestRelease(): Promise<LatestRelease> {
  // Unauthenticated GitHub API; releases/latest excludes drafts/prereleases.
  const options: https.RequestOptions = {
    hostname: "api.github.com",
    path: `/repos/${RELEASES_REPO}/releases/latest`,
    method: "GET",
    headers: { "User-Agent": "cursor-theater-extension", Accept: "application/vnd.github+json" },
  };
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let body = "";
      res.on("data", (c) => (body += c));
      res.on("end", () => {
        if (!res.statusCode || res.statusCode < 200 || res.statusCode >= 300) {
          reject(new Error(`GitHub API ${res.statusCode}`));
          return;
        }
        try {
          const j = JSON.parse(body);
          const vsix = Array.isArray(j.assets)
            ? j.assets.find((a: { name?: string }) => typeof a.name === "string" && a.name.endsWith(".vsix"))
            : undefined;
          resolve({ tag: String(j.tag_name || ""), htmlUrl: String(j.html_url || ""), vsixUrl: vsix?.browser_download_url });
        } catch (e) {
          reject(e as Error);
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(10000, () => req.destroy(new Error("timeout")));
    req.end();
  });
}

async function checkForUpdates() {
  const fallbackUrl = `https://github.com/${RELEASES_REPO}/releases/latest`;
  let latest: LatestRelease;
  try {
    latest = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Cursor Theater: checking for updates…" },
      () => fetchLatestRelease()
    );
  } catch {
    const open = await vscode.window.showWarningMessage(
      `Couldn't reach GitHub to check for updates (you're on v${extensionVersion}).`,
      "Open releases"
    );
    if (open) {
      void vscode.env.openExternal(vscode.Uri.parse(fallbackUrl));
    }
    return;
  }
  if (!latest.tag || compareVersions(latest.tag, extensionVersion) <= 0) {
    void vscode.window.showInformationMessage(`Cursor Theater is up to date (v${extensionVersion}).`);
    return;
  }
  const target = latest.vsixUrl || latest.htmlUrl || fallbackUrl;
  const label = latest.vsixUrl ? "Download .vsix" : "Open release";
  const pick = await vscode.window.showInformationMessage(
    `Update available: ${latest.tag.replace(/^cursor-v/i, "v")} (you have v${extensionVersion}).`,
    label,
    "Release notes"
  );
  if (pick === label) {
    void vscode.env.openExternal(vscode.Uri.parse(target));
  } else if (pick === "Release notes") {
    void vscode.env.openExternal(vscode.Uri.parse(latest.htmlUrl || fallbackUrl));
  }
}

async function showMenu() {
  type Item = vscode.QuickPickItem & { action: "editor" | "side" | "refresh" | "update" };
  const items: Item[] = [
    { label: "$(window) Full-screen theater", detail: "Open in a full editor tab", action: "editor" },
    { label: "$(layout-sidebar-right) Side-bar theater", detail: "Reveal the docked side view — drag it to either side bar or the panel", action: "side" },
    { label: "$(refresh) Refresh now", detail: "Rescan conversations immediately", action: "refresh" },
    { label: `$(cloud-download) Check for updates (v${extensionVersion})`, detail: "Compare with the latest GitHub release", action: "update" },
  ];
  const pick = await vscode.window.showQuickPick(items, { placeHolder: "Cursor Theater" });
  if (!pick) {
    return;
  }
  if (pick.action === "editor") {
    openTheater();
  } else if (pick.action === "side") {
    revealSideView();
  } else if (pick.action === "update") {
    void checkForUpdates();
  } else {
    void runScan();
  }
}

export function activate(context: vscode.ExtensionContext) {
  extensionPath = context.extensionPath;
  extensionVersion = readVersion();

  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
  statusItem.text = "$(play-circle) Agents Theater";
  statusItem.tooltip = "Click to open Cursor Theater";
  statusItem.command = "cursorTheater.show";
  statusItem.show();

  context.subscriptions.push(
    statusItem,
    vscode.commands.registerCommand("cursorTheater.open", () => openTheater()),
    vscode.commands.registerCommand("cursorTheater.show", () => showMenu()),
    vscode.commands.registerCommand("cursorTheater.showSide", () => revealSideView()),
    vscode.window.registerWebviewViewProvider("cursorTheater.view", new TheaterViewProvider(), {
      webviewOptions: { retainContextWhenHidden: true },
    })
  );

  startWatching();
  void runScan();
}

export function deactivate() {
  stopWatching();
  if (panel) {
    panel.dispose();
    panel = undefined;
  }
  view = undefined;
}
