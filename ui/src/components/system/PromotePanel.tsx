// Version, promote/rollback (each behind a Confirm), fed by store.state.promote
// (promote.progress WS events) as a stepped progress list.

import { useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import { Badge, Button, Confirm, FieldRow, Input } from "../ui";

export function PromotePanel() {
  const version = useStore((s) => s.version);
  const promote = useStore((s) => s.promote);
  const [ref, setRef] = useState("");
  const [confirmPromote, setConfirmPromote] = useState(false);
  const [confirmRollback, setConfirmRollback] = useState(false);

  async function doPromote() {
    const ok = await store.guard(() => api.promote(ref.trim()));
    if (ok !== undefined) store.toast("info", "Promotion started", ref.trim());
  }

  async function doRollback() {
    const ok = await store.guard(() => api.rollback());
    if (ok !== undefined) store.toast("info", "Rollback started");
  }

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3 text-[12.5px]">
        <span>
          Version <span className="font-mono">{version?.version ?? "—"}</span>
        </span>
        {version?.release ? (
          <span>
            Release <span className="font-mono">{version.release}</span>
          </span>
        ) : null}
        {version?.ref ? (
          <span>
            Ref <span className="font-mono">{version.ref}</span>
          </span>
        ) : null}
      </div>

      <div className="flex items-end gap-2">
        <FieldRow label="Promote to ref" help="A commit reachable in the assistant's own repo.">
          <Input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main / commit sha" />
        </FieldRow>
        <Button variant="primary" disabled={!ref.trim()} onClick={() => setConfirmPromote(true)}>
          Promote
        </Button>
        <Button variant="danger" onClick={() => setConfirmRollback(true)}>
          Rollback
        </Button>
      </div>

      {promote.length > 0 ? (
        <ol className="mt-3 space-y-1">
          {promote.map((p, i) => (
            <li key={i} className="flex items-center gap-2 text-[12px]">
              <Badge tone={p.ok ? "green" : "red"}>{p.ok ? "ok" : "error"}</Badge>
              <span className="font-medium text-neutral-700 dark:text-neutral-300">{p.stage}</span>
              <span className="truncate text-neutral-500">{p.message}</span>
            </li>
          ))}
        </ol>
      ) : null}

      {confirmPromote ? (
        <Confirm
          title="Promote"
          body={
            <>
              This snapshots the database and vault, builds and probes a new release at{" "}
              <span className="font-mono">{ref.trim()}</span>, then swaps it in. A failed probe rolls back
              automatically.
            </>
          }
          confirmLabel="Promote"
          onConfirm={doPromote}
          onClose={() => setConfirmPromote(false)}
        />
      ) : null}
      {confirmRollback ? (
        <Confirm
          title="Rollback"
          body="Swaps back to the previous release and restarts. If the database schema advanced since, this restores the pre-promotion snapshot and any state written since is lost."
          confirmLabel="Rollback"
          danger
          onConfirm={doRollback}
          onClose={() => setConfirmRollback(false)}
        />
      ) : null}
    </div>
  );
}
