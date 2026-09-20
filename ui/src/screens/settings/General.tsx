import type { SettingsPayload } from "../../lib/types";
import { Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

export function GeneralTab({ settings }: { settings: SettingsPayload }) {
  return (
    <Section title="General" description="Identity, network and appearance.">
      <ScalarFields group="general" schema={settings.schema} values={settings.values} />
    </Section>
  );
}
