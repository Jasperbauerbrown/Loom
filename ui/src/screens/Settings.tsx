// Settings screen: a tab shell over the settings schema (03 §13.2). Each tab renders
// its group's scalar fields plus whatever row-shaped tables belong to it.

import { useEffect } from "react";
import { store, useStore } from "../lib/store";
import { navigate } from "../lib/router";
import { cls } from "../lib/format";
import { Empty, Section, Spinner } from "../components/ui";
import type { SettingsPayload } from "../lib/types";
import { GeneralTab } from "./settings/General";
import { ModelsTab } from "./settings/Models";
import { ConversationTab } from "./settings/Conversation";
import { KnowledgeTab } from "./settings/Knowledge";
import { PermissionsTab } from "./settings/Permissions";
import { IntegrationsTab } from "./settings/Integrations";
import { TriggersTab } from "./settings/Triggers";
import { SystemTab } from "./settings/System";
import { ScalarFields } from "./settings/SettingsField";

// Server-declared scalar groups (03 §13.2 / config.py GROUPS), in display order, with
// "integrations" inserted — it has no scalar fields but owns MCP servers, connectors and
// secrets, and is a first-class Settings tab per the brief.
const TAB_ORDER = [
  "general",
  "models",
  "conversation",
  "knowledge",
  "permissions",
  "tools",
  "integrations",
  "triggers",
  "system",
];

const INTEGRATIONS_LABEL = "Integrations";

export function Settings({ group: param }: { group: string }) {
  // `#/settings/models/presets`: the first segment picks the tab, the rest is the tab's
  // own sub-tab, so a sub-tab is a link (and a screenshot target), not only a click.
  const [group, sub = ""] = param.split("/", 2);
  const settings = useStore((s) => s.settings);
  const settingsStale = useStore((s) => s.settingsStale);
  const providers = useStore((s) => s.providers);
  const roots = useStore((s) => s.roots);
  const mcpServers = useStore((s) => s.mcpServers);
  const schedules = useStore((s) => s.schedules);

  useEffect(() => {
    if (!settings) void store.loadSettings();
  }, [settings]);

  useEffect(() => {
    if (settingsStale) void store.loadSettings();
  }, [settingsStale]);

  // Load the row-shaped data each tab needs, once, lazily per tab.
  useEffect(() => {
    if (group === "models" && providers.length === 0) void store.loadModels();
    if (group === "permissions" && roots.length === 0) void store.loadPermissions();
    if (group === "integrations" && mcpServers.length === 0) void store.loadIntegrations();
    if (group === "triggers" && schedules.length === 0) void store.loadSchedules();
  }, [group]);

  if (!settings) {
    return (
      <div className="flex items-center gap-2 p-6 text-[13px] text-neutral-500">
        <Spinner /> Loading settings…
      </div>
    );
  }

  // Known tabs first, then any group the backend declares that this build does not know
  // about — no setting may exist without a UI (charter §2.2).
  const tabs = [
    ...TAB_ORDER.filter((key) => key === "integrations" || key in settings.groups),
    ...Object.keys(settings.groups).filter((key) => !TAB_ORDER.includes(key)),
  ];
  const activeGroup = tabs.includes(group) ? group : tabs[0];

  return (
    <div className="flex h-full flex-col md:flex-row">
      <nav className="flex shrink-0 gap-1 overflow-x-auto border-b border-neutral-200 p-2 md:w-44 md:flex-col md:overflow-visible md:border-b-0 md:border-r md:p-3 dark:border-neutral-800">
        {tabs.map((key) => (
          <button
            key={key}
            onClick={() => navigate(`/settings/${key}`)}
            className={cls(
              "whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[13px] transition-colors",
              activeGroup === key
                ? "bg-sky-100 font-medium text-sky-800 dark:bg-sky-950 dark:text-sky-300"
                : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800",
            )}
          >
            {key === "integrations" ? INTEGRATIONS_LABEL : settings.groups[key]}
          </button>
        ))}
      </nav>
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        <SettingsTabBody group={activeGroup} sub={sub} settings={settings} />
      </div>
    </div>
  );
}

function SettingsTabBody({ group, sub, settings }: { group: string; sub: string; settings: SettingsPayload }) {
  switch (group) {
    case "general":
      return <GeneralTab settings={settings} />;
    case "models":
      return <ModelsTab settings={settings} sub={sub} />;
    case "conversation":
      return <ConversationTab settings={settings} />;
    case "knowledge":
      return <KnowledgeTab settings={settings} />;
    case "permissions":
      return <PermissionsTab settings={settings} />;
    case "integrations":
      return <IntegrationsTab />;
    case "triggers":
      return <TriggersTab settings={settings} />;
    case "system":
      return <SystemTab settings={settings} />;
    default: {
      // A group this build has no tab for still renders its fields from the schema.
      const fields = settings.schema.filter((f) => f.group === group);
      if (fields.length === 0) return <Empty>Nothing to configure here.</Empty>;
      return (
        <Section title={settings.groups[group] ?? group}>
          <ScalarFields group={group} schema={settings.schema} values={settings.values} />
        </Section>
      );
    }
  }
}
