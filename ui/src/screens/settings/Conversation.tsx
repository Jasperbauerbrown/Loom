import type { SettingsPayload } from "../../lib/types";
import { Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

export function ConversationTab({ settings }: { settings: SettingsPayload }) {
  return (
    <Section title="Conversation" description="Turn budgets, compaction, timeouts and artifacts.">
      <ScalarFields group="conversation" schema={settings.schema} values={settings.values} />
    </Section>
  );
}
