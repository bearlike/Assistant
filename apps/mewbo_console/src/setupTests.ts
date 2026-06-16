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