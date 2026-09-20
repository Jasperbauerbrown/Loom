// Row and payload shapes from the backend (03 §4.2 schema, §12 API, §21.1 conventions).
// JSON columns arrive decoded; `*_at` are ISO-8601 UTC strings.

export type ConversationKind = "desk" | "thread";
export type ConversationStatus =
  | "idle"
  | "running"
  | "waiting_approval"
  | "waiting_input"
  | "done"
  | "failed"
  | "archived";

export interface Conversation {
  id: string;
  kind: ConversationKind;
  parent_id: string | null;
  title: string;
  goal: string;
  scope_note: string | null;
  roots: string[];
  depth: number;
  role: string;
  preset: string | null;
  status: ConversationStatus;
  step_budget: number | null;
  summary: string;
  summary_cutoff: number;
  token_factor: number;
  maint_watermark: number;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  archived_at: string | null;
  /** optional: doing-line from the thread's status block, when the API provides it */
  status_line?: string | null;
}

export type EventKind =
  | "user"
  | "assistant"
  | "tool_call"
  | "tool_result"
  | "thinking"
  | "system"
  | "compaction"
  | "status";

export interface ConvEvent {
  id: number;
  conversation_id: string;
  kind: EventKind;
  content: string;
  meta: Record<string, unknown>;
  ts: string;
  /** client-side only: set while an assistant/thinking event is still streaming */
  streaming?: boolean;
}

/** `tool_call.content` is JSON: a list of these. */
export interface ToolCallSpec {
  id: string;
  name: string;
  arguments: unknown;
}

export interface ToolCall {
  call_id: string;
  conversation_id: string;
  event_id?: number;
  name: string;
  args: unknown;
  policy?: string;
  reason?: string;
  decision?: string;
  ts: string;
  result?: ToolResult;
}

export interface ToolResult {
  call_id: string;
  ok: boolean;
  preview: string;
  artifact_id?: string | null;
  ts?: string;
}

export interface Approval {
  id: string;
  conversation_id: string;
  event_id: number;
  call_id: string;
  tool: string;
  args: Record<string, unknown>;
  reason: string;
  options: { add_root?: string } & Record<string, unknown>;
  status: "pending" | "allowed" | "denied";
  decision: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface Item {
  id: string;
  kind: "notification" | "setup" | "thread_event" | "schedule_result" | "knowledge" | string;
  title: string;
  body: string;
  conversation_id: string | null;
  source: string;
  action: Record<string, unknown>;
  created_at: string;
  seen_at: string | null;
  dismissed_at: string | null;
}

export interface Provider {
  name: string;
  kind: "ollama" | "openai_compatible";
  base_url: string;
  api_key_ref: string | null;
  params: Record<string, unknown>;
  enabled: number | boolean;
  created_at?: string;
  updated_at?: string;
}

export interface Preset {
  name: string;
  provider: string;
  model: string;
  params: Record<string, unknown>;
  notes: string;
  enabled: number | boolean;
  created_at?: string;
  updated_at?: string;
  /** optional enrichment: how many roles/conversations point at this preset */
  usage?: { roles: number; conversations: number } | number;
}

export interface ModelRole {
  role: string;
  preset: string | null;
}

export type PolicyMode = "allow" | "ask" | "classify";

export interface ToolPolicy {
  tool: string;
  mode: PolicyMode;
  source?: string;
  read_only?: boolean;
  explicit?: boolean;
}

export interface Root {
  path: string;
  added_at?: string;
  added_by?: string;
}

export interface ProtectedPath {
  path: string;
}

export interface McpServer {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command: string | null;
  args: string[];
  url: string | null;
  env: Record<string, string>;
  enabled: number | boolean;
  status: string;
  last_error: string | null;
  tools_cache: unknown;
  cached_at: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface Connector {
  name: string;
  enabled: number | boolean;
  config: Record<string, unknown>;
  status: string;
  last_error: string | null;
  updated_at?: string;
  /** JSON-schema-ish description of `config`, supplied by the connector manifest */
  config_schema?: JsonSchema | null;
  description?: string;
}

export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  properties?: Record<string, JsonSchema>;
  required?: string[];
  enum?: (string | number)[];
  default?: unknown;
  minimum?: number;
  maximum?: number;
  items?: JsonSchema;
}

export interface Schedule {
  id: string;
  spec: string;
  target: string;
  payload: string;
  policy: "interrupt" | "queue" | null;
  created_by: string;
  enabled: number | boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  runs: number;
  created_at?: string;
}

export interface Artifact {
  id: string;
  conversation_id: string;
  event_id: number | null;
  path: string;
  bytes: number;
  head: string;
  tail: string;
  created_at: string;
}

export interface SettingsField {
  key: string;
  group: string;
  type: "str" | "int" | "float" | "bool" | "list" | "enum";
  default: unknown;
  label: string;
  help: string;
  ui: "text" | "number" | "toggle" | "select" | "list" | "path" | "time" | "secret";
  min: number | null;
  max: number | null;
  choices: string[];
  restart: boolean;
}

export interface SettingsPayload {
  values: Record<string, unknown>;
  schema: SettingsField[];
  groups: Record<string, string>;
}

export interface Occupancy {
  occupancy_tokens: number;
  limit_tokens: number;
  effective_ctx: number;
  token_factor: number;
}

export interface QueueState {
  depth: number;
  running: unknown[];
}

export interface Health {
  ok?: boolean;
  status?: string;
  [k: string]: unknown;
}

export interface VersionInfo {
  version: string;
  release?: string | null;
  ref?: string | null;
  [k: string]: unknown;
}

export interface LogLine {
  ts?: string;
  level?: string;
  logger?: string;
  message?: string;
  [k: string]: unknown;
}

export interface Snapshot {
  id: string;
  path?: string;
  created_at?: string;
  bytes?: number;
  [k: string]: unknown;
}

export interface VaultNoteRef {
  path: string;
  title?: string;
  type?: string;
}

export interface VaultTreeNode {
  path: string;
  title: string;
  type: string;
  children?: VaultTreeNode[];
}

export interface VaultNote {
  path: string;
  title: string;
  type: string;
  content: string;
  frontmatter?: Record<string, unknown>;
  aliases?: string[];
  links: VaultNoteRef[];
  backlinks: VaultNoteRef[];
  modified?: string | null;
}

export interface VaultSearchHit {
  path: string;
  title: string;
  type: string;
  snippet?: string;
  score?: number;
}

export interface VaultChange {
  sha: string;
  ts: string;
  message: string;
  paths: string[];
}

export interface VaultStatus {
  fields: {
    state?: string;
    goal?: string;
    done?: string;
    doing?: string;
    next?: string;
    blockers?: string;
    updated?: string;
    [k: string]: string | undefined;
  };
  owner?: string | null;
}

export interface CoreMeter {
  path: string;
  chars: number;
  cap: number;
}

export interface Procedure {
  name: string;
  path?: string;
  approved?: boolean;
  description?: string;
  [k: string]: unknown;
}

export interface GraphData {
  nodes: { id: string; title?: string; type?: string }[];
  edges: { src: string; dst: string }[];
}

export interface SecretsInfo {
  names: string[];
  backend: string;
  warning?: string | null;
}

export interface SetupState {
  needed: boolean;
  providers?: { name: string; kind: string; base_url: string; models?: string[] }[];
  timezone?: string;
  projects_root?: string;
  embedding_model?: string | null;
  models?: string[];
  [k: string]: unknown;
}

/** WS envelope (03 §12.2). */
export interface BusEvent {
  type: string;
  ts: string;
  conversation_id: string | null;
  data: any;
}
