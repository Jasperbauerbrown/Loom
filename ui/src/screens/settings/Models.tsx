// Models tab: Providers / Presets / Roles sub-tabs (03 §13.2, brief §"Models").

import { useMemo, useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import type { Preset, Provider, SettingsPayload } from "../../lib/types";
import {
  Badge,
  Button,
  Card,
  Confirm,
  Dialog,
  Empty,
  FieldRow,
  Input,
  JsonBlock,
  Select,
  Table,
  Td,
  Textarea,
  Toggle,
  Tr,
} from "../../components/ui";
import { cls } from "../../lib/format";
import { navigate } from "../../lib/router";
import { ScalarFields } from "./SettingsField";

type SubTab = "providers" | "presets" | "roles";
const SUB_TABS: SubTab[] = ["providers", "presets", "roles"];

export function ModelsTab({ settings, sub: routed = "" }: { settings: SettingsPayload; sub?: string }) {
  // The sub-tab lives in the URL (`#/settings/models/<sub>`); an unknown or empty
  // segment means the first tab.
  const sub: SubTab = (SUB_TABS as string[]).includes(routed) ? (routed as SubTab) : "providers";
  const setSub = (t: SubTab) => navigate(`/settings/models/${t}`);
  const providers = useStore((s) => s.providers);
  const presets = useStore((s) => s.presets);
  const roles = useStore((s) => s.roles);
  const conversations = useStore((s) => s.conversations);
  const secrets = useStore((s) => s.secrets);

  return (
    <div>
      <div className="mb-4 flex gap-1 border-b border-neutral-200 dark:border-neutral-800">
        {SUB_TABS.map((t) => (
          <button
            key={t}
            onClick={() => setSub(t)}
            className={cls(
              "border-b-2 px-3 py-1.5 text-[13px] capitalize",
              sub === t
                ? "border-sky-600 font-medium text-sky-700 dark:border-sky-500 dark:text-sky-400"
                : "border-transparent text-neutral-500 hover:text-neutral-800 dark:hover:text-neutral-200",
            )}
          >
            {t}
          </button>
        ))}
      </div>
      {sub === "providers" && <ProvidersPanel providers={providers} secretNames={secrets?.names ?? []} />}
      {sub === "presets" && (
        <PresetsPanel
          presets={presets}
          providers={providers}
          roles={roles}
          conversations={Object.values(conversations)}
        />
      )}
      {sub === "roles" && <RolesPanel roles={roles} presets={presets} settings={settings} />}
    </div>
  );
}

// -- Providers ----------------------------------------------------------------

function ProvidersPanel({ providers, secretNames }: { providers: Provider[]; secretNames: string[] }) {
  const [editing, setEditing] = useState<Provider | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<Provider | null>(null);
  const [probeResults, setProbeResults] = useState<Record<string, { models?: string[]; error?: string }>>({});
  const [probing, setProbing] = useState<string | null>(null);

  async function probe(name: string) {
    setProbing(name);
    const res = await store.guard(() => api.probeProvider(name));
    setProbing(null);
    if (res) setProbeResults((prev) => ({ ...prev, [name]: { models: res.models, error: res.error } }));
  }

  return (
    <Card className="p-3">
      <div className="mb-2 flex justify-end">
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Add provider
        </Button>
      </div>
      {providers.length === 0 ? (
        <Empty>No providers configured.</Empty>
      ) : (
        <Table head={["Name", "Kind", "Base URL", "Enabled", "Probe", ""]}>
          {providers.map((p) => (
            <Tr key={p.name}>
              <Td className="font-medium">{p.name}</Td>
              <Td>{p.kind}</Td>
              <Td className="font-mono text-[12px]">{p.base_url}</Td>
              <Td>{p.enabled ? <Badge tone="green">enabled</Badge> : <Badge>disabled</Badge>}</Td>
              <Td>
                <div className="flex flex-col gap-1">
                  <Button size="sm" onClick={() => probe(p.name)} disabled={probing === p.name}>
                    {probing === p.name ? "Probing…" : "Probe"}
                  </Button>
                  {probeResults[p.name]?.error ? (
                    <span className="text-[11px] text-red-600 dark:text-red-400">{probeResults[p.name].error}</span>
                  ) : probeResults[p.name]?.models ? (
                    <span className="text-[11px] text-neutral-500">
                      {probeResults[p.name]!.models!.length} model(s): {probeResults[p.name]!.models!.join(", ")}
                    </span>
                  ) : null}
                </div>
              </Td>
              <Td>
                <div className="flex gap-1.5">
                  <Button size="sm" onClick={() => setEditing(p)}>
                    Edit
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => setToDelete(p)}>
                    Delete
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
      {(editing || creating) && (
        <ProviderDialog
          provider={editing}
          secretNames={secretNames}
          onClose={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      )}
      {toDelete && (
        <Confirm
          title={`Delete provider "${toDelete.name}"?`}
          body="Presets pointing at this provider will fail to resolve."
          danger
          confirmLabel="Delete"
          onClose={() => setToDelete(null)}
          onConfirm={() => void store.guard(() => api.deleteProvider(toDelete.name)).then(() => store.loadModels())}
        />
      )}
    </Card>
  );
}

function ProviderDialog({
  provider,
  secretNames,
  onClose,
}: {
  provider: Provider | null;
  secretNames: string[];
  onClose: () => void;
}) {
  const [name, setName] = useState(provider?.name ?? "");
  const [kind, setKind] = useState<Provider["kind"]>(provider?.kind ?? "ollama");
  const [baseUrl, setBaseUrl] = useState(provider?.base_url ?? "");
  const [apiKeyRef, setApiKeyRef] = useState(provider?.api_key_ref ?? "");
  const [paramsText, setParamsText] = useState(() => JSON.stringify(provider?.params ?? {}, null, 2));
  const [enabled, setEnabled] = useState(Boolean(provider?.enabled ?? true));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function save() {
    let params: Record<string, unknown>;
    try {
      params = paramsText.trim() ? JSON.parse(paramsText) : {};
    } catch {
      setError("params must be valid JSON");
      return;
    }
    if (!name.trim()) {
      setError("name is required");
      return;
    }
    setSaving(true);
    const body = { name, kind, base_url: baseUrl, api_key_ref: apiKeyRef || null, params, enabled };
    const res = provider
      ? await store.guard(() => api.updateProvider(provider.name, body))
      : await store.guard(() => api.createProvider(body));
    setSaving(false);
    if (res) {
      await store.loadModels();
      onClose();
    } else {
      setError("save failed");
    }
  }

  return (
    <Dialog
      title={provider ? `Edit provider: ${provider.name}` : "Add provider"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} disabled={saving}>
            Save
          </Button>
        </>
      }
    >
      <FieldRow label="Name">
        <Input value={name} disabled={!!provider} onChange={(e) => setName(e.target.value)} />
      </FieldRow>
      <FieldRow label="Kind">
        <Select value={kind} onChange={(e) => setKind(e.target.value as Provider["kind"])}>
          <option value="ollama">ollama</option>
          <option value="openai_compatible">openai_compatible</option>
        </Select>
      </FieldRow>
      <FieldRow label="Base URL">
        <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="http://localhost:11434" />
      </FieldRow>
      <FieldRow label="API key" help="Reference to a stored secret (see Integrations → Secrets).">
        <Select value={apiKeyRef ?? ""} onChange={(e) => setApiKeyRef(e.target.value)}>
          <option value="">(none)</option>
          {secretNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </Select>
      </FieldRow>
      <FieldRow label="Params (JSON)" help="timeout_s, concurrency, stream_idle_s.">
        <Textarea rows={5} className="font-mono" value={paramsText} onChange={(e) => setParamsText(e.target.value)} />
      </FieldRow>
      <FieldRow label="Enabled">
        <Toggle checked={enabled} onChange={setEnabled} />
      </FieldRow>
      {error && <p className="text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </Dialog>
  );
}

// -- Presets --------------------------------------------------------------

function presetUsage(preset: Preset, roles: { preset: string | null }[], conversations: { preset: string | null }[]) {
  if (preset.usage !== undefined) {
    if (typeof preset.usage === "number") return { roles: preset.usage, conversations: 0 };
    return preset.usage;
  }
  return {
    roles: roles.filter((r) => r.preset === preset.name).length,
    conversations: conversations.filter((c) => c.preset === preset.name).length,
  };
}

function PresetsPanel({
  presets,
  providers,
  roles,
  conversations,
}: {
  presets: Preset[];
  providers: Provider[];
  roles: { preset: string | null }[];
  conversations: { preset: string | null }[];
}) {
  const [editing, setEditing] = useState<Preset | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<Preset | null>(null);
  const [testResults, setTestResults] = useState<Record<string, { ok: boolean; latency_ms?: number; content?: string; error?: string }>>({});
  const [testing, setTesting] = useState<string | null>(null);

  async function test(name: string) {
    setTesting(name);
    const res = await store.guard(() => api.testPreset(name));
    setTesting(null);
    if (res) setTestResults((prev) => ({ ...prev, [name]: res }));
  }

  return (
    <Card className="p-3">
      <div className="mb-2 flex justify-end">
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Add preset
        </Button>
      </div>
      {presets.length === 0 ? (
        <Empty>No presets configured.</Empty>
      ) : (
        <Table head={["Name", "Provider", "Model", "Usage", "Test", ""]}>
          {presets.map((p) => {
            const usage = presetUsage(p, roles, conversations);
            const referenced = usage.roles > 0 || usage.conversations > 0;
            return (
              <Tr key={p.name}>
                <Td className="font-medium">{p.name}</Td>
                <Td>{p.provider}</Td>
                <Td className="font-mono text-[12px]">
                  {p.model}
                  {p.params?.multimodal ? (
                    <Badge className="ml-1.5 font-sans" title="Multimodal: images are sent as image parts">
                      img
                    </Badge>
                  ) : null}
                </Td>
                <Td>
                  {referenced ? (
                    <Badge tone="sky" title={`${usage.roles} role(s), ${usage.conversations} conversation(s)`}>
                      {usage.roles} role{usage.roles === 1 ? "" : "s"} · {usage.conversations} conv
                    </Badge>
                  ) : (
                    <Badge>unused</Badge>
                  )}
                </Td>
                <Td>
                  <div className="flex flex-col gap-1">
                    <Button size="sm" onClick={() => test(p.name)} disabled={testing === p.name}>
                      {testing === p.name ? "Testing…" : "Test"}
                    </Button>
                    {testResults[p.name] && (
                      <span className="text-[11px] text-neutral-500">
                        {testResults[p.name].error
                          ? testResults[p.name].error
                          : `${testResults[p.name].latency_ms ?? "?"}ms`}
                      </span>
                    )}
                  </div>
                </Td>
                <Td>
                  <div className="flex gap-1.5">
                    <Button size="sm" onClick={() => setEditing(p)}>
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      disabled={referenced}
                      title={referenced ? "In use by a role or conversation; cannot delete." : undefined}
                      onClick={() => setToDelete(p)}
                    >
                      Delete
                    </Button>
                  </div>
                </Td>
              </Tr>
            );
          })}
        </Table>
      )}
      {testResults && Object.keys(testResults).length > 0 && (
        <div className="mt-2 space-y-1">
          {Object.entries(testResults)
            .filter(([, r]) => r.content)
            .map(([name, r]) => (
              <JsonBlock key={name} value={{ preset: name, ...r }} />
            ))}
        </div>
      )}
      {(editing || creating) && (
        <PresetDialog
          preset={editing}
          providers={providers}
          onClose={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      )}
      {toDelete && (
        <Confirm
          title={`Delete preset "${toDelete.name}"?`}
          body="This cannot be undone."
          danger
          confirmLabel="Delete"
          onClose={() => setToDelete(null)}
          onConfirm={() => void store.guard(() => api.deletePreset(toDelete.name)).then(() => store.loadModels())}
        />
      )}
    </Card>
  );
}

function parseReasoning(v: unknown): { mode: "off" | "on" | "budget"; budget: string } {
  if (typeof v !== "string" || !v) return { mode: "off", budget: "" };
  if (v === "on") return { mode: "on", budget: "" };
  if (v === "off") return { mode: "off", budget: "" };
  const m = /^budget:(\d+)$/.exec(v);
  if (m) return { mode: "budget", budget: m[1] };
  return { mode: "off", budget: "" };
}

function PresetDialog({
  preset,
  providers,
  onClose,
}: {
  preset: Preset | null;
  providers: Provider[];
  onClose: () => void;
}) {
  const [name, setName] = useState(preset?.name ?? "");
  const [provider, setProvider] = useState(preset?.provider ?? providers[0]?.name ?? "");
  const [model, setModel] = useState(preset?.model ?? "");
  const [notes, setNotes] = useState(preset?.notes ?? "");
  const [enabled, setEnabled] = useState(Boolean(preset?.enabled ?? true));
  const p = preset?.params ?? {};
  const [maxTokens, setMaxTokens] = useState(String(p.max_tokens ?? ""));
  const [temperature, setTemperature] = useState(String(p.temperature ?? ""));
  const [topP, setTopP] = useState(String(p.top_p ?? ""));
  const [effectiveCtx, setEffectiveCtx] = useState(String(p.effective_ctx ?? ""));
  const [numCtx, setNumCtx] = useState(String(p.num_ctx ?? ""));
  const [keepAlive, setKeepAlive] = useState(String(p.keep_alive ?? ""));
  const [timeoutS, setTimeoutS] = useState(String(p.timeout_s ?? ""));
  const initReasoning = parseReasoning(p.reasoning);
  const [reasoningMode, setReasoningMode] = useState(initReasoning.mode);
  const [reasoningBudget, setReasoningBudget] = useState(initReasoning.budget);
  const [multimodal, setMultimodal] = useState(Boolean(p.multimodal));
  const [imageTokens, setImageTokens] = useState(String(p.image_tokens ?? ""));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [probedModels, setProbedModels] = useState<string[] | null>(null);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [probing, setProbing] = useState(false);

  async function probe() {
    if (!provider) return;
    setProbing(true);
    setProbeError(null);
    const res = await store.guard(() => api.probeProvider(provider));
    setProbing(false);
    if (!res) return;
    if (res.error) {
      // A failed probe must be visible here, not only in the providers table: it is
      // usually a wrong base URL or key, and typing a model name blind hides that.
      setProbeError(res.error);
      setProbedModels(null);
      return;
    }
    setProbedModels(res.models);
  }

  function num(v: string): number | undefined {
    if (v.trim() === "") return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  }

  async function save() {
    if (!name.trim() || !provider || !model.trim()) {
      setError("name, provider and model are required");
      return;
    }
    const reasoning =
      reasoningMode === "budget" ? `budget:${reasoningBudget || "0"}` : reasoningMode;
    const params: Record<string, unknown> = {
      ...(preset?.params ?? {}),
      max_tokens: num(maxTokens),
      temperature: num(temperature),
      top_p: num(topP),
      effective_ctx: num(effectiveCtx),
      num_ctx: num(numCtx),
      keep_alive: keepAlive || undefined,
      timeout_s: num(timeoutS),
      reasoning,
      multimodal,
      image_tokens: num(imageTokens),
    };
    setSaving(true);
    const body = { name, provider, model, notes, enabled, params };
    const res = preset
      ? await store.guard(() => api.updatePreset(preset.name, body))
      : await store.guard(() => api.createPreset(body));
    setSaving(false);
    if (res) {
      await store.loadModels();
      onClose();
    } else {
      setError("save failed");
    }
  }

  return (
    <Dialog
      title={preset ? `Edit preset: ${preset.name}` : "Add preset"}
      wide
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} disabled={saving}>
            Save
          </Button>
        </>
      }
    >
      <div className="grid grid-cols-2 gap-x-4">
        <FieldRow label="Name">
          <Input value={name} disabled={!!preset} onChange={(e) => setName(e.target.value)} />
        </FieldRow>
        <FieldRow label="Provider">
          <Select value={provider} onChange={(e) => setProvider(e.target.value)}>
            {providers.map((pr) => (
              <option key={pr.name} value={pr.name}>
                {pr.name}
              </option>
            ))}
          </Select>
        </FieldRow>
        <FieldRow label="Model" help={probeError ? `Probe failed: ${probeError}` : "Probe the provider to fill in choices, or type one."}>
          <div className="flex gap-1.5">
            {probedModels ? (
              <Select value={model} onChange={(e) => setModel(e.target.value)}>
                <option value={model}>{model || "(select)"}</option>
                {probedModels.filter((m) => m !== model).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </Select>
            ) : (
              <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="model name" />
            )}
            <Button size="sm" onClick={probe} disabled={probing}>
              {probing ? "…" : "Probe"}
            </Button>
          </div>
        </FieldRow>
        <FieldRow label="Enabled">
          <Toggle checked={enabled} onChange={setEnabled} />
        </FieldRow>
        <FieldRow label="Max tokens">
          <Input type="number" value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} />
        </FieldRow>
        <FieldRow label="Temperature">
          <Input type="number" step="any" value={temperature} onChange={(e) => setTemperature(e.target.value)} />
        </FieldRow>
        <FieldRow label="Top P">
          <Input type="number" step="any" value={topP} onChange={(e) => setTopP(e.target.value)} />
        </FieldRow>
        <FieldRow label="Effective context">
          <Input type="number" value={effectiveCtx} onChange={(e) => setEffectiveCtx(e.target.value)} />
        </FieldRow>
        <FieldRow label="num_ctx">
          <Input type="number" value={numCtx} onChange={(e) => setNumCtx(e.target.value)} />
        </FieldRow>
        <FieldRow label="Keep alive">
          <Input value={keepAlive} onChange={(e) => setKeepAlive(e.target.value)} placeholder="5m" />
        </FieldRow>
        <FieldRow label="Timeout (s)">
          <Input type="number" value={timeoutS} onChange={(e) => setTimeoutS(e.target.value)} />
        </FieldRow>
        <FieldRow label="Reasoning">
          <div className="flex gap-1.5">
            <Select value={reasoningMode} onChange={(e) => setReasoningMode(e.target.value as any)}>
              <option value="off">off</option>
              <option value="on">on</option>
              <option value="budget">budget</option>
            </Select>
            {reasoningMode === "budget" && (
              <Input
                type="number"
                value={reasoningBudget}
                onChange={(e) => setReasoningBudget(e.target.value)}
                placeholder="tokens"
              />
            )}
          </div>
        </FieldRow>
        <FieldRow
          label="Multimodal (vision input)"
          help="Send images to this model as image parts. Text-only models get a placeholder line instead."
        >
          <Toggle checked={multimodal} onChange={setMultimodal} label="Multimodal" />
        </FieldRow>
        <FieldRow label="Image tokens" help="Estimated prompt tokens per image, for occupancy and compaction.">
          <Input type="number" value={imageTokens} onChange={(e) => setImageTokens(e.target.value)} placeholder="1000" />
        </FieldRow>
        {preset?.params?.token_factor_seed !== undefined && (
          <FieldRow label="Token factor seed" help="Calibrated automatically; read-only.">
            <Input value={String(preset.params.token_factor_seed)} disabled readOnly />
          </FieldRow>
        )}
      </div>
      <FieldRow label="Notes">
        <Textarea rows={2} value={notes} onChange={(e) => setNotes(e.target.value)} />
      </FieldRow>
      {error && <p className="text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </Dialog>
  );
}

// -- Roles ------------------------------------------------------------------

const ROLE_NAMES = ["desk", "worker", "maintenance", "utility", "embedding"];

function RolesPanel({
  roles,
  presets,
  settings,
}: {
  roles: { role: string; preset: string | null }[];
  presets: Preset[];
  settings: SettingsPayload;
}) {
  const byRole = useMemo(() => {
    const m = new Map<string, string | null>();
    for (const r of roles) m.set(r.role, r.preset);
    return m;
  }, [roles]);

  async function setRole(role: string, preset: string) {
    await store.guard(() => api.setModelRole(role, preset || null));
    await store.loadModels();
  }

  return (
    <Card className="p-3">
      <div className="divide-y divide-neutral-100 dark:divide-neutral-800/70">
        {ROLE_NAMES.map((role) => (
          <FieldRow key={role} label={role}>
            <Select value={byRole.get(role) ?? ""} onChange={(e) => void setRole(role, e.target.value)}>
              <option value="">(none)</option>
              {presets.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </Select>
          </FieldRow>
        ))}
      </div>
      <div className="mt-4 border-t border-neutral-200 pt-2 dark:border-neutral-800">
        <ScalarFields group="models" schema={settings.schema} values={settings.values} />
      </div>
    </Card>
  );
}
