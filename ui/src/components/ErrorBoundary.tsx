// One defined behaviour for a render error (charter §2.1: failure is an expected input).
// Without this a single bad row from the bus unmounts the whole React tree and the page
// goes white with no way back but a reload — which is exactly what a malformed
// `conversation.updated` did to the desk (2026-09-22). The error is shown where it
// happened, the rest of the app keeps working, and the boundary resets when the route
// changes (App keys it on the route) or when the person retries.

import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button } from "./ui";

interface Props {
  children: ReactNode;
  /** Where the failure happened, for the card's first line: "the desk", "Loom". */
  what?: string;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // The console is the only place with the component stack; keep it whole.
    console.error("render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="flex h-full items-center justify-center p-6">
        <div className="max-w-lg rounded-lg border border-red-300 bg-red-50 p-4 dark:border-red-900 dark:bg-red-950/40">
          <h2 className="text-[14px] font-semibold text-red-900 dark:text-red-200">
            {this.props.what ?? "This screen"} hit an error
          </h2>
          <p className="mt-1 text-[12.5px] text-red-900/80 dark:text-red-200/80">
            Nothing was lost — the backend is the source of truth. Retry, or reload the page.
          </p>
          <pre className="mt-2 max-h-40 overflow-auto rounded border border-red-200 bg-white p-2 font-mono text-[11.5px] whitespace-pre-wrap dark:border-red-900 dark:bg-neutral-950">
            {String(error?.stack || error?.message || error)}
          </pre>
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="primary" onClick={() => this.setState({ error: null })}>
              Retry
            </Button>
            <Button size="sm" onClick={() => location.reload()}>
              Reload
            </Button>
          </div>
        </div>
      </div>
    );
  }
}
