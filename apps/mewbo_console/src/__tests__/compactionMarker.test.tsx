/**
 * Render coverage for `CompactionMarkerRow` — the conversation pane's
 * horizon marker for a `context_compacted` boundary (see
 * `utils/timeline.ts`'s `context_compacted` parsing and
 * `ConversationTimeline.tsx`'s `role: "compaction"` branch). Exported as its
 * own component specifically so this can render without mounting the whole
 * `ConversationTimeline` tree, which pulls in `TurnScroller`'s
 * `IntersectionObserver` usage — unpolyfilled in this suite's jsdom setup.
 *
 * The one thing worth locking down here is the wording: this marker must
 * never read as "your earlier messages were deleted" — they weren't, they're
 * still rendered above it. Only what the model receives going forward
 * narrows to a summary plus a recent window.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { CompactionMarkerRow } from "../components/ConversationTimeline";

afterEach(cleanup);

describe("CompactionMarkerRow", () => {
  it("renders the boundary label without implying deletion", () => {
    render(<CompactionMarkerRow compaction={{ mode: "auto" }} />);
    expect(
      screen.getByText("Context compacted — earlier turns summarized, not removed"),
    ).toBeInTheDocument();
    // Neither of these words should appear anywhere in the row's text.
    expect(screen.queryByText(/deleted|removed from|cleared/i)).toBeNull();
  });

  it("surfaces the tooltip clarifying the transcript above is unchanged", () => {
    render(<CompactionMarkerRow compaction={{ mode: "auto" }} />);
    expect(
      screen.getByTitle(
        "Older messages are represented by a summary in the model's next request. The transcript above is unchanged.",
      ),
    ).toBeInTheDocument();
  });

  it("shows tokens-freed detail when the payload carried it", () => {
    render(<CompactionMarkerRow compaction={{ mode: "auto", tokensSaved: 12000 }} />);
    expect(screen.getByText("12,000 tokens freed")).toBeInTheDocument();
  });

  it("omits the detail suffix when no token count is available", () => {
    render(<CompactionMarkerRow compaction={{ mode: "mid_loop" }} />);
    expect(screen.queryByText(/tokens freed/)).toBeNull();
  });
});
