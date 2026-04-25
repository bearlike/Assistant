import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { VitePWA } from 'vite-plugin-pwa'
// Aliases `streamlit.whl` / `stlite_lib.whl` imports (used inside
// `@stlite/react/vite-utils` to power the `wheelUrls` binding) to the wheel
// files shipped inside the @stlite/react package so Vite can resolve them.
// Without this plugin Rollup fails with `Rollup failed to resolve import
// "stlite_lib.whl"` at build time; at runtime stlite's worker skips the
// bundled streamlit install and `import streamlit` crashes.
import vitePluginStliteReact from '@stlite/react/vite-plugin'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

// https://vitejs.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiTarget =
    env.VITE_API_BASE_URL || env.VITE_API_BASE || "http://127.0.0.1:5124";
  const allowedHostsEnv = env.VITE_ALLOWED_HOSTS || "";
  const allowedHosts = Array.from(
    new Set(
      ["truss.hurricane.home"]
        .concat(
          allowedHostsEnv
            .split(",")
            .map((host) => host.trim())
            .filter(Boolean)
        )
    )
  );

  return {
    plugins: [
      react(),
      vitePluginStliteReact(),
      VitePWA({
        registerType: "prompt",
        injectRegister: null,
        includeAssets: [
          "favicon.ico",
          "favicon-16x16.png",
          "favicon-32x32.png",
          "apple-touch-icon.png",
          "android-chrome-192x192.png",
          "android-chrome-512x512.png",
          "logo-bg.svg",
          "logo-icon.svg",
          "logo-transparent.svg",
          "session-ide-floral-background-animation.svg"
        ],
        manifest: {
          name: "Truss",
          short_name: "Truss",
          start_url: "/",
          display: "standalone",
          theme_color: "#0a0a0a",
          background_color: "#0a0a0a",
          icons: [
            { src: "/favicon.ico", sizes: "48x48", type: "image/x-icon" },
            {
              src: "/android-chrome-192x192.png",
              sizes: "192x192",
              type: "image/png"
            },
            {
              src: "/android-chrome-512x512.png",
              sizes: "512x512",
              type: "image/png",
              purpose: "any maskable"
            }
          ]
        },
        workbox: {
          globPatterns: ["**/*.{js,css,html,ico,png,svg,woff,woff2}"],
          // runtime-config.js is rendered at container start; never precache.
          // StliteWidgetPanel and its Streamlit/Plotly/DeckGL sub-chunks are
          // lazily loaded WASM-heavy assets that exceed the 2 MiB precache
          // limit — exclude them and let the network serve them on demand.
          globIgnores: ["**/runtime-config.js", "**/StliteWidgetPanel-*.js", "**/PlotlyChart-*.js", "**/DeckGlJsonChart-*.js"],
          navigateFallback: "index.html",
          // `/ide/` is proxied to per-session code-server containers (see
          // docker/nginx-reverse-proxy.conf + docker/nginx-ide-proxy.conf).
          // It is NOT a SPA route — the NavigationRoute handler would otherwise
          // claim the navigation and serve index.html, leaving the user on the
          // Truss shell instead of Coder. Note the trailing slash: this must
          // NOT match `/ide-loader/:sessionId`, which *is* a SPA route.
          navigateFallbackDenylist: [/^\/api/, /^\/runtime-config\.js$/, /^\/ide\//],
          cleanupOutdatedCaches: true,
          // Activate a new SW and take over all clients immediately on install.
          // Paired with `registerType: "prompt"` + UpdatePrompt: the user still
          // sees a notification and triggers the page reload themselves, but
          // the SW no longer sits "waiting" across deploys — so a missed prompt
          // never leaves a tab pinned to a stale precache whose hashed bundles
          // have since been deleted from the server (the 404 cascade we just hit).
          clientsClaim: true,
          skipWaiting: true,
          runtimeCaching: [
            {
              // Web IDE is a separate upstream — pass through untouched so the
              // SW never caches a 302, login form, or editor HTML. Must come
              // before the generic navigate matcher below.
              urlPattern: /^\/ide\//,
              handler: "NetworkOnly"
            },
            {
              // HTML shell: network-first so users pick up new hashed asset
              // URLs as soon as they're online. Cache only as an offline fallback.
              urlPattern: ({ request }) => request.mode === "navigate",
              handler: "NetworkFirst",
              options: {
                cacheName: "truss-html",
                networkTimeoutSeconds: 3,
                expiration: { maxEntries: 10, maxAgeSeconds: 60 * 60 * 24 }
              }
            },
            {
              urlPattern: /\/runtime-config\.js$/,
              handler: "NetworkOnly"
            },
            {
              urlPattern: /\/api\//,
              handler: "NetworkOnly"
            }
          ]
        },
        devOptions: {
          enabled: false,
          type: "module"
        }
      })
    ],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, './src')
      }
    },
    server: {
      allowedHosts,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          secure: false
        }
      }
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/setupTests.ts",
      exclude: ["tests/**", "node_modules/**"]
    }
  };
});
