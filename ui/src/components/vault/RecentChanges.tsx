// Vault git log with diff viewer and revert.

import { useEffect, useState } from "react";
import type { VaultChange } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Button, Confirm, Empty, Spinner } from "../ui";
import { relativeTime } from "../../lib/format";

function DiffView({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 font-mono text-[11.5px] leading-4 dark:border-neutral-800 dark:bg-neutral-950">
      {lines.map((line, i) => (
        <div
          key={i}
          className={
            line.startsWith("+") && !line.startsWith("+++")
              ? "text-emerald-700 dark:text-emerald-400"
              : line.startsWith("-") && !line.startsWith("---")
                ? "text-red-700 dark:text-red-400"
                : "text-neutral-500"
          }
        >
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

export function RecentChanges({ notePath }: { notePath?: string }) {
  const [changes, setChanges] = useState<VaultChange[] | null>(null);
  const [openSha, setOpenSha] = useState<string | null>(null);
  const [diff, setDiff] = useState<string | null>(null);
  const [confirmRevert, setConfirmRevert] = useState<{ sha: string; path?: string } | null>(null);

  useEffect(() => {
    void load();
  }, [notePath]);

  async function load() {
    setChanges(null);
    const rows = notePath ? await store.guard(() => api.vaultHistory(notePath)) : await store.guard(() => api.recentChanges(30));
    setChanges(rows ?? []);
  }

  async function openDiff(sha: string) {
    if (openSha === sha) {
      setOpenSha(null);
      setDiff(null);
      return;
    }
    setOpenSha(sha);
    setDiff(null);
    const res = await store.guard(() => api.vaultDiff(sha, notePath));
    setDiff(res?.diff ?? "");
  }

  async function doRevert() {
    if (!confirmRevert) return;
    const ok = await store.guard(() => api.revert(confirmRevert.sha, confirmRevert.path));
    if (ok !== undefined) store.toast("info", "Reverted", confirmRevert.sha.slice(0, 8));
    await load();
  }

  if (changes === null) return <Spinner />;
  if (changes.length === 0) return <Empty>No changes yet.</Empty>;

  return (
    <div className="space-y-1">
      {changes.map((c) => (
        <div key={c.sha} className="rounded-md border border-neutral-200 dark:border-neutral-800">
          <button
            type="button"
            onClick={() => openDiff(c.sha)}
            className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-[12.5px] hover:bg-neutral-50 dark:hover:bg-neutral-900"
          >
            <span className="truncate">
              <span className="font-mono text-[11px] text-neutral-500">{c.sha.slice(0, 8)}</span>{" "}
              {c.message || "(no message)"}
            </span>
            <span className="shrink-0 text-[11px] text-neutral-500">{relativeTime(c.ts)}</span>
          </button>
          {openSha === c.sha ? (
            <div className="border-t border-neutral-200 px-2.5 py-2 dark:border-neutral-800">
              <div className="mb-1 flex items-center justify-between">
                <span className="truncate text-[11px] text-neutral-500">{c.paths.join(", ")}</span>
                <Button size="sm" variant="danger" onClick={() => setConfirmRevert({ sha: c.sha, path: notePath })}>
                  Revert
                </Button>
              </div>
              {diff === null ? <Spinner /> : diff ? <DiffView diff={diff} /> : <Empty>No diff.</Empty>}
            </div>
          ) : null}
        </div>
      ))}
      {confirmRevert ? (
        <Confirm
          title="Revert change"
          body={
            <>
              Revert {confirmRevert.path ? <span className="font-mono">{confirmRevert.path}</span> : "the vault"} to
              before commit <span className="font-mono">{confirmRevert.sha.slice(0, 8)}</span>? This writes a new
              commit undoing it.
            </>
          }
          confirmLabel="Revert"
          danger
          onConfirm={doRevert}
          onClose={() => setConfirmRevert(null)}
        />
      ) : null}
    </div>
  );
}
