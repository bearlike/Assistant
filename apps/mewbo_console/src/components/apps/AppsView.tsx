/**
 * Top-level Apps route resolver. Reads the current apps sub-route and delegates
 * to the right screen, splitting the Apps section from App.tsx's routing tree
 * so it can evolve in its own namespace (the wiki's `WikiApp` pattern).
 *
 * Default export so App.tsx can lazy-import it.
 */
import { Suspense, lazy, useEffect } from "react";
import { Loader2 } from "lucide-react";

import { AppsLanding } from "./AppsLanding";
import { useAppsRoute } from "./router";

// The detail view statically pulls the stlite render panel's chunk graph, so
// lazy it: the landing (the common first paint) must not download the app
// renderer. `AppsLanding` stays eager — it IS the landing.
const AppDetail = lazy(() => import("./AppDetail").then((m) => ({ default: m.AppDetail })));

export function AppsView() {
  const route = useAppsRoute();

  useEffect(() => {
    document.title = route.kind === "landing" ? "Agentic Apps | Mewbo" : "App | Mewbo";
  }, [route]);

  if (route.kind === "detail") {
    return (
      <Suspense fallback={<AppsFallback />}>
        <AppDetail appId={route.appId} sessionId={route.sessionId} />
      </Suspense>
    );
  }
  return <AppsLanding />;
}

function AppsFallback() {
  return (
    <div className="flex flex-1 items-center justify-center">
      <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--muted-foreground))]" />
    </div>
  );
}
