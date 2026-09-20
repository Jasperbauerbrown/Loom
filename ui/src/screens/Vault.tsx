// Vault screen (route `#/vault/<path>`). Tree + search on the left, note view/editor
// in the centre, auxiliary panels (recent changes, wanted notes, core meters,
// procedures, graph) on the right. See docs/03-technical-design.md §13.1, §4.3.

import { useEffect, useState } from "react";
import type { VaultNote } from "../lib/types";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import { Button, Collapsible, Empty, Section, Spinner } from "../components/ui";
import { VaultTree } from "../components/vault/VaultTree";
import { VaultSearch } from "../components/vault/VaultSearch";
import { NoteEditor } from "../components/vault/NoteEditor";
import { RecentChanges } from "../components/vault/RecentChanges";
import { WantedNotes } from "../components/vault/WantedNotes";
import { CoreMeters } from "../components/vault/CoreMeters";
import { Procedures } from "../components/vault/Procedures";
import { GraphView } from "../components/vault/GraphView";
import { NewNoteDialog } from "../components/vault/NewNoteDialog";
import { notePath } from "../lib/router";
import { localTime } from "../lib/format";
import { IconPlus } from "../components/Icons";

export function Vault({ path }: { path: string }) {
  const vaultTree = useStore((s) => s.vaultTree);
  const knowledgeStale = useStore((s) => s.knowledgeStale);
  const [note, setNote] = useState<VaultNote | null>(null);
  const [noteLoading, setNoteLoading] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [graphCenter, setGraphCenter] = useState<string | null>(null);

  useEffect(() => {
    if (vaultTree === null) void store.loadVaultTree();
  }, [vaultTree]);

  useEffect(() => {
    void loadNote(path);
    setGraphCenter(path || null);
  }, [path]);

  // re-load the tree and the open note when the vault changes underneath us
  useEffect(() => {
    if (knowledgeStale === 0) return;
    void store.loadVaultTree();
    if (path) void loadNote(path);
  }, [knowledgeStale]);

  async function loadNote(p: string) {
    if (!p) {
      setNote(null);
      return;
    }
    setNoteLoading(true);
    const row = await store.guard(() => api.vaultNote(p));
    setNoteLoading(false);
    setNote(row ?? null);
  }

  return (
    <div className="flex h-full min-h-0">
      {showNew ? (
        <NewNoteDialog onClose={() => setShowNew(false)} onCreated={() => store.loadVaultTree()} />
      ) : null}

      <div className="flex w-64 shrink-0 flex-col border-r border-neutral-200 p-3 dark:border-neutral-800">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-neutral-800 dark:text-neutral-200">Vault</h2>
          <Button size="sm" onClick={() => setShowNew(true)} aria-label="New note">
            <IconPlus className="h-3.5 w-3.5" /> New
          </Button>
        </div>
        <VaultSearch />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <VaultTree tree={vaultTree} currentPath={path} />
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {noteLoading ? (
          <Spinner />
        ) : note ? (
          <div>
            <NoteEditor note={note} onSaved={() => loadNote(path)} />

            <div className="mt-2 text-[11px] text-neutral-500">Modified {localTime(note.modified)}</div>

            <button
              type="button"
              onClick={() => {
                setShowGraph(!showGraph);
                setGraphCenter(path);
              }}
              className="mt-3 text-[12px] text-sky-700 hover:underline dark:text-sky-400"
            >
              {showGraph ? "Hide graph" : "Show graph"}
            </button>
            {showGraph && graphCenter ? (
              <div className="mt-2">
                <GraphView center={graphCenter} onSelect={(p) => (window.location.hash = notePath(p))} />
              </div>
            ) : null}

            <div className="mt-4 grid grid-cols-2 gap-4">
              <div>
                <h3 className="mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">
                  Links ({note.links.length})
                </h3>
                {note.links.length === 0 ? (
                  <Empty>No outgoing links.</Empty>
                ) : (
                  <ul className="space-y-0.5">
                    {note.links.map((l) => (
                      <li key={l.path}>
                        <a href={notePath(l.path)} className="text-[12.5px] text-sky-700 hover:underline dark:text-sky-400">
                          {l.title || l.path}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div>
                <h3 className="mb-1 text-[12px] font-semibold text-neutral-600 dark:text-neutral-400">
                  Backlinks ({note.backlinks.length})
                </h3>
                {note.backlinks.length === 0 ? (
                  <Empty>No backlinks.</Empty>
                ) : (
                  <ul className="space-y-0.5">
                    {note.backlinks.map((l) => (
                      <li key={l.path}>
                        <a href={notePath(l.path)} className="text-[12.5px] text-sky-700 hover:underline dark:text-sky-400">
                          {l.title || l.path}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>

            <div className="mt-6">
              <Section title="History">
                <RecentChanges notePath={path} />
              </Section>
            </div>
          </div>
        ) : (
          <Empty>Select a note, or create a new one.</Empty>
        )}
      </div>

      <div className="w-72 shrink-0 overflow-y-auto border-l border-neutral-200 p-3 dark:border-neutral-800">
        <Collapsible summary="Recent changes" defaultOpen>
          <RecentChanges />
        </Collapsible>
        <div className="mt-4">
          <Collapsible summary="Wanted notes">
            <WantedNotes />
          </Collapsible>
        </div>
        <div className="mt-4">
          <Collapsible summary="Core fill">
            <CoreMeters />
          </Collapsible>
        </div>
        <div className="mt-4">
          <Collapsible summary="Procedures">
            <Procedures />
          </Collapsible>
        </div>
      </div>
    </div>
  );
}
