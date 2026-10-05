import type { SettingsPayload } from "../../lib/types";
import { useState } from "react";
import { setToken, token } from "../../lib/api";
import { Button, FieldRow, Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

function BrowserToken() {
  const [has, setHas] = useState(() => Boolean(token()));
  return (
    <FieldRow
      label="Token stored in this browser"
      help="The LAN token this browser sends (kept as loom_token in local storage). Clearing it makes the next request ask for it again."
    >
      <Button
        disabled={!has}
        onClick={() => {
          setToken(null);
          setHas(false);
        }}
      >
        {has ? "Clear stored token" : "No token stored"}
      </Button>
    </FieldRow>
  );
}

export function GeneralTab({ settings }: { settings: SettingsPayload }) {
  return (
    <Section title="General" description="Identity, network and appearance.">
      <ScalarFields group="general" schema={settings.schema} values={settings.values} />
      <BrowserToken />
    </Section>
  );
}
