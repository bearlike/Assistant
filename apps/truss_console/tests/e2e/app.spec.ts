import { expect, test, Page, Route } from "@playwright/test";

type MockEventPayload = { events: unknown[]; running: boolean };

type MockApiOptions = {
  sessions?: Record<string, unknown>[];
  notifications?: Record<string, unknown>[];
  tools?: Record<string, unknown>[];
  events?: Record<string, MockEventPayload>;
  createSessionId?: string;
  onQuery?: (route: Route, body: Record<string, unknown>) => Promise<void> | void;
};

async function fulfillJson(route: Route, status: number, body: unknown) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body)
  });
}

async function setupApiRoutes(page: Page, options: MockApiOptions) {
  const {
    sessions = [],
    notifications = [],
    tools = [],
    events = {},
    createSessionId = "sess-123",
    onQuery
  } = options;

  await page.route("**/api/notifications", async (route) => {
    if (route.request().method() === "GET") {
      return fulfillJson(route, 200, { notifications });
    }
    return route.fallback();
  });

  await page.route("**/api/notifications/dismiss", async (route) => {
    if (route.request().method() === "POST") {
      return fulfillJson(route, 200, { dismissed: true });
    }
    return route.fallback();
  });

  await page.route("**/api/notifications/clear", async (route) => {
    if (route.request().method() === "POST") {
      return fulfillJson(route, 200, { cleared: true });
    }
    return route.fallback();
  });

  await page.route("**/api/tools", async (route) => {
    if (route.request().method() === "GET") {
      return fulfillJson(route, 200, { tools });
    }
    return route.fallback();
  });

  await page.route("**/api/sessions**", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      return fulfillJson(route, 200, { sessions });
    }
    if (method === "POST") {
      return fulfillJson(route, 200, { session_id: createSessionId });
    }
    return route.fallback();
  });

  await page.route("**/api/sessions/*/events**", async (route) => {
    if (route.request().method() !== "GET") {
      return route.fallback();
    }
    const url = new URL(route.request().url());
    const parts = url.pathname.split("/");
    const sessionId = parts[parts.indexOf("sessions") + 1];
    const payload = events[sessionId] || { events: [], running: false };
    return fulfillJson(route, 200, { session_id: sessionId, ...payload });
  });

  await page.route("**/api/sessions/*/query", async (route) => {
    if (route.request().method() !== "POST") {
      return route.fallback();
    }
    const body = route.request().postDataJSON() as Record<string, unknown>;
    if (onQuery) {
      await onQuery(route, body);
      return;
    }
    return fulfillJson(route, 202, { accepted: true });
  });

  await page.route("**/api/sessions/*/archive", async (route) => {
    const method = route.request().method();
    if (method === "POST" || method === "DELETE") {
      return fulfillJson(route, 200, { archived: method === "POST" });
    }
    return route.fallback();
  });
}

function trackConsoleErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") {
      errors.push(msg.text());
    }
  });
  page.on("pageerror", (err) => {
    errors.push(err.message);
  });
  return errors;
}

test("create session from landing", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await setupApiRoutes(page, {
    sessions: [],
    notifications: [],
    tools: [],
    createSessionId: "sess-123",
    events: {
      "sess-123": { events: [], running: false }
    }
  });

  await page.goto("/");
  await page.getByLabel("Task description").fill("Draft a short plan");
  await page.getByLabel("Send query").click();
  await expect(page).toHaveURL(/\/s\/sess-123/);
  expect(errors).toEqual([]);
});

test("create session with Hi", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await setupApiRoutes(page, {
    sessions: [],
    notifications: [],
    tools: [],
    createSessionId: "sess-hi",
    events: {
      "sess-hi": { events: [], running: false }
    }
  });

  await page.goto("/");
  await page.getByLabel("Task description").fill("Hi");
  const [request] = await Promise.all([
    page.waitForRequest((req) =>
      req.url().includes("/api/sessions/sess-hi/query") &&
      req.method() === "POST"
    ),
    page.getByLabel("Send query").click()
  ]);
  const body = request.postDataJSON() as Record<string, unknown>;
  expect(body.query).toBe("Hi");
  expect(errors).toEqual([]);
});

test("create session with tool-call query", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  const query = "Search the internet for is Krishnakanth Alagiri is?";
  await setupApiRoutes(page, {
    sessions: [],
    notifications: [],
    tools: [],
    createSessionId: "sess-search",
    events: {
      "sess-search": { events: [], running: false }
    }
  });

  await page.goto("/");
  await page.getByLabel("Task description").fill(query);
  const [request] = await Promise.all([
    page.waitForRequest((req) =>
      req.url().includes("/api/sessions/sess-search/query") &&
      req.method() === "POST"
    ),
    page.getByLabel("Send query").click()
  ]);
  const body = request.postDataJSON() as Record<string, unknown>;
  expect(body.query).toBe(query);
  expect(errors).toEqual([]);
});

test("send query in existing session", async ({ page }) => {
  await setupApiRoutes(page, {
    sessions: [
      {
        session_id: "sess-abc",
        title: "Existing session",
        created_at: "2026-02-10T08:00:00Z",
        status: "completed",
        done_reason: "completed",
        running: false,
        context: { mcp_tools: [] }
      }
    ],
    notifications: [],
    tools: [],
    events: {
      "sess-abc": { events: [], running: false }
    }
  });

  await page.goto("/");
  await page.getByText("Existing session").click();
  const inputBar = page.getByTestId("inputbar-detail");
  await inputBar.getByLabel("Session query").fill("Add a note");

  const [request] = await Promise.all([
    page.waitForRequest((req) =>
      req.url().includes("/api/sessions/sess-abc/query") &&
      req.method() === "POST"
    ),
    inputBar.getByLabel("Send query").click()
  ]);

  const body = request.postDataJSON() as Record<string, unknown>;
  expect(body.query).toBe("Add a note");
  expect(body.mode).toBe("act");
});

test("shows loader while polling", async ({ page }) => {
  await setupApiRoutes(page, {
    sessions: [
      {
        session_id: "sess-run",
        title: "Running session",
        created_at: "2026-02-10T08:10:00Z",
        status: "running",
        done_reason: null,
        running: true,
        context: { mcp_tools: [] }
      }
    ],
    notifications: [],
    tools: [],
    events: {
      "sess-run": {
        events: [
          {
            ts: "2026-02-10T08:10:05Z",
            type: "user",
            payload: { text: "Process incoming logs" }
          }
        ],
        running: true
      }
    }
  });

  await page.goto("/");
  await page.getByText("Running session").click();
  await expect(page.getByText("Running...")).toBeVisible();
  await expect(page.getByText("Open trace")).toBeVisible();
  const bubble = page.getByText("Process incoming logs").locator("..");
  await expect(bubble.getByText("Open trace")).toHaveCount(0);
});

test("shows error on failed query", async ({ page }) => {
  await setupApiRoutes(page, {
    sessions: [
      {
        session_id: "sess-fail",
        title: "Failure session",
        created_at: "2026-02-10T08:20:00Z",
        status: "completed",
        done_reason: "completed",
        running: false,
        context: { mcp_tools: [] }
      }
    ],
    notifications: [],
    tools: [],
    events: {
      "sess-fail": { events: [], running: false }
    },
    onQuery: async (route) => {
      await fulfillJson(route, 500, { message: "Boom" });
    }
  });

  await page.goto("/");
  await page.getByText("Failure session").click();
  const inputBar = page.getByTestId("inputbar-detail");
  await inputBar.getByLabel("Session query").fill("Cause error");
  await inputBar.getByLabel("Send query").click();
  await expect(
    page.getByRole("heading", { name: "Request error" }).first()
  ).toBeVisible();
  await expect(page.getByText("Boom").first()).toBeVisible();
});

test("notification bell opens panel without crashing", async ({ page }) => {
  const errors = trackConsoleErrors(page);
  await setupApiRoutes(page, {
    sessions: [],
    notifications: [
      {
        id: "notif-1",
        title: "Session started",
        message: "Session sess-1 started.",
        level: "info",
        created_at: "2026-02-10T09:00:00Z",
        event_type: "started",
        session_id: "sess-1"
      }
    ],
    tools: []
  });

  await page.goto("/");
  await page.getByLabel("Notifications").click();
  await expect(page.getByText("Notifications", { exact: true })).toBeVisible();
  await expect(page.getByText("Session started")).toBeVisible();
  expect(errors).toEqual([]);
});
