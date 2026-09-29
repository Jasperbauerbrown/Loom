// Graph (`#/graph/<note path>`): the vault link graph plus the desk/thread tree, as a
// full screen (03 §13.1). The picture itself is `GraphView` (also used compact, in the
// Vault side panel); this screen only resolves what to centre on when the route carries
// no path.

import { useEffect, useState } from "react";
import type { VaultTreeNode } from "../lib/types";
import { store, useStore } from "../lib/store";
import { navigate, graphPath } from "../lib/router";
import { GraphView, recallGraphCenter } from "../components/vault/GraphView";
import { Spinner } from "../components/ui";

function firstNotePath(nodes: VaultTreeNode[] | null): string | null {
  for (const node of nodes ?? []) {
    if (node.kind === "note") return node.path;
    const hit = firstNotePath(node.children ?? null);
    if (hit) return hit;
  }
  return null;
}

export function Graph({ path }: { path: string }) {
  const vaultTree = useStore((s) => s.vaultTree);
  useEffect(() => {
    if (vaultTree === null) void store.loadVaultTree();
  }, [vaultTree]);

  const [fallback, setFallback] = useState<string | null>(null);
  useEffect(() => {
    if (path || fallback) return;
    const remembered = recallGraphCenter();
    if (remembered) {
      setFallback(remembered);
      return;
    }
    const first = firstNotePath(vaultTree);
    if (first) setFallback(first);
  }, [path, fallback, vaultTree]);

  const center = path || fallback;

  if (!center) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner />
      </div>
    );
  }

  return (
    <div className="h-full min-h-0 p-3">
      <GraphView center={center} onSelect={(p) => navigate(graphPath(p))} />
    </div>
  );
}
