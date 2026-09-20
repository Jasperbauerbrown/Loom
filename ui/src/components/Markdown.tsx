// Markdown block. Sanitised HTML (lib/markdown.ts) set as innerHTML; wikilinks are
// ordinary hash links into the vault screen so no click handling is needed.

import { useMemo } from "react";
import { renderInline, renderMarkdown } from "../lib/markdown";
import { cls } from "../lib/format";

export function Markdown({ source, className, inline }: { source: string; className?: string; inline?: boolean }) {
  const html = useMemo(() => (inline ? renderInline(source) : renderMarkdown(source)), [source, inline]);
  return <div className={cls("md", className)} dangerouslySetInnerHTML={{ __html: html }} />;
}
