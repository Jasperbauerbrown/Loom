// The store: one immutable state tree, one reducer per WS event type (03 §12.2), REST
// for initial loads. Every mutation on any surface arrives as a bus event, so the UI and
// the backend never disagree (charter §2.2).
//
// `reduce(state, event)` is pure and unit-tested; `Store` wraps it with subscriptions,
// loaders and toasts. Unknown event types are returned unchanged — surfaces render what
// they understand and ignore the rest.

import { useRef, useSyncExternalStore } from "react";
import { api, ApiError } from "./api";
import type {
  Approval,
  BusEvent,
  Connector,
  Conversation,
  ConvEvent,
  Health,
  Item,
  MaintenanceStage,
  MaintenanceStatus,
  McpServer,
  ModelRole,
  Occupancy,
  Preset,
  PromoteLogEntry,
  Provider,
  QueuedMessage,
  QueueState,
  Root,
  Schedule,
  SecretsInfo,
  SettingsPayload,
  Snapshot,
  SupervisorStatus,
  ToolCall,
  ToolCallSpec,
  ToolPolicy,
  VaultTreeNode,
  VersionInfo,
} from "./types";

export type ConnectionState = "connecting" | "open" | "closed";

export interface StreamBuffer {
  content: string;
  reasoning: string;
  event_id?: number;
  active: boolean;
}

export interface Toast {
  id: number;
  kind: "error" | "info" | "notification";
  title: string;
  body?: string;
  item_id?: string;
  conversation_id?: string | null;
}

export interface PromoteStage {
  stage: string;
  ok: boolean;
  message: string;
  ts: string;
}

export interface State {
  connection: ConnectionState;
  conversations: Record<string, Conversation>;
  events: Record<string, ConvEvent[]>;
  /** true when the oldest event of a conversation has been reached */
  historyComplete: Record<string, boolean>;
  streaming: Record<string, StreamBuffer>;
  toolCalls: Record<string, ToolCall>;
  approvals: Record<string, Approval>;
  items: Record<string, Item>;
  occupancy: Record<string, Occupancy>;
  /** inbox rows posted but not yet rendered into the transcript, per conversation */
  queued: Record<string, QueuedMessage[]>;
  /** composer text per conversation; survives navigation (and reloads via sessionStorage) */
  drafts: Record<string, string>;

  settings: SettingsPayload | null;
  settingsStale: boolean;
  providers: Provider[];
  presets: Preset[];
  roles: ModelRole[];
  modelsStale: boolean;
  toolPolicies: ToolPolicy[];
  roots: Root[];
  protectedPaths: string[];
  mcpServers: McpServer[];
  connectors: Connector[];
  schedules: Schedule[];
  secrets: SecretsInfo | null;

  queue: QueueState;
  health: Health | null;
  version: VersionInfo | null;
  snapshots: Snapshot[];
  /** live `promote.progress` events, oldest first */
  promote: PromoteStage[];
  /** the persisted promote.log, newest last */
  promoteLog: PromoteLogEntry[];
  supervisor: SupervisorStatus | null;
  maintenance: MaintenanceStatus | null;
  /** set when a pass finished: the System screen refetches the report */
  maintenanceStale: boolean;
  /** live `maintenance.progress` events for the pass in flight */
  maintenanceProgress: MaintenanceStage[];

  vaultTree: VaultTreeNode[] | null;
  knowledgeStale: number;

  toasts: Toast[];
  unseenCount: number;
}

export const initialState: State = {
  connection: "connecting",
  conversations: {},
  events: {},
  historyComplete: {},
  streaming: {},
  toolCalls: {},
  approvals: {},
  items: {},
  occupancy: {},
  queued: {},
  drafts: {},
  settings: null,
  settingsStale: false,
  providers: [],
  presets: [],
  roles: [],
  modelsStale: false,
  toolPolicies: [],
  roots: [],
  protectedPaths: [],
  mcpServers: [],
  connectors: [],
  schedules: [],
  secrets: null,
  queue: { depth: 0, running: [] },
  health: null,
  version: null,
  snapshots: [],
  promote: [],
  promoteLog: [],
  supervisor: null,
  maintenance: null,
  maintenanceStale: false,
  maintenanceProgress: [],
  vaultTree: null,
  knowledgeStale: 0,
  toasts: [],
  unseenCount: 0,
};

let toastSeq = 1;

// --- helpers ---------------------------------------------------------------

function nextId(list: ConvEvent[]): number {
  let max = 0;
  for (const e of list) if (e.id > max) max = e.id;
  return max + 1;
}

/** Insert or replace by id, keeping the list ordered by id ascending. */
export function upsertEvent(list: ConvEvent[], event: ConvEvent): ConvEvent[] {
  const idx = list.findIndex((e) => e.id === event.id);
  if (idx >= 0) {
    const out = list.slice();
    out[idx] = { ...out[idx], ...event };
    return out;
  }
  if (list.length === 0 || list[list.length - 1].id < event.id) return [...list, event];
  const out = [...list, event];
  out.sort((a, b) => a.id - b.id);
  return out;
}

export function mergeEvents(list: ConvEvent[], incoming: ConvEvent[]): ConvEvent[] {
  const byId = new Map<number, ConvEvent>();
  for (const e of list) byId.set(e.id, e);
  for (const e of incoming) byId.set(e.id, { ...byId.get(e.id), ...e });
  return [...byId.values()].sort((a, b) => a.id - b.id);
}

export function parseToolCalls(event: ConvEvent): ToolCallSpec[] {
  if (event.kind !== "tool_call") return [];
  try {
    const parsed = JSON.parse(event.content);
    if (Array.isArray(parsed)) return parsed as ToolCallSpec[];
    if (parsed && typeof parsed === "object") return [parsed as ToolCallSpec];
  } catch {
    /* malformed content is an expected input: render nothing rather than crash */
  }
  return [];
}

/** Register tool calls/results found in persisted events so history and live agree. */
export function indexToolCalls(
  calls: Record<string, ToolCall>,
  conversationId: string,
  events: ConvEvent[],
): Record<string, ToolCall> {
  let out = calls;
  const copy = () => (out === calls ? (out = { ...calls }) : out);
  for (const e of events) {
    if (e.kind === "tool_call") {
      const meta = (e.meta || {}) as Record<string, any>;
      for (const spec of parseToolCalls(e)) {
        const prev = out[spec.id];
        copy()[spec.id] = {
          ...prev,
          call_id: spec.id,
          conversation_id: conversationId,
          event_id: e.id,
          name: spec.name,
          args: spec.arguments,
          policy: meta.policy ?? prev?.policy,
          reason: meta.reason ?? prev?.reason,
          decision: meta.decision ?? prev?.decision,
          ts: e.ts,
        };
      }
    } else if (e.kind === "tool_result") {
      const meta = (e.meta || {}) as Record<string, any>;
      const callId = meta.call_id;
      if (!callId) continue;
      const prev = out[callId];
      copy()[callId] = {
        ...(prev ?? {
          call_id: callId,
          conversation_id: conversationId,
          name: meta.name ?? "",
          args: undefined,
          ts: e.ts,
        }),
        result: {
          call_id: callId,
          ok: meta.ok !== false,
          preview: e.content,
          artifact_id: meta.artifact_id ?? null,
          ts: e.ts,
        },
      };
    }
  }
  return out;
}

/** Drop the queued row a persisted `user` event came from (by `meta.inbox_id`). */
export function dequeue(
  queued: Record<string, QueuedMessage[]>,
  conversationId: string,
  event: ConvEvent,
): Record<string, QueuedMessage[]> {
  if (event.kind !== "user") return queued;
  const inboxId = (event.meta as Record<string, unknown> | undefined)?.inbox_id;
  if (typeof inboxId !== "number") return queued;
  const list = queued[conversationId];
  if (!list || !list.some((q) => q.id === inboxId)) return queued;
  return { ...queued, [conversationId]: list.filter((q) => q.id !== inboxId) };
}

/** A key/value store with the Web Storage shape; injectable so tests need no DOM. */
export interface KeyValueStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
}

export const DRAFTS_KEY = "loom.drafts";

function sessionStore(): KeyValueStorage | null {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null; // access itself can throw (blocked storage)
  }
}

/** Drafts saved by an earlier page in this tab; anything unreadable is "no drafts". */
export function loadDrafts(storage: KeyValueStorage | null = sessionStore()): Record<string, string> {
  if (!storage) return {};
  try {
    const raw = storage.getItem(DRAFTS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    const out: Record<string, string> = {};
    for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof v === "string" && v) out[k] = v;
    }
    return out;
  } catch {
    return {};
  }
}

export function saveDrafts(drafts: Record<string, string>, storage: KeyValueStorage | null = sessionStore()): void {
  if (!storage) return;
  try {
    storage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
  } catch {
    /* quota or blocked storage: the draft still lives in memory for this page */
  }
}

function countUnseen(items: Record<string, Item>): number {
  let n = 0;
  for (const it of Object.values(items)) if (!it.seen_at && !it.dismissed_at) n++;
  return n;
}

// --- the reducer -----------------------------------------------------------

export function reduce(state: State, event: BusEvent): State {
  const cid = event.conversation_id ?? (event.data && (event.data as any).conversation_id) ?? null;
  const data: any = event.data ?? {};

  switch (event.type) {
    case "conversation.created":
    case "conversation.updated": {
      const row = data as Conversation;
      if (!row || !row.id) return state;
      return { ...state, conversations: { ...state.conversations, [row.id]: row } };
    }

    case "message.start": {
      if (!cid) return state;
      return {
        ...state,
        streaming: {
          ...state.streaming,
          [cid]: { content: "", reasoning: "", event_id: data.event_id, active: true },
        },
      };
    }

    case "message.delta": {
      if (!cid) return state;
      const prev = state.streaming[cid] ?? { content: "", reasoning: "", active: true };
      const text = typeof data.text === "string" ? data.text : "";
      const buf: StreamBuffer =
        data.channel === "reasoning"
          ? { ...prev, reasoning: prev.reasoning + text, active: true }
          : { ...prev, content: prev.content + text, active: true };
      return { ...state, streaming: { ...state.streaming, [cid]: buf } };
    }

    case "event.appended": {
      // Every persisted event arrives here — the user's own messages, other surfaces'
      // messages, tool events, repairs. Upserting by id keeps history and live identical.
      const row = data as ConvEvent;
      const id = row?.conversation_id ?? cid;
      if (!row || typeof row.id !== "number" || !id) return state;
      const list = state.events[id] ?? [];
      return {
        ...state,
        events: { ...state.events, [id]: upsertEvent(list, { ...row, conversation_id: id }) },
        toolCalls: indexToolCalls(state.toolCalls, id, [row]),
        queued: dequeue(state.queued, id, row),
      };
    }

    case "inbox.posted": {
      // A message is queued the moment it is posted; the runner renders it into the
      // transcript at the next step boundary (a `user` event with meta.inbox_id).
      if (!cid || typeof data.id !== "number") return state;
      const list = state.queued[cid] ?? [];
      if (list.some((q) => q.id === data.id)) return state;
      const row: QueuedMessage = {
        id: data.id,
        conversation_id: cid,
        source: typeof data.source === "string" ? data.source : "user",
        content: typeof data.content === "string" ? data.content : "",
        meta: data.meta && typeof data.meta === "object" ? data.meta : {},
        created_at: typeof data.created_at === "string" ? data.created_at : event.ts,
      };
      const next = [...list, row].sort((a, b) => a.id - b.id);
      return { ...state, queued: { ...state.queued, [cid]: next } };
    }

    case "message.end": {
      if (!cid) return state;
      const buf = state.streaming[cid];
      const list = state.events[cid] ?? [];
      const streaming = { ...state.streaming };
      delete streaming[cid];
      const row = data.event as ConvEvent | undefined;
      if (row && typeof row.id === "number") {
        // The persisted row is the truth; keep the streamed reasoning when the row has
        // none of its own (reasoning may live in a separate `thinking` event).
        const meta = { ...(row.meta ?? {}) } as Record<string, unknown>;
        if (buf?.reasoning && !meta.reasoning) meta.reasoning = buf.reasoning;
        const final: ConvEvent = { ...row, conversation_id: cid, meta };
        return {
          ...state,
          streaming,
          events: { ...state.events, [cid]: upsertEvent(list, final) },
          toolCalls: indexToolCalls(state.toolCalls, cid, [final]),
        };
      }
      if (!buf || (!buf.content && !buf.reasoning)) return { ...state, streaming };
      const id = data.event_id ?? buf.event_id ?? nextId(list);
      const assistant: ConvEvent = {
        id,
        conversation_id: cid,
        kind: "assistant",
        content: buf.content,
        meta: buf.reasoning ? { reasoning: buf.reasoning } : {},
        ts: event.ts,
      };
      return { ...state, streaming, events: { ...state.events, [cid]: upsertEvent(list, assistant) } };
    }

    case "tool.call": {
      if (!cid || !data.call_id) return state;
      const list = state.events[cid] ?? [];
      const ev: ConvEvent = {
        id: data.event_id ?? nextId(list),
        conversation_id: cid,
        kind: "tool_call",
        content: JSON.stringify([{ id: data.call_id, name: data.name, arguments: data.args }]),
        meta: { policy: data.policy, reason: data.reason, call_id: data.call_id },
        ts: event.ts,
      };
      const events = { ...state.events, [cid]: upsertEvent(list, ev) };
      return {
        ...state,
        events,
        toolCalls: {
          ...state.toolCalls,
          [data.call_id]: {
            ...state.toolCalls[data.call_id],
            call_id: data.call_id,
            conversation_id: cid,
            event_id: ev.id,
            name: data.name,
            args: data.args,
            policy: data.policy,
            reason: data.reason,
            ts: event.ts,
          },
        },
      };
    }

    case "tool.result": {
      if (!cid || !data.call_id) return state;
      const list = state.events[cid] ?? [];
      const ev: ConvEvent = {
        id: data.event_id ?? nextId(list),
        conversation_id: cid,
        kind: "tool_result",
        content: data.preview ?? "",
        meta: { call_id: data.call_id, ok: data.ok, artifact_id: data.artifact_id ?? null },
        ts: event.ts,
      };
      const prev = state.toolCalls[data.call_id];
      return {
        ...state,
        events: { ...state.events, [cid]: upsertEvent(list, ev) },
        toolCalls: {
          ...state.toolCalls,
          [data.call_id]: {
            ...(prev ?? {
              call_id: data.call_id,
              conversation_id: cid,
              args: undefined,
              ts: event.ts,
            }),
            name: data.name ?? prev?.name ?? "",
            policy: data.policy ?? prev?.policy,
            decision: data.decision ?? prev?.decision,
            reason: data.reason ?? prev?.reason,
            result: {
              call_id: data.call_id,
              ok: data.ok !== false,
              preview: data.preview ?? "",
              artifact_id: data.artifact_id ?? null,
              ts: event.ts,
            },
          },
        },
      };
    }

    case "approval.requested": {
      const row = data as Approval;
      if (!row || !row.id) return state;
      return { ...state, approvals: { ...state.approvals, [row.id]: row } };
    }

    case "approval.resolved": {
      const row = data as Approval;
      if (!row || !row.id) return state;
      const approvals = { ...state.approvals };
      delete approvals[row.id];
      return { ...state, approvals };
    }

    case "item.created":
    case "item.updated": {
      const row = data as Item;
      if (!row || !row.id) return state;
      const items = { ...state.items, [row.id]: row };
      return { ...state, items, unseenCount: countUnseen(items) };
    }

    case "notification": {
      const toast: Toast = {
        id: toastSeq++,
        kind: "notification",
        title: data.title ?? "Notification",
        body: data.body,
        item_id: data.item_id,
        conversation_id: cid,
      };
      return { ...state, toasts: [...state.toasts, toast] };
    }

    case "knowledge.changed":
      return { ...state, knowledgeStale: state.knowledgeStale + 1, vaultTree: null };

    case "settings.changed":
      return { ...state, settingsStale: true };

    case "models.changed":
      return { ...state, modelsStale: true };

    case "connector.status": {
      const row = data as Connector;
      if (!row || !row.name) return state;
      const idx = state.connectors.findIndex((c) => c.name === row.name);
      const connectors = state.connectors.slice();
      if (idx >= 0) connectors[idx] = { ...connectors[idx], ...row };
      else connectors.push(row);
      return { ...state, connectors };
    }

    case "mcp.status": {
      const row = data as McpServer;
      if (!row || !row.id) return state;
      const idx = state.mcpServers.findIndex((m) => m.id === row.id);
      const mcpServers = state.mcpServers.slice();
      if (idx >= 0) mcpServers[idx] = { ...mcpServers[idx], ...row };
      else mcpServers.push(row);
      return { ...state, mcpServers };
    }

    case "system.queue":
      return { ...state, queue: { depth: data.depth ?? 0, running: data.running ?? [] } };

    case "promote.progress":
      return {
        ...state,
        promote: [
          ...state.promote,
          { stage: data.stage ?? "", ok: data.ok !== false, message: data.message ?? "", ts: data.ts ?? event.ts },
        ],
      };

    case "maintenance.progress": {
      // `start` opens a pass: drop the previous pass's lines so the panel shows this one.
      const stage: MaintenanceStage = {
        stage: data.stage ?? "",
        message: data.message ?? "",
        ts: event.ts,
      };
      const lines = stage.stage === "start" ? [stage] : [...state.maintenanceProgress, stage];
      const maintenance = state.maintenance
        ? { ...state.maintenance, running: stage.stage !== "done" }
        : state.maintenance;
      return { ...state, maintenanceProgress: lines, maintenance, maintenanceStale: stage.stage === "done" };
    }

    case "schedule.created":
    case "schedule.updated": {
      const row = data as Schedule;
      if (!row || !row.id) return state;
      const idx = state.schedules.findIndex((s) => s.id === row.id);
      const schedules = state.schedules.slice();
      if (idx >= 0) schedules[idx] = { ...schedules[idx], ...row };
      else schedules.push(row);
      return { ...state, schedules };
    }

    case "schedule.deleted": {
      const row = data as Schedule;
      if (!row || !row.id) return state;
      const schedules = state.schedules.filter((s) => s.id !== row.id);
      return schedules.length === state.schedules.length ? state : { ...state, schedules };
    }

    default:
      return state; // unknown types are ignored, never an error
  }
}

// --- the store -------------------------------------------------------------

type Listener = () => void;

export class Store {
  state: State = { ...initialState, drafts: loadDrafts() };
  private listeners = new Set<Listener>();

  subscribe = (fn: Listener) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  getState = () => this.state;

  set(update: (s: State) => State) {
    const next = update(this.state);
    if (next === this.state) return;
    this.state = next;
    for (const fn of this.listeners) fn();
  }

  dispatch = (event: BusEvent) => {
    this.set((s) => reduce(s, event));
  };

  // -- toasts ---------------------------------------------------------------
  toast(kind: Toast["kind"], title: string, body?: string) {
    const toast: Toast = { id: toastSeq++, kind, title, body };
    this.set((s) => ({ ...s, toasts: [...s.toasts, toast] }));
    return toast.id;
  }

  dismissToast(id: number) {
    this.set((s) => ({ ...s, toasts: s.toasts.filter((t) => t.id !== id) }));
  }

  error(e: unknown) {
    const message = e instanceof ApiError ? e.message : String((e as Error)?.message ?? e);
    this.toast("error", message);
    return message;
  }

  /** Run an API call, surfacing failures as toasts. Returns undefined on failure. */
  async guard<T>(fn: () => Promise<T>): Promise<T | undefined> {
    try {
      return await fn();
    } catch (e) {
      this.error(e);
      return undefined;
    }
  }

  /** Like `guard`, but a 404 means "this capability is not installed here" and is an
   * empty result rather than an error the user must read. */
  async guardOptional<T>(fn: () => Promise<T>): Promise<T | undefined> {
    try {
      return await fn();
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return undefined;
      this.error(e);
      return undefined;
    }
  }

  setConnection(connection: ConnectionState) {
    this.set((s) => (s.connection === connection ? s : { ...s, connection }));
  }

  // -- drafts ---------------------------------------------------------------
  /** The composer text for one conversation. Empty text forgets the draft. */
  setDraft(id: string, text: string) {
    this.set((s) => {
      if ((s.drafts[id] ?? "") === text) return s;
      const drafts = { ...s.drafts };
      if (text) drafts[id] = text;
      else delete drafts[id];
      saveDrafts(drafts);
      return { ...s, drafts };
    });
  }

  // -- loaders --------------------------------------------------------------
  async loadConversations() {
    const rows = await this.guard(() => api.conversations());
    if (!rows) return;
    this.set((s) => {
      const conversations = { ...s.conversations };
      for (const row of rows) conversations[row.id] = row;
      return { ...s, conversations };
    });
  }

  async loadConversation(id: string) {
    const row = await this.guard(() => api.conversation(id));
    if (!row) return;
    this.set((s) => ({ ...s, conversations: { ...s.conversations, [row.id]: row } }));
  }

  /** Load a page of history. `before` pages backwards from the oldest known event. */
  async loadEvents(id: string, opts: { before?: number; limit?: number } = {}) {
    const limit = opts.limit ?? 100;
    const rows = await this.guard(() => api.events(id, { before: opts.before, limit }));
    if (!rows) return;
    this.set((s) => {
      const merged = mergeEvents(s.events[id] ?? [], rows);
      return {
        ...s,
        events: { ...s.events, [id]: merged },
        toolCalls: indexToolCalls(s.toolCalls, id, rows),
        historyComplete: { ...s.historyComplete, [id]: rows.length < limit },
      };
    });
  }

  async loadApprovals() {
    const rows = await this.guard(() => api.approvals({ status: "pending" }));
    if (!rows) return;
    this.set((s) => {
      const approvals: Record<string, Approval> = {};
      for (const row of rows) approvals[row.id] = row;
      return { ...s, approvals };
    });
  }

  async loadItems() {
    const rows = await this.guard(() => api.items());
    if (!rows) return;
    this.set((s) => {
      const items: Record<string, Item> = {};
      for (const row of rows) items[row.id] = row;
      return { ...s, items, unseenCount: countUnseen(items) };
    });
  }

  /** Inbox rows not yet in the transcript. REST is the truth here: a reconnect may have
   * missed `inbox.posted`, or the row may already have been rendered. A 404 means an
   * older backend without the endpoint — then nothing is shown as queued. */
  async loadQueued(id: string) {
    const rows = await this.guardOptional(() => api.inbox(id));
    if (!rows) return;
    const sorted = [...rows].sort((a, b) => a.id - b.id);
    this.set((s) => ({ ...s, queued: { ...s.queued, [id]: sorted } }));
  }

  async loadOccupancy(id: string) {
    const row = await this.guard(() => api.occupancy(id));
    if (!row) return;
    this.set((s) => ({ ...s, occupancy: { ...s.occupancy, [id]: row } }));
  }

  async loadSettings() {
    const payload = await this.guard(() => api.settings());
    if (!payload) return;
    this.set((s) => ({ ...s, settings: payload, settingsStale: false }));
  }

  async loadModels() {
    const [providers, presets, roles] = await Promise.all([
      this.guard(() => api.providers()),
      this.guard(() => api.presets()),
      this.guard(() => api.modelRoles()),
    ]);
    this.set((s) => ({
      ...s,
      providers: providers ?? s.providers,
      presets: presets ?? s.presets,
      roles: roles ?? s.roles,
      modelsStale: false,
    }));
  }

  async loadPermissions() {
    const [toolPolicies, roots, protectedPaths] = await Promise.all([
      this.guard(() => api.toolPolicies()),
      this.guard(() => api.roots()),
      this.guard(() => api.protectedPaths()),
    ]);
    this.set((s) => ({
      ...s,
      toolPolicies: toolPolicies ?? s.toolPolicies,
      roots: roots ?? s.roots,
      protectedPaths: protectedPaths
        ? protectedPaths.map((p: any) => (typeof p === "string" ? p : p.path))
        : s.protectedPaths,
    }));
  }

  async loadIntegrations() {
    const [mcpServers, connectors, secrets] = await Promise.all([
      this.guardOptional(() => api.mcpServers()),
      this.guardOptional(() => api.connectors()),
      this.guardOptional(() => api.secrets()),
    ]);
    this.set((s) => ({
      ...s,
      mcpServers: mcpServers ?? s.mcpServers,
      connectors: connectors ?? s.connectors,
      secrets: secrets ?? s.secrets,
    }));
  }

  async loadSchedules() {
    const schedules = await this.guardOptional(() => api.schedules());
    if (!schedules) return;
    this.set((s) => ({ ...s, schedules }));
  }

  async loadSystem() {
    const [health, version, queue] = await Promise.all([
      this.guard(() => api.health()),
      this.guard(() => api.version()),
      this.guard(() => api.queue()),
    ]);
    this.set((s) => ({
      ...s,
      health: health ?? s.health,
      version: version ?? s.version,
      queue: queue ?? s.queue,
    }));
  }

  async loadMaintenance() {
    const status = await this.guardOptional(() => api.maintenance());
    if (!status) return;
    this.set((s) => ({ ...s, maintenance: status, maintenanceStale: false }));
  }

  /** Supervisor status + the persisted promotion log (03 §15.2 step 10). */
  async loadPromote() {
    const [supervisor, promoteLog] = await Promise.all([
      this.guardOptional(() => api.supervisor()),
      this.guardOptional(() => api.promoteLog()),
    ]);
    this.set((s) => ({
      ...s,
      supervisor: supervisor ?? s.supervisor,
      promoteLog: promoteLog ?? s.promoteLog,
    }));
  }

  async loadSnapshots() {
    const snapshots = await this.guardOptional(() => api.snapshots());
    if (!snapshots) return;
    this.set((s) => ({ ...s, snapshots }));
  }

  async loadVaultTree() {
    const tree = await this.guard(() => api.vaultTree());
    if (!tree) return;
    this.set((s) => ({ ...s, vaultTree: tree }));
  }

  /** Everything a fresh connection needs (also run after a reconnect). */
  async refreshAll() {
    await Promise.all([
      this.loadConversations(),
      this.loadApprovals(),
      this.loadItems(),
      this.loadSystem(),
    ]);
  }
}

export const store = new Store();

// --- React binding ---------------------------------------------------------

export function shallowEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  const ka = Object.keys(a as object);
  const kb = Object.keys(b as object);
  if (ka.length !== kb.length) return false;
  for (const k of ka) {
    if (!Object.is((a as any)[k], (b as any)[k])) return false;
  }
  return true;
}

export function useStore<T>(selector: (s: State) => T, isEqual: (a: T, b: T) => boolean = shallowEqual): T {
  const cache = useRef<{ state: State; value: T } | null>(null);
  const getSnapshot = () => {
    const state = store.getState();
    const prev = cache.current;
    if (prev && prev.state === state) return prev.value;
    const value = selector(state);
    if (prev && isEqual(prev.value, value)) {
      cache.current = { state, value: prev.value };
      return prev.value;
    }
    cache.current = { state, value };
    return value;
  };
  return useSyncExternalStore(store.subscribe, getSnapshot, getSnapshot);
}

/** Conversations sorted newest-updated first. */
export function conversationList(s: State): Conversation[] {
  return Object.values(s.conversations).sort((a, b) => (a.updated_at < b.updated_at ? 1 : -1));
}

export function pendingApprovals(s: State): Approval[] {
  return Object.values(s.approvals)
    .filter((a) => a.status === "pending")
    .sort((a, b) => (a.created_at < b.created_at ? -1 : 1));
}

export function deskId(s: State): string | null {
  for (const c of Object.values(s.conversations)) if (c.kind === "desk") return c.id;
  return null;
}
