// First-run setup (03 §3.3): one required choice (the model), everything else
// pre-filled and confirmable. Shown only while `GET /api/system/setup` says `needed`.

import { useEffect, useState } from "react";
import { api, ApiError } from "../lib/api";
import type { SetupState } from "../lib/types";
import { Button, Card, Collapsible, FieldRow, Input, Select, Spinner } from "../components/ui";

interface ProviderInfo {
  name: string;
  kind: string;
  base_url: string;
  models?: string[];
}

function detectedTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function modelsFrom(res: string[] | { models: string[] } | undefined | null): string[] {
  if (!res) return [];
  if (Array.isArray(res)) return res;
  if (Array.isArray(res.models)) return res.models;
  return [];
}

export function Setup({ onDone }: { onDone: () => void }) {
  const [loading, setLoading] = useState(true);
  const [state, setState] = useState<SetupState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const [providerName, setProviderName] = useState("ollama");
  const [showOpenRouter, setShowOpenRouter] = useState(false);
  const [orBaseUrl, setOrBaseUrl] = useState("https://openrouter.ai/api/v1");
  const [orApiKey, setOrApiKey] = useState("");

  const [modelList, setModelList] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [model, setModel] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState<string | null>(null);

  const [timezone, setTimezone] = useState(detectedTimezone());
  const [projectsRoot, setProjectsRoot] = useState("");

  // -- initial load -----------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await api.setupState();
        if (cancelled) return;
        setState(s);
        const providers: ProviderInfo[] = Array.isArray(s?.providers) ? s.providers : [];
        const ollama = providers.find((p) => p.kind === "ollama") ?? providers[0];
        if (ollama) setProviderName(ollama.name);
        if (s?.timezone) setTimezone(s.timezone);
        if (s?.projects_root) setProjectsRoot(s.projects_root);
        const initialModels = modelsFrom(ollama?.models ?? s?.models);
        if (initialModels.length) {
          setModelList(initialModels);
          const embed = initialModels.find((m) => m.toLowerCase().includes("nomic-embed-text"));
          if (embed) setEmbeddingModel(embed);
          if (!model && initialModels.length === 1) setModel(initialModels[0]);
        }
      } catch (e) {
        if (!cancelled) setError((e as Error).message ?? "Could not load setup state.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // -- refresh models when provider changes ------------------------------------
  async function refreshModels(provider: string) {
    setModelsLoading(true);
    try {
      const res = await api.models(provider || undefined);
      const list = modelsFrom(res);
      setModelList(list);
      const embed = list.find((m) => m.toLowerCase().includes("nomic-embed-text"));
      setEmbeddingModel(embed ?? null);
      if (model && !list.includes(model)) setModel("");
    } catch {
      setModelList([]);
    } finally {
      setModelsLoading(false);
    }
  }

  useEffect(() => {
    if (!loading) void refreshModels(providerName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [providerName, loading]);

  async function finish() {
    if (!model) return;
    setSubmitting(true);
    setError(null);
    try {
      await api.finishSetup({
        provider: providerName,
        model,
        embedding_model: embeddingModel ?? undefined,
        timezone,
        projects_root: projectsRoot,
        ...(showOpenRouter && orApiKey
          ? { openrouter: { base_url: orBaseUrl, api_key: orApiKey } }
          : {}),
      });
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Setup failed. Check the details above and try again.");
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-[13px] text-neutral-500">
        <Spinner className="mr-2" /> Loading setup…
      </div>
    );
  }

  const providers: ProviderInfo[] = Array.isArray(state?.providers) ? (state!.providers as ProviderInfo[]) : [];

  return (
    <div className="flex h-full items-start justify-center overflow-y-auto p-8">
      <Card className="w-full max-w-lg p-5">
        <h1 className="mb-1 text-[17px] font-semibold text-neutral-900 dark:text-neutral-100">
          Welcome to Loom
        </h1>
        <p className="mb-4 text-[12.5px] text-neutral-500">
          A minute of setup, then the desk. Everything here can be changed later in Settings.
        </p>

        {error ? (
          <div className="mb-3 rounded-md border border-red-300 bg-red-50 px-2.5 py-1.5 text-[12.5px] text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-300">
            {error}
          </div>
        ) : null}

        <FieldRow label="Provider" help="Local Ollama is detected automatically.">
          <Select value={providerName} onChange={(e) => setProviderName(e.target.value)}>
            {providers.length === 0 ? <option value="ollama">ollama (default)</option> : null}
            {providers.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name} ({p.kind}) — {p.base_url}
              </option>
            ))}
          </Select>
        </FieldRow>

        <Collapsible
          summary={showOpenRouter ? "OpenRouter key configured" : "Add OpenRouter key"}
          defaultOpen={showOpenRouter}
        >
          <div
            onFocus={() => setShowOpenRouter(true)}
            className="space-y-2 rounded-md border border-neutral-200 p-2.5 dark:border-neutral-800"
          >
            <FieldRow label="Base URL">
              <Input
                value={orBaseUrl}
                onChange={(e) => {
                  setOrBaseUrl(e.target.value);
                  setShowOpenRouter(true);
                }}
                placeholder="https://openrouter.ai/api/v1"
              />
            </FieldRow>
            <FieldRow label="API key">
              <Input
                type="password"
                value={orApiKey}
                onChange={(e) => {
                  setOrApiKey(e.target.value);
                  setShowOpenRouter(true);
                }}
                placeholder="sk-or-…"
              />
            </FieldRow>
          </div>
        </Collapsible>

        <FieldRow label="Model" help="The one required choice." hint="required">
          <div className="flex items-center gap-2">
            <Select value={model} onChange={(e) => setModel(e.target.value)} disabled={modelsLoading}>
              <option value="">{modelsLoading ? "Loading models…" : "Select a model…"}</option>
              {modelList.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
            {modelsLoading ? <Spinner /> : null}
          </div>
        </FieldRow>

        <FieldRow label="Embedding model">
          {embeddingModel ? (
            <p className="text-[12.5px] text-neutral-700 dark:text-neutral-300">
              Using <span className="font-mono">{embeddingModel}</span>
            </p>
          ) : (
            <div className="space-y-1.5">
              <p className="text-[12.5px] text-neutral-500">
                No <span className="font-mono">nomic-embed-text</span> model found. Embeddings stay off
                until it appears. Run:
              </p>
              <pre className="select-all overflow-x-auto rounded-md border border-neutral-200 bg-neutral-50 p-2 font-mono text-[12px] dark:border-neutral-800 dark:bg-neutral-950">
                ollama pull nomic-embed-text
              </pre>
            </div>
          )}
        </FieldRow>

        <FieldRow label="Timezone">
          <Input value={timezone} onChange={(e) => setTimezone(e.target.value)} />
        </FieldRow>

        <FieldRow label="Projects root" help="Where the assistant looks for project folders.">
          <Input
            value={projectsRoot}
            onChange={(e) => setProjectsRoot(e.target.value)}
            placeholder="~/Projects"
          />
        </FieldRow>

        <div className="mt-4 flex justify-end">
          <Button variant="primary" disabled={!model || submitting} onClick={() => void finish()}>
            {submitting ? <Spinner /> : null}
            Finish
          </Button>
        </div>
      </Card>
    </div>
  );
}
