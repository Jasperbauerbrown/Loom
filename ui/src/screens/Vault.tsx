// Vault screen (route `#/vault/<path>`). Tree + search on the left, note view/editor
// in the centre, auxiliary panels (recent changes, wanted notes, core meters,
// procedures, graph) on the right. See docs/03-technical-design.md §13.1, §4.3.

import { useEffect, useState } from "react";
import type { VaultNote, VaultTreeNode } from "../lib/types";
import { api } from "../lib/api";
import { store, useStore } from "../lib/store";
import { Button, Collapsible, Confirm, Empty, Section, Spinner } from "../components/ui";
import { VaultTree } from "../components/vault/VaultTree";
import { VaultSearch } from "../components/vault/VaultSearch";
import { NoteEditor } from "../components/vault/NoteEditor";
import { RecentChanges } from "../components/vault/RecentChanges";
import { WantedNotes } from "../components/vault/WantedNotes";
import { CoreMeters } from "../components/vault/CoreMeters";
import { Procedures } from "../components/vault/Procedures";
import { GraphView } from "../components/vault/GraphView";
import { NewNoteDialog } from "../components/vault/NewNoteDialog";
import { navigate, notePath } from "../lib/router";
import { localTime } from "../lib/format";
import { IconFolder, IconPlus } from "../components/Icons";

/** The node at `path` in the (grouped) tree, or null. */
function findNode(nodes: VaultTreeNode[] | null, path: string): VaultTreeNode | null {
  for (const node of nodes ?? []) {
    if (node.path === path) return node;
    const hit = findNode(node.children ?? null, path);
    if (hit) return hit;
  }
  return null;
}

function countNotes(node: VaultTreeNode | null): number {
  if (!node) return 0;
  if (!node.children || node.children.length === 0) return node.kind === "dir" ? 0 : 1;
  return node.children.reduce((n, c) => n + countNotes(c), 0);
}

function parentOf(path: string): string {
  const cut = path.lastIndexOf("/");
  return cut === -1 ? "" : path.slice(0, cut);
}

export function Vault({ path }: { path: string }) {
  const vaultTree = useStore((s) => s.vaultTree);
  const knowledgeStale = useStore((s) => s.knowledgeStale);
  const [note, setNote] = useState<VaultNote | null>(null);
  const [noteLoading, setNoteLoading] = useState(false);
  const [showNew, setShowNew] = useState(false);
  const [showGraph, setShowGraph] = useState(false);
  const [graphCenter, setGraphCenter] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{ kind: "note" | "folder"; path: string } | null>(null);

  // A vault path that is not a `.md` note is a folder: the tree's folder actions and the
  // place we land after deleting a note.
  const isFolder = path !== "" && !path.endsWith(".md");
  const folderNode = isFolder ? findNode(vaultTree, path) : null;
  const folderCount = countNotes(folderNode);

  async function openFolder(target: string) {
    const res = await store.guard(() => api.openPath(target || "."));
    if (res) store.toast("info", "Opened in file manager", res.opened);
  }

  async function deleteNote(target: string) {
    const res = await store.guard(() => api.deleteNote(target));
    if (!res) return;
    store.toast("info", "Note deleted", target);
    const parent = parentOf(target);
    navigate(parent ? `/vault/${parent}` : "/vault");
    void store.loadVaultTree();
  }

  async function deleteFolder(target: string) {
    const res = await store.guard(() => api.deleteFolder(target));
    if (!res) return;
    store.toast("info", `Folder deleted (${res.deleted.length} notes)`, `${target} — revert it from Recent changes.`);
    if (path === target || path.startsWith(`${target}/`)) {
      const parent = parentOf(target);
      navigate(parent ? `/vault/${parent}` : "/vault");
    }
    void store.loadVaultTree();
  }

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
    if (!p || !p.endsWith(".md")) {
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
      {confirmDelete?.kind === "note" ? (
        <Confirm
          title="Delete note"
          body={<>Delete <code className="font-mono">{confirmDelete.path}</code>? It is one git commit, revertible from Recent changes.</>}
          confirmLabel="Delete note"
          danger
          onConfirm={() => void deleteNote(confirmDelete.path)}
          onClose={() => setConfirmDelete(null)}
        />
      ) : null}
      {confirmDelete?.kind === "folder" ? (
        <Confirm
          title="Delete folder"
          body={
            <>
              Everything under <code className="font-mono">{confirmDelete.path}</code> —{" "}
              {countNotes(findNode(vaultTree, confirmDelete.path))} notes — will be removed in one commit; revertible
              from Recent changes.
            </>
          }
          confirmLabel="Delete folder"
          danger
          onConfirm={() => void deleteFolder(confirmDelete.path)}
          onClose={() => setConfirmDelete(null)}
        />
      ) : null}

      <div className="flex w-64 shrink-0 flex-col border-r border-neutral-200 p-3 dark:border-neutral-800">
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-[13px] font-semibold text-neutral-800 dark:text-neutral-200">Vault</h2>
          <div className="flex gap-1.5">
            <Button size="sm" onClick={() => void openFolder("")} title="Open the vault folder in the OS file manager">
              <IconFolder className="h-3.5 w-3.5" /> Open vault
            </Button>
            <Button size="sm" onClick={() => setShowNew(true)} aria-label="New note">
              <IconPlus className="h-3.5 w-3.5" /> New
            </Button>
          </div>
        </div>
        <VaultSearch />
        <div className="min-h-0 flex-1 overflow-y-auto">
          <VaultTree
            tree={vaultTree}
            currentPath={path}
            onOpenFolder={(p) => void openFolder(p)}
            onDeleteFolder={(p) => setConfirmDelete({ kind: "folder", path: p })}
          />
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {noteLoading ? (
          <Spinner />
        ) : note ? (
          <div>
            <NoteEditor
              note={note}
              onSaved={() => loadNote(path)}
              onDelete={() => setConfirmDelete({ kind: "note", path: note.path })}
              onOpenFolder={() => void openFolder(parentOf(note.path) || ".")}
            />

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
        ) : isFolder ? (
          <div>
            <h1 className="text-[17px] font-semibold text-neutral-900 dark:text-neutral-100">{path}</h1>
            <div className="mt-1 text-[12px] text-neutral-500">
              Folder · {folderCount} {folderCount === 1 ? "note" : "notes"}
            </div>
            <div className="mt-3 flex gap-2">
              <Button size="sm" onClick={() => void openFolder(path)}>
                <IconFolder className="h-3.5 w-3.5" /> Open in file manager
              </Button>
              <Button
                size="sm"
                variant="danger"
                disabled={path === "core"}
                title={path === "core" ? "core/ cannot be deleted" : undefined}
                onClick={() => setConfirmDelete({ kind: "folder", path })}
              >
                Delete folder…
              </Button>
            </div>
            <div className="mt-6">
              <Empty>Select a note from the tree.</Empty>
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
