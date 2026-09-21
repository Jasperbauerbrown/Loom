// Left-column note tree, grouped by type, collapsible folders.

import { useMemo, useState, type ReactNode } from "react";
import type { VaultTreeNode } from "../../lib/types";
import { href } from "../../lib/router";
import { cls } from "../../lib/format";
import { IconChevron, IconFile, IconFolder, IconTrash } from "../Icons";
import { Empty } from "../ui";

interface Actions {
  onDeleteFolder: (path: string) => void;
  onOpenFolder: (path: string) => void;
}

function Node({
  node,
  depth,
  currentPath,
  actions,
}: {
  node: VaultTreeNode;
  depth: number;
  currentPath: string;
  actions: Actions;
}) {
  const [open, setOpen] = useState(true);
  const isFolder = node.kind === "dir" || !!(node.children && node.children.length);
  const hasChildren = !!(node.children && node.children.length);
  const active = !isFolder && node.path === currentPath;
  const protectedFolder = node.path === "core" || node.path === "";
  return (
    <div>
      {isFolder ? (
        <div className="group flex items-center">
          <button
            type="button"
            onClick={() => setOpen(!open)}
            style={{ paddingLeft: 6 + depth * 12 }}
            className="flex min-w-0 flex-1 items-center gap-1.5 truncate rounded px-1.5 py-1 text-left text-[12.5px] text-neutral-700 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800"
            title={node.path}
          >
            <IconChevron className={cls("h-3 w-3 shrink-0 transition-transform", open && hasChildren && "rotate-90")} />
            <IconFolder className="h-3.5 w-3.5 shrink-0 opacity-60" />
            <span className="truncate">{node.title || node.path}</span>
          </button>
          <button
            type="button"
            onClick={() => actions.onOpenFolder(node.path)}
            aria-label={`Open ${node.path} in file manager`}
            title="Open in file manager"
            className="invisible shrink-0 rounded px-1 py-0.5 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700 group-hover:visible focus:visible dark:hover:bg-neutral-800 dark:hover:text-neutral-200"
          >
            <IconFolder className="h-3.5 w-3.5" />
          </button>
          <button
            type="button"
            onClick={() => actions.onDeleteFolder(node.path)}
            disabled={protectedFolder}
            aria-label={`Delete folder ${node.path}`}
            title={protectedFolder ? "core/ cannot be deleted" : "Delete folder…"}
            className="invisible shrink-0 rounded px-1 py-0.5 text-neutral-400 hover:bg-red-50 hover:text-red-600 group-hover:visible focus:visible disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-neutral-400 dark:hover:bg-red-950/40 dark:hover:text-red-400"
          >
            <IconTrash className="h-3.5 w-3.5" />
          </button>
        </div>
      ) : (
        <a
          href={href(`/vault/${node.path.split("/").map(encodeURIComponent).join("/")}`)}
          style={{ paddingLeft: 6 + depth * 12 }}
          className={cls(
            "flex items-center gap-1.5 truncate rounded px-1.5 py-1 text-[12.5px]",
            active
              ? "bg-sky-100 text-sky-900 dark:bg-sky-950 dark:text-sky-200"
              : "text-neutral-700 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800",
          )}
          title={node.path}
        >
          <IconFile className="h-3.5 w-3.5 shrink-0 opacity-60" />
          <span className="truncate">{node.title || node.path}</span>
        </a>
      )}
      {isFolder && open && hasChildren ? (
        <div>
          {node.children!.map((c) => (
            <Node key={`${c.path}:${c.title}`} node={c} depth={depth + 1} currentPath={currentPath} actions={actions} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

/** Notes under these nodes, at any depth (folders themselves do not count). */
function countNotes(nodes: VaultTreeNode[]): number {
  let n = 0;
  for (const node of nodes) {
    const isFolder = node.kind === "dir" || !!(node.children && node.children.length);
    n += isFolder ? countNotes(node.children ?? []) : 1;
  }
  return n;
}

export function VaultTree({
  tree,
  currentPath,
  onDeleteFolder,
  onOpenFolder,
}: {
  tree: VaultTreeNode[] | null;
  currentPath: string;
  onDeleteFolder: (path: string) => void;
  onOpenFolder: (path: string) => void;
}) {
  const actions: Actions = { onDeleteFolder, onOpenFolder };
  const groups = useMemo(() => {
    const byType = new Map<string, VaultTreeNode[]>();
    for (const n of tree ?? []) {
      const list = byType.get(n.type || "other") ?? [];
      list.push(n);
      byType.set(n.type || "other", list);
    }
    return [...byType.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [tree]);

  if (tree === null) return <Empty>Loading…</Empty>;
  if (groups.length === 0) return <Empty>No notes yet.</Empty>;

  return (
    <div>
      {groups.map(([type, nodes]) => (
        <GroupHeader key={type} type={type} count={countNotes(nodes)}>
          {nodes.map((n) => (
            <Node key={`${n.path}:${n.title}`} node={n} depth={0} currentPath={currentPath} actions={actions} />
          ))}
        </GroupHeader>
      ))}
    </div>
  );
}

function GroupHeader({ type, count, children }: { type: string; count: number; children: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mb-1">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-1 rounded px-1.5 py-1 text-left text-[11.5px] font-semibold uppercase tracking-wide text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
      >
        <IconChevron className={cls("h-3 w-3 transition-transform", open && "rotate-90")} />
        {type} <span className="font-normal normal-case text-neutral-400">({count})</span>
      </button>
      {open ? <div>{children}</div> : null}
    </div>
  );
}
