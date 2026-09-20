// Note body: markdown view / textarea editor with wikilink completion, plus a
// frontmatter form (falls back to raw text when values aren't flat scalars).

import { useEffect, useRef, useState } from "react";
import type { VaultNote, VaultSearchHit } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Button, FieldRow, Input, Textarea } from "../ui";
import { Markdown } from "../Markdown";
import { WikilinkPopup } from "./WikilinkPopup";
import {
  isFlatFrontmatter,
  joinFrontmatter,
  parseFlatFrontmatter,
  serializeFlatFrontmatter,
  splitFrontmatter,
} from "./frontmatter";

export function NoteEditor({ note, onSaved }: { note: VaultNote; onSaved: () => void }) {
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState("");
  const [fmRaw, setFmRaw] = useState<string | null>(null);
  const [fmFields, setFmFields] = useState<[string, string][] | null>(null);
  const [saving, setSaving] = useState(false);
  const [wiki, setWiki] = useState<{ query: string; start: number; hits: VaultSearchHit[]; active: number } | null>(
    null,
  );
  const taRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const { frontmatter, body: rawBody } = splitFrontmatter(note.content);
    setBody(rawBody);
    setFmRaw(frontmatter);
    if (frontmatter !== null) {
      const parsed = parseFlatFrontmatter(frontmatter);
      if (isFlatFrontmatter(parsed)) {
        setFmFields(Object.entries(parsed).map(([k, v]) => [k, Array.isArray(v) ? v.join(", ") : String(v)]));
      } else {
        setFmFields(null);
      }
    } else {
      setFmFields(null);
    }
    setEditing(false);
    setWiki(null);
  }, [note.path]);

  function currentFrontmatterRaw(): string | null {
    if (fmFields) {
      const obj: Record<string, string> = {};
      for (const [k, v] of fmFields) if (k.trim()) obj[k.trim()] = v;
      return serializeFlatFrontmatter(obj);
    }
    return fmRaw;
  }

  async function save() {
    setSaving(true);
    const content = joinFrontmatter(currentFrontmatterRaw(), body);
    const ok = await store.guard(() => api.saveNote(note.path, content));
    setSaving(false);
    if (ok) {
      store.toast("info", "Saved", note.path);
      setEditing(false);
      onSaved();
    }
  }

  async function runWikiSearch(query: string, start: number) {
    const hits = query.trim() ? await store.guard(() => api.vaultSearch(query)) : [];
    setWiki((w) => (w && w.start === start ? { ...w, query, hits: hits ?? [], active: 0 } : w));
  }

  function onBodyChange(value: string) {
    setBody(value);
    const ta = taRef.current;
    if (!ta) return;
    const pos = ta.selectionStart;
    const upto = value.slice(0, pos);
    const openIdx = upto.lastIndexOf("[[");
    const closeIdx = upto.lastIndexOf("]]");
    if (openIdx === -1 || openIdx < closeIdx) {
      setWiki(null);
      return;
    }
    const query = upto.slice(openIdx + 2);
    if (query.includes("\n")) {
      setWiki(null);
      return;
    }
    setWiki({ query, start: openIdx, hits: wiki?.start === openIdx ? wiki.hits : [], active: 0 });
    void runWikiSearch(query, openIdx);
  }

  function pickWikilink(hit: VaultSearchHit) {
    if (!wiki) return;
    const ta = taRef.current;
    const pos = ta ? ta.selectionStart : body.length;
    const before = body.slice(0, wiki.start);
    const afterCloseIdx = body.slice(pos).startsWith("]]") ? pos + 2 : pos;
    const after = body.slice(afterCloseIdx);
    const insert = `[[${hit.path}]]`;
    const next = before + insert + after;
    setBody(next);
    setWiki(null);
    requestAnimationFrame(() => {
      if (!taRef.current) return;
      const caret = before.length + insert.length;
      taRef.current.focus();
      taRef.current.setSelectionRange(caret, caret);
    });
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (!wiki) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setWiki((w) => (w ? { ...w, active: Math.min(w.active + 1, Math.max(w.hits.length - 1, 0)) } : w));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setWiki((w) => (w ? { ...w, active: Math.max(w.active - 1, 0) } : w));
    } else if (e.key === "Enter") {
      if (wiki.hits.length === 0) return;
      e.preventDefault();
      pickWikilink(wiki.hits[wiki.active]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      setWiki(null);
    }
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between">
        <div className="flex items-baseline gap-2">
          <h1 className="text-[17px] font-semibold text-neutral-900 dark:text-neutral-100">{note.title || note.path}</h1>
          <span className="text-[11.5px] text-neutral-500">{note.type}</span>
        </div>
        <div className="flex gap-2">
          {editing ? (
            <>
              <Button size="sm" onClick={() => setEditing(false)} disabled={saving}>
                Cancel
              </Button>
              <Button size="sm" variant="primary" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </Button>
            </>
          ) : (
            <Button size="sm" onClick={() => setEditing(true)}>
              Edit
            </Button>
          )}
        </div>
      </div>

      {fmFields || fmRaw !== null ? (
        <div className="mb-3 rounded-md border border-neutral-200 bg-neutral-50 px-3 py-1 dark:border-neutral-800 dark:bg-neutral-900/60">
          {fmFields ? (
            editing ? (
              <div>
                {fmFields.map(([k, v], i) => (
                  <FieldRow key={i} label={<Input value={k} onChange={(e) => {
                    const next = fmFields.slice();
                    next[i] = [e.target.value, v];
                    setFmFields(next);
                  }} className="max-w-[10rem] font-mono text-[12px]" />}>
                    <div className="flex gap-2">
                      <Input
                        value={v}
                        onChange={(e) => {
                          const next = fmFields.slice();
                          next[i] = [k, e.target.value];
                          setFmFields(next);
                        }}
                      />
                      <Button size="sm" variant="ghost" onClick={() => setFmFields(fmFields.filter((_, j) => j !== i))}>
                        ✕
                      </Button>
                    </div>
                  </FieldRow>
                ))}
                <Button size="sm" variant="ghost" onClick={() => setFmFields([...fmFields, ["", ""]])}>
                  + field
                </Button>
              </div>
            ) : (
              <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 py-2 text-[12px]">
                {fmFields.map(([k, v], i) => (
                  <div key={i} className="contents">
                    <dt className="font-mono text-neutral-500">{k}</dt>
                    <dd className="truncate text-neutral-700 dark:text-neutral-300">{v}</dd>
                  </div>
                ))}
              </dl>
            )
          ) : editing ? (
            <Textarea
              className="my-2 font-mono text-[12px]"
              rows={Math.min(10, Math.max(3, (fmRaw ?? "").split("\n").length))}
              value={fmRaw ?? ""}
              onChange={(e) => setFmRaw(e.target.value)}
            />
          ) : (
            <pre className="overflow-x-auto py-2 font-mono text-[11.5px] text-neutral-600 dark:text-neutral-400">{fmRaw}</pre>
          )}
        </div>
      ) : null}

      {editing ? (
        <div className="relative">
          <textarea
            ref={taRef}
            className="w-full min-h-[40vh] resize-y rounded-md border border-neutral-300 bg-white px-2 py-1.5 font-mono text-[13px] text-neutral-900 focus:border-sky-500 focus:outline-none focus:ring-1 focus:ring-sky-500 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
            value={body}
            onChange={(e) => onBodyChange(e.target.value)}
            onKeyDown={onKeyDown}
          />
          {wiki ? <WikilinkPopup hits={wiki.hits} activeIndex={wiki.active} onPick={pickWikilink} /> : null}
        </div>
      ) : (
        <Markdown source={body} />
      )}
    </div>
  );
}
