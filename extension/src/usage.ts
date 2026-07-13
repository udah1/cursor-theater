// Cursor Usage - read the user's live dashboard usage from cursor.com, reusing the
// session token Cursor already stores locally (no separate login).
//
// AUTH: the cursor.com dashboard authenticates with the cookie
//   WorkosCursorSessionToken=<userSub>::<accessToken>
// where <accessToken> is the JWT at ItemTable key `cursorAuth/accessToken` in the
// global state.vscdb, and <userSub> is that JWT's `sub` claim (with the leading
// "auth0|" stripped). We read both READ-ONLY via the sqlite3 CLI (same approach as
// composer.ts), build the cookie on demand, and NEVER persist or log it. On 401/403
// we re-read the token once (Cursor rotates it) and retry; if still unauthorized we
// report needsAuth so the UI can prompt the user to sign into Cursor.
//
// These are undocumented internal endpoints (the same ones cursor.com/dashboard
// calls); they can change without notice. Personal, read-only use.
import { execFile } from "child_process";
import * as https from "https";
import { promisify } from "util";
import { cursorStateDb } from "./composer";
import { UsageData, UsageModelRow } from "./types";

const execFileAsync = promisify(execFile);

const DB = cursorStateDb();
const REQ_TIMEOUT_MS = 8000;
// A normal desktop Chrome UA, mimicking the dashboard.
const UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36";

interface Auth {
  cookie: string;
  sub: string;
  teamId: number | null;
  plan: string;
  email: string;
}

// Thrown when a request comes back 401/403, so the orchestrator can re-read the
// (possibly rotated) token and retry exactly once.
class AuthError extends Error {}

function decodeJwtSub(jwt: string): string | null {
  try {
    const part = jwt.split(".")[1];
    if (!part) {
      return null;
    }
    const padded = part + "=".repeat((4 - (part.length % 4)) % 4);
    const json = Buffer.from(padded.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf8");
    const sub = JSON.parse(json).sub;
    if (typeof sub !== "string" || !sub) {
      return null;
    }
    // WorkOS subs look like "auth0|user_01..."; the cookie/?user= form drops the prefix.
    return sub.replace(/^auth0\|/, "");
  } catch {
    return null;
  }
}

/** Read Cursor's stored session token + team/plan from the global DB (read-only). */
async function readAuth(): Promise<Auth | null> {
  const sql =
    "SELECT key,value FROM ItemTable WHERE key IN (" +
    "'cursorAuth/accessToken','cursorAuth/cachedTeam'," +
    "'cursorAuth/stripeMembershipType','cursorAuth/cachedEmail');";
  let rows: Array<{ key: string; value: string }> = [];
  try {
    const res = await execFileAsync("sqlite3", ["-readonly", "-json", DB, sql], {
      timeout: 4000,
      maxBuffer: 8 * 1024 * 1024,
    });
    const out = (res.stdout || "").trim();
    rows = out ? JSON.parse(out) : [];
  } catch {
    return null;
  }
  const map = new Map<string, string>();
  for (const r of rows) {
    if (r && typeof r.key === "string") {
      map.set(r.key, r.value);
    }
  }
  const accessToken = map.get("cursorAuth/accessToken");
  if (!accessToken) {
    return null;
  }
  const sub = decodeJwtSub(accessToken);
  if (!sub) {
    return null;
  }
  let teamId: number | null = null;
  try {
    const team = JSON.parse(map.get("cursorAuth/cachedTeam") || "{}");
    if (team && typeof team.teamId === "number") {
      teamId = team.teamId;
    }
  } catch {
    /* no team */
  }
  const cookie = "WorkosCursorSessionToken=" + encodeURIComponent(sub + "::" + accessToken);
  return {
    cookie,
    sub,
    teamId,
    plan: (map.get("cursorAuth/stripeMembershipType") || "").trim(),
    email: (map.get("cursorAuth/cachedEmail") || "").trim(),
  };
}

function request(method: "GET" | "POST", pathName: string, cookie: string, body?: unknown): Promise<any> {
  const data = body !== undefined ? JSON.stringify(body) : undefined;
  const headers: Record<string, string> = {
    Cookie: cookie,
    Accept: "application/json",
    Referer: "https://cursor.com/dashboard",
    Origin: "https://cursor.com", // required for POST CSRF checks; harmless on GET
    "User-Agent": UA,
  };
  if (data !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = String(Buffer.byteLength(data));
  }
  const options: https.RequestOptions = { hostname: "cursor.com", path: pathName, method, headers };
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      const status = res.statusCode || 0;
      let raw = "";
      res.on("data", (c) => (raw += c));
      res.on("end", () => {
        if (status === 401 || status === 403) {
          reject(new AuthError(`unauthorized ${status}`));
          return;
        }
        if (status < 200 || status >= 300) {
          reject(new Error(`http ${status}`));
          return;
        }
        try {
          resolve(raw ? JSON.parse(raw) : {});
        } catch (e) {
          reject(e as Error);
        }
      });
    });
    req.on("error", reject);
    req.setTimeout(REQ_TIMEOUT_MS, () => req.destroy(new Error("timeout")));
    if (data !== undefined) {
      req.write(data);
    }
    req.end();
  });
}

const num = (v: unknown): number => (typeof v === "number" && isFinite(v) ? v : Number(v) || 0);
const getRequestCountFromSpendCents = (cents: number): number => (cents > 0 ? Math.ceil(cents / 4) : 0);
const centsToDollars = (cents: number): number => cents / 100;
const DAY_MS = 86400000;

// One full fetch attempt with a given cookie. Throws AuthError on 401/403 so the
// caller can refresh the token and retry.
async function runOnce(auth: Auth): Promise<UsageData> {
  // Identity/validation - also yields the authoritative sub for /api/usage.
  const me = await request("GET", "/api/auth/me", auth.cookie);
  const sub = typeof me?.sub === "string" && me.sub ? me.sub : auth.sub;
  const email = typeof me?.email === "string" && me.email ? me.email : auth.email;

  const [usage, summary] = await Promise.all([
    request("GET", "/api/usage?user=" + encodeURIComponent(sub), auth.cookie),
    request("GET", "/api/usage-summary", auth.cookie),
  ]);

  const legacy = (usage && usage["gpt-4"]) || {};
  const isTeam = summary?.limitType === "team";
  const planUsedCents = num(summary?.individualUsage?.plan?.used);

  // Team-only extras; each is best-effort so one failure never blanks the panel.
  let requestQuotaPerSeat: number | null = null;
  let hardLimitPerUser: number | null = null;
  let perModel: UsageModelRow[] = [];
  let totalCostCents: number | null = null;
  if (isTeam && auth.teamId != null) {
    const teamId = auth.teamId;
    const [teamsRes, hardRes, aggRes] = await Promise.allSettled([
      request("POST", "/api/dashboard/teams", auth.cookie, { activeOnly: false }),
      request("POST", "/api/dashboard/get-hard-limit", auth.cookie, { teamId }),
      request("POST", "/api/dashboard/get-aggregated-usage-events", auth.cookie, { teamId }),
    ]);
    if (teamsRes.status === "fulfilled") {
      const team = (teamsRes.value?.teams || []).find((x: { id?: number }) => x && x.id === teamId);
      if (team && typeof team.requestQuotaPerSeat === "number") {
        requestQuotaPerSeat = team.requestQuotaPerSeat;
      }
    }
    if (hardRes.status === "fulfilled" && typeof hardRes.value?.hardLimitPerUser === "number") {
      hardLimitPerUser = hardRes.value.hardLimitPerUser;
    }
    if (aggRes.status === "fulfilled" && Array.isArray(aggRes.value?.aggregations)) {
      perModel = aggRes.value.aggregations.map((a: Record<string, unknown>) => ({
        model: String(a.modelIntent || ""),
        costCents: num(a.totalCents),
        requests: num(a.requestCost),
        inputTokens: num(a.inputTokens),
        outputTokens: num(a.outputTokens),
        cacheReadTokens: num(a.cacheReadTokens),
        cacheWriteTokens: num(a.cacheWriteTokens),
      }));
      totalCostCents = num(aggRes.value.totalCostCents);
    }
  }

  // Included-request usage (matches the dashboard's legacy-request logic).
  const usedFromSpend = planUsedCents > 0 ? getRequestCountFromSpendCents(planUsedCents) : undefined;
  const used = isTeam ? usedFromSpend ?? num(legacy.numRequests) : num(legacy.numRequests);
  const limit =
    isTeam && requestQuotaPerSeat != null ? 500 * requestQuotaPerSeat : num(legacy.maxRequestUsage);
  const remaining = Math.max(0, limit - used);
  const pct = limit > 0 ? Math.round((used / limit) * 1000) / 10 : 0;

  // On-demand (usage-based) spend, all reported in cents.
  const onDemand = summary?.individualUsage?.onDemand || {};
  const onDemandUsed = centsToDollars(num(onDemand.used));
  const onDemandLimit = centsToDollars(num(onDemand.limit));
  const onDemandRemaining = centsToDollars(num(onDemand.remaining));

  // Billing cycle + burn rate.
  const cycleStartMs = Date.parse(summary?.billingCycleStart) || null;
  const cycleEndMs = Date.parse(summary?.billingCycleEnd) || null;
  const now = Date.now();
  let daysLeft: number | null = null;
  let requestsPerDay: number | null = null;
  let projectedRequests: number | null = null;
  let projectedToExceed = false;
  if (cycleStartMs && cycleEndMs) {
    const daysElapsed = (now - cycleStartMs) / DAY_MS;
    daysLeft = Math.max(0, (cycleEndMs - now) / DAY_MS);
    const cycleLengthDays = (cycleEndMs - cycleStartMs) / DAY_MS;
    requestsPerDay = used / Math.max(0.5, daysElapsed);
    projectedRequests = Math.round(requestsPerDay * cycleLengthDays);
    projectedToExceed = limit > 0 && projectedRequests > limit;
  }

  return {
    state: "ok",
    fetchedAtMs: now,
    email,
    plan: (summary?.membershipType as string) || auth.plan,
    isTeam,
    used,
    limit,
    remaining,
    pct,
    // Raw dashboard percentages (usage-based accounts show these instead of the
    // request ratio), kept so the UI can surface the number the dashboard shows.
    planPercentUsed:
      typeof summary?.individualUsage?.plan?.totalPercentUsed === "number"
        ? summary.individualUsage.plan.totalPercentUsed
        : null,
    onDemandUsed,
    onDemandLimit,
    onDemandRemaining,
    hardLimitPerUser,
    cycleStartMs,
    cycleEndMs,
    daysLeft: daysLeft != null ? Math.floor(daysLeft) : null,
    requestsPerDay: requestsPerDay != null ? Math.round(requestsPerDay * 10) / 10 : null,
    projectedRequests,
    projectedToExceed,
    perModel,
    totalCostCents,
  };
}

/**
 * Fetch and parse the current user's Cursor usage. Never throws: returns a
 * discriminated UsageData with state "ok" | "needsAuth" | "error".
 */
export async function fetchUsage(): Promise<UsageData> {
  const now = () => Date.now();
  let auth = await readAuth();
  if (!auth) {
    return { state: "needsAuth", fetchedAtMs: now() };
  }
  try {
    return await runOnce(auth);
  } catch (e) {
    if (e instanceof AuthError) {
      // Token may have just rotated - re-read once and retry.
      auth = await readAuth();
      if (!auth) {
        return { state: "needsAuth", fetchedAtMs: now() };
      }
      try {
        return await runOnce(auth);
      } catch (e2) {
        if (e2 instanceof AuthError) {
          return { state: "needsAuth", fetchedAtMs: now() };
        }
        return { state: "error", error: (e2 as Error).message, fetchedAtMs: now() };
      }
    }
    return { state: "error", error: (e as Error).message, fetchedAtMs: now() };
  }
}
