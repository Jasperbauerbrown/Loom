import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import "./index.css";

// The outer boundary catches the shell itself (nav, theme, the socket lifecycle); App
// puts a second one around the routed screen, so a screen failure keeps the nav usable.
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary what="Loom">
      <App />
    </ErrorBoundary>
  </StrictMode>,
);
