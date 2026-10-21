/**
 * v3 插件离线测试：用假 ctx 驱动，不碰 DSH、不碰真记忆库。
 * argv 形状：[python, bridge, <命令>, ...参数] —— 命令在 argv[2]。
 * 覆盖：用户写入 / 短消息过滤 / 助手结论归档 / 失败落 spool / 恢复后补投。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import assert from 'node:assert'

const TMP = path.join(os.tmpdir(), 'boshi-v3-test')
fs.rmSync(TMP, { recursive: true, force: true })
fs.mkdirSync(TMP, { recursive: true })

const LOG = path.join(TMP, 'dsh-plugin.jsonl')
const SPOOL = path.join(TMP, 'dsh-plugin-spool.jsonl')

const mod = await import('./boshi-auto-memory.v3.mjs')
const plugin = mod.default

// ---------- 假 ctx ----------
const handlers = new Map()
const sections = []
const specs = []
let failSaves = false

const cmd = (s) => s.argv[2]
const args = (s) => s.argv.slice(3)

function makeHandle(spec) {
  specs.push(spec)
  const c = cmd(spec)
  let payload
  if (c === 'save' && failSaves) payload = { error: 'simulated backend down' }
  else if (c === 'save') payload = { id: 'mem-' + specs.length, ok: true }
  else if (c === 'profile') payload = { total_memories: 1234, hot_topic: 't', recent_memories: [] }
  else if (c === 'search') payload = { query: args(spec)[0], total: 0, results: [] }
  else if (c === 'time_range') payload = []
  else payload = {}
  const text = JSON.stringify(payload)
  return {
    collected: { stdout: { readFrom: () => ({ text, nextOffset: text.length, lossy: false }) } },
    done: Promise.resolve({ exitCode: 0, signal: null }),
    terminate() {},
    waitForExit: async () => true,
  }
}

const ctx = {
  subprocess: { spawn: (spec) => makeHandle(spec) },
  systemPrompt: { section: (s) => { sections.push(s); return () => {} } },
  get: () => undefined, // agents 服务取不到 → isRootAgent 不过滤
  on: (name, fn) => { handlers.set(name, fn); return () => handlers.delete(name) },
}

const dispose = plugin.apply(ctx, {
  python: 'FAKE_PY', bridge: 'FAKE_BRIDGE', cwd: TMP,
  logFile: LOG, spoolFile: SPOOL, captureAssistant: true,
})

const tick = () => new Promise((r) => setTimeout(r, 5))
await tick()

const readLog = () => fs.readFileSync(LOG, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l))
const readSpool = () => {
  try { return fs.readFileSync(SPOOL, 'utf8').trim().split('\n').filter(Boolean).map((l) => JSON.parse(l)) } catch { return [] }
}
const saves = (topic) => specs.filter((s) => cmd(s) === 'save' && args(s)[1] === topic)
const user = (text, kind = 'user') => handlers.get('agent/inbox/claimed')({
  agent: { id: 'S1' }, turn: 1,
  message: { role: 'user', source: { kind }, content: [{ type: 'text', text }] },
})

let pass = 0
const check = (name, fn) => {
  try { fn(); console.log('  ✅ ' + name); pass++ }
  catch (e) { console.log('  ❌ ' + name + '  → ' + String(e.message).split('\n')[0]) }
}

console.log('== 1. 启动 ==')
check('注册了 3 个 systemPrompt section', () => assert.strictEqual(sections.length, 3))
check('inbox/claimed 已监听', () => assert.ok(handlers.has('agent/inbox/claimed')))
check('assistant-stream 已监听', () => assert.ok(handlers.has('agent/assistant-stream')))
check('turn-stopping 已监听', () => assert.ok(handlers.has('agent/turn-stopping')))
check('plugin-start 已落日志', () => assert.ok(readLog().some((r) => r.op === 'plugin-start')))
check('启动时刷了一次画像', () => assert.ok(specs.some((s) => cmd(s) === 'profile')))

console.log('== 2. env 注入（id 与后端） ==')
check('每条 spawn 都带 BOSHI_PROFILE=dsh', () => assert.ok(specs.every((s) => s.env.BOSHI_PROFILE === 'dsh')))
check('每条 spawn 都带 BOSHI_BACKEND=pg', () => assert.ok(specs.every((s) => s.env.BOSHI_BACKEND === 'pg')))
check('展开了 process.env（继承宿主环境）', () => assert.ok(Object.keys(specs[0].env).some((k) => k.toLowerCase() === 'path'), '未见 path 键'))
check('PYTHONIOENCODING=utf-8', () => assert.strictEqual(specs[0].env.PYTHONIOENCODING, 'utf-8'))

console.log('== 3. 用户消息写入 ==')
user('你好世界，这是一条正常消息')
await tick()
check('写入 topic=conversation', () => assert.strictEqual(saves('conversation').length, 1))
check('内容原样传入', () => assert.strictEqual(args(saves('conversation')[0])[0], '你好世界，这是一条正常消息'))
check('save 成功已落日志(ok=true)', () => assert.ok(readLog().some((r) => r.op === 'save' && r.ok === true)))
check('召回走了 search', () => assert.ok(readLog().some((r) => r.op === 'recall' && r.mode === 'search')))

console.log('== 4. 过滤（可配：minChars=2 / skipSymbolOnly=true） ==')
user('嗯'); user('？？？'); user('继续')
await tick()
check('1 字符「嗯」被跳过', () => assert.ok(!saves('conversation').some((s) => args(s)[0] === '嗯')))
check('2 字符「继续」已收录（v2 会丢）', () => assert.ok(saves('conversation').some((s) => args(s)[0] === '继续')))
check('纯标点「？？？」被跳过', () => assert.ok(!saves('conversation').some((s) => args(s)[0] === '？？？')))
check('skip 已落日志', () => assert.ok(readLog().filter((r) => r.op === 'skip').length >= 2))

console.log('== 5. 非用户来源不入库 ==')
const n5 = saves('conversation').length
user('运行时上下文不应入库', 'runtime-context')
await tick()
check('runtime-context 被排除', () => assert.strictEqual(saves('conversation').length, n5))

console.log('== 6. 助手半边：assistant_conclusion ==')
const stream = handlers.get('agent/assistant-stream')
stream({ agent: { id: 'S1' }, frame: { type: 'start', attemptId: 'A1', revision: 1, turn: 6, step: 1 } })
stream({ agent: { id: 'S1' }, frame: { type: 'chunk', attemptId: 'A1', revision: 1, index: 0, time: 0, chunk: { type: 'reasoning-delta', index: 0, text: '【思考过程，不应入库】' } } })
stream({ agent: { id: 'S1' }, frame: { type: 'chunk', attemptId: 'A1', revision: 1, index: 1, time: 0, chunk: { type: 'text-delta', index: 1, text: '这是结论的第一段。' } } })
stream({ agent: { id: 'S1' }, frame: { type: 'chunk', attemptId: 'A1', revision: 1, index: 1, time: 0, chunk: { type: 'text-delta', index: 1, text: '这是结论的第二段，足够长以满足最小长度要求。' } } })
stream({ agent: { id: 'S1' }, frame: { type: 'end', attemptId: 'A1', revision: 1, index: 1, outcome: { kind: 'committed', eventType: 'assistant/message', seq: 42 } } })
handlers.get('agent/turn-stopping')({ agent: { id: 'S1' }, turn: 6 })
await tick()
check('助手结论已归档', () => assert.strictEqual(saves('assistant_conclusion').length, 1))
check('内容是正文而非 reasoning', () => {
  const c = args(saves('assistant_conclusion')[0])[0]
  assert.ok(c.includes('结论的第一段'), '缺第一段')
  assert.ok(c.includes('结论的第二段'), '缺第二段')
  assert.ok(!c.includes('思考过程'), 'reasoning 泄漏进来了')
})

console.log('== 7. abandoned / assistant-attempt 不入库 ==')
const n7 = saves('assistant_conclusion').length
stream({ agent: { id: 'S1' }, frame: { type: 'start', attemptId: 'A2', revision: 1, turn: 7, step: 1 } })
stream({ agent: { id: 'S1' }, frame: { type: 'chunk', attemptId: 'A2', revision: 1, index: 0, time: 0, chunk: { type: 'text-delta', index: 0, text: '这段应被丢弃，因为 outcome 是 abandoned。' } } })
stream({ agent: { id: 'S1' }, frame: { type: 'end', attemptId: 'A2', revision: 1, index: 0, outcome: { kind: 'abandoned' } } })
handlers.get('agent/turn-stopping')({ agent: { id: 'S1' }, turn: 7 })
stream({ agent: { id: 'S1' }, frame: { type: 'start', attemptId: 'A3', revision: 2, turn: 8, step: 1 } })
stream({ agent: { id: 'S1' }, frame: { type: 'chunk', attemptId: 'A3', revision: 2, index: 0, time: 0, chunk: { type: 'text-delta', index: 0, text: '这是重试中的半成品，assistant/attempt，不应归档。' } } })
stream({ agent: { id: 'S1' }, frame: { type: 'end', attemptId: 'A3', revision: 2, index: 0, outcome: { kind: 'committed', eventType: 'assistant/attempt', seq: 43 } } })
handlers.get('agent/turn-stopping')({ agent: { id: 'S1' }, turn: 8 })
await tick()
check('abandoned / attempt 均不入库', () => assert.strictEqual(saves('assistant_conclusion').length, n7))

console.log('== 8. 失败落 spool（不再静默丢弃） ==')
failSaves = true
const n8 = saves('conversation').length
user('这条应该进补投队列')
await tick(); await tick()
check('save 真的被调用了', () => assert.strictEqual(saves('conversation').length, n8 + 1))
check('spool 里有这条', () => assert.ok(readSpool().some((r) => r.content === '这条应该进补投队列')))
check('失败已落日志(ok=false)', () => assert.ok(readLog().some((r) => r.op === 'save' && r.ok === false)))

console.log('== 9. 后端恢复后自动补投 ==')
failSaves = false
user('后端恢复了')
await tick(); await tick(); await tick()
check('补投队列已清空', () => assert.strictEqual(readSpool().length, 0))
check('spool-flush 已落日志', () => assert.ok(readLog().some((r) => r.op === 'spool-flush' && r.flushed >= 1)))

console.log('== 10. 卸载 ==')
check('disposer 可执行', () => { dispose() })

console.log('\n结果：' + pass + ' 项通过')
if (pass < 28) { console.log('⚠️ 有未通过项，先别部署'); process.exitCode = 1 }
