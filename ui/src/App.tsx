// App shell: nav, routing, theme, the WS lifecycle and the setup gate.

import { useEffect, useState } from "react";
import { api } from "./lib/api";
import { deskId, store, useStore } from "./lib/store";
import { WsClient } from "./lib/ws";
import { href, navigate, useRoute } from "./lib/router";
import { cls } from "./lib/format";
import { Toasts } from "./components/Toasts";
import {
  IconApprovals,
  IconDesk,
  IconSettings,
  IconSystem,
  IconThreads,
  IconVault,
} from "./components/Icons";
import { Desk } from "./screens/Desk";
import { ThreadView } from "./screens/ThreadView";
import { Threads } from "./screens/Threads";
import { Vault } from "./screens/Vault";
import { Approvals } from "./screens/Approvals";
import { Settings } from "./screens/Settings";
import { System } from "./screens/System";
import { Setup } from "./screens/Setup";

const ws = new WsClient(store);

function useTheme() {
  const theme = useStore((s) => (s.settings?.values["general.theme"] as string) ?? "system", Object.is);
  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const dark = theme === "dark" || (theme === "system" && media.matches);
      document.documentElement.classList.toggle("dark", dark);
    };
    apply();
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [theme]);
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
      className="max-w-56 rounded-md border border-neutral-300 bg-white px-2 py-1 text-[12px] dark:border-neutral-700 dark:bg-neutral-900"
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
  const [setupNeeded, setSetupNeeded] = useState<boolean | null>(null);
  const connection = useStore((s) => s.connection, Object.is);
  const pending = useStore((s) => Object.values(s.approvals).filter((a) => a.status === "pending").length, Object.is);
  const unseen = useStore((s) => s.unseenCount, Object.is);
  const settingsStale = useStore((s) => s.settingsStale, Object.is);
  const modelsStale = useStore((s) => s.modelsStale, Object.is);
  const desk = useStore(deskId, Object.is);
  useTheme();

  // Boot: setup gate, first loads, socket.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const state = await api.setupState();
        if (!cancelled) setSetupNeeded(Boolean(state?.needed));
      } catch {
        if (!cancelled) setSetupNeeded(false); // backend older/unreachable: show the app
      }
    })();
    void store.loadSettings();
    void store.refreshAll();
    ws.start();
    return () => {
      cancelled = true;
      ws.stop();
    };
  }, []);

  // settings.changed / models.changed → refetch, so every surface agrees.
  useEffect(() => {
    if (settingsStale) void store.loadSettings();
  }, [settingsStale]);
  useEffect(() => {
    if (modelsStale) void store.loadModels();
  }, [modelsStale]);

  // The setup screen owns the app until it is finished.
  if (setupNeeded === null) {
    return <div className="flex h-full items-center justify-center text-[13px] text-neutral-500">Loading…</div>;
  }
  if (setupNeeded || route.name === "setup") {
    return (
      <>
        <Setup
          onDone={() => {
            setSetupNeeded(false);
            void store.refreshAll();
            navigate("/");
          }}
        />
        <Toasts />
      </>
    );
  }

  const conversationId = route.name === "conversation" ? route.param : desk;

  return (
    <div className="flex h-full">
      <nav className="flex w-52 shrink-0 flex-col gap-1 border-r border-neutral-200 bg-neutral-50 p-2.5 dark:border-neutral-800 dark:bg-neutral-900/60">
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
        <NavLink to="/vault" icon={<IconVault />} label="Vault" active={route.name === "vault"} />
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
        <div className="min-h-0 flex-1">
          {route.name === "threads" ? (
            <Threads />
          ) : route.name === "vault" ? (
            <Vault path={route.param} />
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
      </main>
      <Toasts />
    </div>
  );
}

function ConversationRoute({ id }: { id: string }) {
  const conversation = useStore((s) => s.conversations[id], Object.is);
  useEffect(() => {
    if (!conversation) void store.loadConversation(id);
  }, [id, conversation]);
  if (conversation?.kind === "thread") return <ThreadView id={id} />;
  return <Desk id={id} />;
}
