import { describe, it, expect } from "vitest";
import {
  buildAppKernelOptions,
  buildKernelOptions,
  FACADE_VERSION,
  STLITE_THEME,
} from "../stliteBoot";
import type { WidgetReadyPayload } from "../../types"
import type { AppContext, AppFrontend } from "../../types/apps";

const payload: WidgetReadyPayload = {
  widget_id: "w1",
  session_id: "s1",
  files: { "app.py": "import streamlit as st\nst.write('hi')", "data.json": "{}" },
  requirements: ["pandas", "numpy"],
};

const wheelUrls = { streamlit: "/wheels/streamlit.whl", stliteLib: "/wheels/stlite_lib.whl" };

// The exact `.streamlit/config.toml` bytes facade 0.1.6's `_write_config`
// emits, per theme — seeding these prevents an extra boot rerun.
const CONFIG_TOML: Record<"dark" | "light", string> = {
  dark:
    '[theme]\nbase = "dark"\nprimaryColor = "#d97757"\nbackgroundColor = "#131211"\n' +
    'secondaryBackgroundColor = "#2a2927"\ntextColor = "#faf9f5"\n',
  light:
    '[theme]\nbase = "light"\nprimaryColor = "#d97757"\nbackgroundColor = "#faf9f5"\n' +
    'secondaryBackgroundColor = "#f0efe7"\ntextColor = "#3d3a2a"\n',
};

describe("buildKernelOptions", () => {
  it("fronts the authored entrypoint with the wrapper AT ITS OWN PATH, relocating the source verbatim", () => {
    const options = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    // The entrypoint keeps the AUTHORED name — Streamlit derives the multipage
    // sidebar's main-page label from this filename, and an injected
    // `_mewbo_main.py` used to surface to users as "Mewbo Main".
    expect(options.entrypoint).toBe("app.py");
    // ...so `app.py` is now the wrapper, and the authored source moved to a
    // private sibling, byte-for-byte.
    expect(options.files["_mewbo_app_app.py"]).toEqual({ data: payload.files["app.py"] });
    expect(options.files["data.json"]).toEqual({ data: payload.files["data.json"] });
    // The wrapper runs the relocated original via runpy, under __main__.
    expect((options.files["app.py"] as { data: string }).data).toContain(
      'runpy.run_path("_mewbo_app_app.py", run_name="__main__")',
    );
    // The old wrapper name must be gone entirely, not merely unused.
    expect(options.files).not.toHaveProperty("_mewbo_main.py");
    expect(options.wheelUrls).toBe(wheelUrls);
  });

  it("keeps the relocated entrypoint in the authored entrypoint's DIRECTORY", () => {
    // Sibling-module imports resolve relative to the file's own directory, so
    // relocating `src/main.py` to a root-level name would break them.
    const options = buildAppKernelOptions(
      { entrypoint: "src/main.py", files: { "src/main.py": "y = 2" }, requirements: [] },
      { token: "t", api_base: "b", app_id: "a" },
      { theme: "dark", wheelUrls },
    );
    expect(options.entrypoint).toBe("src/main.py");
    expect(options.files["src/_mewbo_app_main.py"]).toEqual({ data: "y = 2" });
    expect((options.files["src/main.py"] as { data: string }).data).toContain(
      'runpy.run_path("src/_mewbo_app_main.py", run_name="__main__")',
    );
  });

  it("appends the facade requirement to a NEW array, preserving order", () => {
    const options = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    expect(options.requirements).toEqual(["pandas", "numpy", `streamlit-facade==${FACADE_VERSION}`]);
    // Must not mutate / alias the caller's array.
    expect(options.requirements).not.toBe(payload.requirements);
    expect(payload.requirements).toEqual(["pandas", "numpy"]);
  });

  it("does NOT append a second facade requirement when one is already declared", () => {
    const declared: WidgetReadyPayload = {
      ...payload,
      requirements: ["streamlit-facade==0.9.9", "pandas"],
    };
    const options = buildKernelOptions(declared, { theme: "dark", wheelUrls });
    expect(options.requirements).toEqual(["streamlit-facade==0.9.9", "pandas"]);
    expect(
      options.requirements.filter((r) => r.startsWith("streamlit-facade")),
    ).toHaveLength(1);
  });

  it("seeds .streamlit/config.toml byte-identically for dark and light", () => {
    const dark = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    expect((dark.files[".streamlit/config.toml"] as { data: string }).data).toBe(CONFIG_TOML.dark);

    const light = buildKernelOptions(payload, { theme: "light", wheelUrls });
    expect((light.files[".streamlit/config.toml"] as { data: string }).data).toBe(CONFIG_TOML.light);
  });

  it("lets the injected wrapper / relocated source / config.toml WIN same-named authored files", () => {
    // An app cannot pre-declare any injected name to smuggle its own code in:
    // the wrapper path, the relocated-source path, and the config are all
    // written AFTER the authored map is spread.
    const smuggler: WidgetReadyPayload = {
      ...payload,
      files: {
        ...payload.files,
        "_mewbo_app_app.py": "raise RuntimeError('smuggled source')",
        ".streamlit/config.toml": "[theme]\nbase = \"evil\"\n",
      } as WidgetReadyPayload["files"],
    };
    const options = buildKernelOptions(smuggler, { theme: "dark", wheelUrls });
    expect((options.files["app.py"] as { data: string }).data).toContain("Injected by Mewbo");
    // The relocated slot holds the AUTHORED entrypoint, never the smuggled file.
    expect(options.files["_mewbo_app_app.py"]).toEqual({ data: payload.files["app.py"] });
    expect((options.files["_mewbo_app_app.py"] as { data: string }).data).not.toContain("smuggled");
    expect((options.files[".streamlit/config.toml"] as { data: string }).data).toBe(CONFIG_TOML.dark);
  });

  it("applies config-native chrome hiding + compact density in streamlitConfig", () => {
    const { streamlitConfig } = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    expect(streamlitConfig["client.toolbarMode"]).toBe("viewer");
    expect(streamlitConfig["ui.hideTopBar"]).toBe(true);
    expect(streamlitConfig["theme.baseRadius"]).toBe("6px");
    expect(streamlitConfig["theme.baseFontSize"]).toBe(14);
    // Pill buttons violate the console shape vocabulary — the key must be gone.
    expect(Object.keys(streamlitConfig)).not.toContain("theme.buttonRadius");
  });

  it("omits pyodideUrl when not provided, preserving stlite's CDN fallback", () => {
    const options = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    expect(options).not.toHaveProperty("pyodideUrl");
  });

  it("includes pyodideUrl only when provided", () => {
    const options = buildKernelOptions(payload, {
      theme: "dark",
      wheelUrls,
      pyodideUrl: "https://example.test/pyodide.mjs",
    });
    expect(options.pyodideUrl).toBe("https://example.test/pyodide.mjs");
  });

  it("merges the STLITE_THEME palette for the requested theme into streamlitConfig", () => {
    const dark = buildKernelOptions(payload, { theme: "dark", wheelUrls });
    expect(dark.streamlitConfig["theme.base"]).toBe("dark");
    for (const [key, value] of Object.entries(STLITE_THEME.dark)) {
      expect((dark.streamlitConfig as Record<string, unknown>)[key]).toBe(value);
    }

    const light = buildKernelOptions(payload, { theme: "light", wheelUrls });
    expect(light.streamlitConfig["theme.base"]).toBe("light");
    for (const [key, value] of Object.entries(STLITE_THEME.light)) {
      expect((light.streamlitConfig as Record<string, unknown>)[key]).toBe(value);
    }

    // Themes carry distinct backgrounds, so swapping the theme must actually
    // change the merged config rather than sharing one baked-in palette.
    expect((dark.streamlitConfig as Record<string, unknown>)["theme.backgroundColor"]).not.toBe(
      (light.streamlitConfig as Record<string, unknown>)["theme.backgroundColor"],
    );
  });
});

describe("buildAppKernelOptions", () => {
  const appContext: AppContext = {
    token: "tok-123",
    api_base: "https://mewbo.test/api",
    app_id: "app-1a2b3c4d",
  };

  it("passes the whole multi-file frontend map through, wrapped as {data}, behind the injected entrypoint", () => {
    const frontend: AppFrontend = {
      // A non-`app.py` entrypoint proves the wrapper names the AUTHORED one.
      entrypoint: "home.py",
      files: {
        "home.py": "import streamlit as st\nst.write('home')",
        "pages/tasks.py": "import streamlit as st\nst.write('tasks')",
        "lib/util.py": "X = 1",
      },
      requirements: ["pandas"],
    };
    const options = buildAppKernelOptions(frontend, appContext, { theme: "dark", wheelUrls });

    // The entrypoint keeps the author's own filename, so the sidebar label
    // reads "Home" rather than a platform detail.
    expect(options.entrypoint).toBe("home.py");
    expect(options.requirements).toEqual(["pandas", `streamlit-facade==${FACADE_VERSION}`]);
    // Every NON-entrypoint authored file survives verbatim at its own path.
    for (const [name, source] of Object.entries(frontend.files)) {
      if (name === frontend.entrypoint) continue;
      expect(options.files[name]).toEqual({ data: source });
    }
    // ...and the entrypoint's source survives verbatim at the relocated path.
    expect(options.files["_mewbo_app_home.py"]).toEqual({ data: frontend.files["home.py"] });
    // The wrapper sits at the authored path and runs the relocated original.
    expect((options.files["home.py"] as { data: string }).data).toContain(
      'runpy.run_path("_mewbo_app_home.py", run_name="__main__")',
    );
  });

  it("threads an explicit basePath through, and omits it when unset", () => {
    const frontend: AppFrontend = {
      entrypoint: "app.py",
      files: { "app.py": "import streamlit as st" },
      requirements: [],
    };
    const base = { theme: "dark", wheelUrls } as const;

    // Unset: stlite keeps its `window.location.pathname` default, which is the
    // right reading only when the hosting page IS the app.
    expect(buildAppKernelOptions(frontend, appContext, base)).not.toHaveProperty("basePath");

    // Set: framed surfaces pin it, because the frame's own pathname is the
    // widget-host page, not the app.
    const pinned = buildAppKernelOptions(frontend, appContext, {
      ...base,
      basePath: "mewbo-app",
    });
    expect(pinned.basePath).toBe("mewbo-app");
  });

  it("injects _app_context.json and lets the injected value WIN a same-named frontend file", () => {
    // A frontend that names _app_context.json itself must NOT be able to smuggle
    // its own token/base in — the server-injected context is authoritative.
    const frontend: AppFrontend = {
      entrypoint: "app.py",
      files: {
        "app.py": "import streamlit as st",
        "_app_context.json": JSON.stringify({ token: "STOLEN", api_base: "evil", app_id: "x" }),
      },
      requirements: [],
    };
    const options = buildAppKernelOptions(frontend, appContext, { theme: "dark", wheelUrls });

    expect(options.files["_app_context.json"]).toEqual({ data: JSON.stringify(appContext) });
    // And the real authored entrypoint is still present alongside it, verbatim,
    // at the relocated path the wrapper runs.
    expect(options.files["_mewbo_app_app.py"]).toEqual({ data: "import streamlit as st" });
  });
});
