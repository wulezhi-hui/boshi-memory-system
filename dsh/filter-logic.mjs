/**
 * 伯仕记忆 — 消息过滤逻辑（插件同目录，dsh 插件与回归测试共用）
 * =================================================================
 * 设计要点：**长度无法区分中文填充词与有效短指令**，所以按词表精确剔除。
 * 实测本机日志：
 *   噪声「好」1字、「可以」「继续」2字
 *   有效「重启了」「不是吧」3字、「改成1M」4字
 * 任何 minChars 取值都会误杀一侧，故 minChars 保持 2，靠词表判断。
 *
 * 纯函数、无依赖。
 *
 * ⚠️ 改动本文件后，必须同时递增 boshi-auto-memory.mjs 里 import 的
 *    ?v=N 查询参数（Node ESM 按完整 URL 缓存），并递增 cordis.patch.yml
 *    入口的 ?v=N，否则不会重载。
 */

/** 纯应诺/推进语词表。未收 'no'：单独否定可能是明确决策（"no，回退"）。 */
export const DEFAULT_FILLER_WORDS = [
  // 单字应诺
  '嗯', '恩', '好', '行', '对', '是', 'ok', 'okay', 'yes',
  // 双字/三字应诺
  '嗯嗯', '好的', '好了', '好嘞', '好滴', '好吧', '行吧', '可以', '对的', '是的',
  '收到', '谢谢', '多谢', '妙', '棒', '辛苦', '辛苦啦', '麻烦', '麻烦了', '明白', '明白了',
  // 推进语
  '继续', '接着', '在吗', '来吧', '开始吧', '然后',
]

/** 尾部语气助词：剥离后才做词表匹配（「继续吧」→「继续」，「可以了」→「可以」）。 */
export const TRAILING_PARTICLES = '吧啊呀哦嘛呢呗啦哈哟喔咯么了哒滴'

/**
 * 归一化：剥离空白/标点/符号，转小写，去掉尾部语气助词。
 * 只削尾不改中，避免把「好吧那就这样」误判成填充词。
 *
 * 注意：削到剩 1 个字符即停（s.length > 1），所以「了了了」这类不会削成空串。
 */
export function normalizeBare(text) {
  let s = String(text).replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase()
  let prev
  do {
    prev = s
    const last = s.slice(-1)
    if (s.length > 1 && TRAILING_PARTICLES.includes(last)) s = s.slice(0, -1)
  } while (s !== prev)
  return s
}

/**
 * 整条命中词表，或其简单倍写形式命中（「好了好了」→「好了」）。
 *
 * 顺序很重要：必须在**削尾助词之前**判倍写，否则「好了好了」会被削成
 * 「好了好」，不再是 abab 形式而漏判。所以先用裸串判倍写，再对削尾结果查词表。
 */
export function isFillerText(text, fillerWords) {
  // 裸串：只剥标点空白，不削尾助词
  const raw = String(text).replace(/[\s\p{P}\p{S}]/gu, '').toLowerCase()
  if (!raw) return false

  // ① 倍写判定（削尾之前）
  for (let n = 1; n <= Math.floor(raw.length / 2); n++) {
    if (raw.length === n * 2 && raw.slice(0, n) === raw.slice(n) && fillerWords.has(raw.slice(0, n))) {
      return true
    }
  }

  // ② 削尾助词后查词表
  const bare = normalizeBare(text)
  if (bare && fillerWords.has(bare)) return true
  if (raw !== bare && fillerWords.has(raw)) return true

  return false
}

/** 组装过滤判定（语义与插件 config 一致）。 */
export function makeIsTrivial({ minChars = 2, skipSymbolOnly = true, fillerWords } = {}) {
  const words = new Set(
    (Array.isArray(fillerWords) ? fillerWords : DEFAULT_FILLER_WORDS).map((w) => String(w).toLowerCase()),
  )
  return function isTrivial(text) {
    if (!text) return true
    if (text.length < minChars) return true
    if (skipSymbolOnly && /^[\s\p{P}\p{S}]+$/u.test(text)) return true
    if (isFillerText(text, words)) return true
    return false
  }
}
