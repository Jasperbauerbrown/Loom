// The LAN token prompt. `api.ts` raises it on any 401 (one shared prompt however many
// calls failed); answering stores the token as `loom_token` and the failed calls retry.

import { useEffect, useState } from "react";
import { answerTokenPrompt, onTokenPrompt, tokenPromptOpen } from "../lib/api";
import { Button, Dialog, Input } from "./ui";

export function TokenDialog() {
  const [open, setOpen] = useState(tokenPromptOpen());
  const [text, setText] = useState("");
  useEffect(() => onTokenPrompt(setOpen), []);
  if (!open) return null;
  const submit = () => {
    const value = text.trim();
    if (!value) return;
    setText("");
    answerTokenPrompt(value);
  };
  const cancel = () => {
    setText("");
    answerTokenPrompt(null);
  };
  return (
    <Dialog
      title="Access token required"
      onClose={cancel}
      footer={
        <>
          <Button onClick={cancel}>Cancel</Button>
          <Button variant="primary" onClick={submit} disabled={!text.trim()}>
            Save and retry
          </Button>
        </>
      }
    >
      <p className="mb-3 text-[13px] leading-5 text-neutral-700 dark:text-neutral-300">
        This Loom asks for its LAN token (Settings, General, <code>general.lan_token</code> on the host). It is stored
        in this browser only.
      </p>
      <Input
        type="password"
        autoFocus
        aria-label="LAN token"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        className="w-full"
      />
    </Dialog>
  );
}
