import type { SettingsPayload } from "../../lib/types";
import { Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

export function SystemTab({ settings }: { settings: SettingsPayload }) {
  return (
    <Section title="System" description="Logging and release safety. See the System screen for health, logs and promotion.">
      <ScalarFields group="system" schema={settings.schema} values={settings.values} />
    </Section>
  );
}
