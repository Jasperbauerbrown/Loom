// Integrations tab: MCP servers, connectors (generic config-schema form), secrets.
// Not a scalar settings group — this tab is entirely row-shaped tables.

import { useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import type { Connector, JsonSchema, McpServer } from "../../lib/types";
import {
  Badge,
  Button,
  Card,
  Confirm,
  Dialog,
  Empty,
  FieldRow,
  Input,
  Section,
  Select,
  Table,
  Td,
  Textarea,
  Toggle,
  Tr,
} from "../../components/ui";

export function IntegrationsTab() {
  const mcpServers = useStore((s) => s.mcpServers);
  const connectors = useStore((s) => s.connectors);
  const secrets = useStore((s) => s.secrets);

  return (
    <div>
      <Section title="MCP servers" description="External tool servers, over stdio or HTTP.">
        <McpPanel servers={mcpServers} secretNames={secrets?.names ?? []} />
      </Section>

      <Section title="Connectors" description="Built-in integrations, each with its own configuration.">
        <ConnectorsPanel connectors={connectors} />
      </Section>

      <Section title="Secrets" description={`Backend: ${secrets?.backend ?? "unknown"}`}>
        {secrets?.warning && (
          <div className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
            {secrets.warning}
          </div>
        )}
        <SecretsPanel names={secrets?.names ?? []} />
      </Section>
    </div>
  );
}

// -- MCP servers --------------------------------------------------------------

// One tone per status class (connectors/loader.py, connectors/mcp/manager.py).
function statusTone(status: string): "green" | "amber" | "red" | "neutral" {
  if (status === "ok" || status === "connected" || status === "loaded") return "green";
  if (status === "error" || status === "failed") return "red";
  if (status === "unknown" || status === "disabled" || !status) return "neutral";
  return "amber"; // needs_config, connecting, …
}

function McpPanel({ servers, secretNames }: { servers: McpServer[]; secretNames: string[] }) {
  const [editing, setEditing] = useState<McpServer | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<McpServer | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  async function refresh(id: string) {
    setBusy(id);
    await store.guard(() => api.refreshMcpServer(id));
    setBusy(null);
    await store.loadIntegrations();
  }
  async function test(id: string) {
    setBusy(id);
    const res = await store.guard(() => api.testMcpServer(id));
    setBusy(null);
    if (!res) return;
    const server = (res.server ?? {}) as { name?: string; version?: string };
    const detail = [
      server.name ? `${server.name}${server.version ? ` ${server.version}` : ""}` : "",
      res.latency_ms !== null && res.latency_ms !== undefined ? `${res.latency_ms} ms` : "",
      `${res.tools ?? 0} tool(s)`,
    ]
      .filter(Boolean)
      .join(" · ");
    store.toast(
      res.ok ? "info" : "error",
      res.ok ? "MCP test ok" : "MCP test failed",
      res.ok ? detail : (res.error ?? undefined),
    );
  }

  return (
    <Card className="p-3">
      <div className="mb-2 flex justify-end">
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Add server
        </Button>
      </div>
      {servers.length === 0 ? (
        <Empty>No MCP servers configured.</Empty>
      ) : (
        <Table head={["Name", "Transport", "Status", "Last error", "Enabled", ""]}>
          {servers.map((m) => (
            <Tr key={m.id}>
              <Td className="font-medium">{m.name}</Td>
              <Td>{m.transport}</Td>
              <Td>
                <Badge tone={statusTone(m.status)}>{m.status || "unknown"}</Badge>
              </Td>
              <Td className="max-w-[16rem] text-[12px] text-red-600 dark:text-red-400">
                <span className="block truncate" title={m.last_error ?? undefined}>
                  {m.last_error ?? ""}
                </span>
              </Td>
              <Td>{m.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}</Td>
              <Td>
                <div className="flex flex-wrap gap-1.5">
                  <Button size="sm" onClick={() => refresh(m.id)} disabled={busy === m.id}>
                    Refresh tools
                  </Button>
                  <Button size="sm" onClick={() => test(m.id)} disabled={busy === m.id}>
                    Test
                  </Button>
                  <Button size="sm" onClick={() => setEditing(m)}>
                    Edit
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => setToDelete(m)}>
                    Delete
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
      {(editing || creating) && (
        <McpDialog
          server={editing}
          secretNames={secretNames}
          onClose={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      )}
      {toDelete && (
        <Confirm
          title={`Delete MCP server "${toDelete.name}"?`}
          body="Tools it exposes will no longer be available."
          danger
          confirmLabel="Delete"
          onClose={() => setToDelete(null)}
          onConfirm={() => void store.guard(() => api.deleteMcpServer(toDelete.id)).then(() => store.loadIntegrations())}
        />
      )}
    </Card>
  );
}

function McpDialog({
  server,
  secretNames,
  onClose,
}: {
  server: McpServer | null;
  secretNames: string[];
  onClose: () => void;
}) {
  const [name, setName] = useState(server?.name ?? "");
  const [transport, setTransport] = useState<McpServer["transport"]>(server?.transport ?? "stdio");
  const [command, setCommand] = useState(server?.command ?? "");
  const [argsText, setArgsText] = useState((server?.args ?? []).join("\n"));
  const [url, setUrl] = useState(server?.url ?? "");
  const [envRows, setEnvRows] = useState<{ key: string; value: string }[]>(
    Object.entries(server?.env ?? {}).map(([key, value]) => ({ key, value })),
  );
  const [enabled, setEnabled] = useState(Boolean(server?.enabled ?? true));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function addEnvRow() {
    setEnvRows([...envRows, { key: "", value: "" }]);
  }
  function updateEnvRow(i: number, patch: Partial<{ key: string; value: string }>) {
    setEnvRows(envRows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }
  function removeEnvRow(i: number) {
    setEnvRows(envRows.filter((_, idx) => idx !== i));
  }
  function insertSecretRef(i: number, secretName: string) {
    updateEnvRow(i, { value: `{{secret:${secretName}}}` });
  }

  async function save() {
    if (!name.trim()) {
      setError("name is required");
      return;
    }
    const env: Record<string, string> = {};
    for (const r of envRows) if (r.key.trim()) env[r.key.trim()] = r.value;
    const body = {
      name,
      transport,
      command: transport === "stdio" ? command : null,
      args: transport === "stdio" ? argsText.split("\n").map((s) => s.trim()).filter(Boolean) : [],
      url: transport === "http" ? url : null,
      env,
      enabled,
    };
    setSaving(true);
    const res = server
      ? await store.guard(() => api.updateMcpServer(server.id, body))
      : await store.guard(() => api.createMcpServer(body));
    setSaving(false);
    if (res) {
      await store.loadIntegrations();
      onClose();
    } else {
      setError("save failed");
    }
  }

  return (
    <Dialog
      title={server ? `Edit MCP server: ${server.name}` : "Add MCP server"}
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
      <FieldRow label="Name">
        <Input value={name} onChange={(e) => setName(e.target.value)} />
      </FieldRow>
      <FieldRow label="Transport">
        <Select value={transport} onChange={(e) => setTransport(e.target.value as McpServer["transport"])}>
          <option value="stdio">stdio</option>
          <option value="http">http</option>
        </Select>
      </FieldRow>
      {transport === "stdio" ? (
        <>
          <FieldRow label="Command">
            <Input value={command} onChange={(e) => setCommand(e.target.value)} placeholder="npx some-mcp-server" />
          </FieldRow>
          <FieldRow label="Args" help="One per line.">
            <Textarea rows={3} className="font-mono" value={argsText} onChange={(e) => setArgsText(e.target.value)} />
          </FieldRow>
        </>
      ) : (
        <FieldRow label="URL">
          <Input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="http://localhost:1234/mcp" />
        </FieldRow>
      )}
      <FieldRow label="Environment" help="Use the secret picker to insert {{secret:NAME}} without exposing the value.">
        <div className="space-y-1.5">
          {envRows.map((r, i) => (
            <div key={i} className="flex gap-1.5">
              <Input
                className="w-1/3"
                value={r.key}
                onChange={(e) => updateEnvRow(i, { key: e.target.value })}
                placeholder="KEY"
              />
              <Input value={r.value} onChange={(e) => updateEnvRow(i, { value: e.target.value })} placeholder="value" />
              <Select
                className="w-auto"
                value=""
                onChange={(e) => e.target.value && insertSecretRef(i, e.target.value)}
              >
                <option value="">secret…</option>
                {secretNames.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </Select>
              <Button size="sm" variant="danger" onClick={() => removeEnvRow(i)}>
                ✕
              </Button>
            </div>
          ))}
          <Button size="sm" onClick={addEnvRow}>
            Add variable
          </Button>
        </div>
      </FieldRow>
      <FieldRow label="Enabled">
        <Toggle checked={enabled} onChange={setEnabled} />
      </FieldRow>
      {error && <p className="text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </Dialog>
  );
}

// -- Connectors ---------------------------------------------------------------

function ConnectorsPanel({ connectors }: { connectors: Connector[] }) {
  const [busy, setBusy] = useState<string | null>(null);

  async function toggle(name: string, enabled: boolean) {
    setBusy(name);
    await store.guard(() => api.updateConnector(name, { enabled }));
    setBusy(null);
    await store.loadIntegrations();
  }
  async function reload(name: string) {
    setBusy(name);
    await store.guard(() => api.reloadConnector(name));
    setBusy(null);
    await store.loadIntegrations();
  }

  if (connectors.length === 0) return <Empty>No connectors available.</Empty>;

  return (
    <div className="space-y-3">
      {connectors.map((c) => (
        <Card key={c.name} className="p-3">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div>
              <span className="text-[13px] font-medium">{c.name}</span>{" "}
              <Badge tone={statusTone(c.status)}>{c.status || "unknown"}</Badge>
              {c.available === false && (
                <Badge tone="red" className="ml-1">
                  not importable
                </Badge>
              )}
              {c.description && <p className="text-[12px] text-neutral-500">{c.description}</p>}
              {c.tools && c.tools.length > 0 && (
                <p className="mt-0.5 font-mono text-[11.5px] text-neutral-500">{c.tools.join(", ")}</p>
              )}
              {c.last_error && c.status !== "needs_config" && (
                <p className="text-[12px] text-red-600 dark:text-red-400">{c.last_error}</p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={() => reload(c.name)} disabled={busy === c.name}>
                Reload
              </Button>
              <Toggle checked={Boolean(c.enabled)} onChange={(v) => void toggle(c.name, v)} disabled={busy === c.name} />
            </div>
          </div>
          {c.status === "needs_config" && (
            <div className="mb-2 rounded-md border border-amber-300 bg-amber-50 px-2.5 py-2 text-[12.5px] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
              Needs configuration before it can load
              {c.missing_config && c.missing_config.length > 0 ? (
                <>
                  : <span className="font-mono">{c.missing_config.join(", ")}</span>
                </>
              ) : c.last_error ? (
                <>: {c.last_error}</>
              ) : null}
            </div>
          )}
          {c.config_schema && <ConnectorConfigForm connector={c} schema={c.config_schema} />}
        </Card>
      ))}
    </div>
  );
}

function ConnectorConfigForm({ connector, schema }: { connector: Connector; schema: JsonSchema }) {
  const [values, setValues] = useState<Record<string, unknown>>({ ...connector.config });
  const [error, setError] = useState<string | null>(null);
  const props = schema.properties ?? {};

  async function save(key: string, value: unknown) {
    const next = { ...values, [key]: value };
    setValues(next);
    const res = await store.guard(() => api.updateConnector(connector.name, { config: next }));
    if (res === undefined) setError("save failed");
    else {
      setError(null);
      await store.loadIntegrations();
    }
  }

  return (
    <div className="divide-y divide-neutral-100 border-t border-neutral-200 dark:divide-neutral-800/70 dark:border-neutral-800">
      {Object.entries(props).map(([key, prop]) => {
        const value = values[key];
        if (prop.enum) {
          return (
            <FieldRow key={key} label={prop.title ?? key} help={prop.description}>
              <Select value={String(value ?? "")} onChange={(e) => void save(key, e.target.value)}>
                {prop.enum.map((opt) => (
                  <option key={String(opt)} value={String(opt)}>
                    {String(opt)}
                  </option>
                ))}
              </Select>
            </FieldRow>
          );
        }
        if (prop.type === "boolean") {
          return (
            <FieldRow key={key} label={prop.title ?? key} help={prop.description}>
              <Toggle checked={Boolean(value)} onChange={(v) => void save(key, v)} />
            </FieldRow>
          );
        }
        if (prop.type === "number" || prop.type === "integer") {
          return (
            <FieldRow key={key} label={prop.title ?? key} help={prop.description}>
              <Input
                type="number"
                min={prop.minimum ?? undefined}
                max={prop.maximum ?? undefined}
                defaultValue={String(value ?? "")}
                onBlur={(e) => void save(key, e.target.value === "" ? undefined : Number(e.target.value))}
              />
            </FieldRow>
          );
        }
        return (
          <FieldRow key={key} label={prop.title ?? key} help={prop.description}>
            <Input defaultValue={String(value ?? "")} onBlur={(e) => void save(key, e.target.value)} />
          </FieldRow>
        );
      })}
      {error && <p className="py-1 text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}

// -- Secrets --------------------------------------------------------------

function SecretsPanel({ names }: { names: string[] }) {
  const [adding, setAdding] = useState(false);
  const [toDelete, setToDelete] = useState<string | null>(null);

  return (
    <Card className="p-3">
      <div className="mb-2 flex justify-end">
        <Button size="sm" variant="primary" onClick={() => setAdding(true)}>
          Add secret
        </Button>
      </div>
      {names.length === 0 ? (
        <Empty>No secrets stored.</Empty>
      ) : (
        <Table head={["Name", ""]}>
          {names.map((n) => (
            <Tr key={n}>
              <Td className="font-mono text-[12px]">{n}</Td>
              <Td>
                <div className="flex gap-1.5">
                  <Button size="sm" onClick={() => setAdding(true)}>
                    Replace
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => setToDelete(n)}>
                    Delete
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
      {adding && <SecretDialog onClose={() => setAdding(false)} />}
      {toDelete && (
        <Confirm
          title={`Delete secret "${toDelete}"?`}
          body="Anything referencing it (providers, MCP env) will fail until replaced."
          danger
          confirmLabel="Delete"
          onClose={() => setToDelete(null)}
          onConfirm={() => void store.guard(() => api.deleteSecret(toDelete)).then(() => store.loadIntegrations())}
        />
      )}
    </Card>
  );
}

function SecretDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  async function save() {
    if (!name.trim() || !value) {
      setError("name and value are required");
      return;
    }
    setSaving(true);
    const res = await store.guard(() => api.putSecret(name.trim(), value));
    setSaving(false);
    if (res !== undefined) {
      await store.loadIntegrations();
      onClose();
    } else {
      setError("save failed");
    }
  }

  return (
    <Dialog
      title="Add / replace secret"
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
        <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="OPENROUTER_API_KEY" />
      </FieldRow>
      <FieldRow label="Value">
        <Input type="password" value={value} onChange={(e) => setValue(e.target.value)} />
      </FieldRow>
      {error && <p className="text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </Dialog>
  );
}
