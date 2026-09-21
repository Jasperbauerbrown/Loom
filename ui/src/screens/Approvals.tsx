// Approvals screen (`#/approvals`). Cross-conversation queue of pending tool-call
// approvals, live via the bus (approval.requested/resolved keep the store current).

import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { pendingApprovals, store, useStore } from "../lib/store";
import type { Approval } from "../lib/types";
import { navigate } from "../lib/router";
import { localTime, relativeTime } from "../lib/format";
import { Button, Card, Collapsible, Empty, JsonBlock } from "../components/ui";

function ApprovalCard({ approval }: { approval: Approval }) {
  const conversation = useStore((s) => s.conversations[approval.conversation_id], Object.is);
  // Widening the roots is a deliberate, second click: unchecked by default (2026-09-21).
  const [addRoot, setAddRoot] = useState(false);
  const [busy, setBusy] = useState<"allow" | "deny" | "allow_always" | null>(null);
  const rootToAdd = approval.options?.add_root;

  useEffect(() => {
    if (!conversation) void store.loadConversation(approval.conversation_id);
  }, [approval.conversation_id, conversation]);

  async function decide(decision: "allow" | "deny" | "allow_always") {
    setBusy(decision);
    await store.guard(() =>
      api.decideApproval(approval.id, decision, decision === "allow_always" && rootToAdd ? addRoot : undefined),
    );
    setBusy(null);
  }

  return (
    <Card className="p-3">
      <div className="mb-1.5 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[13px] font-semibold text-neutral-900 dark:text-neutral-100">
              {approval.tool}
            </span>
            <span className="text-[11.5px] text-neutral-500" title={localTime(approval.created_at)}>
              {relativeTime(approval.created_at)}
            </span>
          </div>
          <button
            type="button"
            className="mt-0.5 truncate text-[12px] text-sky-700 hover:underline dark:text-sky-400"
            onClick={() => navigate(`/c/${approval.conversation_id}`)}
          >
            {conversation?.title || conversation?.goal || approval.conversation_id}
          </button>
        </div>
      </div>

      {approval.reason ? (
        <p className="mb-2 rounded-md bg-amber-50 px-2 py-1 text-[12px] text-amber-900 dark:bg-amber-950/40 dark:text-amber-200">
          {approval.reason}
        </p>
      ) : null}

      <Collapsible summary="Arguments" defaultOpen={false} className="mb-2">
        <JsonBlock value={approval.args} />
      </Collapsible>

      {rootToAdd ? (
        <label className="mb-2 flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
          <input
            type="checkbox"
            checked={addRoot}
            onChange={(e) => setAddRoot(e.target.checked)}
            className="h-3.5 w-3.5"
          />
          <span>
            Also allow this root with &ldquo;Allow always&rdquo;: <span className="font-mono">{rootToAdd}</span>
          </span>
        </label>
      ) : null}

      <div className="flex justify-end gap-2">
        <Button variant="danger" size="sm" disabled={busy !== null} onClick={() => void decide("deny")}>
          Deny
        </Button>
        <Button size="sm" disabled={busy !== null} onClick={() => void decide("allow")}>
          Allow
        </Button>
        <Button variant="primary" size="sm" disabled={busy !== null} onClick={() => void decide("allow_always")}>
          Allow always
        </Button>
      </div>
    </Card>
  );
}

export function Approvals() {
  const approvals = useStore(pendingApprovals, (a, b) => a.length === b.length && a.every((x, i) => x.id === b[i].id));

  useEffect(() => {
    void store.loadApprovals();
  }, []);

  return (
    <div className="h-full overflow-y-auto p-4">
      <h1 className="mb-3 text-[16px] font-semibold text-neutral-900 dark:text-neutral-100">Approvals</h1>
      {approvals.length === 0 ? (
        <Empty>Nothing pending. Approvals will appear here as soon as a tool needs your sign-off.</Empty>
      ) : (
        <div className="space-y-3">
          {approvals.map((a) => (
            <ApprovalCard key={a.id} approval={a} />
          ))}
        </div>
      )}
    </div>
  );
}
