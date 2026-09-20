// Triggers tab: scalars plus the schedules table.

import { useState } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import type { Schedule, SettingsPayload } from "../../lib/types";
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
import { relativeTime } from "../../lib/format";
import { ScalarFields } from "./SettingsField";

export function TriggersTab({ settings }: { settings: SettingsPayload }) {
  const schedules = useStore((s) => s.schedules);
  const conversations = useStore((s) => s.conversations);

  return (
    <div>
      <Section title="Triggers" description="How thread events, schedules and connector events are delivered.">
        <ScalarFields group="triggers" schema={settings.schema} values={settings.values} />
      </Section>

      <Section title="Schedules">
        <SchedulesPanel schedules={schedules} conversations={Object.values(conversations)} />
      </Section>
    </div>
  );
}

function SchedulesPanel({
  schedules,
  conversations,
}: {
  schedules: Schedule[];
  conversations: { id: string; kind: string; title: string }[];
}) {
  const [editing, setEditing] = useState<Schedule | null>(null);
  const [creating, setCreating] = useState(false);
  const [toDelete, setToDelete] = useState<Schedule | null>(null);
  const [running, setRunning] = useState<string | null>(null);

  async function run(id: string) {
    setRunning(id);
    await store.guard(() => api.runSchedule(id));
    setRunning(null);
    await store.loadSchedules();
  }

  const targetLabel = (target: string) => {
    if (target === "desk") return "desk";
    const c = conversations.find((c) => c.id === target);
    return c ? c.title || c.id : target;
  };

  return (
    <Card className="p-3">
      <div className="mb-2 flex justify-end">
        <Button size="sm" variant="primary" onClick={() => setCreating(true)}>
          Add schedule
        </Button>
      </div>
      {schedules.length === 0 ? (
        <Empty>No schedules configured.</Empty>
      ) : (
        <Table head={["Spec", "Target", "Policy", "Next run", "Last run", "Runs", "Enabled", ""]}>
          {schedules.map((s) => (
            <Tr key={s.id}>
              <Td className="font-mono text-[12px]">{s.spec}</Td>
              <Td>{targetLabel(s.target)}</Td>
              <Td>{s.policy ? <Badge>{s.policy}</Badge> : <Badge tone="neutral">default</Badge>}</Td>
              <Td className="text-[12px]">
                <span title={s.next_run_at ?? undefined}>{relativeTime(s.next_run_at)}</span>
              </Td>
              <Td className="text-[12px]">
                <span title={s.last_run_at ?? undefined}>{relativeTime(s.last_run_at)}</span>
              </Td>
              <Td>{s.runs}</Td>
              <Td>{s.enabled ? <Badge tone="green">on</Badge> : <Badge>off</Badge>}</Td>
              <Td>
                <div className="flex flex-wrap gap-1.5">
                  <Button size="sm" onClick={() => run(s.id)} disabled={running === s.id}>
                    Run now
                  </Button>
                  <Button size="sm" onClick={() => setEditing(s)}>
                    Edit
                  </Button>
                  <Button size="sm" variant="danger" onClick={() => setToDelete(s)}>
                    Delete
                  </Button>
                </div>
              </Td>
            </Tr>
          ))}
        </Table>
      )}
      {(editing || creating) && (
        <ScheduleDialog
          schedule={editing}
          conversations={conversations}
          onClose={() => {
            setEditing(null);
            setCreating(false);
          }}
        />
      )}
      {toDelete && (
        <Confirm
          title="Delete schedule?"
          body={toDelete.spec}
          danger
          confirmLabel="Delete"
          onClose={() => setToDelete(null)}
          onConfirm={() => void store.guard(() => api.deleteSchedule(toDelete.id)).then(() => store.loadSchedules())}
        />
      )}
    </Card>
  );
}

function ScheduleDialog({
  schedule,
  conversations,
  onClose,
}: {
  schedule: Schedule | null;
  conversations: { id: string; kind: string; title: string }[];
  onClose: () => void;
}) {
  const [spec, setSpec] = useState(schedule?.spec ?? "");
  const [target, setTarget] = useState(schedule?.target ?? "desk");
  const [payload, setPayload] = useState(schedule?.payload ?? "");
  const [policy, setPolicy] = useState<string>(schedule?.policy ?? "");
  const [enabled, setEnabled] = useState(Boolean(schedule?.enabled ?? true));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const threadTargets = conversations.filter((c) => c.kind === "thread");

  async function save() {
    if (!spec.trim()) {
      setError("spec is required");
      return;
    }
    const body: Partial<Schedule> = {
      spec,
      target,
      payload,
      policy: (policy || null) as Schedule["policy"],
      enabled,
    };
    setSaving(true);
    const res = schedule
      ? await store.guard(() => api.updateSchedule(schedule.id, body))
      : await store.guard(() => api.createSchedule(body));
    setSaving(false);
    if (res) {
      await store.loadSchedules();
      onClose();
    } else {
      setError("save failed");
    }
  }

  return (
    <Dialog
      title={schedule ? "Edit schedule" : "Add schedule"}
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
      <FieldRow label="Spec" help="Cron expression or interval, as accepted by the scheduler.">
        <Input value={spec} onChange={(e) => setSpec(e.target.value)} placeholder="0 9 * * *" className="font-mono" />
      </FieldRow>
      <FieldRow label="Target">
        <Select value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="desk">desk</option>
          {threadTargets.map((c) => (
            <option key={c.id} value={c.id}>
              {c.title || c.id}
            </option>
          ))}
        </Select>
      </FieldRow>
      <FieldRow label="Payload">
        <Textarea rows={3} value={payload} onChange={(e) => setPayload(e.target.value)} />
      </FieldRow>
      <FieldRow label="Policy">
        <Select value={policy} onChange={(e) => setPolicy(e.target.value)}>
          <option value="">default</option>
          <option value="interrupt">interrupt</option>
          <option value="queue">queue</option>
        </Select>
      </FieldRow>
      <FieldRow label="Enabled">
        <Toggle checked={enabled} onChange={setEnabled} />
      </FieldRow>
      {error && <p className="text-[12px] text-red-600 dark:text-red-400">{error}</p>}
    </Dialog>
  );
}
