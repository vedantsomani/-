#!/usr/bin/env node
// Run backend (uvicorn, :8000) and frontend (vite, :5173) together. Ctrl+C stops both.
//
// The backend is restarted by THIS script when a .py file under backend/app changes,
// instead of `uvicorn --reload`: on Windows uvicorn's reloader stops its worker with a
// console CTRL_C event, which never arrives when the process has no console (as when
// spawned from Node), so reloads hang forever.
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, watch } from 'node:fs'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const win = process.platform === 'win32'
const py = join(root, 'backend', '.venv', win ? 'Scripts/python.exe' : 'bin/python')

if (!existsSync(py)) {
  console.error(`No backend venv at ${py}\nRun: npm run setup   (see README)`)
  process.exit(1)
}
if (!existsSync(join(root, 'frontend', 'node_modules'))) {
  console.error('Frontend deps missing. Run: npm run setup')
  process.exit(1)
}

const API = {
  name: 'api',
  color: 36,
  cmd: py,
  args: ['-m', 'uvicorn', 'app.main:app', '--port', '8000'],
  cwd: join(root, 'backend'),
}
const WEB = { name: 'web', color: 35, cmd: 'npm', args: ['run', 'dev'], cwd: join(root, 'frontend'), shell: win }

let stopping = false
const children = new Map()

function prefixPipe(stream, out, prefix) {
  let buf = ''
  stream.on('data', (d) => {
    buf += d
    const lines = buf.split(/\r?\n/)
    buf = lines.pop()
    for (const l of lines) out.write(prefix + l + '\n')
  })
}

function start(p) {
  const child = spawn(p.cmd, p.args, { cwd: p.cwd, shell: p.shell ?? false, env: process.env })
  const prefix = `\x1b[${p.color}m[${p.name}]\x1b[0m `
  prefixPipe(child.stdout, process.stdout, prefix)
  prefixPipe(child.stderr, process.stderr, prefix)
  child.on('exit', (code) => {
    if (children.get(p.name) !== child) return // replaced by a restart
    console.log(`${prefix}exited (${code})`)
    if (!stopping && p !== API) shutdown(code ?? 0)
  })
  children.set(p.name, child)
  return child
}

function kill(child) {
  if (!child || child.exitCode !== null) return
  if (win) spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' })
  else child.kill('SIGTERM')
}

start(API)
start(WEB)

// backend auto-restart
let timer = null
watch(join(root, 'backend', 'app'), { recursive: true }, (_event, file) => {
  if (!file || !String(file).endsWith('.py')) return
  clearTimeout(timer)
  timer = setTimeout(() => {
    console.log(`\x1b[36m[api]\x1b[0m ${file} changed - restarting`)
    const old = children.get('api')
    children.delete('api')
    kill(old)
    start(API)
  }, 300)
})

function shutdown(code) {
  if (stopping) return
  stopping = true
  for (const c of children.values()) kill(c)
  setTimeout(() => process.exit(code), 500)
}
process.on('SIGINT', () => shutdown(0))
process.on('SIGTERM', () => shutdown(0))
