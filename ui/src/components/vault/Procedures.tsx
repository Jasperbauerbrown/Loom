// Agent Skills (procedures) list with an approve action for unapproved skills.

import { useEffect, useState } from "react";
import type { Procedure } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Badge, Button, Empty, Spinner } from "../ui";

export function Procedures() {
  const [procs, setProcs] = useState<Procedure[] | null>(null);

  async function load() {
    const rows = await store.guard(() => api.vaultProcedures());
    setProcs(rows ?? []);
  }

  useEffect(() => {
    void load();
  }, []);

  async function approve(name: string) {
    const ok = await store.guard(() => api.approveProcedure(name));
    if (ok) {
      store.toast("info", "Procedure approved", name);
      await load();
    }
  }

  if (procs === null) return <Spinner />;
  if (procs.length === 0) return <Empty>No procedures.</Empty>;

  return (
    <ul className="space-y-1.5">
      {procs.map((p) => (
        <li key={p.name} className="flex items-center justify-between gap-2 text-[12.5px]">
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <span className="truncate font-medium text-neutral-800 dark:text-neutral-200">{p.name}</span>
              <Badge tone={p.approved ? "green" : "amber"}>{p.approved ? "approved" : "pending"}</Badge>
            </div>
            {p.description ? <div className="truncate text-[11px] text-neutral-500">{p.description}</div> : null}
          </div>
          {!p.approved ? (
            <Button size="sm" onClick={() => approve(p.name)}>
              Approve
            </Button>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
