import { hydrateRoot } from "react-dom/client";
import { App } from "./App.tsx";
import { routeFor } from "./site.ts";
import { captureAttribution } from "./analytics.ts";
import "./styles.css";
captureAttribution();
hydrateRoot(
  document.getElementById("root")!,
  <App route={routeFor(location.pathname)} />,
);
