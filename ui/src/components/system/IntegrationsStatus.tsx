// Read-only connector / MCP status lists for the System screen (full CRUD lives
// in Settings; this is just health at a glance).

import { useStore } from "../../lib/store";
import { Badge, Empty, Table, Td, Tr } from "../ui";

function statusTone(status: string): "green" | "amber" | "red" | "neutral" {
  const s = status.toLowerCase();
  if (s.includes("error") || s.includes("down") || s.includes("fail")) return "red";
  if (s.includes("connect") || s.includes("pending")) return "amber";
  if (s.includes("ok") || s.includes("running") || s.includes("enabled") || s.includes("ready")) return "green";
  return "neutral";
}

export function IntegrationsStatus() {
  const mcpServers = useStore((s) => s.mcpServers);
  const connectors = useStore((s) => s.connectors);

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div>
        <h3 className="mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">MCP servers</h3>
        {mcpServers.length === 0 ? (
          <Empty>No MCP servers configured.</Empty>
        ) : (
          <Table head={["Name", "Status", "Last error"]}>
            {mcpServers.map((m) => (
              <Tr key={m.id}>
                <Td>{m.name}</Td>
                <Td>
                  <Badge tone={statusTone(m.status)}>{m.status}</Badge>
                </Td>
                <Td className="truncate text-[11.5px] text-neutral-500">{m.last_error ?? ""}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </div>
      <div>
        <h3 className="mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">Connectors</h3>
        {connectors.length === 0 ? (
          <Empty>No connectors configured.</Empty>
        ) : (
          <Table head={["Name", "Status", "Last error"]}>
            {connectors.map((c) => (
              <Tr key={c.name}>
                <Td>{c.name}</Td>
                <Td>
                  <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                </Td>
                <Td className="truncate text-[11.5px] text-neutral-500">{c.last_error ?? ""}</Td>
              </Tr>
            ))}
          </Table>
        )}
      </div>
    </div>
  );
}
