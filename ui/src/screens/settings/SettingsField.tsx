// Renders one scalar settings field from the schema, and saves it on blur/Enter via
// api.patchSettings. Never a hand-written form field: every scalar in the product goes
// through this component (charter: every setting is UI-visible).

import { useEffect, useState } from "react";
import { api } from "../../lib/api";
import { store } from "../../lib/store";
import { fieldValue, toInputValue, validate } from "../../lib/validate";
import type { SettingsField as SettingsFieldSchema } from "../../lib/types";
import { FieldRow, Input, Select, Textarea, Toggle } from "../../components/ui";
import { controlKind } from "./controls";

export function SettingsFieldControl({
  field,
  values,
}: {
  field: SettingsFieldSchema;
  values: Record<string, unknown>;
}) {
  const stored = fieldValue(field, values);
  const kind = controlKind(field);
  const [text, setText] = useState(() => toInputValue(field, stored));
  const [boolValue, setBoolValue] = useState(() => Boolean(stored));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [hasSecret, setHasSecret] = useState(() => kind === "secret" && Boolean(stored));

  useEffect(() => {
    setText(toInputValue(field, stored));
    setBoolValue(Boolean(stored));
    setHasSecret(kind === "secret" && Boolean(stored));
    setError(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [field.key, JSON.stringify(stored)]);

  async function commit(raw: unknown) {
    const result = validate(field, raw);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError(null);
    setSaving(true);
    const res = await store.guard(() => api.patchSettings({ [field.key]: result.value }));
    setSaving(false);
    if (res === undefined) {
      // revert to the last known-good stored value
      setText(toInputValue(field, stored));
      setBoolValue(Boolean(stored));
      return;
    }
    if (field.key in res.values) {
      const next = res.values[field.key];
      setText(toInputValue(field, next));
      setBoolValue(Boolean(next));
      if (kind === "secret") setHasSecret(Boolean(next));
    }
  }

  async function commitBool(next: boolean) {
    setBoolValue(next);
    const result = validate(field, next);
    if (!result.ok) {
      setError(result.error);
      setBoolValue(!next);
      return;
    }
    setError(null);
    const res = await store.guard(() => api.patchSettings({ [field.key]: result.value }));
    if (res === undefined) setBoolValue(!next);
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      (e.target as HTMLElement).blur();
    }
  };

  let control: React.ReactNode;
  switch (kind) {
    case "toggle":
      control = <Toggle checked={boolValue} onChange={commitBool} disabled={saving} label={field.label} />;
      break;
    case "select":
      control = (
        <Select
          value={text}
          disabled={saving}
          onChange={(e) => {
            setText(e.target.value);
            void commit(e.target.value);
          }}
        >
          {field.choices.map((c) => (
            <option key={c} value={c}>
              {c === "" ? "(none)" : c}
            </option>
          ))}
        </Select>
      );
      break;
    case "list":
      control = (
        <Textarea
          rows={4}
          value={text}
          disabled={saving}
          placeholder="one value per line"
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void commit(text)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void commit(text);
          }}
        />
      );
      break;
    case "path":
      control = (
        <Input
          className="font-mono"
          value={text}
          disabled={saving}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void commit(text)}
          onKeyDown={onKeyDown}
        />
      );
      break;
    case "time":
      control = (
        <Input
          type="time"
          value={text}
          disabled={saving}
          onChange={(e) => {
            setText(e.target.value);
            void commit(e.target.value);
          }}
        />
      );
      break;
    case "number":
      control = (
        <Input
          type="number"
          value={text}
          min={field.min ?? undefined}
          max={field.max ?? undefined}
          step={field.type === "float" ? "any" : 1}
          disabled={saving}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void commit(text)}
          onKeyDown={onKeyDown}
        />
      );
      break;
    case "secret":
      control = (
        <Input
          type="password"
          value={text}
          placeholder={hasSecret ? "set (enter a new value to replace)" : "not set"}
          disabled={saving}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => {
            if (text) void commit(text);
          }}
          onKeyDown={onKeyDown}
        />
      );
      break;
    default:
      control = (
        <Input
          value={text}
          disabled={saving}
          onChange={(e) => setText(e.target.value)}
          onBlur={() => void commit(text)}
          onKeyDown={onKeyDown}
        />
      );
  }

  return (
    <FieldRow
      label={field.label}
      help={field.help || undefined}
      error={error}
      hint={field.restart ? "restart required" : undefined}
    >
      {control}
    </FieldRow>
  );
}

/** Renders every scalar field belonging to `group`, in schema order. */
export function ScalarFields({
  group,
  schema,
  values,
}: {
  group: string;
  schema: SettingsFieldSchema[];
  values: Record<string, unknown>;
}) {
  const fields = schema.filter((f) => f.group === group);
  if (fields.length === 0) return null;
  return (
    <div className="divide-y divide-neutral-100 dark:divide-neutral-800/70">
      {fields.map((f) => (
        <SettingsFieldControl key={f.key} field={f} values={values} />
      ))}
    </div>
  );
}
