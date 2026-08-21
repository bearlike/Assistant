import "@testing-library/jest-dom/vitest";
import { configure } from "@testing-library/react";
import { vi } from "vitest";

// Testing Library's own async window is 1000 ms and is INDEPENDENT of vitest's
// `testTimeout` — raising one does nothing for the other, which is why a suite
// can be given a generous test budget and still fail inside `findBy*`. On the
// shared CI runner this suite takes roughly twice its local wall time, and a
// component whose content arrives through a query (the composer's mic, a nav
// tree, a lazily-mounted pane) can miss a 1 s window purely because the box was
// busy. The tell is that the FAILING SET CHANGES between runs of the same
// commit — load, not a regression.
//
// Deliberately smaller than `testTimeout` (20 s): a genuinely missing element
// still fails inside its own window and reports "unable to find …" with the
// rendered DOM, which names the defect, rather than expiring the whole test and
// reporting only that time ran out.
configure({ asyncUtilTimeout: 5_000 });

// vite-plugin-pwa virtual module has no build-time source — stub it for tests.
vi.mock("virtual:pwa-register/react", () => ({
  useRegisterSW: () => ({
    needRefresh: [false, vi.fn()],
    offlineReady: [false, vi.fn()],
    updateServiceWorker: vi.fn(),
  }),
}));

// jsdom lacks ResizeObserver — stub it for components that observe layout (e.g. cmdk)
class ResizeObserverStub {
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
}
globalThis.ResizeObserver = globalThis.ResizeObserver ?? ResizeObserverStub;

// jsdom lacks Element.scrollIntoView — cmdk calls it on the selected item when
// a list mounts with rows (e.g. the file-mention picker / slash palette).
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}

// jsdom implements Range but NOT its layout methods. CodeMirror 6 (the
// system-instructions editor) measures character size by calling
// `getClientRects()` on a Range, and it does so from a requestAnimationFrame —
// so the throw lands as an UNHANDLED error after the test itself has already
// passed, which Vitest correctly flags as a false-positive risk. Empty rects
// are the honest answer in a DOM with no layout: CM6 reads that as "not
// measurable yet" and skips the measure pass, which is exactly right here.
if (typeof Range !== "undefined" && !Range.prototype.getClientRects) {
  Range.prototype.getClientRects = () =>
    Object.assign([], { item: () => null }) as unknown as DOMRectList;
  Range.prototype.getBoundingClientRect = () => new DOMRect();
}

// jsdom lacks PointerEvent entirely (no window.PointerEvent) — Radix's
// DropdownMenu/Popover/Select triggers open via `onPointerDown`, and
// `@testing-library/user-event` only synthesizes pointer events when
// `window.PointerEvent` exists, so without this polyfill `userEvent.click()`
// on a Radix trigger silently never opens the menu (no error — the click
// itself still fires, but Radix's open handler never runs). Minimal
// MouseEvent-backed stand-in; only the fields Radix reads are populated.
if (typeof window.PointerEvent === "undefined") {
  // Not a full `implements PointerEvent` — jsdom's own PointerEvent (when
  // present) omits stylus-only members too (altitudeAngle, azimuthAngle,
  // getCoalescedEvents, getPredictedEvents); Radix never reads them.
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number;
    width: number;
    height: number;
    pressure: number;
    tangentialPressure: number;
    tiltX: number;
    tiltY: number;
    twist: number;
    pointerType: string;
    isPrimary: boolean;

    constructor(type: string, params: PointerEventInit = {}) {
      super(type, params);
      this.pointerId = params.pointerId ?? 0;
      this.width = params.width ?? 1;
      this.height = params.height ?? 1;
      this.pressure = params.pressure ?? 0;
      this.tangentialPressure = params.tangentialPressure ?? 0;
      this.tiltX = params.tiltX ?? 0;
      this.tiltY = params.tiltY ?? 0;
      this.twist = params.twist ?? 0;
      this.pointerType = params.pointerType ?? "mouse";
      this.isPrimary = params.isPrimary ?? false;
    }
  }
  // @ts-expect-error — jsdom's MouseEvent isn't a full PointerEvent supertype
  window.PointerEvent = PointerEventPolyfill;
}

// jsdom's Element also lacks the pointer-capture trio Radix calls on the
// same triggers (guarded no-ops; Radix only checks these don't throw).
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
}
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => undefined;
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => undefined;
}

// jsdom implements neither blob URLs nor media playback, and the read-aloud
// button needs both: it turns a synthesized audio Blob into an object URL and
// hands it to an `<audio>` element.
//
// The two need OPPOSITE treatments, which is the trap. `URL.createObjectURL` is
// simply absent, so a guard works. `HTMLMediaElement.prototype.play` EXISTS —
// jsdom defines it and its body raises "Not implemented" into the virtual
// console — so a `if (!play)` guard never fires and the stub never installs.
// These are overwritten outright for that reason.
if (typeof URL.createObjectURL !== "function") {
  let blobUrlSeq = 0;
  URL.createObjectURL = () => `blob:mewbo-test/${++blobUrlSeq}`;
  URL.revokeObjectURL = () => undefined;
}
HTMLMediaElement.prototype.play = function play(this: HTMLMediaElement) {
  this.dispatchEvent(new Event("play"));
  return Promise.resolve();
};
HTMLMediaElement.prototype.pause = function pause() {
  /* no-op: nothing is decoding, so there is nothing to suspend */
};
HTMLMediaElement.prototype.load = function load() {
  /* no-op: the chunked reader preloads the next clip, and jsdom raises
     "Not implemented" into the virtual console for the real method */
};

// jsdom lacks matchMedia — stub it for hooks that use media queries (e.g. useIsMobile)
Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});

// ---------------------------------------------------------------------------
// Audio capture — `MediaRecorder` + `getUserMedia` (the composer's mic)
// ---------------------------------------------------------------------------
//
// jsdom implements NEITHER, and the mic feature-detects both: without these the
// control renders nothing and a test asserting on it passes for the wrong
// reason. They are installed here rather than per-test because absence is a
// jsdom gap like every other entry in this file.
//
// Unlike the passive stubs above, these are DRIVEN by tests: `MediaRecorderStub`
// records every instance it constructs on a static, so a test can drive one
// recorder's transitions and assert on the ORDER of stop/upload/track-release
// rather than merely that a recorder exists. A test wanting to observe track
// release spies on `getUserMedia` and returns its own stream, so the default
// below stays a plain "permission granted" and never has to grow assertions.
class MediaStreamTrackStub {
  kind = "audio";
  readyState: "live" | "ended" = "live";
  stop() {
    // The browser's recording indicator goes out only when every track ends,
    // so a test asserting on privacy asserts on exactly this flag.
    this.readyState = "ended";
  }
}

class MediaStreamStub {
  private readonly tracks = [new MediaStreamTrackStub()];
  getTracks() {
    return this.tracks;
  }
  getAudioTracks() {
    return this.tracks;
  }
}

class MediaRecorderStub {
  /** Every recorder ever constructed, newest last. Reset between tests. */
  static instances: MediaRecorderStub[] = [];
  /** Containers this fake browser claims to support; a test may narrow it. */
  static supportedTypes: string[] = ["audio/webm;codecs=opus", "audio/webm"];
  static isTypeSupported(type: string) {
    return MediaRecorderStub.supportedTypes.includes(type);
  }

  state: "inactive" | "recording" | "paused" = "inactive";
  mimeType: string;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;

  constructor(_stream: unknown, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? "audio/webm";
    MediaRecorderStub.instances.push(this);
  }

  start() {
    this.state = "recording";
  }
  pause() {
    this.state = "paused";
  }
  resume() {
    this.state = "recording";
  }

  /** Mirrors the real thing: state flips synchronously, events land later. */
  stop() {
    this.state = "inactive";
    queueMicrotask(() => {
      this.ondataavailable?.({ data: new Blob(["fake-audio"], { type: this.mimeType }) });
      this.onstop?.();
    });
  }
}

Object.defineProperty(globalThis, "MediaRecorder", {
  writable: true,
  configurable: true,
  value: MediaRecorderStub,
});

Object.defineProperty(navigator, "mediaDevices", {
  writable: true,
  configurable: true,
  value: {
    getUserMedia: () => Promise.resolve(new MediaStreamStub()),
  },
});