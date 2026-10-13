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
  /** Vault notes shown in the model's context every turn (03 §6.7): pinned by the
   *  thread (`knowledge_read(pin=true)`), by the spawner (`notes` at create) or here. */
  pinned_notes: string[];
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
  /** The daily-log note holding the thread's walkthrough section, set by finish. */
  walkthrough: string | null;
  /** The isolated git checkout this thread works in (03 §6.7), on branch `loom/<id>`;
   *  null unless it was spawned with isolation="worktree". Archiving releases it. */
  worktree: string | null;
  /** optional: doing-line from the thread's status, when the API provides it */
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

/** A stored image (03 §4.4): `img_<ulid>` beside the conversation's spill artifacts,
 *  served by GET /conversations/{id}/images/{image_id}. Rows carry refs, never bytes. */
export interface ImageRef {
  id: string;
  mime: string;
}

export interface ConvEvent {
  id: number;
  conversation_id: string;
  kind: EventKind;
  content: string;
  /** `images?: ImageRef[]` on user and tool_result rows; `inbox_id`/`source` on user rows;
   *  `text?: string` on tool_call rows: the model's narration before its calls, rendered
   *  as an assistant bubble above the tool rows */
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
  /** stopped by the user's cancel (03 §6.4); `ok` is false too */
  cancelled?: boolean;
  artifact_id?: string | null;
  /** images the tool returned, stored by the dispatcher (03 §4.4) */
  images?: ImageRef[];
  ts?: string;
}

/** The answers an approval takes, widening left to right: once, this conversation only
 *  (tool + exactly these arguments), the rule itself. Mirrors `approvals.DECISIONS`. */
export type ApprovalDecision = "allow" | "allow_conversation" | "allow_always" | "deny";

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
  decision: ApprovalDecision | null;
  decided_at: string | null;
  created_at: string;
}

/** An inbox row posted to a conversation and not yet rendered into its transcript:
 *  what the next step boundary will deliver. `GET /conversations/{id}/inbox` and the
 *  `inbox.posted` bus event carry this shape. */
export interface QueuedMessage {
  id: number;
  conversation_id: string;
  source: string;
  content: string;
  /** `images?: ImageRef[]` when the message was posted with attachments */
  meta: Record<string, unknown>;
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

/** `all`: any call touching the path asks. `writes`: only a call that writes, edits,
 *  moves or deletes there asks (a procedure's scripts: read and run freely). */
export type ProtectedMode = "all" | "writes";

export interface ProtectedPath {
  path: string;
  mode: ProtectedMode;
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

/** A connector row as `connectors.describe()` builds it: the db row plus everything the
 * Settings form needs to render without a second request. */
export interface Connector {
  name: string;
  enabled: number | boolean;
  config: Record<string, unknown>;
  /** one class per status (connectors/loader.py) */
  status: "disabled" | "needs_config" | "loaded" | "error" | string;
  last_error: string | null;
  updated_at?: string;
  /** JSON-schema-ish description of `config`, supplied by the connector manifest */
  config_schema?: JsonSchema | null;
  description?: string;
  facets?: string[];
  /** the tool names this connector registers */
  tools?: string[];
  /** false when the package would not import */
  available?: boolean;
  /** required config keys with no value — the reason for `needs_config` */
  missing_config?: string[];
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

/** One turn's token totals: `GET /conversations/{id}/usage` `turns` (03 §12.1) — the
 *  `assistant`/`tool_call` steps between one `user` event and the next. */
export interface ConversationTurnUsage {
  user_event_id: number;
  started_at: string;
  prompt_tokens: number;
  completion_tokens: number;
  steps: number;
}

/** `GET /conversations/{id}/usage`: token totals summed from `meta.usage` across the
 *  conversation's transcript, derived on every call and never stored (03 §12.1). */
export interface ConversationUsage {
  prompt_tokens: number;
  completion_tokens: number;
  steps: number;
  steps_with_usage: number;
  turns: ConversationTurnUsage[];
  /** Provider-reported totals after the latest manual Clear context marker. */
  since_clear: ConversationUsageTotals;
}

export interface ConversationUsageTotals {
  prompt_tokens: number;
  completion_tokens: number;
  steps: number;
  steps_with_usage: number;
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

/** `GET /system/snapshots`: a pre-promotion snapshot folder (03 §4.1). */
export interface Snapshot {
  id: string;
  path?: string;
  bytes?: number;
  has_state?: boolean;
  created_at?: string;
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
  /** the top-level group this node sits under (the vault's first-level folder) */
  type: string;
  /** what the node itself is, as `vault.tree()` reports it */
  kind?: "dir" | "note";
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

export interface StatusFields {
  state?: string;
  goal?: string;
  done?: string;
  doing?: string;
  next?: string;
  blockers?: string;
  updated?: string;
  [k: string]: string | undefined;
}

/** `GET`/`PUT /conversations/{id}/status`: a thread's status, kept as `status` events in
 *  its own transcript (`transcript`); the desk has none (`none`). */
export interface ThreadStatus {
  fields: StatusFields | null;
  owner: string | null;
  source: "transcript" | "none";
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

/** `GET /conversations/graph` (03 §12.1): the desk/thread tree plus what each
 *  conversation pins or wrote up, for the Graph screen's conversations layer. */
export interface ConversationGraphNode {
  id: string;
  kind: ConversationKind;
  title: string;
  goal: string;
  status: ConversationStatus;
  parent_id: string | null;
}

export interface ConversationGraphEdge {
  src: string;
  dst: string;
  kind: "spawned" | "pinned" | "walkthrough";
}

export interface ConversationGraphData {
  nodes: ConversationGraphNode[];
  edges: ConversationGraphEdge[];
}

export interface SecretsInfo {
  names: string[];
  backend: string;
  warning?: string | null;
}

/** WS envelope (03 §12.2). */
export interface BusEvent {
  type: string;
  ts: string;
  conversation_id: string | null;
  data: any;
}

// -- maintenance (03 §8.6) ----------------------------------------------------
// Shapes mirror `knowledge/maintenance.py`: `new_report()` and `Maintenance.status()`.

export interface MaintenanceConsolidated {
  conversation_id: string;
  events: number;
  writes: number;
}

export interface MaintenanceReport {
  started_at: string;
  finished_at: string | null;
  reason: string;
  consolidated: MaintenanceConsolidated[];
  curated: { candidates: number; applied: number };
  archived: number;
  /** spill rows dropped plus images deleted */
  pruned_artifacts: number;
  /** every file the artifacts prune removed (scratch included), and their bytes; absent on pre-2026-10-05 reports */
  pruned_files?: number;
  pruned_bytes?: number;
  /** entries the prune could not remove (locked or permission-denied) */
  prune_failures?: number;
  committed: string | null;
  external_commit: string | null;
  errors: string[];
  /** set on the in-flight marker returned by a second `run()` */
  in_progress?: boolean;
}

export interface MaintenanceStatus {
  enabled: boolean;
  running: boolean;
  /** the reason of the pass in flight, if any */
  reason: string | null;
  idle_seconds: number;
  idle_seconds_remaining: number | null;
  next_nightly_at: string;
  last_report: MaintenanceReport | null;
}

/** `GET /maintenance/artifacts` (2026-10-05): artifacts disk use. */
export interface ArtifactsUsageRow {
  id: string;
  /** null for a folder with no conversation row */
  title: string | null;
  kind: string | null;
  status: string | null;
  bytes: number;
  files: number;
}

export interface ArtifactsUsage {
  total_bytes: number;
  total_files: number;
  folders: number;
  /** the largest conversation folders, biggest first */
  conversations: ArtifactsUsageRow[];
}

export interface MaintenanceStage {
  stage: string;
  message: string;
  ts: string;
}

// -- promotion (03 §15) -------------------------------------------------------
// `promote.Plan.to_json()`.

/** `POST /system/pull`: the development checkout fast-forwarded from its upstream. */
export interface PullResult {
  ok: boolean;
  code: string;
  message: string;
  checkout: string;
  branch: string;
  upstream: string;
  before: string;
  after: string;
  commits: string[];
  live_commit: string;
}

export interface PromotePlan {
  ref: string;
  ok: boolean;
  error: string;
  code: string;
  commit: string;
  short: string;
  current_commit: string;
  changed_files: string[];
  protected_files: string[];
  dirty_files: string[];
  clean: boolean;
  worktree: string;
  repo: string;
}

/** One JSON line of `promote.log`; also the payload of `promote.progress`. */
export interface PromoteLogEntry {
  ts: string;
  stage: string;
  ok: boolean;
  message: string;
  ref?: string;
  conversation_id?: string;
}

/** `GET /system/supervisor`: the supervisor's own status, or why there is none. */
export interface SupervisorStatus {
  ok?: boolean;
  running: boolean;
  error?: string | null;
  pid?: number | null;
  release?: string | null;
  previous?: string | null;
  uptime_s?: number;
  crashes?: number;
  crashes_in_window?: number;
  backoff_s?: number;
  healthy?: boolean;
  last_error?: string;
  port?: number;
}

// -- background processes (03 §9.6, 2026-09-22) -------------------------------------

export type ProcessStatus = "starting" | "running" | "exited" | "stopped" | "failed" | "lost";

/** A long-running program the model started with `shell(background=true)`: a row
 *  written before the spawn, supervised while the backend runs, reconciled on start. */
export interface ProcessRow {
  id: string;
  conversation_id: string;
  title: string;
  command: string;
  cwd: string;
  pid: number | null;
  status: ProcessStatus;
  exit_code: number | null;
  error: string | null;
  log_path: string;
  log_bytes?: number;
  started_at: string;
  ended_at: string | null;
}

/** One window of a process log. Without `offset` the backend returns the tail. */
export interface ProcessLogPage {
  id: string;
  offset: number;
  end: number;
  total: number;
  text: string;
}
