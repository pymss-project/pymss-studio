import { readFile, writeFile, mkdir, mkdtemp, rm } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { build } from 'vite'
import vue from '@vitejs/plugin-vue'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const check = process.argv.includes('--check')
const studio = JSON.parse(execFileSync('python', ['-X', 'utf8', '-c',
  'import json,sys,tomllib; from pathlib import Path; print(json.dumps(tomllib.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["tool"]["pymss"]["studio"]))',
  path.join(root, 'pyproject.toml'),
], { encoding: 'utf8' }))
if (!Number.isInteger(studio.sdk_version) || studio.sdk_version < 1) throw new Error('Studio sdk_version must be a positive integer')
if (!Array.isArray(studio.required_capabilities) || !studio.required_capabilities.every(value => typeof value === 'string' && /^[a-z][A-Za-z0-9]*(?:\.[a-z][A-Za-z0-9]*)+$/.test(value))) throw new Error('Studio required_capabilities must contain exact SDK capability names')
const english = JSON.parse(await readFile(path.join(root, 'ui/src/locales/en.json'), 'utf8'))
if (!english || typeof english !== 'object' || Array.isArray(english)) throw new Error('ui/src/locales/en.json must contain an English message object')
const temporary = await mkdtemp(path.join(os.tmpdir(), 'audio-gain-ui-'))
try {
  await build({
    configFile: false, root, logLevel: 'warn', plugins: [vue()],
    define: {
      __STUDIO_REQUIREMENTS__: JSON.stringify({ sdkVersion: studio.sdk_version, requiredCapabilities: studio.required_capabilities }),
      'process.env.NODE_ENV': JSON.stringify('production'), __VUE_OPTIONS_API__: true, __VUE_PROD_DEVTOOLS__: false, __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: false,
    },
    build: {
      outDir: temporary, emptyOutDir: true, cssCodeSplit: false,
      license: { fileName: 'THIRD-PARTY-NOTICES.txt' },
      lib: { entry: path.join(root, 'ui/src/main.ts'), name: 'AudioGain', formats: ['iife'], fileName: () => 'tool.js', cssFileName: 'tool' },
      rollupOptions: { output: { inlineDynamicImports: true } },
    },
  })
  const javascript = (await readFile(path.join(temporary, 'tool.js'), 'utf8')).replace(/<\/script/gi, '<\\/script')
  const css = await readFile(path.join(temporary, 'tool.css'), 'utf8')
  const html = `<!doctype html>\n<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>${css}</style></head><body><div id="app"></div><script src="../../../sdk/pymss-sdk.js"></script><script>${javascript}</script></body></html>\n`
  const output = path.join(root, 'ui/dist')
  const notices = await readFile(path.join(temporary, 'THIRD-PARTY-NOTICES.txt'), 'utf8')
  if (check) {
    if (await readFile(path.join(output, 'index.html'), 'utf8') !== html || await readFile(path.join(root, 'THIRD-PARTY-NOTICES.txt'), 'utf8') !== notices) throw new Error('Built UI is stale; run pnpm build')
  } else {
    await mkdir(output, { recursive: true })
    await writeFile(path.join(output, 'index.html'), html)
    await writeFile(path.join(root, 'THIRD-PARTY-NOTICES.txt'), notices)
  }
  process.stdout.write(`${check ? 'Checked' : 'Built'} audio gain UI: ${Math.round(Buffer.byteLength(html) / 1024)} KiB\n`)
} finally { await rm(temporary, { recursive: true, force: true }) }
