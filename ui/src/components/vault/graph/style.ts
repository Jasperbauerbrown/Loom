// Colour for the graph (03 §13.1). Every colour is a Tailwind utility, so dark mode
// and the palettes keep working. The accent (`sky-*`) is what a palette recolours, so
// it is kept for emphasis — the centre, the selection, a hovered neighbourhood — and
// never used as a note-type hue, which would collide with it under some palettes.

export type Hue =
  | "blue"
  | "emerald"
  | "amber"
  | "violet"
  | "rose"
  | "teal"
  | "orange"
  | "fuchsia"
  | "cyan"
  | "lime"
  | "neutral";

/** The vault's own folder types keep one hue each everywhere; any other type hashes
 *  into the same set, so a type's colour never changes between views. */
const KNOWN: Record<string, Hue> = {
  project: "blue",
  person: "emerald",
  device: "amber",
  topic: "violet",
  core: "rose",
  course: "lime",
  procedure: "orange",
  connector: "fuchsia",
  log: "cyan",
  invalid: "neutral",
};

const CYCLE: Hue[] = ["blue", "emerald", "amber", "violet", "rose", "teal", "orange", "fuchsia", "cyan", "lime"];

export function hueOf(type?: string): Hue {
  if (!type) return "neutral";
  const known = KNOWN[type];
  if (known) return known;
  let h = 0;
  for (let i = 0; i < type.length; i++) h = (h * 31 + type.charCodeAt(i)) >>> 0;
  return CYCLE[h % CYCLE.length];
}

// Full class strings, spelled out so Tailwind's scanner sees them. 500 reads on both
// white and near-black; dark mode lifts to 400 for a touch more glow on dark grounds.
export const HUE_FILL: Record<Hue, string> = {
  blue: "fill-blue-500 dark:fill-blue-400",
  emerald: "fill-emerald-500 dark:fill-emerald-400",
  amber: "fill-amber-500 dark:fill-amber-400",
  violet: "fill-violet-500 dark:fill-violet-400",
  rose: "fill-rose-500 dark:fill-rose-400",
  teal: "fill-teal-500 dark:fill-teal-400",
  orange: "fill-orange-500 dark:fill-orange-400",
  fuchsia: "fill-fuchsia-500 dark:fill-fuchsia-400",
  cyan: "fill-cyan-500 dark:fill-cyan-400",
  lime: "fill-lime-500 dark:fill-lime-400",
  neutral: "fill-neutral-400 dark:fill-neutral-500",
};

export const HUE_SWATCH: Record<Hue, string> = {
  blue: "bg-blue-500 dark:bg-blue-400",
  emerald: "bg-emerald-500 dark:bg-emerald-400",
  amber: "bg-amber-500 dark:bg-amber-400",
  violet: "bg-violet-500 dark:bg-violet-400",
  rose: "bg-rose-500 dark:bg-rose-400",
  teal: "bg-teal-500 dark:bg-teal-400",
  orange: "bg-orange-500 dark:bg-orange-400",
  fuchsia: "bg-fuchsia-500 dark:bg-fuchsia-400",
  cyan: "bg-cyan-500 dark:bg-cyan-400",
  lime: "bg-lime-500 dark:bg-lime-400",
  neutral: "bg-neutral-400 dark:bg-neutral-500",
};

/** Conversation status → fill; the same meanings as the status badges elsewhere. */
export function statusHue(status?: string): { fill: string; swatch: string } {
  switch (status) {
    case "running":
      return { fill: "fill-sky-500 dark:fill-sky-400", swatch: "bg-sky-500 dark:bg-sky-400" };
    case "waiting_approval":
    case "waiting_input":
      return { fill: "fill-amber-500 dark:fill-amber-400", swatch: "bg-amber-500 dark:bg-amber-400" };
    case "done":
      return {
        fill: "fill-emerald-500 dark:fill-emerald-400",
        swatch: "bg-emerald-500 dark:bg-emerald-400",
      };
    case "failed":
      return { fill: "fill-red-500 dark:fill-red-400", swatch: "bg-red-500 dark:bg-red-400" };
    default:
      return { fill: "fill-neutral-400 dark:fill-neutral-500", swatch: "bg-neutral-400 dark:bg-neutral-500" };
  }
}

/** Edge kind → stroke colour (through `currentColor`). Plain links are quiet neutrals;
 *  the conversations layer's edges carry meaning, so they are hued. */
export function edgeTone(kind?: string): string {
  switch (kind) {
    case "pinned":
      return "text-violet-500 dark:text-violet-400";
    case "walkthrough":
      return "text-amber-500 dark:text-amber-400";
    case "spawned":
      return "text-neutral-500 dark:text-neutral-400";
    default:
      return "text-neutral-400 dark:text-neutral-500";
  }
}
