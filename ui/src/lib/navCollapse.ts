// Sidebar collapse: a per-browser view preference (like the remembered appearance), not
// a setting — a phone and a desktop pointed at the same Loom want different answers.
// Unset → collapsed below NARROW_PX, expanded above.

export const NAV_COLLAPSED_KEY = "loom.navCollapsed";
export const NARROW_PX = 768;

export function recallNavCollapsed(width: number): boolean {
  try {
    const raw = localStorage.getItem(NAV_COLLAPSED_KEY);
    if (raw === "1") return true;
    if (raw === "0") return false;
  } catch {
    /* blocked storage: fall through to the width default */
  }
  return width < NARROW_PX;
}

export function rememberNavCollapsed(collapsed: boolean) {
  try {
    localStorage.setItem(NAV_COLLAPSED_KEY, collapsed ? "1" : "0");
  } catch {
    /* blocked storage: the choice lasts this page load */
  }
}
