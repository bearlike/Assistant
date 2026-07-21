import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

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