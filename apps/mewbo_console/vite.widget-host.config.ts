import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import { defineConfig, type Plugin } from 'vite'

// Standalone build for `widget-host.html` — the framework-free stlite host that
// the Aura Android WebView (and any non-React embedder) loads.
//
// WHY A SEPARATE CONFIG rather than a second `rollupOptions.input` in the main
// vite.config.ts: the widget-host must be RELOCATABLE — it is served from a
// foreign synthetic origin (`https://appassets.androidplatform.net/…`) at a
// mount path we don't control, so every asset it references (its own JS/CSS,
// the stlite wheels, the Pyodide runtime) has to resolve relative to the page.
// That requires `base: './'`. The console's main build, by contrast, MUST stay
// `base: '/'`: its PWA service worker precache + SPA sub-route hard-loads
// (`/s/:id`) depend on absolute asset URLs (see apps/mewbo_console/CLAUDE.md →
// "PWA service worker"). Rather than risk that documented-fragile machinery by
// flipping the global base, the widget-host gets its own clean relative-base
// build into `dist/widget-host/`. Run it AFTER the console build so the SW's
// glob never precaches this bundle.

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(import.meta.url)

/**
 * @stlite/browser's kernel bundle references one wasm asset via
 * `new URL("./assets/<hash>.wasm", "" + import.meta.url)`. The `"" +` breaks
 * Vite's static asset-URL detection, so under the RELATIVE base this build uses
 * the reference passes through as a literal `./assets/<hash>.wasm` and Vite
 * never emits the file. Resolved from a chunk that itself lives in `assets/`,
 * that literal points at `assets/assets/<hash>.wasm`, so copy the wasm there.
 * (The console's base-'/' build doesn't hit this: an absolute `/assets/...`
 * literal happens to resolve, and its own copy is emitted+hashed.) Fonts in the
 * same upstream dir are referenced from stlite.css `url(...)`, which Vite DOES
 * rewrite, so only wasm needs copying. Generic over filenames so an @stlite
 * bump that renames the wasm keeps working.
 */
function copyStliteWasmAssets(): Plugin {
  return {
    name: 'copy-stlite-wasm-assets',
    apply: 'build',
    async closeBundle() {
      const srcDir = path.join(
        path.dirname(require.resolve('@stlite/browser/package.json')),
        'build',
        'assets',
      )
      const destDir = path.resolve(__dirname, 'dist/widget-host/assets/assets')
      const wasm = (await fs.promises.readdir(srcDir)).filter((f) => f.endsWith('.wasm'))
      if (wasm.length === 0) {
        this.warn(`no @stlite/browser wasm assets found in ${srcDir}`)
        return
      }
      await fs.promises.mkdir(destDir, { recursive: true })
      for (const f of wasm) {
        await fs.promises.copyFile(path.join(srcDir, f), path.join(destDir, f))
      }
    },
  }
}

/**
 * Copy the vendored Pyodide runtime into the widget-host output so the page is
 * fully self-contained and relocatable (`host.ts` resolves it as
 * `./pyodide/pyodide.mjs` relative to `document.baseURI`). The console's main
 * build serves the same runtime from `public/pyodide/` → `dist/pyodide/`; this
 * duplicates ~one copy into `dist/widget-host/pyodide/` so an embedder can
 * bundle just this directory. `publicDir` is disabled so the rest of the
 * console's public/ (icons, manifests) does not leak into the widget bundle.
 */
function copyPyodideRuntime(): Plugin {
  return {
    name: 'copy-pyodide-into-widget-host',
    apply: 'build',
    async closeBundle() {
      const src = path.resolve(__dirname, 'public/pyodide')
      const dest = path.resolve(__dirname, 'dist/widget-host/pyodide')
      if (!fs.existsSync(src)) {
        this.warn(
          `Pyodide runtime not found at ${src}. Run \`npm run fetch-pyodide\` ` +
            `(or it runs via postinstall) so the widget host can boot offline.`,
        )
        return
      }
      await fs.promises.cp(src, dest, { recursive: true })
    },
  }
}

// https://vitejs.dev/config/
export default defineConfig({
  root: __dirname,
  // Relocatable: assets referenced relative to the page, not an absolute origin.
  base: './',
  // The console's public/ is the console build's concern; the widget host only
  // needs Pyodide, copied explicitly above.
  publicDir: false,
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  plugins: [copyPyodideRuntime(), copyStliteWasmAssets()],
  optimizeDeps: {
    // Only matters for `vite dev`; keeps esbuild from prebundling the browser
    // kernel (which pulls .whl/.css assets it can't resolve).
    exclude: ['@stlite/browser'],
  },
  build: {
    outDir: 'dist/widget-host',
    // Only clears dist/widget-host — the console build (run first) owns dist/.
    emptyOutDir: true,
    // Match the main build: Vite 8's default lightningcss chokes on some of the
    // vendored stlite CSS; esbuild does not.
    cssMinify: 'esbuild',
    rollupOptions: {
      input: path.resolve(__dirname, 'widget-host.html'),
      output: {
        // The bundled streamlit/stlite wheels MUST keep their exact PEP 427
        // filenames — micropip parses `name-version-...-tag.whl`, and Vite's
        // default `-[hash]` suffix turns `streamlit-1.57.0-cp313-none-any.whl`
        // into an unparseable `…-none-any-CQR0LZsx.whl` (micropip:
        // "Invalid build number: cp313"). Wheels are already version-stamped,
        // so an unhashed name is still cache-safe. Everything else keeps the
        // content hash.
        assetFileNames: (info: { names?: string[]; name?: string }) => {
          const name = info.names?.[0] ?? info.name ?? ''
          return name.endsWith('.whl')
            ? 'assets/[name][extname]'
            : 'assets/[name]-[hash][extname]'
        },
      },
    },
  },
})
