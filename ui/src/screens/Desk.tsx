// The desk: the always-on conversation, with the now board on the right (03 §13.1).

import { Conversation } from "../components/Conversation";
import { ConversationHeader } from "../components/ConversationHeader";
import { NowBoard } from "../components/NowBoard";
import { Toggle } from "../components/ui";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";

const AUTO_APPROVE = "permissions.desk_auto_approve";

/** Mirror of Settings → Permissions → Desk auto-approve; threads are unaffected. */
function AutoApprove() {
  const on = useStore((s) => s.settings?.values[AUTO_APPROVE] === true, Object.is);
  const set = async (value: boolean) => {
    const res = await store.guard(() => api.patchSettings({ [AUTO_APPROVE]: value }));
    if (res) void store.loadSettings();
  };
  return (
    <label
      className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400"
      title="Desk calls run without asking or classifying. Protected paths still ask; threads are unaffected."
    >
      <Toggle checked={on} onChange={(v) => void set(v)} label="Auto-approve" />
      <span className={on ? "font-medium text-amber-700 dark:text-amber-400" : undefined}>auto-approve</span>
    </label>
  );
}

export function Desk({ id }: { id: string }) {
  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-w-0 flex-1 flex-col">
        <ConversationHeader id={id}>
          <AutoApprove />
        </ConversationHeader>
        <div className="min-h-0 flex-1">
          <Conversation id={id} />
        </div>
      </div>
      <div className="hidden w-80 shrink-0 lg:block">
        <NowBoard />
      </div>
    </div>
  );
}
