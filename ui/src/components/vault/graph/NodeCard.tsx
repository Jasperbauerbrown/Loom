// The selected-node card on the Graph screen: what the node is, how it is connected in
// the picture, and the actions (open, centre, open conversation).

import { cls, statusLabel } from "../../../lib/format";
import { graphPath, navigate, notePath } from "../../../lib/router";
import { Button } from "../../ui";
import type { RenderEdge, RenderNode } from "./GraphCanvas";
import { HUE_SWATCH, hueOf, statusHue } from "./style";

export function NodeCard({
  node,
  edges,
  isCenter,
  onClose,
  onCentre,
}: {
  node: RenderNode;
  edges: RenderEdge[];
  isCenter: boolean;
  onClose: () => void;
  onCentre: (node: RenderNode) => void;
}) {
  let out = 0;
  let inn = 0;
  let other = 0;
  const seen = new Set<string>();
  for (const e of edges) {
    if (e.src !== node.id && e.dst !== node.id) continue;
    const key = `${e.src}\u0000${e.dst}\u0000${e.kind ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    if (e.kind && e.kind !== "link") other++;
    else if (e.src === node.id) out++;
    else inn++;
  }
  const conv = node.kind === "conversation";
  const status = statusHue(node.status);

  return (
    <div
      className="absolute left-3 top-3 w-72 rounded-lg border border-neutral-200 bg-white/95 p-3 text-[12px] shadow-lg backdrop-blur dark:border-neutral-800 dark:bg-neutral-900/95"
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="flex items-start gap-2">
        <span
          className={cls(
            "mt-1 shrink-0",
            conv ? "h-2.5 w-3.5 rounded-[3px]" : "h-2.5 w-2.5 rounded-full",
            conv ? status.swatch : HUE_SWATCH[hueOf(node.type)],
          )}
        />
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-semibold text-neutral-900 dark:text-neutral-100" title={node.title}>
            {node.title}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11.5px] text-neutral-500">
            {conv ? (
              <>
                <span>{node.convKind ?? "conversation"}</span>
                <span>·</span>
                <span>{statusLabel(node.status ?? "idle")}</span>
              </>
            ) : (
              <span>{node.type || "note"}</span>
            )}
            {isCenter ? (
              <span className="rounded bg-sky-100 px-1 text-[10.5px] font-medium text-sky-800 dark:bg-sky-950 dark:text-sky-300">
                centre
              </span>
            ) : null}
          </div>
        </div>
        <button
          type="button"
          aria-label="Close"
          title="Close (Esc)"
          onClick={onClose}
          className="-mr-1 -mt-1 rounded p-1 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-700 dark:hover:bg-neutral-800 dark:hover:text-neutral-200"
        >
          <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round">
            <path d="M4 4l8 8M12 4l-8 8" />
          </svg>
        </button>
      </div>

      {conv ? (
        node.goal ? <p className="mt-2 line-clamp-3 text-neutral-600 dark:text-neutral-400">{node.goal}</p> : null
      ) : (
        <div className="mt-2 truncate font-mono text-[11px] text-neutral-500" title={node.id}>
          {node.id}
        </div>
      )}

      <div className="mt-2 text-[11.5px] text-neutral-500">
        {conv ? (
          <span>
            {other} edge{other === 1 ? "" : "s"} in view
          </span>
        ) : (
          <span>
            {out} outgoing · {inn} backlink{inn === 1 ? "" : "s"}
            {other ? ` · ${other} from conversations` : ""} <span className="text-neutral-400">(in view)</span>
          </span>
        )}
      </div>

      <div className="mt-2.5 flex flex-wrap gap-1.5">
        {conv ? (
          <Button size="sm" variant="primary" onClick={() => navigate(`/c/${node.id}`)}>
            Open conversation
          </Button>
        ) : (
          <>
            <Button size="sm" variant="primary" onClick={() => navigate(notePath(node.id))}>
              Open note
            </Button>
            {!isCenter ? (
              <Button size="sm" onClick={() => onCentre(node)}>
                Centre here
              </Button>
            ) : (
              <Button size="sm" onClick={() => navigate(graphPath(node.id))} disabled>
                Centred
              </Button>
            )}
          </>
        )}
      </div>
    </div>
  );
}
