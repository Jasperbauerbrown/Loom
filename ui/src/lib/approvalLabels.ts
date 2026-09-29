// The three allowing answers on an approval card, labelled for the state of the
// "also allow this root" checkbox. Only `allow_always` applies the root (charter
// 2026-09-21); when the box is ticked the labels say so, on both surfaces alike.

export type AllowLabels = {
  allow: string;
  conversation: string;
  always: string;
  /** Tooltip for the two answers that ignore the tick; empty when the box is unticked. */
  withoutRoot: string;
  /** Which button is the primary one: the tick moves it to "Allow always". */
  primary: "allow" | "always";
};

export function allowLabels(rootTicked: boolean): AllowLabels {
  if (!rootTicked) {
    return { allow: "Allow", conversation: "Allow for this conversation", always: "Allow always", withoutRoot: "", primary: "allow" };
  }
  return {
    allow: "Allow, without adding the root",
    conversation: "Allow for this conversation, without adding the root",
    always: "Allow always and add the root",
    withoutRoot: "Runs this call but does not add the root: only “Allow always” does.",
    primary: "always",
  };
}
