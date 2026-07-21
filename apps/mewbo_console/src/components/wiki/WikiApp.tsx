/**
 * Top-level wiki route resolver. Reads the current location, parses the
 * wiki sub-route, and delegates to the right screen. Splits the wiki from
 * the rest of the App.tsx routing tree so the section can evolve in its
 * own namespace.
 *
 * Named export, lazy-imported by App.tsx via the `.then` default-unwrap
 * (the console standardized on named-only exports).
 */

import { useEffect } from "react";

import { ConfigureWizard } from "./ConfigureWizard";
import { IndexingScreen } from "./IndexingScreen";
import { KnowledgeGraph3DScreen } from "./KnowledgeGraph3DScreen";
import { LandingScreen } from "./LandingScreen";
import { QAScreen } from "./QAScreen";
import { WelcomeScreen } from "./WelcomeScreen";
import { WikiScreen } from "./WikiScreen";
import { useWikiRoute } from "./router";
import { DEFAULT_WIKI_SLUG } from "./slug";

export function WikiApp() {
  const route = useWikiRoute();

  // Wiki section likes its own document title scheme.
  useEffect(() => {
    switch (route.kind) {
      case "landing":
        document.title = "Agentic Wiki | Mewbo";
        break;
      case "configure":
        document.title = "Configure Wiki | Mewbo";
        break;
      case "welcome":
        document.title = `${route.slug ?? "Wiki"} | Mewbo`;
        break;
      case "indexing":
        document.title = "Indexing | Mewbo";
        break;
      case "page":
        document.title = `${route.pageId} | Mewbo`;
        break;
      case "qa":
        document.title = "Q&A | Mewbo";
        break;
      case "graph":
        document.title = `Graph${route.slug ? ` · ${route.slug}` : ""} | Mewbo`;
        break;
    }
  }, [route]);

  switch (route.kind) {
    case "landing":
      return <LandingScreen />;
    case "configure":
      return <ConfigureWizard initialUrl={route.url} />;
    case "welcome":
      return (
        <WelcomeScreen
          slug={route.slug ?? DEFAULT_WIKI_SLUG}
          platform={route.platform}
        />
      );
    case "indexing":
      return (
        <IndexingScreen
          jobId={route.jobId}
          slug={route.slug}
          platform={route.platform}
        />
      );
    case "page":
      return (
        <WikiScreen pageId={route.pageId} slug={route.slug} platform={route.platform} />
      );
    case "qa":
      return (
        <QAScreen
          question={route.question}
          pageId={route.pageId}
          slug={route.slug}
          model={route.model}
          answerId={route.answer}
        />
      );
    case "graph":
      return (
        <KnowledgeGraph3DScreen slug={route.slug} platform={route.platform} />
      );
  }
}
