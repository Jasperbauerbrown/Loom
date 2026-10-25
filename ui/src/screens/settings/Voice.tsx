// Settings › Voice (03 §13.2): the voice group's fields from the schema (the hotkey is a
// key-capture control) and the listener's live status — state, error, and the input
// devices it can see, each one click from being the microphone it uses.

import { useEffect } from "react";
import { api } from "../../lib/api";
import { store, useStore } from "../../lib/store";
import { localTime } from "../../lib/format";
import { formatHotkey } from "../../lib/hotkey";
import { voiceBadge } from "../../lib/voice";
import type { SettingsPayload } from "../../lib/types";
import { Badge, Button, Card, Section } from "../../components/ui";
import { ScalarFields } from "./SettingsField";

export function VoiceTab({ settings }: { settings: SettingsPayload }) {
  // Refetch whenever the settings change: the backend restarts the listener on any
  // voice.* change, and `running`/`pid` come only from this endpoint.
  useEffect(() => {
    void store.loadVoiceStatus();
  }, [settings]);

  return (
    <>
      <Section
        title="Voice"
        description="Dictate from any app with a hotkey, or from the composer's mic button. Clips are transcribed by the transcription role (Settings › Models › Roles)."
      >
        <ScalarFields group="voice" schema={settings.schema} values={settings.values} />
      </Section>
      <ListenerStatus settings={settings} />
    </>
  );
}

function ListenerStatus({ settings }: { settings: SettingsPayload }) {
  const status = useStore((s) => s.voiceStatus, Object.is);
  const badge = voiceBadge(status);
  const chosen = String(settings.values["voice.input_device"] ?? "");
  const useDevice = (name: string) => void store.guard(() => api.patchSettings({ "voice.input_device": name }));

  return (
    <Section
      title="Listener"
      description="The small process that owns the hotkey and the microphone. It reports here on every change."
      actions={
        <Button size="sm" onClick={() => void store.loadVoiceStatus()}>
          Refresh
        </Button>
      }
    >
      <Card className="p-3 text-[12.5px]">
        {status ? (
          <>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5">
              <Badge tone={badge.tone} title={badge.title}>
                {badge.label}
              </Badge>
              {status.hotkey ? (
                <span className="text-neutral-600 dark:text-neutral-400">
                  <span className="font-mono">{formatHotkey(status.hotkey)}</span> · {status.mode}
                </span>
              ) : null}
              {status.device ? <span className="text-neutral-600 dark:text-neutral-400">mic: {status.device}</span> : null}
              {status.running && status.pid ? (
                <span className="text-neutral-500">pid {status.pid}</span>
              ) : null}
              {status.updated_at ? (
                <span className="text-neutral-500">updated {localTime(status.updated_at)}</span>
              ) : null}
            </div>
            {status.error ? (
              <p className="mt-2 text-[12px] text-red-600 dark:text-red-400">{status.error}</p>
            ) : null}
            <div className="mt-3 border-t border-neutral-200 pt-2 dark:border-neutral-800">
              <h3 className="mb-1 text-[12px] font-semibold tracking-wide text-neutral-500 uppercase">Input devices</h3>
              {status.devices.length === 0 ? (
                <p className="text-neutral-500">
                  {status.enabled ? "The listener has not reported any devices." : "Turn voice input on to list devices."}
                </p>
              ) : (
                <ul className="divide-y divide-neutral-100 dark:divide-neutral-800/70">
                  <DeviceRow name="" label="System default" active={chosen === ""} onUse={useDevice} />
                  {status.devices.map((name) => (
                    <DeviceRow key={name} name={name} label={name} active={chosen === name} onUse={useDevice} />
                  ))}
                </ul>
              )}
              {chosen && status.devices.length > 0 && !status.devices.includes(chosen) ? (
                <p className="mt-1 text-[11.5px] text-amber-700 dark:text-amber-400">
                  &ldquo;{chosen}&rdquo; is not among the devices the listener sees; it falls back to the system default.
                </p>
              ) : null}
            </div>
          </>
        ) : (
          <p className="text-neutral-500">No status yet.</p>
        )}
      </Card>
    </Section>
  );
}

function DeviceRow({
  name,
  label,
  active,
  onUse,
}: {
  name: string;
  label: string;
  active: boolean;
  onUse: (name: string) => void;
}) {
  return (
    <li className="flex items-center gap-2 py-1">
      <span className="min-w-0 flex-1 truncate" title={label}>
        {label}
      </span>
      {active ? (
        <Badge tone="sky">in use</Badge>
      ) : (
        <Button size="sm" variant="ghost" onClick={() => onUse(name)}>
          Use
        </Button>
      )}
    </li>
  );
}
