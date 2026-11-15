#!/usr/bin/env node
// One-time setup: Python 3.11 venv + backend deps (via uv if present, else python -m venv), frontend deps.
import { execSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '..')
const backend = join(root, 'backend')
const win = process.platform === 'win32'
const run = (cmd, cwd = root) => {
  console.log(`> ${cmd}`)
  execSync(cmd, { cwd, stdio: 'inherit' })
}
const has = (cmd) => {
  try {
    execSync(cmd, { stdio: 'ignore' })
    return true
  } catch {
    return false
  }
}

const venvPy = join(backend, '.venv', win ? 'Scripts/python.exe' : 'bin/python')
const uv = has('uv --version') ? 'uv' : has('python -m uv --version') ? 'python -m uv' : null
if (!existsSync(venvPy)) {
  if (uv) run(`${uv} venv --python 3.11 .venv`, backend)
  else run(`${win ? 'py -3.11' : 'python3.11'} -m venv .venv`, backend)
}
if (uv) run(`${uv} pip install --python "${venvPy}" -r requirements.txt`, backend)
else run(`"${venvPy}" -m pip install -r requirements.txt`, backend)
run('npm install', join(root, 'frontend'))
console.log('\nDone. Start everything with: npm run dev')
