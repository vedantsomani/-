#!/usr/bin/env node
// Run the backend venv's python with the given args, from backend/ (cross-platform).
import { spawnSync } from 'node:child_process'
import { join, resolve } from 'node:path'

const backend = join(resolve(import.meta.dirname, '..'), 'backend')
const py = join(backend, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python')
const r = spawnSync(py, process.argv.slice(2), { cwd: backend, stdio: 'inherit' })
process.exit(r.status ?? 1)
