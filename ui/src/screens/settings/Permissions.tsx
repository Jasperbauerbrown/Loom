// Permissions tab: scalars, tool policies (grouped by source), roots, protected paths.

import { useMemo, useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import type { PolicyMode, SettingsPayload, ToolPolicy } from "../../lib/types";
import { Badge, Button, Card, Empty, Input, Section, Select, Table, Td, Tr } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

function groupSource(p: ToolPolicy): string {
  if (p.source) return p.source;
  if (p.tool.startsWith("mcp:")) return "mcp";
  if (p.tool.startsWith("connector:")) return "connector";
  if (p.tool.startsWith("path:")) return "path-class";
  return "native";
}

export function PermissionsTab({ settings }: { settings: SettingsPayload }) {
  const toolPolicies = useStore((s) => s.toolPolicies);
  const roots = useStore((s) => s.roots);
  const protectedPaths = useStore((s) => s.protectedPaths);

  const grouped = useMemo(() => {
    const m = new Map<string, ToolPolicy[]>();
    for (const p of toolPolicies) {
      const key = groupSource(p);
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(p);
    }
    return m;
  }, [toolPolicies]);

  async function setMode(tool: string, mode: PolicyMode) {
    await store.guard(() => api.setToolPolicy(tool, mode));
    await store.loadPermissions();
  }

  async function reset(tool: string) {
    await store.guard(() => api.resetToolPolicy(tool));
    await store.loadPermissions();
  }

  return (
    <div>
      <Section title="Permissions" description="Defaults and network/shell behaviour.">
        <ScalarFields group="permissions" schema={settings.schema} values={settings.values} />
      </Section>

      <Section title="Tool policies" description="Allow, ask or classify — grouped by where the tool comes from.">
        {toolPolicies.length === 0 ? (
          <Empty>No tool policies.</Empty>
        ) : (
          <div className="space-y-4">
            {[...grouped.entries()].map(([source, rows]) => (
              <div key={source}>
                <h3 className="mb-1 text-[12px] font-semibold uppercase tracking-wide text-neutral-500">{source}</h3>
                <Table head={["Tool", "Mode", "Read-only", ""]}>
                  {rows.map((p) => (
                    <Tr key={p.tool}>
                      <Td className="font-mono text-[12px]">{p.tool}</Td>
                      <Td>
                        <Select value={p.mode} onChange={(e) => void setMode(p.tool, e.target.value as PolicyMode)}>
                          <option value="allow">allow</option>
                          <option value="ask">ask</option>
                          <option value="classify">classify</option>
                        </Select>
                      </Td>
                      <Td>{p.read_only ? <Badge tone="sky">read-only</Badge> : null}</Td>
                      <Td>
                        {p.explicit ? (
                          <Button size="sm" onClick={() => void reset(p.tool)}>
                            Reset to default
                          </Button>
                        ) : null}
                      </Td>
                    </Tr>
                  ))}
                </Table>
              </div>
            ))}
          </div>
        )}
      </Section>

      <Section title="Roots" description="Directories the agent may operate in without a path-class check.">
        <RootsPanel roots={roots.map((r) => r.path)} />
      </Section>

      <Section
        title="Protected paths"
        description="Paths that always require approval to modify. Removing one needs approval too."
      >
        <ProtectedPathsPanel paths={protectedPaths} />
      </Section>
    </div>
  );
}

function RootsPanel({ roots }: { roots: string[] }) {
  const [value, setValue] = useState("");
  async function add() {
    if (!value.trim()) return;
    const res = await store.guard(() => api.addRoot(value.trim()));
    if (res) {
      setValue("");
      await store.loadPermissions();
    }
  }
  async function remove(path: string) {
    await store.guard(() => api.removeRoot(path));
    await store.loadPermissions();
  }
  return (
    <Card className="p-3">
      <div className="mb-2 flex gap-1.5">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void add()}
          placeholder="C:/Users/you/Projects"
          className="font-mono"
        />
        <Button onClick={() => void add()}>Add</Button>
      </div>
      {roots.length === 0 ? (
        <Empty>No roots configured.</Empty>
      ) : (
        <Table head={["Path", ""]}>
          {roots.map((p) => (
            <Tr key={p}>
              <Td className="font-mono text-[12px]">{p}</Td>
              <Td>
                <Button size="sm" variant="danger" onClick={() => void remove(p)}>
                  Remove
                </Button>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
    </Card>
  );
}

function ProtectedPathsPanel({ paths }: { paths: string[] }) {
  const [value, setValue] = useState("");
  const [note, setNote] = useState<string | null>(null);
  async function add() {
    if (!value.trim()) return;
    const res = await store.guard(() => api.addProtectedPath(value.trim()));
    if (res) {
      setValue("");
      await store.loadPermissions();
    }
  }
  async function remove(path: string) {
    const res = await store.guard(() => api.removeProtectedPath(path));
    if (res && (res as any).approval_id) {
      setNote(`Removal of "${path}" needs approval — see Approvals.`);
    } else {
      await store.loadPermissions();
    }
  }
  return (
    <Card className="p-3">
      <div className="mb-2 flex gap-1.5">
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void add()}
          placeholder="C:/Users/you/Projects/secrets"
          className="font-mono"
        />
        <Button onClick={() => void add()}>Add</Button>
      </div>
      {note && <p className="mb-2 text-[12px] text-amber-700 dark:text-amber-400">{note}</p>}
      {paths.length === 0 ? (
        <Empty>No protected paths.</Empty>
      ) : (
        <Table head={["Path", ""]}>
          {paths.map((p) => (
            <Tr key={p}>
              <Td className="font-mono text-[12px]">{p}</Td>
              <Td>
                <Button size="sm" variant="danger" onClick={() => void remove(p)}>
                  Remove
                </Button>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
    </Card>
  );
}
