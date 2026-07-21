#!/usr/bin/env node
// Vendor the Pyodide runtime + the stlite-widget boot wheel closure into
// `public/pyodide/` so booting a Streamlit widget needs ZERO public-internet
// fetches. stlite's worker otherwise loads pyodide.mjs (and, transitively, the
// package wheels) from `cdn.jsdelivr.net`, which silently breaks widgets on any
// offline / LAN-only deploy. See apps/mewbo_console/CLAUDE.md → "Stlite widget
// rendering" for the full rationale.
//
// What lands in public/pyodide/:
//   - Pyodide 0.29.3 core: pyodide.mjs, pyodide.asm.{js,wasm}, python_stdlib.zip
//   - A TRIMMED pyodide-lock.json listing only the boot closure (so packages a
//     widget names in `requirements` but that we didn't vendor fall through to
//     micropip/PyPI cleanly instead of 404-ing against a local indexURL).
//   - The boot closure wheels: streamlit's mandatory Requires-Dist resolved
//     against the lockfile (numpy, pandas, pillow, altair's subtree, starlette,
//     …) plus six pure-python leaves that micropip would otherwise pull from
//     PyPI (blinker, itsdangerous, python-multipart, tenacity, and protobuf —
//     whose lockfile 6.31.1 is below the worker's forced >=7.34.1 — and
//     streamlit-facade, appended to every widget/app's requirements by
//     stliteBoot.ts for platform theming).
//
// This runs from `npm run build` (idempotent — skips when already vendored) and
// from postinstall for local dev. It is NOT wired through npm's install scripts
// in Docker (`npm ci --ignore-scripts`); the build step invokes it there.

import { createHash } from 'node:crypto'
import { mkdir, writeFile, readFile, rm } from 'node:fs/promises'
import { existsSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const OUT = path.resolve(__dirname, '..', 'public', 'pyodide')

const PYODIDE_VERSION = '0.29.3'
const CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full`

// Everything loadPyodide needs, minus the lockfile (rewritten trimmed below).
// pyodide.mjs is the ESM entry the stlite module worker imports; its directory
// becomes Pyodide's indexURL, so every vendored wheel next to it resolves.
const CORE_FILES = [
  'pyodide.mjs',
  'pyodide.asm.js',
  'pyodide.asm.wasm',
  'python_stdlib.zip',
  'package.json',
]

// TOFU anchor for the core runtime + lockfile fetch — the ONE pair of fetches
// in this script with no sha256 to check against (every wheel below verifies
// against `p.sha256`/`w.sha256` from the lockfile, but the lockfile fetch
// itself was previously unverified, so a compromised CDN response could
// rewrite both the runtime and every wheel's expected hash in one move).
// Pinned from the TLS-fetched bytes of PYODIDE_VERSION (the same bytes the
// Playwright zero-fetch test exercised): `sha256sum public/pyodide/<file>`
// for every CORE_FILES entry EXCEPT `pyodide-lock.json` — that file is
// rewritten trimmed before it lands in public/pyodide/ (see step 5 below), so
// its pin must come from the RAW pre-trim response instead, e.g.
// `curl -s https://cdn.jsdelivr.net/pyodide/v<version>/full/pyodide-lock.json | sha256sum`.
// Bump these pins ONLY alongside PYODIDE_VERSION, from a freshly verified
// source — never by re-hashing whatever happens to be on disk.
const CORE_SHA256 = {
  'pyodide.mjs': '23213d27e9dacbda6f25f955fb38dabfc40ada082c04ee253047ea5dddcaa3b5',
  'pyodide.asm.js': '1263f02b5b26099b96112378156f242dd98b39a8201ba7765e5fe3d455c5ce91',
  'pyodide.asm.wasm': 'e2f4ee75b325e35eb31bfb8c613d4dd5098f5502c156a97847686875b5025480',
  'python_stdlib.zip': '4298b6ee445cb724c3973437da47789752b9e6ff4e26619026b283ec801fc46b',
  'package.json': '81f7cf8ee62633854d610a040efe15d3f9e80cfc78fbc32426860419b8712924',
  'pyodide-lock.json': '3256ffc76388de0e37f4b34d42ab484268d1afc675179ff97b2a5bb14f84ccac',
}

// Roots micropip resolves at stlite boot = streamlit's mandatory Requires-Dist
// (non-extra) + stlite_lib's dep + protobuf (the worker forces >=7.34.1,<8).
// Names only; versions come from the live lockfile. RE-CHECK on an @stlite bump:
//   unzip -p node_modules/@stlite/browser/build/wheels/streamlit-*.whl \
//     '*/METADATA' | grep '^Requires-Dist'
const BOOT_ROOTS = [
  // micropip is loaded first, on its own, via `pyodide.loadPackage("micropip")`
  // — before the streamlit install — so it MUST be in the trimmed lockfile even
  // though nothing lists it as a dependency.
  'micropip',
  'altair', 'blinker', 'cachetools', 'numpy', 'packaging', 'pandas', 'pillow',
  'protobuf', 'tenacity', 'typing-extensions', 'starlette', 'anyio',
  'python-multipart', 'itsdangerous', 'fastparquet', 'pyodide-http',
]

// Pure-python boot deps micropip would otherwise fetch from PyPI: either absent
// from the pyodide lockfile (blinker/itsdangerous/python-multipart/tenacity) or
// present but too old (protobuf 6.31.1 < the forced 7.34.1). Pinned + vendored
// with an injected lockfile entry so boot stays fully offline. These are leaves
// (no runtime deps that aren't already in the closure). Bump versions here if a
// streamlit constraint moves; keep them pure `-none-any` wheels.
//
// streamlit-facade is appended to every widget/app's requirements by
// stliteBoot.ts (platform theming), so it must resolve locally or offline
// deploys regress. Its sole dep (streamlit>=1.35) is already the bundled wheel.
const PYPI_WHEELS = [
  { name: 'blinker', version: '1.9.0', file: 'blinker-1.9.0-py3-none-any.whl', imports: ['blinker'], sha256: 'ba0efaa9080b619ff2f3459d1d500c57bddea4a6b424b60a91141db6fd2f08bc', url: 'https://files.pythonhosted.org/packages/10/cb/f2ad4230dc2eb1a74edf38f1a38b9b52277f75bef262d8908e60d957e13c/blinker-1.9.0-py3-none-any.whl' },
  { name: 'itsdangerous', version: '2.2.0', file: 'itsdangerous-2.2.0-py3-none-any.whl', imports: ['itsdangerous'], sha256: 'c6242fc49e35958c8b15141343aa660db5fc54d4f13a1db01a3f5891b98700ef', url: 'https://files.pythonhosted.org/packages/04/96/92447566d16df59b2a776c0fb82dbc4d9e07cd95062562af01e408583fc4/itsdangerous-2.2.0-py3-none-any.whl' },
  { name: 'python-multipart', version: '0.0.32', file: 'python_multipart-0.0.32-py3-none-any.whl', imports: ['multipart', 'python_multipart'], sha256: 'ff6d3f776f16878c894e52e107296ffc890e913c611b1a4ec6c44e2821fe2e23', url: 'https://files.pythonhosted.org/packages/e1/04/e8135ebd1ad02c56ec633277529b2602ff99ff634be76cdba5744cf554fd/python_multipart-0.0.32-py3-none-any.whl' },
  { name: 'tenacity', version: '9.1.4', file: 'tenacity-9.1.4-py3-none-any.whl', imports: ['tenacity'], sha256: '6095a360c919085f28c6527de529e76a06ad89b23659fa881ae0649b867a9d55', url: 'https://files.pythonhosted.org/packages/d7/c1/eb8f9debc45d3b7918a32ab756658a0904732f75e555402972246b0b8e71/tenacity-9.1.4-py3-none-any.whl' },
  { name: 'protobuf', version: '7.35.1', file: 'protobuf-7.35.1-py3-none-any.whl', imports: ['google'], sha256: '4bc97768d8fe4ad6743c8a19403e314511ed9f6d13205b687e52421c023ac1b9', url: 'https://files.pythonhosted.org/packages/19/c7/5f7c636ec43e0c545e28d1f1db71990108306f7bdcb89f069ba97e428e7f/protobuf-7.35.1-py3-none-any.whl' },
  { name: 'streamlit-facade', version: '0.1.6', file: 'streamlit_facade-0.1.6-py3-none-any.whl', imports: ['facade'], sha256: 'd868beba2a8af03af0c779c969d09c6f2127613806f13e19dd588fe4a609b2c3', url: 'https://files.pythonhosted.org/packages/a9/81/1f888ec80560acfbf4bf12d5caf2733eacee423d1f33a4f3e85c836980f7/streamlit_facade-0.1.6-py3-none-any.whl' },
]

// Sentinel so repeated `npm run build` / `npm install` don't re-download ~55 MB.
// Bumping PYODIDE_VERSION or a pinned wheel changes this fingerprint.
const FINGERPRINT = JSON.stringify({
  v: PYODIDE_VERSION,
  pypi: PYPI_WHEELS.map((w) => `${w.name}==${w.version}`),
  roots: BOOT_ROOTS,
  core: CORE_SHA256,
})
const STAMP = path.join(OUT, '.vendor-stamp.json')

const norm = (n) => n.toLowerCase().replace(/[-_.]+/g, '-')

async function fetchBuffer(url, expectSha256) {
  const res = await fetch(url)
  if (!res.ok) throw new Error(`GET ${url} → HTTP ${res.status}`)
  const buf = Buffer.from(await res.arrayBuffer())
  if (expectSha256) {
    const got = createHash('sha256').update(buf).digest('hex')
    if (got !== expectSha256) {
      throw new Error(`sha256 mismatch for ${url}\n  expected ${expectSha256}\n  got      ${got}`)
    }
  }
  return buf
}

async function alreadyVendored() {
  try {
    return (await readFile(STAMP, 'utf8')) === FINGERPRINT
  } catch {
    return false
  }
}

/**
 * Transitive closure of BOOT_ROOTS over the lockfile's `depends` edges.
 * Returns the set of ORIGINAL lockfile keys to keep — pyodide keys packages by
 * their PEP 503 normalized name (`pillow`, `markupsafe`), which is what micropip
 * looks up; the entry's `name` field is display-cased (`Pillow`, `MarkupSafe`)
 * and must NOT be used as the key or micropip won't find it.
 */
function computeClosure(lock) {
  const idx = {} // normalized name -> original lockfile key
  for (const k of Object.keys(lock.packages)) idx[norm(k)] = k
  const overridden = new Set(PYPI_WHEELS.map((w) => norm(w.name)))
  const keep = new Set() // original lockfile keys
  const stack = [...BOOT_ROOTS.map(norm)]
  const seen = new Set()
  while (stack.length) {
    const n = stack.pop()
    if (seen.has(n) || overridden.has(n)) continue // PyPI-pinned ones injected separately
    seen.add(n)
    const key = idx[n]
    if (!key) continue // not in lockfile → micropip resolves it from PyPI at runtime
    keep.add(key)
    for (const d of lock.packages[key].depends || []) stack.push(norm(d))
  }
  return keep
}

async function main() {
  if (await alreadyVendored()) {
    console.log(`[fetch-pyodide] public/pyodide already at v${PYODIDE_VERSION} — skipping.`)
    return
  }
  console.log(`[fetch-pyodide] vendoring Pyodide v${PYODIDE_VERSION} runtime + boot wheels…`)
  await rm(OUT, { recursive: true, force: true })
  await mkdir(OUT, { recursive: true })

  // 1. Core runtime.
  for (const f of CORE_FILES) {
    const buf = await fetchBuffer(`${CDN}/${f}`, CORE_SHA256[f])
    await writeFile(path.join(OUT, f), buf)
    console.log(`  core  ${f}  ${(buf.length / 1e6).toFixed(1)} MB`)
  }

  // 2. Full lockfile → closure (set of original, normalized lockfile keys).
  // Verified against CORE_SHA256 BEFORE it's parsed/trusted — every wheel
  // below is only as trustworthy as this fetch, since wheel sha256s come
  // FROM this same file (the circularity CORE_SHA256 closes).
  const lock = JSON.parse(
    await fetchBuffer(`${CDN}/pyodide-lock.json`, CORE_SHA256['pyodide-lock.json']).then((b) =>
      b.toString('utf8'),
    ),
  )
  const keep = computeClosure(lock)

  // 3. Closure wheels from the CDN (sha256-verified against the lockfile),
  //    preserving each package's original lockfile key.
  const trimmed = {}
  let bytes = 0
  for (const key of keep) {
    const p = lock.packages[key]
    const buf = await fetchBuffer(`${CDN}/${p.file_name}`, p.sha256)
    await writeFile(path.join(OUT, p.file_name), buf)
    trimmed[key] = p
    bytes += buf.length
  }
  console.log(`  closure  ${keep.size} wheels  ${(bytes / 1e6).toFixed(1)} MB`)

  // 4. PyPI residuals + injected lockfile entries, keyed by normalized name
  //    (protobuf REPLACES the stale 6.31.1 lockfile entry so the forced
  //    >=7.34.1 resolves locally).
  for (const w of PYPI_WHEELS) {
    const buf = await fetchBuffer(w.url, w.sha256)
    await writeFile(path.join(OUT, w.file), buf)
    trimmed[norm(w.name)] = {
      name: w.name,
      version: w.version,
      file_name: w.file,
      install_dir: 'site',
      sha256: w.sha256,
      package_type: 'package',
      imports: w.imports,
      depends: [],
      unvendored_tests: false,
    }
    console.log(`  pypi  ${w.file}`)
  }

  // 5. Trimmed lockfile — same info block, only vendored packages.
  await writeFile(
    path.join(OUT, 'pyodide-lock.json'),
    JSON.stringify({ info: lock.info, packages: trimmed }),
  )
  await writeFile(STAMP, FINGERPRINT)
  console.log(`[fetch-pyodide] done — ${Object.keys(trimmed).length} packages in trimmed lockfile.`)
}

main().catch((err) => {
  console.error(`[fetch-pyodide] FAILED: ${err.message}`)
  process.exit(1)
})
