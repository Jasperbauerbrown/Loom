// System screen (route `#/system`). Version/promote/rollback, health, queue, logs,
// connector/MCP status, reindex, snapshots. See docs/03-technical-design.md §13.1.

import { useEffect } from "react";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import { Badge, Button, Empty, Section, Table, Td, Tr } from "../components/ui";
import { PromotePanel } from "../components/system/PromotePanel";
import { LogsPanel } from "../components/system/LogsPanel";
import { IntegrationsStatus } from "../components/system/IntegrationsStatus";
import { localTime, bytes } from "../lib/format";

export function System() {
  const health = useStore((s) => s.health);
  const queue = useStore((s) => s.queue);
  const snapshots = useStore((s) => s.snapshots);

  useEffect(() => {
    void store.loadSystem();
    void store.loadSnapshots();
    void store.loadIntegrations();
  }, []);

  async function doReindex() {
    const ok = await store.guard(() => api.reindex());
    if (ok !== undefined) store.toast("info", "Reindex started");
  }

  const healthy = health?.ok === true || health?.status === "ok";

  return (
    <div className="mx-auto max-w-4xl p-4">
      <Section title="Release">
        <PromotePanel />
      </Section>

      <Section
        title="Health & queue"
        actions={
          <Button size="sm" onClick={doReindex}>
            Reindex vault
          </Button>
        }
      >
        <div className="flex flex-wrap items-center gap-4 text-[12.5px]">
          <span className="flex items-center gap-1.5">
            Backend
            <Badge tone={health ? (healthy ? "green" : "red") : "neutral"}>
              {health ? (healthy ? "ok" : (health.status ?? "error")) : "unknown"}
            </Badge>
          </span>
          <span>
            Queue depth <span className="font-mono">{queue.depth}</span>
          </span>
          <span>
            Running <span className="font-mono">{queue.running.length}</span>
          </span>
        </div>
        {health && Object.keys(health).length > 0 ? (
          <pre className="mt-2 overflow-x-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11px] dark:border-neutral-800 dark:bg-neutral-950">
            {JSON.stringify(health, null, 2)}
          </pre>
        ) : null}
      </Section>

      <Section title="Logs">
        <LogsPanel />
      </Section>

      <Section title="Integrations">
        <IntegrationsStatus />
      </Section>

      <Section title="Snapshots">
        {snapshots.length === 0 ? (
          <Empty>No snapshots yet.</Empty>
        ) : (
          <Table head={["ID", "Created", "Size"]}>
            {snapshots.map((s) => (
              <Tr key={s.id}>
                <Td className="font-mono text-[11.5px]">{s.id}</Td>
                <Td>{localTime(s.created_at)}</Td>
                <Td>{bytes(s.bytes)}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </Section>
    </div>
  );
}
