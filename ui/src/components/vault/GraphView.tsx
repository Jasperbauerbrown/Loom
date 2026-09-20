// Vault link graph: inline SVG, radial layout around the centre node, no physics.
// Click a node to recenter; depth controls how many hops are fetched.

import { useEffect, useState } from "react";
import type { GraphData } from "../../lib/types";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { Empty, Select, Spinner } from "../ui";

const W = 480;
const H = 360;
const CX = W / 2;
const CY = H / 2;

export function GraphView({ center, onSelect }: { center: string; onSelect: (path: string) => void }) {
  const [depth, setDepth] = useState(1);
  const [data, setData] = useState<GraphData | null>(null);

  useEffect(() => {
    (async () => {
      setData(null);
      const g = await store.guard(() => api.vaultGraph(center, depth));
      setData(g ?? { nodes: [], edges: [] });
    })();
  }, [center, depth]);

  if (data === null) return <Spinner />;
  if (data.nodes.length === 0) return <Empty>No graph data.</Empty>;

  const others = data.nodes.filter((n) => n.id !== center);
  const radius = Math.min(W, H) / 2 - 60;
  const positions = new Map<string, { x: number; y: number }>();
  positions.set(center, { x: CX, y: CY });
  others.forEach((n, i) => {
    const angle = (2 * Math.PI * i) / Math.max(1, others.length) - Math.PI / 2;
    positions.set(n.id, { x: CX + radius * Math.cos(angle), y: CY + radius * Math.sin(angle) });
  });

  return (
    <div>
      <div className="mb-2 flex items-center justify-end gap-2">
        <span className="text-[11.5px] text-neutral-500">Depth</span>
        <Select className="w-20" value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
          <option value={1}>1</option>
          <option value={2}>2</option>
          <option value={3}>3</option>
        </Select>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full rounded-md border border-neutral-200 bg-neutral-50 dark:border-neutral-800 dark:bg-neutral-950">
        <g>
          {data.edges.map((e, i) => {
            const a = positions.get(e.src);
            const b = positions.get(e.dst);
            if (!a || !b) return null;
            return (
              <line
                key={i}
                x1={a.x}
                y1={a.y}
                x2={b.x}
                y2={b.y}
                stroke="currentColor"
                strokeOpacity={0.25}
                className="text-neutral-400 dark:text-neutral-600"
              />
            );
          })}
        </g>
        <g>
          {data.nodes.map((n) => {
            const p = positions.get(n.id);
            if (!p) return null;
            const isCenter = n.id === center;
            return (
              <g
                key={n.id}
                transform={`translate(${p.x},${p.y})`}
                className="cursor-pointer"
                onClick={() => onSelect(n.id)}
              >
                <circle r={isCenter ? 9 : 6} className={isCenter ? "fill-sky-600" : "fill-neutral-400 dark:fill-neutral-600"} />
                <text
                  x={0}
                  y={isCenter ? -14 : -10}
                  textAnchor="middle"
                  className="fill-neutral-700 text-[9px] dark:fill-neutral-300"
                >
                  {(n.title || n.id).slice(0, 22)}
                </text>
              </g>
            );
          })}
        </g>
      </svg>
    </div>
  );
}
