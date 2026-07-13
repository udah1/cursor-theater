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
import * as os from "os";
import * as https from "https";
import { scan, PROJECTS_DIR } from "./scan";
import { cursorStateDb } from "./composer";
import { fetchUsage } from "./usage";
import { Payload, UsageData } from "./types";

const SCAN_INTERVAL_MS = 6000; // background cadence (the file watcher drives faster updates when a view is open)
const RELEASES_REPO = "udah1/cursor-theater"; // where Check-for-updates looks; releases are tagged cursor-vX.Y.Z
const USAGE_MIN_INTERVAL_MS = 60000; // never hit the undocumented cursor.com endpoints more than once/min

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

// Update-check state. updateAvailable is set (to the newer release) by both the
// manual menu action and the silent background poll; the status bar reflects it.
let updateAvailable: LatestRelease | undefined;
let lastCounts = { running: 0, idle: 0, aborted: 0, done: 0 };
let updateTimer: NodeJS.Timeout | undefined;

// Cursor Usage state. We refresh when the agent status signature changes (throttled
// to once/USAGE_MIN_INTERVAL_MS) and on manual request; the last result is cached so
// a freshly-opened webview paints immediately.
let lastUsage: UsageData | undefined;
let lastUsageFetchMs = 0;
let usageThrottleTimer: NodeJS.Timeout | undefined;
let usageFetching = false;
let lastStatusSig: string | undefined;

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
  lastCounts = { running, idle, aborted, done };
  renderStatusItem();
}

// Composes the status bar from the latest scan counts + the update state, so the
// live working-count and the "update available" badge never clobber each other.
function renderStatusItem() {
  if (!statusItem) {
    return;
  }
  const { running, idle, aborted, done } = lastCounts;
  const icon = running > 0 ? "$(broadcast)" : "$(play-circle)";
  let text = `${icon} Agents Theater (${running})`;
  let tip = `Cursor Theater - ${running} working, ${idle} idle, ${aborted} stopped, ${done} finished`;
  if (updateAvailable) {
    const next = updateAvailable.tag.replace(/^cursor-v/i, "v");
    text += ` → ${next}`;
    statusItem.backgroundColor = new vscode.ThemeColor("statusBarItem.warningBackground");
    tip = `Update available: ${next} (you have v${extensionVersion})\n` + tip + "\nClick to open the menu";
  } else {
    statusItem.backgroundColor = undefined;
    tip += "\nClick to open the theater";
  }
  statusItem.text = text;
  statusItem.tooltip = tip;
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
    maybeTriggerUsageOnStatusChange(payload);
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

// ---- Cursor Usage (cursor.com dashboard numbers) --------------------------

function usageEnabled(): boolean {
  return vscode.workspace.getConfiguration("cursorTheater").get<boolean>("showUsage", true);
}

// Refresh usage when the set of agent statuses actually changes (a new run starts,
// one finishes, etc.), not on every scan tick. The throttle below caps the rate.
function maybeTriggerUsageOnStatusChange(payload: Payload) {
  if (!usageEnabled()) {
    return;
  }
  const sig = payload.agents
    .map((a) => a.id + ":" + a.status)
    .sort()
    .join("|");
  if (sig !== lastStatusSig) {
    lastStatusSig = sig;
    scheduleUsageRefresh();
  }
}

async function doUsageFetch() {
  if (usageFetching) {
    return;
  }
  usageFetching = true;
  lastUsageFetchMs = Date.now();
  try {
    lastUsage = await fetchUsage();
  } catch (e) {
    lastUsage = { state: "error", error: (e as Error).message, fetchedAtMs: Date.now() };
  } finally {
    usageFetching = false;
  }
  for (const w of liveWebviews()) {
    w.postMessage({ type: "usage", data: lastUsage });
  }
}

// Throttled trigger: fetch immediately if we're outside the cooldown, otherwise
// schedule a single trailing fetch at the end of it (coalescing bursts).
function scheduleUsageRefresh() {
  if (!usageEnabled()) {
    return;
  }
  const since = Date.now() - lastUsageFetchMs;
  if (since >= USAGE_MIN_INTERVAL_MS) {
    void doUsageFetch();
  } else if (!usageThrottleTimer) {
    usageThrottleTimer = setTimeout(() => {
      usageThrottleTimer = undefined;
      void doUsageFetch();
    }, USAGE_MIN_INTERVAL_MS - since);
  }
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
      // Paint the last-known usage instantly, then refresh (throttled).
      if (usageEnabled()) {
        if (lastUsage) {
          webview.postMessage({ type: "usage", data: lastUsage });
        }
        scheduleUsageRefresh();
      }
    } else if (msg && msg.type === "refreshUsage") {
      scheduleUsageRefresh();
    }
  });
  // Safety net for the first paint: the webview also posts "ready", but if that
  // message (or our proactive scan) loses the listener race, this delayed scan
  // guarantees the view receives a payload without waiting for the 6s interval.
  setTimeout(() => void runScan(), 800);
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

// Download a URL to a local file, following GitHub's redirect to its asset CDN.
function downloadFile(url: string, dest: string, redirects = 5): Promise<void> {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { "User-Agent": "cursor-theater-extension", Accept: "application/octet-stream" } }, (res) => {
      const sc = res.statusCode || 0;
      if (sc >= 300 && sc < 400 && res.headers.location) {
        res.resume();
        if (redirects <= 0) {
          reject(new Error("too many redirects"));
          return;
        }
        resolve(downloadFile(new URL(res.headers.location, url).toString(), dest, redirects - 1));
        return;
      }
      if (sc < 200 || sc >= 300) {
        res.resume();
        reject(new Error(`download ${sc}`));
        return;
      }
      const file = fs.createWriteStream(dest);
      res.pipe(file);
      file.on("finish", () => file.close(() => resolve()));
      file.on("error", (e) => fs.unlink(dest, () => reject(e)));
    });
    req.on("error", reject);
    req.setTimeout(60000, () => req.destroy(new Error("timeout")));
  });
}

// Download the release .vsix and install it in-place via the editor command, then
// offer to reload (a new version of an already-installed extension needs a reload).
async function installUpdate(latest: LatestRelease) {
  const fallbackUrl = latest.htmlUrl || `https://github.com/${RELEASES_REPO}/releases/latest`;
  if (!latest.vsixUrl) {
    void vscode.env.openExternal(vscode.Uri.parse(fallbackUrl));
    return;
  }
  const next = latest.tag.replace(/^cursor-v/i, "v");
  const dest = path.join(os.tmpdir(), `cursor-theater-${latest.tag.replace(/[^\w.\-]/g, "_")}.vsix`);
  try {
    await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `Cursor Theater: downloading ${next}…`, cancellable: false },
      () => downloadFile(latest.vsixUrl as string, dest)
    );
    await vscode.commands.executeCommand("workbench.extensions.installExtension", vscode.Uri.file(dest));
  } catch (e) {
    const open = await vscode.window.showErrorMessage(
      `Couldn't install the update automatically (${(e as Error).message}). You can install it manually.`,
      "Open release"
    );
    if (open) {
      void vscode.env.openExternal(vscode.Uri.parse(fallbackUrl));
    }
    return;
  } finally {
    fs.unlink(dest, () => {}); // best-effort cleanup; install has already read the file
  }
  const reload = await vscode.window.showInformationMessage(
    `Cursor Theater ${next} installed. Reload to finish updating.`,
    "Reload window"
  );
  if (reload) {
    void vscode.commands.executeCommand("workbench.action.reloadWindow");
  }
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
    updateAvailable = undefined;
    renderStatusItem();
    void vscode.window.showInformationMessage(`Cursor Theater is up to date (v${extensionVersion}).`);
    return;
  }
  updateAvailable = latest;
  renderStatusItem();
  const next = latest.tag.replace(/^cursor-v/i, "v");
  const msg = `Update available: ${next} (you have v${extensionVersion}).`;
  if (latest.vsixUrl) {
    const pick = await vscode.window.showInformationMessage(msg, "Update now", "Release notes");
    if (pick === "Update now") {
      await installUpdate(latest);
    } else if (pick === "Release notes") {
      void vscode.env.openExternal(vscode.Uri.parse(latest.htmlUrl || fallbackUrl));
    }
  } else {
    const open = await vscode.window.showInformationMessage(msg, "Open release");
    if (open) {
      void vscode.env.openExternal(vscode.Uri.parse(latest.htmlUrl || fallbackUrl));
    }
  }
}

// Silent periodic check: only flips the status-bar badge, never pops a dialog.
async function backgroundUpdateCheck() {
  try {
    const latest = await fetchLatestRelease();
    updateAvailable = latest.tag && compareVersions(latest.tag, extensionVersion) > 0 ? latest : undefined;
    renderStatusItem();
  } catch {
    // offline / rate-limited: leave the current badge state untouched
  }
}

function startUpdateChecks(context: vscode.ExtensionContext) {
  const minutes = vscode.workspace.getConfiguration("cursorTheater").get<number>("updateCheckMinutes", 60);
  if (!minutes || minutes <= 0) {
    return; // disabled
  }
  // Defer the first check so it never blocks activation; then poll on the interval.
  const kickoff = setTimeout(() => void backgroundUpdateCheck(), 8000);
  updateTimer = setInterval(() => void backgroundUpdateCheck(), minutes * 60 * 1000);
  context.subscriptions.push({ dispose: () => clearTimeout(kickoff) });
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
  startUpdateChecks(context);
  void runScan();
}

export function deactivate() {
  stopWatching();
  if (updateTimer) {
    clearInterval(updateTimer);
    updateTimer = undefined;
  }
  if (usageThrottleTimer) {
    clearTimeout(usageThrottleTimer);
    usageThrottleTimer = undefined;
  }
  if (panel) {
    panel.dispose();
    panel = undefined;
  }
  view = undefined;
}
