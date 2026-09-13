/**
 * 伯仕记忆系统 — DSH 自动记忆插件
 * =================================
 * 对标 Hermes 插件模式（MemoryProvider）的三件套，供 DSH 持久化加载：
 *   1. sync_turn   每轮把用户消息异步存入伯仕
 *   2. prefetch    每轮异步召回相关记忆，注入下一轮系统提示词
 *   3. 画像注入    把用户画像/热区注入系统提示词
 *
 * 依赖：~/.boshi/boshi_bridge.py（输出 JSON 的桥接层）。
 *
 * 用法（DSH 的 cordis.patch.yml）：
 *   - insert:
 *       - id: boshi-auto-memory
 *         name: './plugins/boshi-auto-memory.mjs'
 *         config:
 *           python: '<venv python 路径>'
 *           bridge: '<~/.boshi/boshi_bridge.py 路径>'
 *           cwd: '<~/.boshi 路径>'
 */
export default {
  inject: ['subprocess', 'systemPrompt'],
  apply(ctx, config = {}) {
    const subprocess = ctx.subprocess
    const systemPrompt = ctx.systemPrompt

    const PYTHON = config.python || 'C:/Users/wulezhi/.boshi/venv/Scripts/python.exe'
    const BRIDGE = config.bridge || 'C:/Users/wulezhi/.boshi/boshi_bridge.py'
    const CWD = config.cwd || 'C:/Users/wulezhi/.boshi'

    // 内存缓存（画像 / 召回），section 的 text provider 同步读它
    let profileText = ''
    let recallText = ''

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

    // 过滤零散输入：≤2 字符，或纯标点/符号/空白（Unicode 感知，不误伤中文）
    function isTrivial(text) {
      if (!text) return true
      if (text.length <= 2) return true
      if (/^[\s\p{P}\p{S}]+$/u.test(text)) return true
      return false
    }

    function isGraphNoise(s) {
      // 图谱边形态：实体A --[关系]--> 实体B（双连字符+方括号+双连字符+箭头）
      // 精确匹配，不误伤 [2026-07-04]、[Intro]、[Verse] 等方括号开头的正常记忆
      return typeof s === 'string' && s.includes('--[') && s.includes('-->')
    }

    // 来源黑名单：排除图谱自动提取的边碎片，其它来源全部放行
    // 比白名单安全——新来源不会被误杀
    function isGraphFragment(meta) {
      if (!meta) return false
      // type=relation 的图谱边（metadata.type === 'relation'）
      if (meta.type === 'relation') return true
      // source=auto_extract 的图谱自动提边
      if (meta.source === 'auto_extract') return true
      return false
    }

    // 异步调用 bridge，解析 JSON，回调 onResult
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
          env: { PYTHONIOENCODING: 'utf-8' },
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
        console.error('boshi bridge spawn failed:', String(e))
      }
    }

    function formatProfile(json) {
      if (!json || json.error || typeof json.total_memories !== 'number') return ''
      let text = '记忆库 ' + json.total_memories + ' 条'
      if (json.hot_topic && !isGraphNoise(json.hot_topic)) text += '；热区「' + json.hot_topic + '」'
      const recent = json.recent_memories || []
      const items = []
      for (const m of recent) {
        if (m && m.content && !isGraphNoise(m.content)) items.push(String(m.content).slice(0, 80))
      }
      if (items.length > 0) text += '\n近期：' + items.map((x) => '\n- ' + x).join('')
      return text
    }

    function formatRecall(json) {
      if (!json || json.error) return ''
      const results = json.results || []
      if (results.length === 0) return ''
      // 过滤：排除图谱噪声 + 排除图谱边碎片
      const items = []
      for (const r of results) {
        if (!r || !r.content || isGraphNoise(r.content)) continue
        if (isGraphFragment(r.metadata)) continue
        items.push(String(r.content).slice(0, 120))
      }
      return items.length > 0 ? '与当前话题相关的历史记忆：' + items.map((x) => '\n- ' + x).join('') : ''
    }

    function formatTimeRange(json) {
      if (!json || json.error || !Array.isArray(json)) return ''
      const items = []
      for (const r of json) {
        if (!r || !r.content || isGraphNoise(r.content)) continue
        if (isGraphFragment(r.metadata)) continue
        items.push(String(r.content).slice(0, 150))
      }
      return items.length > 0 ? '时间线记忆（按写入时间降序）：' + items.map((x) => '\n- ' + x).join('') : ''
    }

    function refreshProfile() {
      callBridge(['profile'], (json) => {
        if (json) profileText = formatProfile(json)
      })
    }

    const disposers = []

    // 检测用户消息中的相对时间词，返回 Unix 时间戳范围 [since, until]
    function detectTimeWindow(text) {
      const now = Math.floor(Date.now() / 1000)
      const HOUR = 3600
      const DAY = 86400
      if (/刚才|刚刚|方才/.test(text)) {
        // 刚才 = 最近 1 小时
        return [now - HOUR, now]
      }
      if (/上午|早上|早晨/.test(text)) {
        // 今天上午 = 当天 8:00 - 12:00
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 8, 0, 0)
        const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12, 0, 0)
        return [Math.floor(start.getTime() / 1000), Math.floor(end.getTime() / 1000)]
      }
      if (/下午|傍晚/.test(text)) {
        // 今天下午 = 当天 12:00 - 18:00
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 12, 0, 0)
        const end = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 18, 0, 0)
        return [Math.floor(start.getTime() / 1000), Math.floor(end.getTime() / 1000)]
      }
      if (/晚上|夜里|半夜/.test(text)) {
        // 今天晚上 = 当天 18:00 - 现在
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 18, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      if (/今天|今日/.test(text)) {
        // 今天 = 当天 0:00 - 现在
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), 0, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      if (/昨天|昨日/.test(text)) {
        // 昨天 = 昨天 0:00 - 今天 0:00
        const nowD = new Date()
        const todayStart = new Date(nowD.getFullYear(), nowD.getMonth(), nowD.getDate(), 0, 0, 0)
        const yStart = todayStart.getTime() - DAY
        return [Math.floor(yStart / 1000), Math.floor(todayStart.getTime() / 1000)]
      }
      if (/本周|这周|这星期/.test(text)) {
        // 本周 = 周一 0:00 - 现在
        const d = new Date()
        const dayOfWeek = d.getDay() || 7 // 周日=7
        const monday = new Date(d.getFullYear(), d.getMonth(), d.getDate() - dayOfWeek + 1, 0, 0, 0)
        return [Math.floor(monday.getTime() / 1000), now]
      }
      if (/本月|这个月/.test(text)) {
        // 本月 = 1号 0:00 - 现在
        const d = new Date()
        const start = new Date(d.getFullYear(), d.getMonth(), 1, 0, 0, 0)
        return [Math.floor(start.getTime() / 1000), now]
      }
      return null
    }

    // 1) 每轮：存用户消息 + 异步召回相关记忆
    disposers.push(ctx.on('agent/inbox/claimed', (payload) => {
      const message = payload && payload.message
      if (!isRealUser(message)) return
      const text = extractText(message)
      if (isTrivial(text)) return
      const content = text.length > 500 ? text.slice(0, 500) + '…' : text
      callBridge(['save', content, 'conversation'])

      // 检测时间窗口：如果有，走 time_range 查询；否则走 search
      const tw = detectTimeWindow(text)
      if (tw) {
        // 时间线查询：since, until（Unix 秒），top_k=50
        // DSH 插件通过 subprocess 调 Python bridge：
        //   python boshi_bridge.py time_range <since> [until] [top_k]
        // until 可选：不传 = 不限止时间（至今）
        // 注意：PowerShell 下空字符串参数会被丢弃，所以 until 为空时不传该参数
        const bridgeArgs = ['time_range', String(tw[0])]
        if (tw[1]) bridgeArgs.push(String(tw[1]))
        bridgeArgs.push('50')
        callBridge(bridgeArgs, (json) => {
          if (json) recallText = formatTimeRange(json)
        })
      } else {
        // 语义搜索：搜 15 条再截断到 3 条（Hermes 的 top_k=5+FTS 模式）
        callBridge(['search', content, '15'], (json) => {
          if (json) recallText = formatRecall(json)
        })
      }
    }))

    // 2) 画像注入
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-profile',
      order: 40,
      text: () => profileText ? '## 伯仕记忆（用户画像）\n' + profileText : '',
    }))

    // 3) 召回注入
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-recall',
      order: 45,
      text: () => recallText ? '## 伯仕记忆（相关回忆）\n' + recallText : '',
    }))

    // 4) 记忆使用规则注入（让模型知道何时用什么工具查）
    disposers.push(systemPrompt.section({
      name: 'boshi-memory-rules',
      order: 48,
      text: () => `## 伯仕记忆使用规则
- 问"今天/刚才/上午/下午/昨天/本周/本月干了啥" → 用 mcp__boshi__boshi_time_range(since, until, top_k) 查时间线
  - since/until 是 Unix 时间戳（秒）
  - 结果中 source=auto_extract 的是图谱边碎片，忽略
  - 重点看 source=conversation/assistant_conclusion/hermes_plugin/boshi_api 的条目
- 问"XX相关的记忆/经验/之前怎么做的" → 用 mcp__boshi__boshi_search(query) 查语义
  - 结果中 source=auto_extract 的是图谱边碎片，忽略
  - 重点看 source=conversation/assistant_conclusion 的条目
- 问"最近的记忆" → 用 mcp__boshi__boshi_recent(n)
- 记忆库里 85% 是知识图谱自动提取的边（type=relation），真正的对话记忆只占 11%
  自动召回已过滤噪声，但如果手动查 boshi_time_range 返回结果里仍有 auto_extract 碎片，跳过它们`,
    }))

    // 5) 会话开始时刷新画像
    disposers.push(ctx.on('agent/session-start', () => {
      refreshProfile()
    }))

    // 启动时刷新一次画像
    refreshProfile()

    return () => {
      for (const d of disposers) { try { d() } catch (e) {} }
    }
  },
}
