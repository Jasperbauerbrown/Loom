import type { SettingsPayload } from "../../lib/types";
import { Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

export function KnowledgeTab({ settings }: { settings: SettingsPayload }) {
  return (
    <Section title="Knowledge" description="Vault retrieval, preload and maintenance.">
      <ScalarFields group="knowledge" schema={settings.schema} values={settings.values} />
    </Section>
  );
}
