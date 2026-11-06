/**
 * 2026-10-24 v3.2 环境净化专项测试：假 ctx 捕获 callBridge 的 spawn env，
 * 断言 Python 注入型变量被剔除、保留变量与标记正常。不碰 DSH、不碰真库。
 */
import os from 'node:os'
import path from 'node:path'
import assert from 'node:assert'

// 先投喂毒变量（含大小写变体，模拟 Windows 环境键不区分大小写）
process.env.PYTHONPATH = 'C:\\poison1'
process.env.PythonHome = 'C:\\poison2'
process.env.PYTHONSTARTUP = 'poison3.py'
process.env.PYTHONUSERBASE = 'C:\\poison4'
process.env.HOME = 'keep-me'
process.env.CONDA_PREFIX = 'keep-me-too'

const mod = await import('./boshi-auto-memory.v3.1.mjs')
const plugin = mod.default

let spec = null
const ctx = {
  subprocess: {
    spawn: (s) => {
      spec = s
      return {
        collected: { stdout: { readFrom: () => ({ text: '{}', nextOffset: 2, lossy: false }) } },
        done: Promise.resolve({ exitCode: 0, signal: null }),
      }
    },
  },
  systemPrompt: { section: () => () => {} },
  get: () => undefined,
  on: () => () => {},
}

plugin.apply(ctx, {
  python: 'FAKE_PY', bridge: 'FAKE_BRIDGE', cwd: os.tmpdir(),
  logFile: path.join(os.tmpdir(), 'boshi-env-test.jsonl'),
  spoolFile: path.join(os.tmpdir(), 'boshi-env-test-spool.jsonl'),
  captureAssistant: false,
})
// plugin-start 会刷一次画像 → 同步触发一次 spawn
assert.ok(spec, 'spawn should have been called at plugin start')
const env = spec.env

const keys = Object.keys(env)
const stillThere = keys.filter((k) => /^PYTHON(PATH|HOME|STARTUP|USERBASE)$/i.test(k))
assert.deepStrictEqual(stillThere, [], 'all python-injected vars must be dropped: ' + String(stillThere))
assert.strictEqual(env.PYTHONNOUSERSITE, '1')
assert.strictEqual(env.PYTHONIOENCODING, 'utf-8')
assert.strictEqual(env.BOSHI_PROFILE, 'dsh')
assert.strictEqual(env.BOSHI_BACKEND, 'pg')
assert.strictEqual(env.HOME, 'keep-me')
assert.strictEqual(env.CONDA_PREFIX, 'keep-me-too')
console.log('ENV-SANITIZE OK · dropped PYTHON* (incl. case variants), kept non-python vars, markers set')
