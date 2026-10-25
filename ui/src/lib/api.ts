// REST client (03 §12.1 + the extras listed in the build brief). One request helper:
// JSON in, JSON out, `{"detail": ...}` errors raised as ApiError and surfaced as toasts.

import type {
  ProcessLogPage,
  ProcessRow,
  Approval,
  ApprovalDecision,
  Artifact,
  Connector,
  Conversation,
  ConversationGraphData,
  ConversationUsage,
  ConvEvent,
  CoreMeter,
  GraphData,
  Health,
  Item,
  LogLine,
  MaintenanceStatus,
  McpServer,
  ModelRole,
  Occupancy,
  Preset,
  Procedure,
  PromoteLogEntry,
  PromotePlan,
  PullResult,
  ProtectedMode,
  ProtectedPath,
  Provider,
  QueuedMessage,
  QueueState,
  Root,
  Schedule,
  SecretsInfo,
  SettingsPayload,
  Snapshot,
  SupervisorStatus,
  ThreadStatus,
  ToolPolicy,
  VaultChange,
  VaultNote,
  VaultSearchHit,
  VaultTreeNode,
  VersionInfo,
  VoiceStatus,
  AudioResult,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.name = "ApiError";
  }
}

export function token(): string | null {
  try {
    return localStorage.getItem("loom_token") ?? memoryToken;
  } catch {
    return memoryToken;
  }
}

/** The GET url of a stored image, usable as `<img src>` and as an href. An `<img>` cannot
 *  send a header, so the LAN token rides the query string, the way `/ws` takes it. */
export function imageUrl(conversationId: string, imageId: string): string {
  const t = token();
  return `/api/conversations/${conversationId}/images/${imageId}${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

/** The GET url of a stored voice clip, for `<audio src>` (token in the query, as above). */
export function audioUrl(conversationId: string, audioId: string): string {
  const t = token();
  return `/api/conversations/${conversationId}/audio/${encodeURIComponent(audioId)}${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

const tokenListeners = new Set<() => void>();
let memoryToken: string | null = null; // survives blocked storage for this page load

export function setToken(value: string | null) {
  memoryToken = value || null;
  try {
    if (value) localStorage.setItem("loom_token", value);
    else localStorage.removeItem("loom_token");
  } catch {
    /* private mode: tokens simply do not persist */
  }
  tokenListeners.forEach((fn) => fn());
}

/** Called after every token change (set or cleared); the socket reconnects on it. */
export function onTokenChange(fn: () => void): () => void {
  tokenListeners.add(fn);
  return () => void tokenListeners.delete(fn);
}

// --- the 401 gate ---------------------------------------------------------------
// Any /api call answered 401 asks for the LAN token through one shared prompt: however
// many calls fail at once there is one dialog, and each waits on it and retries.
let pendingPrompt: Promise<boolean> | null = null;
const promptListeners = new Set<(open: boolean) => void>();
let promptResolve: ((ok: boolean) => void) | null = null;

export function tokenPromptOpen(): boolean {
  return pendingPrompt !== null;
}
export function onTokenPrompt(fn: (open: boolean) => void): () => void {
  promptListeners.add(fn);
  return () => void promptListeners.delete(fn);
}
function requestToken(): Promise<boolean> {
  if (!pendingPrompt) {
    pendingPrompt = new Promise<boolean>((resolve) => {
      promptResolve = resolve;
    });
    promptListeners.forEach((fn) => fn(true));
  }
  return pendingPrompt;
}
/** The dialog's answer: a token to store and retry with, or null to give up. */
export function answerTokenPrompt(value: string | null) {
  const resolve = promptResolve;
  pendingPrompt = null;
  promptResolve = null;
  if (value) setToken(value);
  promptListeners.forEach((fn) => fn(false));
  resolve?.(Boolean(value));
}

/** fetch with the stored token; a 401 prompts once (shared) and retries with the new token. */
export async function authedFetch(url: string, init: RequestInit): Promise<Response> {
  for (;;) {
    const headers: Record<string, string> = { ...(init.headers as Record<string, string> | undefined) };
    const t = token();
    if (t) headers["Authorization"] = `Bearer ${t}`;
    const res = await fetch(url, { ...init, headers });
    if (res.status !== 401) return res;
    if (!(await requestToken())) return res;
  }
}

function qs(params: Record<string, unknown> | undefined): string {
  if (!params) return "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) {
      if (v.length === 0) continue;
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v.join(","))}`);
    } else {
      parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
    }
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  params?: Record<string, unknown>,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await authedFetch(`/api${path}${qs(params)}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error).message}`);
  }
  const text = await res.text();
  let parsed: unknown = undefined;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = text;
    }
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in (parsed as any)
        ? String((parsed as any).detail)
        : typeof parsed === "string" && parsed
          ? parsed
          : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail);
  }
  return parsed as T;
}

/** POST a raw body (03 §12.1: no multipart, no extra dependency); same error shape as
 *  `request`. Used by image and audio uploads. */
async function rawPost<T>(
  path: string,
  body: Blob,
  headers: Record<string, string>,
  params?: Record<string, unknown>,
): Promise<T> {
  let res: Response;
  try {
    res = await authedFetch(`/api${path}${qs(params)}`, { method: "POST", headers, body });
  } catch (e) {
    throw new ApiError(0, `network error: ${(e as Error).message}`);
  }
  const text = await res.text();
  let parsed: any = undefined;
  try {
    parsed = text ? JSON.parse(text) : undefined;
  } catch {
    parsed = text;
  }
  if (!res.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String(parsed.detail)
        : typeof parsed === "string" && parsed
          ? parsed
          : `${res.status} ${res.statusText}`;
    throw new ApiError(res.status, detail);
  }
  return parsed as T;
}

const get = <T,>(p: string, params?: Record<string, unknown>) => request<T>("GET", p, undefined, params);
const post = <T,>(p: string, body?: unknown, params?: Record<string, unknown>) =>
  request<T>("POST", p, body ?? {}, params);
const put = <T,>(p: string, body?: unknown) => request<T>("PUT", p, body ?? {});
const patch = <T,>(p: string, body?: unknown) => request<T>("PATCH", p, body ?? {});
const del = <T,>(p: string, params?: Record<string, unknown>) =>
  request<T>("DELETE", p, undefined, params);

export const api = {
  request,

  // -- conversations ---------------------------------------------------------
  conversations: (params?: { kind?: string; status?: string }) =>
    get<Conversation[]>("/conversations", params),
  conversation: (id: string) => get<Conversation>(`/conversations/${id}`),
  createConversation: (body: {
    kind: "thread";
    goal: string;
    title?: string;
    excerpt?: string;
    /** vault notes to pin into the thread's context from its first turn */
    notes?: string[];
    roots?: string[];
    preset?: string;
    role?: string;
    parent_id?: string;
    /** "worktree": the thread works in its own git checkout branched from `isolation_root` (the first root by default) */
    isolation?: "none" | "worktree";
    isolation_root?: string;
  }) => post<Conversation>("/conversations", body),
  /** Title, preset, step budget or the pinned-notes list; a missing note is a 400. */
  updateConversation: (
    id: string,
    body: { title?: string; preset?: string; clear_preset?: boolean; step_budget?: number; pinned_notes?: string[] },
  ) => patch<Conversation>(`/conversations/${id}`, body),
  events: (id: string, params?: { after?: number; before?: number; limit?: number; kinds?: string; tail?: boolean }) =>
    get<ConvEvent[]>(`/conversations/${id}/events`, params),
  sendMessage: (id: string, content: string, images?: string[]) =>
    post<unknown>(`/conversations/${id}/messages`, images && images.length ? { content, images } : { content }),
  /** Raw-body upload (03 §12.1: no multipart, no extra dependency). The reply names the
   *  stored image; `sendMessage` then carries the id. 415 non-image, 413 over the cap. */
  uploadImage: (id: string, file: File) =>
    rawPost<{ id: string; mime: string; bytes: number }>(`/conversations/${id}/images`, file, {
      "Content-Type": file.type || "application/octet-stream",
      "X-Filename": encodeURIComponent(file.name || "image"),
    }),
  /** A voice clip (WAV, raw body like images): stored, then transcribed by the
   *  transcription role. `draft` returns the text (and emits `voice.draft`); `send` posts
   *  it as a user message (202). 409 no transcription model, 502 transcription failed. */
  uploadAudio: (id: string, wav: Blob, action: "send" | "draft", source: "composer" | "hotkey" = "composer") =>
    rawPost<AudioResult>(`/conversations/${id}/audio`, wav, { "Content-Type": "audio/wav" }, { action, source }),
  /** Re-transcribe a stored clip (the Retry on a failed voice message); same outcomes. */
  retryAudio: (id: string, audioId: string, action: "send" | "draft") =>
    post<AudioResult>(`/conversations/${id}/audio/${encodeURIComponent(audioId)}/retry`, undefined, { action }),
  voiceStatus: () => get<VoiceStatus>("/voice/status"),
  /** Inbox rows not yet in the transcript: what the next step boundary will deliver. */
  inbox: (id: string) => get<QueuedMessage[]>(`/conversations/${id}/inbox`),
  cancel: (id: string) => post<unknown>(`/conversations/${id}/cancel`),
  /** Cut the replayed context; the transcript stays. 409 while a turn runs or waits. */
  clearConversation: (id: string) => post<Conversation>(`/conversations/${id}/clear`),
  archive: (id: string) => post<Conversation>(`/conversations/${id}/archive`),
  occupancy: (id: string) => get<Occupancy>(`/conversations/${id}/occupancy`),
  /** Token totals from `meta.usage`, derived on every call (03 §12.1). */
  usage: (id: string) => get<ConversationUsage>(`/conversations/${id}/usage`),
  conversationArtifacts: (id: string) => get<Artifact[]>(`/conversations/${id}/artifacts`),

  // -- artifacts -------------------------------------------------------------
  artifact: (id: string, params?: { offset?: number; limit?: number }) =>
    get<{ id: string; text?: string; content?: string; bytes: number; offset?: number; limit?: number }>(
      `/artifacts/${id}`,
      params,
    ),

  // -- background processes (03 §9.6) -------------------------------------------
  processes: (params?: { conversation_id?: string; status?: string }) =>
    get<{ processes: ProcessRow[] }>("/processes", params).then((r) => r.processes ?? []),
  process: (id: string) => get<ProcessRow>(`/processes/${id}`),
  processLogs: (id: string, params?: { offset?: number; limit?: number }) =>
    get<ProcessLogPage>(`/processes/${id}/logs`, params),
  stopProcess: (id: string) => post<ProcessRow>(`/processes/${id}/stop`),

  // -- approvals / items -----------------------------------------------------
  approvals: (params?: { status?: string }) => get<Approval[]>("/approvals", params),
  decideApproval: (id: string, decision: ApprovalDecision, add_root?: boolean) =>
    post<Approval>(`/approvals/${id}`, { decision, ...(add_root ? { add_root } : {}) }),
  items: (params?: { unseen?: boolean }) => get<Item[]>("/items", params),
  seenItem: (id: string) => post<Item>(`/items/${id}/seen`),
  dismissItem: (id: string) => post<Item>(`/items/${id}/dismiss`),
  /** Run a maintenance hand-back's stored call; the click is the approval (03 §8.6). */
  applyItem: (id: string) => post<Item>(`/items/${id}/apply`),

  // -- vault -----------------------------------------------------------------
  /** The backend answers one root dir node; the UI groups the top-level folders by type. */
  vaultTree: async (): Promise<VaultTreeNode[]> => {
    const raw = await get<any>("/vault/tree");
    const roots: any[] = Array.isArray(raw) ? raw : (raw?.children ?? []);
    const walk = (node: any, type: string): VaultTreeNode => ({
      path: node.path ?? "",
      title: node.title ?? node.name ?? node.path ?? "",
      type: node.type === "dir" || node.type === "note" ? type : (node.type ?? type),
      kind: node.type === "dir" || node.type === "note" ? node.type : undefined,
      children: (node.children ?? []).map((child: any) => walk(child, type)),
    });
    return roots.map((node) => walk(node, node.name ?? node.type ?? "other"));
  },
  /** `raw` is the whole document (frontmatter + body); links come back as paths. */
  vaultNote: async (path: string): Promise<VaultNote> => {
    const raw = await get<any>("/vault/note", { path });
    const ref = (p: any) =>
      typeof p === "string" ? { path: p, title: p.split("/").pop() ?? p } : { path: p.path, title: p.title ?? p.path };
    return {
      ...raw,
      path: raw.path ?? path,
      title: raw.title ?? (raw.frontmatter?.title as string) ?? path,
      type: raw.type ?? "",
      content: raw.raw ?? raw.content ?? raw.body ?? "",
      modified: raw.modified ?? (raw.frontmatter?.modified as string | undefined) ?? null,
      links: (raw.links ?? []).map(ref),
      backlinks: (raw.backlinks ?? []).map(ref),
    };
  },
  saveNote: (path: string, content: string) => put<VaultNote>("/vault/note", { path, content }),
  deleteNote: (path: string) => del<{ path: string; deleted: boolean }>("/vault/note", { path }),
  /** One revertible commit for the whole folder; 400 with `detail` for core/ and the root. */
  deleteFolder: (path: string) => del<{ path: string; deleted: string[] }>("/vault/folder", { path }),
  vaultSearch: (q: string, type?: string) => get<VaultSearchHit[]>("/vault/search", { q, type }),
  vaultHistory: (path: string) => get<VaultChange[]>("/vault/history", { path }),
  recentChanges: (n = 30) => get<VaultChange[]>("/vault/recent-changes", { n }),
  vaultDiff: (sha: string, path?: string) => get<{ diff: string }>("/vault/diff", { sha, path }),
  revert: (commit: string, path?: string) =>
    post<unknown>("/vault/revert", { commit, ...(path ? { path } : {}) }),
  vaultGraph: async (center?: string, depth = 1, limit?: number): Promise<GraphData> => {
    const raw = await get<any>("/vault/graph", { center, depth, limit });
    return {
      nodes: (raw?.nodes ?? []).map((n: any) => ({ id: n.id ?? n.path, title: n.title, type: n.type })),
      edges: (raw?.edges ?? []).map((e: any) => ({ src: e.src, dst: e.dst })),
    };
  },
  /** The desk/thread tree, pinned notes and walkthroughs as graph nodes/edges — the
   *  Graph screen's conversations layer (03 §13.1). */
  conversationGraph: () => get<ConversationGraphData>("/conversations/graph"),
  /** A thread's status: the `status` events in its transcript. */
  conversationStatus: (id: string) => get<ThreadStatus>(`/conversations/${id}/status`),
  saveConversationStatus: (id: string, fields: Record<string, string>) =>
    put<ThreadStatus>(`/conversations/${id}/status`, { fields }),
  vaultCore: () => get<CoreMeter[]>("/vault/core"),
  vaultWanted: () => get<{ target: string; sources: string[] }[]>("/vault/wanted"),
  vaultProcedures: () => get<Procedure[]>("/vault/procedures"),
  approveProcedure: (name: string) => post<Procedure>(`/vault/procedures/${encodeURIComponent(name)}/approve`),

  // -- settings --------------------------------------------------------------
  settings: () => get<SettingsPayload>("/settings"),
  patchSettings: (patchBody: Record<string, unknown>) =>
    patch<{ values: Record<string, unknown> }>("/settings", patchBody),

  // -- models ----------------------------------------------------------------
  providers: () => get<Provider[]>("/providers"),
  createProvider: (body: Partial<Provider>) => post<Provider>("/providers", body),
  updateProvider: (name: string, body: Partial<Provider>) =>
    patch<Provider>(`/providers/${encodeURIComponent(name)}`, body),
  deleteProvider: (name: string) => del<unknown>(`/providers/${encodeURIComponent(name)}`),
  probeProvider: (name: string, role?: string) =>
    post<{ ok?: boolean; models: string[]; error?: string }>(
      `/providers/${encodeURIComponent(name)}/probe${role ? `?role=${encodeURIComponent(role)}` : ""}`,
    ),
  presets: () => get<Preset[]>("/presets"),
  createPreset: (body: Partial<Preset>) => post<Preset>("/presets", body),
  updatePreset: (name: string, body: Partial<Preset>) =>
    patch<Preset>(`/presets/${encodeURIComponent(name)}`, body),
  deletePreset: (name: string) => del<unknown>(`/presets/${encodeURIComponent(name)}`),
  testPreset: (name: string) =>
    post<{ ok: boolean; latency_ms?: number; usage?: Record<string, number>; content?: string; error?: string }>(
      `/presets/${encodeURIComponent(name)}/test`,
    ),
  modelRoles: () => get<ModelRole[]>("/model-roles"),
  setModelRole: (role: string, preset: string | null) =>
    put<ModelRole>(`/model-roles/${encodeURIComponent(role)}`, { preset }),
  /** `/models` answers `{providers: [{provider, models, error}]}`; the UI wants names. */
  models: async (provider?: string): Promise<string[]> => {
    const raw = await get<any>("/models", { provider });
    if (Array.isArray(raw)) return raw as string[];
    if (Array.isArray(raw?.models)) return raw.models as string[];
    const out: string[] = [];
    for (const entry of raw?.providers ?? []) {
      if (provider && entry.provider !== provider) continue;
      for (const model of entry.models ?? []) if (!out.includes(model)) out.push(model);
    }
    return out;
  },

  // -- permissions -----------------------------------------------------------
  toolPolicies: () => get<ToolPolicy[]>("/tool-policies"),
  setToolPolicy: (tool: string, mode: string) =>
    put<ToolPolicy>(`/tool-policies/${encodeURIComponent(tool)}`, { mode }),
  resetToolPolicy: (tool: string) => del<unknown>(`/tool-policies/${encodeURIComponent(tool)}`),
  roots: () => get<Root[]>("/roots"),
  addRoot: (path: string) => post<Root>("/roots", { path }),
  removeRoot: (path: string) => del<unknown>("/roots", { path }),
  protectedPaths: () => get<ProtectedPath[]>("/protected-paths"),
  addProtectedPath: (path: string, mode: ProtectedMode = "all") =>
    post<ProtectedPath>("/protected-paths", { path, mode }),
  /** `writes` → `all` applies at once (the row); `all` → `writes` weakens it, so the
   *  answer is a pending approval (202), like a removal. */
  setProtectedPathMode: (path: string, mode: ProtectedMode) =>
    put<ProtectedPath | { approval?: { id: string }; path: string }>("/protected-paths", { path, mode }),
  removeProtectedPath: (path: string) =>
    request<{ approval?: { id: string }; path?: string }>("DELETE", "/protected-paths", undefined, { path }),

  // -- integrations ----------------------------------------------------------
  mcpServers: () => get<McpServer[]>("/mcp-servers"),
  createMcpServer: (body: Partial<McpServer>) => post<McpServer>("/mcp-servers", body),
  updateMcpServer: (id: string, body: Partial<McpServer>) => patch<McpServer>(`/mcp-servers/${id}`, body),
  deleteMcpServer: (id: string) => del<unknown>(`/mcp-servers/${id}`),
  refreshMcpServer: (id: string) => post<McpServer>(`/mcp-servers/${id}/refresh`),
  /** `tools` is the tool *count* the throwaway client saw (connectors/mcp/manager.py). */
  testMcpServer: (id: string) =>
    post<{
      ok: boolean;
      latency_ms: number | null;
      tools: number;
      server?: Record<string, unknown>;
      error?: string | null;
    }>(`/mcp-servers/${id}/test`),
  connectors: () => get<Connector[]>("/connectors"),
  updateConnector: (name: string, body: Partial<Connector>) =>
    patch<Connector>(`/connectors/${encodeURIComponent(name)}`, body),
  reloadConnector: (name: string) => post<Connector>(`/connectors/${encodeURIComponent(name)}/reload`),
  secrets: () => get<SecretsInfo>("/secrets"),
  putSecret: (name: string, value: string) => put<unknown>(`/secrets/${encodeURIComponent(name)}`, { value }),
  deleteSecret: (name: string) => del<unknown>(`/secrets/${encodeURIComponent(name)}`),

  // -- triggers --------------------------------------------------------------
  schedules: () => get<Schedule[]>("/schedules"),
  createSchedule: (body: Partial<Schedule>) => post<Schedule>("/schedules", body),
  updateSchedule: (id: string, body: Partial<Schedule>) => patch<Schedule>(`/schedules/${id}`, body),
  deleteSchedule: (id: string) => del<unknown>(`/schedules/${id}`),
  runSchedule: (id: string) => post<unknown>(`/schedules/${id}/run`),

  // -- system ----------------------------------------------------------------
  health: () => get<Health>("/system/health"),
  version: () => get<VersionInfo>("/system/version"),
  queue: () => get<QueueState>("/system/queue"),
  logs: async (params?: { since?: string; level?: string; limit?: number }): Promise<LogLine[]> => {
    const raw = await get<any>("/system/logs", params);
    return Array.isArray(raw) ? raw : (raw?.records ?? raw?.lines ?? []);
  },
  /** What promoting `ref` would change; 400/409 for a bad ref or a dirty tree. */
  promotePlan: (ref: string) => get<PromotePlan>("/system/promote/plan", { ref }),
  /** 202 on start; 409 with the protected-file list unless `acknowledge_protected`.
   *  `skip_tests` leaves the supervisor's suite out; the promote log says so. */
  promote: (ref: string, acknowledge_protected = false, skip_tests = false) =>
    post<unknown>("/system/promote", { ref, acknowledge_protected, skip_tests }),
  /** Fast-forward the checkout from its upstream; 409 when dirty, detached or diverged. */
  pullCheckout: () => post<PullResult>("/system/pull", {}),
  promoteLog: (limit = 200) => get<PromoteLogEntry[]>("/system/promote/log", { limit }),
  rollback: () => post<unknown>("/system/rollback"),
  restart: () => post<unknown>("/system/restart"),
  supervisor: () => get<SupervisorStatus>("/system/supervisor"),
  reindex: () => post<unknown>("/system/reindex"),
  /** Show a folder (or a file's folder) in the OS file manager; vault-relative is fine. */
  openPath: (path: string) => post<{ opened: string }>("/system/open", { path }),
  snapshots: () => get<Snapshot[]>("/system/snapshots"),

  // -- maintenance (03 §8.6) -------------------------------------------------
  maintenance: () => get<MaintenanceStatus>("/maintenance"),
  /** 202 with the fresh status; 409 when a pass is already in flight. */
  runMaintenance: () => post<{ started: boolean } & MaintenanceStatus>("/maintenance/run"),
  /** Artifacts disk use: a walk of the whole folder, so ask on demand, never on a timer. */
  artifactsUsage: (limit = 10) => get<import("./types").ArtifactsUsage>("/maintenance/artifacts", { limit }),
};

export type Api = typeof api;
