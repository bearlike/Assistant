import { cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { parseSseStream } from "@/api/sse";

afterEach(cleanup);

describe("parseSseStream", () => {
  it("completes an aborted teardown without an unhandled rejection", async () => {
    const cancellation = new Error("Body stream was aborted");
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode('event: message\ndata: {"value":1}\n\n'),
        );
      },
      cancel() {
        return Promise.reject(cancellation);
      },
    });
    const signal = new AbortController();
    const unhandled: PromiseRejectionEvent[] = [];
    const onUnhandled = (event: PromiseRejectionEvent) => unhandled.push(event);
    window.addEventListener("unhandledrejection", onUnhandled);

    try {
      const parser = parseSseStream<{ type: string; value: number }>(
        new Response(stream),
        signal.signal,
      );
      await expect(parser.next()).resolves.toMatchObject({
        done: false,
        value: { type: "message", value: 1 },
      });

      signal.abort();
      await expect(parser.next()).resolves.toMatchObject({ done: true });
      await Promise.resolve();

      expect(unhandled).toEqual([]);
    } finally {
      window.removeEventListener("unhandledrejection", onUnhandled);
    }
  });

  it("propagates a transport error when the stream was not aborted", async () => {
    const transportError = new Error("connection closed unexpectedly");
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.error(transportError);
      },
    });
    const parser = parseSseStream(new Response(stream));

    await expect(parser.next()).rejects.toThrow("connection closed unexpectedly");
  });
});
