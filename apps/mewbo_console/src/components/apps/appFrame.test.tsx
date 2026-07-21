/**
 * `AppFrame` — the iframe render seam for Mewbo Apps.
 *
 * What these lock down is the handshake, because its failure mode is silent:
 * the widget host drops any payload that arrives before it has attached its own
 * `message` listener, so a mis-ordered post produces a frame that simply never
 * renders, with no error on either side.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { act, cleanup, render } from "@testing-library/react";

import { AppFrame } from "./AppFrame";
import type { AppContext, AppFrontend } from "../../types/apps";

/**
 * The host page path differs between dev and prod (a separate relative-base
 * build emits `dist/widget-host/`, while the dev server serves root HTML files
 * by name). Vitest runs with `import.meta.env.DEV === true`, so this is the
 * branch under test here; the production path is exercised by the build.
 */
const DEV_WIDGET_HOST_URL = "/widget-host.html";

const frontend: AppFrontend = {
  entrypoint: "home.py",
  files: { "home.py": "import streamlit as st", "pages/one.py": "x = 1" },
  requirements: ["pandas"],
};

const appContext: AppContext = {
  token: "tok-1",
  api_base: "https://mewbo.test",
  app_id: "app-1",
  scope: "read",
};

function getFrame(): HTMLIFrameElement {
  const frame = document.querySelector("iframe");
  if (!frame) throw new Error("no iframe rendered");
  return frame as HTMLIFrameElement;
}

/** Spy on the frame's own `postMessage` — that is the channel under test. */
function spyOnFramePost(frame: HTMLIFrameElement) {
  const target = frame.contentWindow;
  if (!target) throw new Error("iframe has no contentWindow");
  return vi.spyOn(target, "postMessage").mockImplementation(() => undefined);
}

/** Simulate the host's boot signal, as it really arrives: from the frame. */
function postReady(frame: HTMLIFrameElement, source?: Window | null) {
  act(() => {
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "mewbo-widget-host-ready" },
        origin: window.location.origin,
        source: (source === undefined ? frame.contentWindow : source) as Window,
      }),
    );
  });
}

beforeEach(() => {
  document.documentElement.classList.remove("light");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AppFrame handshake", () => {
  it("assigns src imperatively after mount, never as a JSX prop", () => {
    // The ready-before-listener race is closed by assigning `src` only once the
    // listener is live. A JSX `src` would start the load during React's commit,
    // i.e. before effects run. Asserting the resolved URL lands on the element
    // is what proves the imperative path ran.
    const { container } = render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = container.querySelector("iframe") as HTMLIFrameElement;
    expect(frame.getAttribute("src")).toBe(DEV_WIDGET_HOST_URL);
  });

  it("posts NOTHING before the host signals ready", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const post = spyOnFramePost(getFrame());
    expect(post).not.toHaveBeenCalled();
  });

  it("posts the payload once the host signals ready", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);

    postReady(frame);

    expect(post).toHaveBeenCalledTimes(1);
    expect(post.mock.calls[0][1]).toBe(window.location.origin);
  });

  it("still posts when ready arrives immediately after mount (the fast-host case)", () => {
    // A cached host can boot and signal in the very next task. The listener is
    // attached in a mount effect, so this must already be covered — if it ever
    // regresses to a timer-driven post, this is the case that breaks first.
    const { container } = render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = container.querySelector("iframe") as HTMLIFrameElement;
    const post = spyOnFramePost(frame);
    postReady(frame);
    expect(post).toHaveBeenCalledTimes(1);
  });

  it("ignores a ready signal from a window that is not this frame", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);

    postReady(frame, window); // a sibling frame / the parent, not ours
    expect(post).not.toHaveBeenCalled();
  });

  it("ignores a ready signal from a foreign origin", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);

    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: { type: "mewbo-widget-host-ready" },
          origin: "https://evil.test",
          source: frame.contentWindow as Window,
        }),
      );
    });
    expect(post).not.toHaveBeenCalled();
  });
});

describe("AppFrame payload", () => {
  it("posts the frozen cross-stream wire shape", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);
    postReady(frame);

    // Aura mirrors these field names exactly — a rename here is a
    // cross-surface break, so assert the whole object, not just presence.
    expect(post.mock.calls[0][0]).toEqual({
      type: "mewbo-app-payload",
      payload: {
        entrypoint: "home.py",
        files: frontend.files,
        requirements: frontend.requirements,
        app_context: appContext,
      },
      theme: "dark",
    });
  });

  it("carries the console's light theme when the console is in light mode", () => {
    document.documentElement.classList.add("light");
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);
    postReady(frame);

    expect((post.mock.calls[0][0] as { theme: string }).theme).toBe("light");
  });
});

describe("AppFrame re-post and remount", () => {
  it("re-posts the payload on a console theme flip", async () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);
    postReady(frame);
    expect(post).toHaveBeenCalledTimes(1);

    // Re-posting is the ONLY theme update path: kernel options are init-only,
    // so the host tears down and remounts the kernel on a re-post.
    await act(async () => {
      document.documentElement.classList.add("light");
      await Promise.resolve();
    });

    expect(post).toHaveBeenCalledTimes(2);
    expect((post.mock.calls[1][0] as { theme: string }).theme).toBe("light");
  });

  it("does NOT re-post when the parent re-renders with an equal-but-new appContext", () => {
    // The caller builds `appContext` inline, so a new object identity arrives
    // on every parent render. Depending on it would reboot the Pyodide kernel
    // each time — an expensive, user-visible flash with no cause.
    const { rerender } = render(<AppFrame frontend={frontend} appContext={appContext} />);
    const frame = getFrame();
    const post = spyOnFramePost(frame);
    postReady(frame);
    expect(post).toHaveBeenCalledTimes(1);

    rerender(<AppFrame frontend={frontend} appContext={{ ...appContext }} />);
    rerender(<AppFrame frontend={frontend} appContext={{ ...appContext }} />);

    expect(post).toHaveBeenCalledTimes(1);
  });

  it("remounts on a token change and posts the NEW token", () => {
    // `AppDetail` keys this component on `token_id`, so a fresh token or a
    // version rollback arrives as a remount rather than an in-place update.
    const { rerender } = render(
      <AppFrame key="tok-1" frontend={frontend} appContext={appContext} />,
    );
    const firstPost = spyOnFramePost(getFrame());
    postReady(getFrame());
    expect(firstPost).toHaveBeenCalledTimes(1);

    const next: AppContext = { ...appContext, token: "tok-2" };
    rerender(<AppFrame key="tok-2" frontend={frontend} appContext={next} />);

    const frame = getFrame();
    const secondPost = spyOnFramePost(frame);
    postReady(frame);

    expect(secondPost).toHaveBeenCalledTimes(1);
    const sent = secondPost.mock.calls[0][0] as { payload: { app_context: AppContext } };
    expect(sent.payload.app_context.token).toBe("tok-2");
  });
});

describe("AppFrame sandbox", () => {
  it("is same-origin enough to boot Pyodide and reach the API, but cannot retarget the console", () => {
    render(<AppFrame frontend={frontend} appContext={appContext} />);
    const sandbox = getFrame().getAttribute("sandbox") ?? "";

    // Required for the SDK's same-origin fetch + Pyodide's worker/WASM boot.
    expect(sandbox).toContain("allow-scripts");
    expect(sandbox).toContain("allow-same-origin");
    // The containment that this frame actually exists to provide: embedded app
    // code must not be able to navigate the console itself.
    expect(sandbox).not.toContain("allow-top-navigation");
  });
});
