// Release & promotion (03 §15, §13.1). The flow is plan-before-promote: a ref is
// resolved by `/system/promote/plan` first, the protected paths in the diff are shown,
// and the Promote button stays disabled until they are acknowledged. "Skip the test
// suite" is off for every new plan and is repeated in the confirmation. Progress is the
// persisted `promote.log` plus the live `promote.progress` events the store collects —
// the backend is restarted mid-promotion, so the file is the source of truth.

import { useEffect, useState } from "react";
import { api, ApiError } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import { Badge, Button, Card, Collapsible, Confirm, Empty, FieldRow, Input, Spinner } from "../ui";
import { localTime } from "../../lib/format";
import type { PromotePlan } from "../../lib/types";

function uptime(seconds: number | undefined): string {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return m ? `${m}m` : `${seconds}s`;
}

export function PromotePanel() {
  const version = useStore((s) => s.version);
  const supervisor = useStore((s) => s.supervisor);
  const live = useStore((s) => s.promote);
  const log = useStore((s) => s.promoteLog);
  const [ref, setRef] = useState("");
  const [plan, setPlan] = useState<PromotePlan | null>(null);
  const [planning, setPlanning] = useState(false);
  const [planError, setPlanError] = useState<string | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [skipTests, setSkipTests] = useState(false);
  const [confirmPromote, setConfirmPromote] = useState(false);
  const [confirmRollback, setConfirmRollback] = useState(false);
  const [confirmRestart, setConfirmRestart] = useState(false);

  useEffect(() => {
    void store.loadPromote();
  }, []);

  async function doPlan() {
    setPlanning(true);
    setPlanError(null);
    setAcknowledged(false);
    setSkipTests(false);
    try {
      setPlan(await api.promotePlan(ref.trim()));
    } catch (e) {
      setPlan(null);
      setPlanError(e instanceof ApiError ? e.message : String((e as Error)?.message ?? e));
    } finally {
      setPlanning(false);
    }
  }

  async function doPromote() {
    const ok = await store.guard(() => api.promote(ref.trim(), acknowledged, skipTests));
    if (ok !== undefined)
      store.toast("info", "Promotion started", skipTests ? `${ref.trim()} (tests skipped)` : ref.trim());
  }

  async function doRollback() {
    const ok = await store.guard(() => api.rollback());
    if (ok !== undefined) store.toast("info", "Rollback started");
  }

  async function doRestart() {
    const ok = await store.guard(() => api.restart());
    if (ok !== undefined) store.toast("info", "Restart requested");
  }

  const needsAck = Boolean(plan && plan.protected_files.length > 0);
  const canPromote = Boolean(plan?.ok) && (!needsAck || acknowledged);
  const supervised = Boolean(supervisor?.running);

  // The persisted log first, then anything that arrived live since this page loaded.
  const stages = [...log.map((l) => ({ stage: l.stage, ok: l.ok, message: l.message, ts: l.ts })), ...live];

  return (
    <div>
      {/* -- supervisor -------------------------------------------------------- */}
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12.5px]">
        <span>
          Version <span className="font-mono">{version?.version ?? "—"}</span>
        </span>
        <span className="flex items-center gap-1.5">
          Supervisor
          <Badge tone={supervised ? "green" : "amber"}>{supervised ? "running" : "not running"}</Badge>
        </span>
        {supervised ? (
          <>
            <span>
              Release <span className="font-mono">{supervisor?.release ?? "dev checkout"}</span>
            </span>
            {supervisor?.previous ? (
              <span>
                Previous <span className="font-mono">{supervisor.previous}</span>
              </span>
            ) : null}
            <span>
              Uptime <span className="font-mono">{uptime(supervisor?.uptime_s)}</span>
            </span>
            <span>
              Crashes <span className="font-mono">{supervisor?.crashes ?? 0}</span>
            </span>
            {supervisor?.pid ? (
              <span>
                PID <span className="font-mono">{supervisor.pid}</span>
              </span>
            ) : null}
            <Button size="sm" className="ml-auto" onClick={() => setConfirmRestart(true)}>
              Restart
            </Button>
          </>
        ) : null}
      </div>

      {!supervised ? (
        <div className="mb-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[12.5px] text-amber-800 dark:border-amber-900 dark:bg-amber-950/40 dark:text-amber-300">
          Not running under the supervisor; start with <span className="font-mono">loom serve</span>. Promotion,
          rollback and restart need it — planning still works.
          {supervisor?.error && !/not running/i.test(supervisor.error) ? (
            <span className="block opacity-80">{supervisor.error}</span>
          ) : null}
        </div>
      ) : null}
      {supervisor?.last_error ? (
        <p className="mb-2 text-[12px] text-red-600 dark:text-red-400">{supervisor.last_error}</p>
      ) : null}

      {/* -- plan -------------------------------------------------------------- */}
      <div className="flex flex-wrap items-end gap-2">
        <FieldRow label="Promote to ref" help="A commit, tag or branch in the assistant's own repo.">
          <Input
            value={ref}
            onChange={(e) => {
              setRef(e.target.value);
              setPlan(null);
              setAcknowledged(false);
              setSkipTests(false);
            }}
            placeholder="main / commit sha"
          />
        </FieldRow>
        <Button disabled={!ref.trim() || planning} onClick={doPlan}>
          {planning ? <Spinner /> : null} Plan
        </Button>
        <Button
          variant="primary"
          disabled={!canPromote || !supervised}
          title={supervised ? undefined : "the supervisor is not running"}
          onClick={() => setConfirmPromote(true)}
        >
          Promote
        </Button>
        <Button variant="danger" disabled={!supervised} onClick={() => setConfirmRollback(true)}>
          Rollback
        </Button>
      </div>

      {planError ? <p className="mt-2 text-[12px] text-red-600 dark:text-red-400">{planError}</p> : null}

      {plan ? (
        <Card className="mt-3 p-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[12.5px]">
            <span>
              Commit <span className="font-mono">{plan.short || plan.commit.slice(0, 7)}</span>{" "}
              <span className="font-mono text-neutral-500">{plan.commit}</span>
            </span>
            <span>
              Live <span className="font-mono">{plan.current_commit.slice(0, 7) || "—"}</span>
            </span>
            <Badge tone={plan.clean ? "green" : "amber"}>{plan.clean ? "clean tree" : "dirty tree"}</Badge>
            <Badge tone={skipTests ? "amber" : "sky"}>
              {skipTests ? "tests skipped" : "tests run before the swap"}
            </Badge>
          </div>

          {plan.protected_files.length > 0 ? (
            <div className="mt-2 rounded-md border border-amber-300 bg-amber-50 p-2 dark:border-amber-900 dark:bg-amber-950/40">
              <p className="text-[12.5px] font-medium text-amber-800 dark:text-amber-300">
                This diff changes {plan.protected_files.length} protected file(s):
              </p>
              <ul className="mt-1 font-mono text-[11.5px] text-amber-900 dark:text-amber-200">
                {plan.protected_files.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
              <label className="mt-2 flex items-center gap-2 text-[12.5px] text-amber-900 dark:text-amber-200">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                  className="h-3.5 w-3.5"
                />
                I understand these protected files change
              </label>
            </div>
          ) : (
            <p className="mt-2 text-[12px] text-neutral-500">No protected paths in this diff.</p>
          )}

          <label
            className="mt-2 flex items-center gap-2 text-[12.5px] text-neutral-700 dark:text-neutral-300"
            title="The probe and the health watch still run. Use when this commit already passed the suite elsewhere."
          >
            <input
              type="checkbox"
              checked={skipTests}
              onChange={(e) => setSkipTests(e.target.checked)}
              className="h-3.5 w-3.5"
            />
            Skip the test suite
            <span className="text-neutral-500">(the probe and health watch still run)</span>
          </label>

          {plan.dirty_files.length > 0 ? (
            <Collapsible summary={`${plan.dirty_files.length} uncommitted file(s)`}>
              <ul className="font-mono text-[11.5px] text-neutral-600 dark:text-neutral-400">
                {plan.dirty_files.map((f) => (
                  <li key={f}>{f}</li>
                ))}
              </ul>
            </Collapsible>
          ) : null}
          <Collapsible summary={`${plan.changed_files.length} changed file(s)`}>
            <ul className="font-mono text-[11.5px] text-neutral-600 dark:text-neutral-400">
              {plan.changed_files.map((f) => (
                <li key={f}>{f}</li>
              ))}
            </ul>
          </Collapsible>
        </Card>
      ) : null}

      {/* -- progress ---------------------------------------------------------- */}
      <h3 className="mt-4 mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">
        Promotion progress
      </h3>
      {stages.length === 0 ? (
        <Empty>No promotion has run yet.</Empty>
      ) : (
        <ol className="space-y-1">
          {stages.map((p, i) => (
            <li key={i} className="flex items-center gap-2 text-[12px]">
              <Badge tone={p.ok ? "green" : "red"}>{p.ok ? "ok" : "error"}</Badge>
              <span className="font-mono text-[11px] text-neutral-500">{localTime(p.ts)}</span>
              <span className="font-medium text-neutral-700 dark:text-neutral-300">{p.stage}</span>
              <span className="truncate text-neutral-500">{p.message}</span>
            </li>
          ))}
        </ol>
      )}

      {confirmPromote ? (
        <Confirm
          title="Promote"
          body={
            <>
              This snapshots the database and vault, builds a release at{" "}
              <span className="font-mono">{plan?.short || ref.trim()}</span>,{" "}
              {skipTests ? "skips the test suite, " : "runs the test suite in it, "}probes it, then swaps it in and
              restarts. {skipTests ? "A failed probe" : "A failed suite or probe"} stops it before the swap.
              {needsAck ? " The protected files listed above will change." : ""}
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
          body="Swaps back to the previous release and restarts. If migrations ran since the promotion, the pre-promotion snapshot of the database and vault is restored — everything written since that snapshot is lost."
          confirmLabel="Roll back"
          danger
          onConfirm={doRollback}
          onClose={() => setConfirmRollback(false)}
        />
      ) : null}
      {confirmRestart ? (
        <Confirm
          title="Restart"
          body="The supervisor stops the backend and starts it again on the same release. In-flight turns are cancelled."
          confirmLabel="Restart"
          onConfirm={doRestart}
          onClose={() => setConfirmRestart(false)}
        />
      ) : null}
    </div>
  );
}
