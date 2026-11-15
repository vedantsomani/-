#!/usr/bin/env node
// Pre-build (download + process + stack) the demo fields so the live demo is instant.
// Runs `python -m app.prewarm` from the backend venv, once for the whole target list.
//
//   npm run prewarm                       all targets in scripts/demo-targets.txt, concurrently
//   npm run prewarm -- --sequential       one target at a time (gentler on RAM / IRSA)
//   npm run prewarm -- --fov 10           field of view in arcmin (default: backend default, 5)
//   npm run prewarm -- --file my.txt      another targets file
//   npm run prewarm -- --dry-run          print what would run, start nothing
//
// Results land in the backend cache (SPHEREX_CACHE_DIR, default backend/cache). To fill the
// DEPLOYED cache instead, run scripts/prewarm-demo.sh inside the Fly machine (docs/deploy.md).
import { spawnSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const backend = join(root, 'backend')
const py = join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')

let file = join(root, 'scripts', 'demo-targets.txt')
let fov = null
let sequential = false
let dryRun = false

const argv = process.argv.slice(2)
for (let i = 0; i < argv.length; i++) {
  const a = argv[i]
  if (a === '--sequential') sequential = true
  else if (a === '--dry-run') dryRun = true
  else if (a === '--fov') fov = argv[++i]
  else if (a === '--file') file = resolve(argv[++i] ?? '')
  else if (a === '-h' || a === '--help') {
    console.log(readFileSync(import.meta.filename, 'utf8').split('\n').slice(1, 12).map((l) => l.replace(/^\/\/ ?/, '')).join('\n'))
    process.exit(0)
  } else {
    console.error(`Unknown option: ${a} (try --help)`)
    process.exit(2)
  }
}
if (fov !== null && !(Number(fov) > 0)) {
  console.error(`--fov needs a positive number of arcmin, got ${JSON.stringify(fov)}`)
  process.exit(2)
}
if (!existsSync(file)) {
  console.error(`Targets file not found: ${file}`)
  process.exit(2)
}

const targets = readFileSync(file, 'utf8')
  .split(/\r?\n/)
  .map((l) => l.replace(/#.*/, '').trim())
  .filter(Boolean)
if (!targets.length) {
  console.error(`No targets in ${file}`)
  process.exit(2)
}

const fovArgs = fov === null ? [] : ['--fov', String(fov)]
const batches = sequential ? targets.map((t) => [t]) : [targets]

console.log(`${targets.length} target(s) from ${file}`)
console.log(`cache: ${process.env.SPHEREX_CACHE_DIR ?? join(backend, 'cache')}`)
for (const b of batches) console.log(`> python -m app.prewarm ${[...fovArgs, ...b.map((t) => JSON.stringify(t))].join(' ')}`)
if (dryRun) process.exit(0)

if (!existsSync(py)) {
  console.error(`No backend venv at ${py}\nRun: npm run setup`)
  process.exit(1)
}
let failed = 0
for (const b of batches) {
  const r = spawnSync(py, ['-m', 'app.prewarm', ...fovArgs, ...b], { cwd: backend, stdio: 'inherit' })
  if (r.status !== 0) {
    failed++
    console.error(`prewarm exited with ${r.status ?? r.signal} for: ${b.join(' | ')}`)
  }
}
process.exit(failed ? 1 : 0)
