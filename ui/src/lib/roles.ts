// Model roles in Settings › Models › Roles (03 §5.2, §13.2).

/** Display order and help for the roles this build knows. The rows themselves come from
 *  `GET /model-roles` (it lists every role the backend has), so a role added on the
 *  backend still gets a dropdown here, after these, with no help line. */
export const ROLE_HELP: Record<string, string> = {
  desk: "The always-on conversation.",
  worker: "Threads: long work in parallel.",
  maintenance: "Edits the vault during maintenance passes.",
  utility: "Fast short answers (e.g. approval judgements); unset falls back to the desk.",
  embedding: "Vectors for vault search (an embedding model, e.g. nomic-embed-text).",
  transcription:
    "Speech to text for voice input (hotkey and composer mic). Needs an openai_compatible provider with /audio/transcriptions — a hosted API (OpenAI, Groq, OpenRouter) or a local whisper server. Preset params used: language, prompt (vocabulary hint), temperature, timeout_s. Audio goes to this provider. No fallback: unset means voice input is off.",
};
const ROLE_ORDER = Object.keys(ROLE_HELP);

/** Known roles first in their fixed order, then any the backend adds. */
export function roleRows(roles: { role: string }[]): string[] {
  const listed = roles.map((r) => r.role);
  if (listed.length === 0) return ROLE_ORDER;
  return [...ROLE_ORDER.filter((r) => listed.includes(r)), ...listed.filter((r) => !ROLE_ORDER.includes(r))];
}
