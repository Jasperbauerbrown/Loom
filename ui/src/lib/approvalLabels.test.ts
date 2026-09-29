import { describe, expect, it } from "vitest";
import { allowLabels } from "./approvalLabels";

describe("allowLabels", () => {
  it("keeps the plain labels and the primary Allow when the root box is unticked", () => {
    const l = allowLabels(false);
    expect([l.allow, l.conversation, l.always]).toEqual(["Allow", "Allow for this conversation", "Allow always"]);
    expect(l.primary).toBe("allow");
    expect(l.withoutRoot).toBe("");
  });

  it("says which answer adds the root once the box is ticked", () => {
    const l = allowLabels(true);
    expect(l.always).toMatch(/add the root/);
    expect(l.allow).toMatch(/without adding the root/);
    expect(l.conversation).toMatch(/without adding the root/);
    expect(l.primary).toBe("always");
    expect(l.withoutRoot).toMatch(/only .Allow always. does/);
  });
});
