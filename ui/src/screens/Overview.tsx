// Overview (`#/overview`): the desk and every active thread side by side, each pane a
// live `Conversation` with its own composer (03 §13.1).

import { useEffect, useMemo, useState } from "react";
import { Conversation } from "../components/Conversation";
import { store, useStore, conversationList } from "../lib/store";
import type { Conversation as ConversationRow, ConversationStatus } from "../lib/types";
import { overviewIds } from "../lib/overview";
import { href } from "../lib/router";
import { cls, statusLabel, statusTitle } from "../lib/format";
import { Badge, Toggle } from "../components/ui";

const COLUMNS_KEY = "loom.overview.columns";
type Columns = 1 | 2 | 3;

function loadColumns(): Columns {
  try {
    const n = Number(localStorage.getItem(COLUMNS_KEY));
    return n === 1 || n === 2 || n === 3 ? n : 2;
  } catch {
    return 2;
  }
}

function saveColumns(n: Columns) {
  try {
    localStorage.setItem(COLUMNS_KEY, String(n));
  } catch {
    /* blocked storage: the choice just doesn't survive a reload */
  }
}

// Same mapping as Threads.tsx, so a thread reads the same status colour everywhere.
function statusTone(status: ConversationStatus): "neutral" | "green" | "amber" | "red" | "sky" {
  switch (status) {
    case "running":
      return "sky";
    case "waiting_approval":
    case "waiting_input":
      return "amber";
    case "done":
      return "green";
    case "failed":
      return "red";
    default:
      return "neutral";
  }
}

function Pane({ conversation }: { conversation: ConversationRow }) {
  const title = conversation.kind === "desk" ? "Desk" : conversation.title || conversation.goal || conversation.id;
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-neutral-200 dark:border-neutral-800">
      <div className="flex items-center gap-2 border-b border-neutral-200 px-3 py-1.5 dark:border-neutral-800">
        <span
          className="min-w-0 flex-1 truncate text-[13px] font-medium text-neutral-900 dark:text-neutral-100"
          title={title}
        >
          {title}
        </span>
        <Badge tone={statusTone(conversation.status)} title={statusTitle(conversation.status)}>
          {statusLabel(conversation.status)}
        </Badge>
        <a
          className="shrink-0 text-[11.5px] text-sky-700 hover:underline dark:text-sky-400"
          href={href(`/c/${conversation.id}`)}
        >
          Open
        </a>
      </div>
      <div className="min-h-0 flex-1">
        <Conversation id={conversation.id} />
      </div>
    </div>
  );
}

export function Overview() {
  const conversations = useStore(conversationList);
  const [showFinished, setShowFinished] = useState(false);
  const [columns, setColumns] = useState<Columns>(loadColumns);

  useEffect(() => {
    void store.loadConversations();
  }, []);

  const ids = useMemo(() => overviewIds(conversations, showFinished), [conversations, showFinished]);
  const byId = useMemo(() => new Map(conversations.map((c) => [c.id, c])), [conversations]);
  const threadCount = ids.reduce((n, id) => n + (byId.get(id)?.kind === "thread" ? 1 : 0), 0);

  function pickColumns(n: Columns) {
    setColumns(n);
    saveColumns(n);
  }

  return (
    <div className="flex h-full min-h-0 flex-col p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h1 className="text-[16px] font-semibold text-neutral-900 dark:text-neutral-100">Overview</h1>
        <div className="flex items-center gap-4">
          <label className="flex items-center gap-1.5 text-[12px] text-neutral-600 dark:text-neutral-400">
            <Toggle checked={showFinished} onChange={setShowFinished} label="Show finished" />
            Show finished
          </label>
          <div className="flex items-center gap-0.5 rounded-md border border-neutral-300 p-0.5 dark:border-neutral-700">
            {([1, 2, 3] as const).map((n) => (
              <button
                key={n}
                type="button"
                aria-pressed={columns === n}
                onClick={() => pickColumns(n)}
                className={cls(
                  "rounded px-2 py-0.5 text-[12px]",
                  columns === n
                    ? "bg-neutral-200 font-medium text-neutral-900 dark:bg-neutral-700 dark:text-neutral-100"
                    : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800",
                )}
              >
                {n}
              </button>
            ))}
          </div>
        </div>
      </div>

      {threadCount === 0 ? <p className="mb-3 text-[12.5px] text-neutral-500">No active threads.</p> : null}

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div
          className="grid gap-3"
          style={{
            gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))`,
            gridAutoRows: "minmax(320px, calc((100vh - 200px) / 2))",
          }}
        >
          {ids.map((id) => {
            const conversation = byId.get(id);
            return conversation ? <Pane key={id} conversation={conversation} /> : null;
          })}
        </div>
      </div>
    </div>
  );
}
