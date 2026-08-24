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
      return typeof s === 'string' && (s.includes('--[') || s.includes('-->') || s.includes('['))
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
      const items = []
      for (const r of results) {
        if (r && r.content && !isGraphNoise(r.content)) items.push(String(r.content).slice(0, 120))
      }
      return items.length > 0 ? '与当前话题相关的历史记忆：' + items.map((x) => '\n- ' + x).join('') : ''
    }

    function refreshProfile() {
      callBridge(['profile'], (json) => {
        if (json) profileText = formatProfile(json)
      })
    }

    const disposers = []

    // 1) 每轮：存用户消息 + 异步召回相关记忆
    disposers.push(ctx.on('agent/inbox/claimed', (payload) => {
      const message = payload && payload.message
      if (!isRealUser(message)) return
      const text = extractText(message)
      if (isTrivial(text)) return
      const content = text.length > 500 ? text.slice(0, 500) + '…' : text
      callBridge(['save', content, 'conversation'])
      callBridge(['search', content, '3'], (json) => {
        if (json) recallText = formatRecall(json)
      })
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

    // 4) 会话开始时刷新画像
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
