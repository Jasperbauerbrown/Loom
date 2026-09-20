// Left-column note tree, grouped by type, collapsible folders.

import { useMemo, useState, type ReactNode } from "react";
import type { VaultTreeNode } from "../../lib/types";
import { href } from "../../lib/router";
import { cls } from "../../lib/format";
import { IconChevron, IconFile, IconFolder } from "../Icons";
import { Empty } from "../ui";

function Node({ node, depth, currentPath }: { node: VaultTreeNode; depth: number; currentPath: string }) {
  const [open, setOpen] = useState(true);
  const hasChildren = !!(node.children && node.children.length);
  const active = !hasChildren && node.path === currentPath;
  return (
    <div>
      {hasChildren ? (
        <button
          type="button"
          onClick={() => setOpen(!open)}
          style={{ paddingLeft: 6 + depth * 12 }}
          className="flex w-full items-center gap-1.5 truncate rounded px-1.5 py-1 text-left text-[12.5px] text-neutral-700 hover:bg-neutral-100 dark:text-neutral-300 dark:hover:bg-neutral-800"
        >
          <IconChevron className={cls("h-3 w-3 shrink-0 transition-transform", open && "rotate-90")} />
          <IconFolder className="h-3.5 w-3.5 shrink-0 opacity-60" />
          <span className="truncate">{node.title || node.path}</span>
        </button>
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
      {hasChildren && open ? (
        <div>
          {node.children!.map((c) => (
            <Node key={`${c.path}:${c.title}`} node={c} depth={depth + 1} currentPath={currentPath} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

export function VaultTree({ tree, currentPath }: { tree: VaultTreeNode[] | null; currentPath: string }) {
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
        <GroupHeader key={type} type={type} count={nodes.length}>
          {nodes.map((n) => (
            <Node key={`${n.path}:${n.title}`} node={n} depth={0} currentPath={currentPath} />
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
