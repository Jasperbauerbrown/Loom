// Artifact drawer: paged read of spilled tool output (03 §4.4).

import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { store } from "../lib/store";
import { bytes } from "../lib/format";
import { Button, Spinner } from "./ui";

const PAGE = 20000;

export function ArtifactDrawer({ id, onClose }: { id: string; onClose: () => void }) {
  const [text, setText] = useState("");
  const [total, setTotal] = useState<number | null>(null);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (from: number) => {
      setLoading(true);
      const res = await store.guard(() => api.artifact(id, { offset: from, limit: PAGE }));
      setLoading(false);
      if (!res) return;
      const chunk = (res.text ?? res.content ?? "") as string;
      setText((prev) => (from === 0 ? chunk : prev + chunk));
      setTotal(res.bytes ?? null);
      setOffset(from + chunk.length);
    },
    [id],
  );

  useEffect(() => {
    setText("");
    setOffset(0);
    void load(0);
  }, [id, load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const more = total !== null && offset < total;

  return (
    <div className="fixed inset-0 z-50 flex justify-end bg-black/30" onMouseDown={onClose}>
      <aside
        onMouseDown={(e) => e.stopPropagation()}
        className="flex h-full w-[min(760px,90vw)] flex-col border-l border-neutral-200 bg-white shadow-2xl dark:border-neutral-800 dark:bg-neutral-950"
      >
        <header className="flex items-center justify-between border-b border-neutral-200 px-4 py-2.5 dark:border-neutral-800">
          <div>
            <div className="text-[13px] font-semibold">Artifact {id}</div>
            <div className="text-[11.5px] text-neutral-500">
              {total !== null ? `${bytes(total)} total` : ""} — showing {offset.toLocaleString()} chars
            </div>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </header>
        <pre className="flex-1 overflow-auto p-4 font-mono text-[12px] leading-5 whitespace-pre-wrap">{text}</pre>
        <footer className="flex items-center gap-2 border-t border-neutral-200 px-4 py-2 dark:border-neutral-800">
          {loading ? <Spinner /> : null}
          {more ? (
            <Button size="sm" onClick={() => void load(offset)} disabled={loading}>
              Load more
            </Button>
          ) : (
            <span className="text-[11.5px] text-neutral-500">End of artifact.</span>
          )}
        </footer>
      </aside>
    </div>
  );
}
