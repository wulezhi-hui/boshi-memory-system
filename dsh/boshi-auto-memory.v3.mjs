/**
 * 伯仕记忆系统 — DSH 自动记忆插件（v3 · dsh 侧增强）
 * ==================================================
 * 上游基线：~/.boshi/dsh/boshi-auto-memory.mjs（v2，2026-09-29）
 * 本版由 dsh 侧维护；改动记录见 ~/.boshi/docs/伯仕工作日志.md
 * 与 ~/.boshi/docs/伯仕记忆系统v7.0_技术架构文档.md
 *
 * 职责（四件套）：
 *   1. sync_turn   每轮把用户消息存入伯仕
 *   2. prefetch    每轮异步召回，注入后续步骤的系统提示词
 *   3. 画像注入    用户画像/热区注入
 *   4. 结论归档    每轮助手最终正文存为 assistant_conclusion（v3 新增）
 *
 * v3 相对 v2 的四项加固：
 *   A. 写入不再"发射后不管"：校验 bridge 返回，失败落本地 spool，后端恢复后自动补投
 *   B. 写入/召回全程落本地日志（~/.boshi/logs/dsh-plugin.jsonl），漏写可查
 *   C. 补上"助手半边"：assistant-stream 累积正文，回合收口时存 assistant_conclusion
 *   D. 过滤可配：minChars / skipSymbolOnly，不再硬编码"≤2 字符丢弃"
 *
 * 依赖：~/.boshi/boshi_bridge.py（输出单行 JSON 的桥接层）。
 *
 * 配置（cordis.patch.yml 的 insert 条目里 config:）—— 全部可选，省略即用默认值：
 *   python / bridge / cwd     路径
 *   profile                   归属 id，默认 'dsh'
 *   backend                   存储后端，默认 'pg'
 *   minChars                  用户消息最短长度（小于则跳过），默认 2
 *   skipSymbolOnly            纯标点/符号消息是否跳过，默认 true
 *   captureAssistant          是否归档助手结论，默认 true
 *   assistantMinChars         助手结论最短长度，默认 10
 *   assistantMaxChars         助手结论截断上限，默认 2000
 *   assistantScope            'root'（仅顶层会话，默认）| 'all'
 *   logFile / spoolFile       日志 / 补投文件路径（默认 <cwd>/logs/）
 *   spoolMax                  补投队列上限，默认 500
 *   flushPerTurn              每轮最多补投条数，默认 3
 */
import fs from 'node:fs'
import path from 'node:path'

export default {
  inject: ['subprocess', 'systemPrompt'],
  apply(ctx, config = {}) {
    const subprocess = ctx.subprocess
    const systemPrompt = ctx.systemPrompt
    // 可选服务：用于把"助手半边"限制在顶层会话，避免收录子代理的中间输出。
    // 用 ctx.get 而非 inject —— 取不到时优雅退化为"不过滤"，不影响插件激活。
    const agents = typeof ctx.get === 'function' ? ctx.get('agents') : undefined

    // ---------------- 配置 ----------------
    const PYTHON = config.python || 'C:/Users/wulezhi/.boshi/venv/Scripts/python.exe'
    const BRIDGE = config.bridge || 'C:/Users/wulezhi/.boshi/boshi_bridge.py'
    const CWD = config.cwd || 'C:/Users/wulezhi/.boshi'
    const PROFILE = config.profile || 'dsh'
    const BACKEND = config.backend || 'pg'
    const MIN_CHARS = Number.isFinite(config.minChars) ? config.minChars : 2
    const SKIP_SYMBOL_ONLY = config.skipSymbolOnly !== false
    const CAPTURE_ASSISTANT = config.captureAssistant !== false
    const ASSISTANT_MIN = Number.isFinite(config.assistantMinChars) ? config.assistantMinChars : 10
    const ASSISTANT_MAX = Number.isFinite(config.assistantMaxChars) ? config.assistantMaxChars : 2000
    const ASSISTANT_SCOPE = config.assistantScope === 'all' ? 'all' : 'root'
    const LOG_FILE = config.logFile || path.join(CWD, 'logs', 'dsh-plugin.jsonl')
    const SPOOL_FILE = config.spoolFile || path.join(CWD, 'logs', 'dsh-plugin-spool.jsonl')
    const SPOOL_MAX = Number.isFinite(config.spoolMax) ? config.spoolMax : 500
    const FLUSH_PER_TURN = Number.isFinite(config.flushPerTurn) ? config.flushPerTurn : 3

    // ---------------- 本地日志 ----------------
    // 唯一目的：让"漏写"可被发现。任何异常都吞掉，绝不能因为写日志影响记忆主流程。
    function log(rec) {
      try {
        fs.appendFileSync(LOG_FILE, JSON.stringify({ ts: new Date().toISOString(), ...rec }) + '\n', 'utf8')
      } catch (e) { /* 日志失败绝不影响主流程 */ }
    }

    // ---------------- 补投队列（spool） ----------------
    // 常驻内存 + 每次变更落盘；避免"读-改-写"与异步回调交错时丢条目。
    let spoolCache = null
    let flushing = false
    let flushedThisTurn = false // 每回合最多补投一次，避免对着空队列反复 spawn
    let backendHealthy = null // null=未知 / true / false

    function spoolLoad() {
      if (spoolCache) return spoolCache
      try {
        spoolCache = fs.readFileSync(SPOOL_FILE, 'utf8')
          .split('\n')
          .filter((l) => l.trim())
          .map((l) => { try { return JSON.parse(l) } catch (e) { return null } })
          .filter(Boolean)
      } catch (e) {
        spoolCache = []
      }
      return spoolCache
    }

    function spoolPersist() {
      try {
        const rows = spoolLoad()
        fs.writeFileSync(SPOOL_FILE, rows.map((r) => JSON.stringify(r)).join('\n') + (rows.length ? '\n' : ''), 'utf8')
      } catch (e) {
        log({ op: 'spool-persist-fail', err: String(e) })
      }
    }

    function spoolPush(content, topic) {
      const rows = spoolLoad()
      rows.push({ content, topic, ts: Date.now() })
      if (rows.length > SPOOL_MAX) {
        const dropped = rows.splice(0, rows.length - SPOOL_MAX)
        log({ op: 'spool-drop', dropped: dropped.length })
      }
      spoolPersist()
    }

    // 补投：仅在后端上一次调用是成功的时候才尝试，避免对着挂掉的后端反复 spawn。
    function flushSpool() {
      if (flushing || flushedThisTurn || backendHealthy !== true) return
      const rows = spoolLoad()
      if (rows.length === 0) return
      flushedThisTurn = true
      flushing = true
      let done = 0
      const finish = (ok) => {
        if (done > 0) rows.splice(0, done)
        spoolPersist()
        flushing = false
        if (done > 0 || !ok) log({ op: 'spool-flush', flushed: done, ok, remaining: rows.length })
      }
      const next = () => {
        if (done >= rows.length || done >= FLUSH_PER_TURN) { finish(true); return }
        const row = rows[done]
        callBridge(['save', row.content, row.topic], (json) => {
          if (!(json && !json.error)) { backendHealthy = false; finish(false); return }
          done += 1
          next()
        })
      }
      next()
    }

    // ---------------- 内存缓存 ----------------
    let profileText = ''
    let recallText = ''
    const attemptText = new Map() // attemptId -> 累积正文
    const turnText = new Map()    // agentId  -> 本回合最近一步的正文

    // ---------------- 消息工具 ----------------
    function extractText(message) {
      if (!message || !Array.isArray(message.content)) return ''
      const parts = []
      for (const b of message.content) {
        if (b && b.type === 'text' && typeof b.text === 'string') parts.push(b.text)
      }
      return parts.join('\n').trim()
    }

    function isRealUser(message) {
      return message && message.role === 'user' && message.source && message.source.kind === 'user'
    }

    // 过滤零散输入：阈值为配置项；"纯标点/符号"判定为 Unicode 感知，不误伤中文
    function isTrivial(text) {
      if (!text) return true
      if (text.length < MIN_CHARS) return true
      if (SKIP_SYMBOL_ONLY && /^[\s\p{P}\p{S}]+$/u.test(text)) return true
      return false
    }

    // 来源黑名单：排除图谱自动提取的边碎片，其它来源全部放行（比白名单安全——新来源不会被误杀）
    function isGraphFragment(meta) {
      if (!meta) return false
      if (meta.type === 'relation') return true
      if (meta.source === 'auto_extract') return true
      return false
    }

    // 顶层会话判定（用于助手捕获过滤）
    function isRootAgent(agentId) {
      if (ASSISTANT_SCOPE === 'all') return true
      if (!agents || typeof agents.roots !== 'function') return true // 取不到就不过滤
      try {
        return agents.roots().some((a) => a && a.id === agentId)
      } catch (e) {
        return true
      }
    }

    // ---------------- bridge ----------------
    function callBridge(args, onResult) {
      try {
        const handle = subprocess.spawn({
          argv: [PYTHON, BRIDGE].concat(args),
          cwd: CWD,
          stdio: {
            stdin: 'ignore',
            stdout: { maxBytes: 200000 },
            stderr: 'ignore',
          },
          graceMs: 30000,
          env: { ...process.env, PYTHONIOENCODING: 'utf-8', BOSHI_PROFILE: PROFILE, BOSHI_BACKEND: BACKEND },
        })
        handle.done.then(() => {
          if (typeof onResult !== 'function') return
          const out = handle.collected.stdout
          if (!out) { onResult(null); return }
          const read = out.readFrom(0)
          try {
            onResult(JSON.parse(read.text.trim()))
          } catch (e) {
            onResult(null)
          }
        }).catch(() => {
          if (typeof onResult === 'function') onResult(null)
        })
      } catch (e) {
        log({ op: 'spawn-fail', err: String(e) })
        if (typeof onResult === 'function') onResult(null)
      }
    }

    // 带校验与补投的写入（v3 核心：失败不再静默丢弃）
    function saveMemory(content, topic) {
      callBridge(['save', content, topic], (json) => {
        const ok = !!(json && !json.error)
        backendHealthy = ok
        log({ op: 'save', topic, ok, chars: content.length, err: ok ? undefined : ((json && json.error) || 'no-result') })
        if (!ok) spoolPush(content, topic)
        else flushSpool() // 成功即说明后端健康：顺手把欠账补一轮
      })
    }

    // ---------------- 注入文本格式化 ----------------
    function formatProfile(json) {
      if (!json || json.error || typeof json.total_memories !== 'number') return ''
      let text = '记忆库 ' + json.total_memories + ' 条'
      if (json.hot_topic) text += '；热区「' + json.hot_topic + '」'
      const recent = json.recent_memories || []
      const items = []
      for (const m of recent) {
        if (m && m.content && !isGraphFragment(m.metadata)) items.push(String(m.content).slice(0, 80))
      }
      if (items.length > 0) text += '\n近期：' + items.map((x) => '\n- ' + x).join('')
      return text
    }

    function formatRecall(json) {
      if (!json || json.error) return ''
      const results = json.results || []
      if (results.length === 0) return ''
      const items = []
      for (const r of results) {
        if (!r || !r.content) continue
        if (isGraphFragment(r.metadata)) continue
        items.push(String(r.content).slice(0, 120))
      }
      // 搜 15 条 → 展示 5 条，避免注入量撑大提示词
      const shown = items.slice(0, 5)
      return shown.length > 0 ? '与当前话题相关的历史记忆：' + shown.map((x) => '\n- ' + x).join('') : ''
    }

    function formatTimeRange(json) {
      if (!json || json.error || !Array.isArray(json)) return ''
      const items = []
      for (const r of json) {
        if (!r || !r.content) continue
        if (isGraphFragment(r.metadata)) continue
        items.push(String(r.content).slice(0, 150))
      }
      // 时间线返回最多 50 条 → 展示 10 条
      const shown = items.slice(0, 10)
      return shown.length > 0 ? '时间线记忆（按写入时间降序）：' + shown.map((x) => '\n- ' + x).join('') : ''
    }

    function refreshProfile() {
      callBridge(['profile'], (json) => {
        const ok = !!(json && !json.error)
        if (ok) profileText = formatProfile(json)
        log({ op: 'profile', ok, chars: profileText.length })
        if (ok) {
          backendHealthy = true
          flushSpool() // 启动/会话开始时，顺手把上次留下的补投清掉
        }
      })
    }

    const disposers = []

    // 检测用户消息中的相对时间词，返回 Unix 时间戳范围 [since, until]
    function detectTimeWindow(text) {
      const now = Math.floor(Date.now() / 1000)
      const HOUR = 3600
      const DAY = 86400
      if (/刚才|刚刚|方才/.test(text)) return [now - HOUR, now]
      if (/上午|早上|早晨/.test(text)) {
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 8, 0, 0)
        const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12, 0, 0)
        return [Math.floor(start.getTime() / 1000), Math.floor(end.getTime() / 1000)]
      }
      if (/下午|傍晚/.test(text)) {
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12, 0, 0)
        const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 18, 0, 0)
        return [Math.floor(start.getTime() / 1000), Math.floor(end.getTime() / 1000)]
      }
      if (/晚上|夜里|半夜/.test(text)) {
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 18, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      if (/今天|今日/.test(text)) {
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      if (/昨天|昨日/.test(text)) {
        const nowD = new Date()
        const todayStart = new Date(nowD.getFullYear(), nowD.getMonth(), nowD.getDate(), 0, 0, 0)
        return [Math.floor((todayStart.getTime() - DAY) / 1000), Math.floor(todayStart.getTime() / 1000)]
      }
      if (/本周|这周|这星期/.test(text)) {
        const d = new Date()
        const dayOfWeek = d.getDay() || 7 // 周日=7
        const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - dayOfWeek + 1, 0, 0, 0)
        return [Math.floor(monday.getTime() / 1000), now]
      }
      if (/本月|这个月/.test(text)) {
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), 1, 0, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      return null
    }

    // 1) 每轮：先补投欠账 → 存用户消息 → 异步召回相关记忆
    disposers.push(ctx.on('agent/inbox/claimed', (payload) => {
      const message = payload && payload.message
      const agentId = payload && payload.agent && payload.agent.id
      if (agentId) turnText.delete(agentId) // 新回合：清掉上一回合残留的助手正文
      if (!isRealUser(message)) return
      const text = extractText(message)
      if (isTrivial(text)) {
        log({ op: 'skip', reason: 'trivial', chars: text.length })
        return
      }
      const content = text.length > 500 ? text.slice(0, 500) + '…' : text

      flushedThisTurn = false
      flushSpool()      // 清上次的欠账（后端健康时才动）
      saveMemory(content, 'conversation')

      // 时间窗口命中 → 走 time_range 时间线；否则走 search 语义检索
      const tw = detectTimeWindow(text)
      if (tw) {
        const bridgeArgs = ['time_range', String(tw[0])]
        if (tw[1]) bridgeArgs.push('--until=' + tw[1])
        bridgeArgs.push('--top-k=50')
        callBridge(bridgeArgs, (json) => {
          const ok = Array.isArray(json)
          if (ok) recallText = formatTimeRange(json)
          log({ op: 'recall', mode: 'time_range', ok, n: ok ? json.length : 0, injected: recallText.length })
        })
      } else {
        callBridge(['search', content, '15'], (json) => {
          const ok = !!(json && !json.error)
          if (ok) recallText = formatRecall(json)
          log({ op: 'recall', mode: 'search', ok, n: ok ? (json.results || []).length : 0, injected: recallText.length })
        })
      }
    }))

    // 2) 助手半边：累积流式正文，回合收口时归档为 assistant_conclusion
    if (CAPTURE_ASSISTANT) {
      disposers.push(ctx.on('agent/assistant-stream', (payload) => {
        const frame = payload && payload.frame
        const agentId = payload && payload.agent && payload.agent.id
        if (!frame || !agentId || !isRootAgent(agentId)) return
        if (frame.type === 'chunk') {
          const chunk = frame.chunk
          if (chunk && chunk.type === 'text-delta' && typeof chunk.text === 'string') {
            const prev = attemptText.get(frame.attemptId) || ''
            // 累积上限留一点余量，最终截断在归档时统一做
            if (prev.length < ASSISTANT_MAX + 500) attemptText.set(frame.attemptId, prev + chunk.text)
          }
          return
        }
        if (frame.type === 'end') {
          const text = (attemptText.get(frame.attemptId) || '').trim()
          attemptText.delete(frame.attemptId)
          const outcome = frame.outcome
          // 只收"已提交的正文消息"；assistant/attempt（重试中的半成品）与 abandoned 都不要
          if (outcome && outcome.kind === 'committed' && outcome.eventType === 'assistant/message' && text) {
            turnText.set(agentId, text) // 覆盖：同一回合多步时，最后一步的正文才是结论
          }
        }
      }))

      disposers.push(ctx.on('agent/turn-stopping', (payload) => {
        const agentId = payload && payload.agent && payload.agent.id
        if (!agentId) return
        const text = turnText.get(agentId)
        turnText.delete(agentId)
        if (!text) return
        if (text.length < ASSISTANT_MIN) {
          log({ op: 'skip', reason: 'assistant-too-short', chars: text.length })
          return
        }
        const content = text.length > ASSISTANT_MAX ? text.slice(0, ASSISTANT_MAX) + '…' : text
        saveMemory(content, 'assistant_conclusion')
      }))
    }

    // 3) 画像注入
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-profile',
      order: 40,
      text: () => profileText ? '## 伯仕记忆（用户画像）\n' + profileText : '',
    }))

    // 4) 召回注入
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-recall',
      order: 45,
      text: () => recallText ? '## 伯仕记忆（相关回忆）\n' + recallText : '',
    }))

    // 5) 记忆使用规则注入（让模型知道何时用什么工具查）
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-rules',
      order: 48,
      text: () => `## 伯仕记忆使用规则
- 问"今天/刚才/上午/下午/昨天/本周/本月干了啥" → 用 mcp__boshi__boshi_time_range(since, until, top_k) 查时间线
  - since/until 是 Unix 时间戳（秒）
  - 结果中 metadata.type=relation 或 metadata.source=auto_extract 的是图谱边碎片，跳过
  - 重点看 topic 为 conversation / assistant_conclusion 的条目（source 通常是 hermes_plugin / boshi_api / state_db_import_milestone）
- 问"XX相关的记忆/经验/之前怎么做的" → 用 mcp__boshi__boshi_search(query) 查语义
  - 结果中 metadata.type=relation 或 metadata.source=auto_extract 的是图谱边碎片，跳过
  - 重点看 topic 为 conversation / assistant_conclusion 的条目
- 问"最近的记忆" → 用 mcp__boshi__boshi_recent(n)
- 记忆库里 85% 是知识图谱自动提取的边（type=relation），真正的对话记忆只占 11%
  自动召回已按元数据过滤噪声，但手动查 boshi_time_range / boshi_search 返回结果里若仍有 auto_extract 碎片，跳过它们`,
    }))

    // 6) 会话开始时刷新画像
    disposers.push(ctx.on('agent/created', () => {
      refreshProfile()
    }))

    log({
      op: 'plugin-start',
      version: 'v3-dsh',
      profile: PROFILE,
      backend: BACKEND,
      minChars: MIN_CHARS,
      skipSymbolOnly: SKIP_SYMBOL_ONLY,
      captureAssistant: CAPTURE_ASSISTANT,
      assistantScope: ASSISTANT_SCOPE,
      logFile: LOG_FILE,
      spoolFile: SPOOL_FILE,
      pendingSpool: spoolLoad().length,
    })

    // 启动时刷新一次画像（顺带触发一次补投）
    refreshProfile()

    return () => {
      for (const d of disposers) { try { d() } catch (e) {} }
    }
  },
}
