# Loom web UI

React 19 + Vite + TypeScript + Tailwind v4. No state, component, router, icon or
sanitiser library (03 §19): those are ~40–200 line files in `src/lib` and
`src/components`. Built to `../ui-dist/` (committed) and served by the backend at `/`.

## Scripts

```bash
npm --prefix ui install
npm --prefix ui run dev      # :5173, proxies /api and /ws to 127.0.0.1:8500
npm --prefix ui run build    # tsc -b && vite build → ui-dist/
npm --prefix ui run test     # vitest (pure units: store reducers, validate, markdown)
```

## Structure

```
src/lib/       api.ts     typed REST client for every endpoint in 03 §12.1
               store.ts   state + `reduce(state, event)` + `useStore(selector)`
               ws.ts      one socket, auto-reconnect 1s→30s, refetch on reconnect
               router.ts  hash router (#/, #/c/<id>, #/threads, #/vault/<path>, …)
               markdown.ts  marked + wikilink extension + allowlist sanitiser
               validate.ts  client mirror of config.Field.coerce
               types.ts, format.ts
src/components/ ui.tsx (primitives), Conversation, ConversationHeader, NowBoard,
               ArtifactDrawer, Markdown, Toasts, Icons
src/screens/   Setup, Desk, Threads, ThreadView, Vault, Approvals, Settings(+settings/),
               System
```

## How the store works

One immutable `State` tree in `src/lib/store.ts`. REST loads seed it
(`store.loadConversations()`, `loadEvents`, `loadSettings`, …); after that the WebSocket
is the only source of change: `ws.ts` parses each `{type, ts, conversation_id, data}`
envelope and calls `store.dispatch(event)`, which runs the pure `reduce(state, event)` —
one case per WS type in 03 §12.2. Unknown types return the state unchanged, so a newer
backend never breaks an older UI.

Streaming assembles in `state.streaming[conversation_id]` (`message.start` opens a
buffer, `message.delta` appends on the `content` or `reasoning` channel, `message.end`
replaces it with the persisted row). `event.appended` upserts every persisted event by
id, so messages sent from the CLI or another surface appear here immediately;
`tool.call` / `tool.result` pair by `call_id` in `state.toolCalls`. `settings.changed`
and `models.changed` set refetch flags that `App` turns into a reload, which is why the
UI and the backend never disagree.

Components read with `useStore(selector)` (`useSyncExternalStore` plus a shallow-equal
cache) and mutate only through `api.*`, wrapped in `store.guard(...)` so every failure
becomes a toast instead of a blank screen.
