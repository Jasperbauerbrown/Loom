// App shell: nav, routing, theme and the WS lifecycle.

import { useEffect } from "react";
import { deskId, store, useStore } from "./lib/store";
import { WsClient } from "./lib/ws";
import { href, navigate, useRoute } from "./lib/router";
import { cls } from "./lib/format";
import { api } from "./lib/api";
import { Toasts } from "./components/Toasts";
import { TokenDialog } from "./components/TokenDialog";
import { ErrorBoundary } from "./components/ErrorBoundary";
import {
  IconApprovals,
  IconDesk,
  IconGraph,
  IconOverview,
  IconSettings,
  IconSystem,
  IconThreads,
  IconVault,
} from "./components/Icons";
import { Desk } from "./screens/Desk";
import { ThreadView } from "./screens/ThreadView";
import { Threads } from "./screens/Threads";
import { Overview } from "./screens/Overview";
import { Vault } from "./screens/Vault";
import { Graph } from "./screens/Graph";
import { Approvals } from "./screens/Approvals";
import { Settings } from "./screens/Settings";
import { System } from "./screens/System";

const ws = new WsClient(store);

// --- appearance -----------------------------------------------------------------
// Two axes, both settings: `general.theme` (system | light | dark) sets the `dark`
// class; `general.palette` sets `data-palette`, which index.css maps onto the colour
// ramps every component is built from. The last applied pair is remembered per browser
// so a reload paints the right look before the settings have loaded.

interface Appearance {
  theme: string;
  palette: string;
}

const APPEARANCE_KEY = "loom.appearance";
const DEFAULT_APPEARANCE: Appearance = { theme: "system", palette: "default" };

function applyAppearance({ theme, palette }: Appearance) {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme === "dark" || (theme === "system" && prefersDark);
  const root = document.documentElement;
  root.classList.toggle("dark", dark);
  if (palette && palette !== "default") root.dataset.palette = palette;
  else delete root.dataset.palette;
}

function rememberAppearance(appearance: Appearance) {
  try {
    localStorage.setItem(APPEARANCE_KEY, JSON.stringify(appearance));
  } catch {
    /* blocked storage: the next load simply starts from the default look */
  }
}

function recallAppearance(): Appearance | null {
  try {
    const raw = localStorage.getItem(APPEARANCE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<Appearance>;
    if (typeof parsed?.theme !== "string" || typeof parsed?.palette !== "string") return null;
    return { theme: parsed.theme, palette: parsed.palette };
  } catch {
    return null;
  }
}

// Before first paint: the remembered look (or the system default), so a reload never
// flashes the default palette or the wrong theme.
const remembered = typeof window !== "undefined" ? recallAppearance() : null;
if (typeof window !== "undefined") applyAppearance(remembered ?? DEFAULT_APPEARANCE);

function useTheme() {
  const loaded = useStore((s) => s.settings !== null, Object.is);
  const theme = useStore((s) => (s.settings?.values["general.theme"] as string) ?? "system", Object.is);
  const palette = useStore((s) => (s.settings?.values["general.palette"] as string) ?? "default", Object.is);
  useEffect(() => {
    // Until the settings arrive the remembered look stands; then the settings are truth.
    const appearance: Appearance = loaded ? { theme, palette } : (remembered ?? DEFAULT_APPEARANCE);
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => applyAppearance(appearance);
    apply();
    if (loaded) rememberAppearance(appearance);
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [loaded, theme, palette]);
}

function NavLink({
  to,
  icon,
  label,
  active,
  badge,
}: {
  to: string;
  icon: React.ReactNode;
  label: string;
  active: boolean;
  badge?: number;
}) {
  return (
    <a
      href={href(to)}
      className={cls(
        "flex items-center gap-2.5 rounded-md px-2.5 py-1.5 text-[13px] transition-colors",
        active
          ? "bg-neutral-200 font-medium text-neutral-900 dark:bg-neutral-800 dark:text-neutral-100"
          : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800/60",
      )}
    >
      <span className="shrink-0">{icon}</span>
      <span className="flex-1 truncate">{label}</span>
      {badge ? (
        <span className="rounded-full bg-amber-500 px-1.5 text-[10.5px] font-semibold text-white">{badge}</span>
      ) : null}
    </a>
  );
}

function ConversationPicker() {
  const conversations = useStore((s) => Object.values(s.conversations), (a, b) => a.length === b.length);
  const route = useRoute();
  const desk = conversations.find((c) => c.kind === "desk");
  const running = conversations
    .filter((c) => c.kind === "thread" && ["running", "waiting_approval", "waiting_input"].includes(c.status))
    .slice(0, 20);
  const current = route.name === "conversation" ? route.param : desk ? desk.id : "";
  return (
    <select
      aria-label="Conversation"
      value={current}
      onChange={(e) => navigate(`/c/${e.target.value}`)}
      className="w-full min-w-0 rounded-md border border-neutral-300 bg-white px-2 py-1 text-[12px] dark:border-neutral-700 dark:bg-neutral-900"
    >
      {desk ? <option value={desk.id}>Desk</option> : null}
      {running.map((c) => (
        <option key={c.id} value={c.id}>
          {c.title || c.goal || c.id}
        </option>
      ))}
      {current && !desk && !running.some((c) => c.id === current) ? (
        <option value={current}>{current}</option>
      ) : null}
    </select>
  );
}

function ConnectionDot() {
  const connection = useStore((s) => s.connection, Object.is);
  const tone =
    connection === "open" ? "bg-emerald-500" : connection === "connecting" ? "bg-amber-500" : "bg-red-500";
  return (
    <span className="flex items-center gap-1.5 text-[11.5px] text-neutral-500" title={`Connection: ${connection}`}>
      <span className={cls("h-2 w-2 rounded-full", tone)} />
      {connection === "open" ? "live" : connection}
    </span>
  );
}

export default function App() {
  const route = useRoute();
  const connection = useStore((s) => s.connection, Object.is);
  const pending = useStore((s) => Object.values(s.approvals).filter((a) => a.status === "pending").length, Object.is);
  const unseen = useStore((s) => s.unseenCount, Object.is);
  const settingsStale = useStore((s) => s.settingsStale, Object.is);
  const modelsStale = useStore((s) => s.modelsStale, Object.is);
  const desk = useStore(deskId, Object.is);
  useTheme();

  // Boot: first loads, socket. Nothing gates the app — a missing model is an ordinary
  // unconfigured setting, said once on the now board (decided 2026-09-22).
  useEffect(() => {
    void store.loadSettings();
    void store.refreshAll();
    ws.start();
    return () => ws.stop();
  }, []);

  // settings.changed / models.changed → refetch, so every surface agrees.
  useEffect(() => {
    if (settingsStale) void store.loadSettings();
  }, [settingsStale]);
  useEffect(() => {
    if (modelsStale) void store.loadModels();
  }, [modelsStale]);

  const conversationId = route.name === "conversation" ? route.param : desk;

  return (
    <div className="flex h-full">
      <nav className="flex w-52 shrink-0 flex-col gap-1 border-r border-neutral-200 bg-neutral-50 p-2.5 dark:border-neutral-800 dark:bg-neutral-900">
        <div className="mb-2 flex items-center justify-between px-1">
          <a href={href("/")} className="text-[15px] font-semibold tracking-tight">
            Loom
          </a>
          <ConnectionDot />
        </div>
        <NavLink
          to="/"
          icon={<IconDesk />}
          label="Desk"
          active={route.name === "desk" || (route.name === "conversation" && route.param === desk)}
          badge={unseen || undefined}
        />
        <NavLink to="/threads" icon={<IconThreads />} label="Threads" active={route.name === "threads" || (route.name === "conversation" && route.param !== desk)} />
        <NavLink to="/overview" icon={<IconOverview />} label="Overview" active={route.name === "overview"} />
        <NavLink to="/vault" icon={<IconVault />} label="Vault" active={route.name === "vault"} />
        <NavLink to="/graph" icon={<IconGraph />} label="Graph" active={route.name === "graph"} />
        <NavLink
          to="/approvals"
          icon={<IconApprovals />}
          label="Approvals"
          active={route.name === "approvals"}
          badge={pending || undefined}
        />
        <NavLink to="/settings/general" icon={<IconSettings />} label="Settings" active={route.name === "settings"} />
        <NavLink to="/system" icon={<IconSystem />} label="System" active={route.name === "system"} />
        <div className="mt-auto px-1 pt-2">
          <ConversationPicker />
        </div>
      </nav>

      <main className="flex min-w-0 flex-1 flex-col">
        {connection !== "open" ? (
          <div className="bg-amber-100 px-3 py-1 text-center text-[12px] text-amber-900 dark:bg-amber-950 dark:text-amber-200">
            {connection === "connecting" ? "Connecting to Loom…" : "Connection lost — retrying…"}
          </div>
        ) : null}
        {/* Keyed on the route: navigating away from a screen that failed clears it. */}
        <ErrorBoundary key={`${route.name}/${route.param}`} what="This screen">
          <div className="min-h-0 flex-1">
            {route.name === "threads" ? (
              <Threads />
            ) : route.name === "overview" ? (
              <Overview />
            ) : route.name === "vault" ? (
              <Vault path={route.param} />
            ) : route.name === "graph" ? (
              <Graph path={route.param} />
            ) : route.name === "approvals" ? (
              <Approvals />
            ) : route.name === "settings" ? (
              <Settings group={route.param} />
            ) : route.name === "system" ? (
              <System />
            ) : conversationId ? (
              <ConversationRoute id={conversationId} />
            ) : (
              <div className="flex h-full items-center justify-center text-[13px] text-neutral-500">
                No conversation yet.
              </div>
            )}
          </div>
        </ErrorBoundary>
      </main>
      <Toasts />
      <TokenDialog />
    </div>
  );
}

function ConversationRoute({ id }: { id: string }) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  useEffect(() => {
    if (!conversation) void store.loadConversation(id);
  }, [id, conversation]);

  // Opening a conversation is how its own items get answered (03 §11.3 item
  // lifecycle): every unseen, undismissed item whose action points here — an approval,
  // a thread's finish/failure report — is marked seen once the person is looking at it.
  // Re-runs if a new such item arrives while the view stays open.
  const unseenIds = useStore((s) =>
    Object.values(s.items)
      .filter(
        (it) =>
          !it.seen_at &&
          !it.dismissed_at &&
          it.action.kind === "open_conversation" &&
          it.action.conversation_id === id,
      )
      .map((it) => it.id)
      .sort(),
  );
  useEffect(() => {
    for (const itemId of unseenIds) void store.guard(() => api.seenItem(itemId));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unseenIds]);

  if (conversation?.kind === "thread") return <ThreadView id={id} />;
  return <Desk id={id} />;
}
