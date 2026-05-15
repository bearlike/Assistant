/**
 * Persisted Q&A model selection. Shared between the wiki page's dock and
 * the QA page's dock so the picker is sticky across navigations and reloads.
 *
 * Seeding priority (first to resolve wins):
 *   1. localStorage (user's prior explicit choice).
 *   2. ``wiki.default_qa_model`` from /v1/wiki/defaults — Q&A-specific
 *      pin, typically a smaller/faster model than indexing.
 *   3. ``wiki.default_model`` from /v1/wiki/defaults — wiki-wide pin.
 *   4. ``llm.default_model`` exposed via /api/models — global fallback.
 */

import { useEffect, useState } from "react";

import { useModels } from "../../hooks/useModels";
import { useWikiDefaults } from "./api/hooks";

const STORAGE_KEY = "wiki:qa-model";

export function useStoredModel(): [string, (next: string) => void] {
  const { defaultModel } = useModels();
  const wikiDefaults = useWikiDefaults();
  const seed =
    wikiDefaults.data?.qaModel ||
    wikiDefaults.data?.model ||
    defaultModel;
  const [model, setModelState] = useState<string>(() => {
    try {
      const saved = window.localStorage.getItem(STORAGE_KEY);
      if (saved) return saved;
    } catch {
      // ignore
    }
    return "";
  });

  // Seed from the resolved default the first time it arrives.
  useEffect(() => {
    if (!model && seed) setModelState(seed);
  }, [seed, model]);

  useEffect(() => {
    if (!model) return;
    try {
      window.localStorage.setItem(STORAGE_KEY, model);
    } catch {
      // ignore
    }
  }, [model]);

  return [model, setModelState];
}
