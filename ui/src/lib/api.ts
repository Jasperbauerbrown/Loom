// REST client (03 §12.1 + the extras listed in the build brief). One request helper:
// JSON in, JSON out, `{"detail": ...}` errors raised as ApiError and surfaced as toasts.

import type {
  Approval,
  Artifact,
  Connector,
  Conversation,
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
  ProtectedPath,
  Provider,
  QueueState,
  Root,
  Schedule,
  SecretsInfo,
  SettingsPayload,
  SetupState,
  Snapshot,
  SupervisorStatus,
  ToolPolicy,
  VaultChange,
  VaultNote,
  VaultSearchHit,
  VaultStatus,
  VaultTreeNode,
  VersionInfo,
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
    return localStorage.getItem("loom_token");
  } catch {
    return null;
  }
}

export function setToken(value: string | null) {
  try {
    if (value) localStorage.setItem("loom_token", value);
    else localStorage.removeItem("loom_token");
  } catch {
    /* private mode: tokens simply do not persist */
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
  const t = token();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res: Response;
  try {
    res = await fetch(`/api${path}${qs(params)}`, {
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
    scope_note?: string;
    roots?: string[];
    preset?: string;
    role?: string;
    parent_id?: string;
  }) => post<Conversation>("/conversations", body),
  events: (id: string, params?: { after?: number; before?: number; limit?: number; kinds?: string }) =>
    get<ConvEvent[]>(`/conversations/${id}/events`, params),
  sendMessage: (id: string, content: string) =>
    post<unknown>(`/conversations/${id}/messages`, { content }),
  cancel: (id: string) => post<unknown>(`/conversations/${id}/cancel`),
  /** Cut the replayed context; the transcript stays. 409 while a turn runs or waits. */
  clearConversation: (id: string) => post<Conversation>(`/conversations/${id}/clear`),
  archive: (id: string) => post<Conversation>(`/conversations/${id}/archive`),
  occupancy: (id: string) => get<Occupancy>(`/conversations/${id}/occupancy`),
  conversationArtifacts: (id: string) => get<Artifact[]>(`/conversations/${id}/artifacts`),

  // -- artifacts -------------------------------------------------------------
  artifact: (id: string, params?: { offset?: number; limit?: number }) =>
    get<{ id: string; text?: string; content?: string; bytes: number; offset?: number; limit?: number }>(
      `/artifacts/${id}`,
      params,
    ),

  // -- approvals / items -----------------------------------------------------
  approvals: (params?: { status?: string }) => get<Approval[]>("/approvals", params),
  decideApproval: (id: string, decision: "allow" | "deny" | "allow_always", add_root?: boolean) =>
    post<Approval>(`/approvals/${id}`, { decision, ...(add_root ? { add_root } : {}) }),
  items: (params?: { unseen?: boolean }) => get<Item[]>("/items", params),
  seenItem: (id: string) => post<Item>(`/items/${id}/seen`),
  dismissItem: (id: string) => post<Item>(`/items/${id}/dismiss`),

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
  vaultGraph: async (center?: string, depth = 1): Promise<GraphData> => {
    const raw = await get<any>("/vault/graph", { center, depth });
    return {
      nodes: (raw?.nodes ?? []).map((n: any) => ({ id: n.id ?? n.path, title: n.title, type: n.type })),
      edges: (raw?.edges ?? []).map((e: any) => ({ src: e.src, dst: e.dst })),
    };
  },
  vaultStatus: (path: string) => get<VaultStatus>("/vault/status", { path }),
  saveVaultStatus: (path: string, fields: Record<string, string>) =>
    put<VaultStatus>("/vault/status", { path, fields }),
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
  probeProvider: (name: string) =>
    post<{ ok?: boolean; models: string[]; error?: string }>(
      `/providers/${encodeURIComponent(name)}/probe`,
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
  addProtectedPath: (path: string) => post<ProtectedPath>("/protected-paths", { path }),
  removeProtectedPath: (path: string) =>
    request<{ detail?: string; approval_id?: string }>("DELETE", "/protected-paths", undefined, { path }),

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
  /** 202 on start; 409 with the protected-file list unless `acknowledge_protected`. */
  promote: (ref: string, acknowledge_protected = false) =>
    post<unknown>("/system/promote", { ref, acknowledge_protected }),
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
  setupState: async (): Promise<SetupState> => {
    const raw = await get<any>("/system/setup");
    return { ...raw, providers: raw?.providers ?? raw?.providers_detected ?? [] };
  },
  finishSetup: (body: {
    provider: string;
    model: string;
    embedding_model?: string;
    timezone: string;
    projects_root: string;
    openrouter?: { base_url: string; api_key: string };
  }) => post<unknown>("/system/setup", body),
};

export type Api = typeof api;
