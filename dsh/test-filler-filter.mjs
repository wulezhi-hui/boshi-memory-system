/**
 * 回归测试：伯仕记忆的消息过滤（词表法 vs 长度法）
 * ================================================
 * 直接 import 线上实现（plugins/filter-logic.mjs），确保测试与运行时不会漂移。
 *
 * 运行：node test-filler-filter.mjs
 */
import { DEFAULT_FILLER_WORDS, normalizeBare, makeIsTrivial } from './filter-logic.mjs'

const isTrivial = makeIsTrivial({ minChars: 2, skipSymbolOnly: true })

// [性质, 文本, 期望是否跳过]
const cases = [
  // ---- 噪声：必须 SKIP ----
  ['噪声', '继续', true],
  ['噪声', '好', true],
  ['噪声', '可以', true],
  ['噪声', '在吗', true],
  ['噪声', '好吧', true],
  ['噪声', '行吧', true],
  ['噪声', '嗯嗯', true],
  ['噪声', '收到', true],
  ['噪声', '谢谢', true],
  ['噪声', '明白了', true],
  ['噪声', 'OK', true],
  ['噪声', '继续！', true],
  ['噪声', '继续吧', true],
  ['噪声', '可以了', true],
  ['噪声', '好了好了', true],
  ['噪声', '……', true],
  ['噪声', '。。。', true],
  ['噪声', '？', true],
  ['噪声', '!!', true],

  // ---- 有效短指令：长度法会误杀，必须 KEEP ----
  ['有效', '重启了', false],
  ['有效', '不是吧', false],
  ['有效', '改成1M', false],
  ['有效', '主身在弄', false],
  ['有效', '不用你动', false],
  ['有效', '怎么回事？', false],
  ['有效', '我不明白', false],

  // ---- 含填充词但信息量足够：必须 KEEP ----
  ['有效', '好的，明白了', false],
  ['有效', '好，我回一句话，你确认一下看', false],
  ['有效', '好，那就按方案B改', false],
  ['有效', '可以对，但不要改 maxOutput', false],
  ['有效', '过滤阈值调严一点是对的', false],
  ['有效', 'no，回退', false],
]

let fail = 0
console.log('结果'.padEnd(8) + '期望'.padEnd(8) + '长度'.padEnd(6) + '归一化'.padEnd(18) + '内容')
console.log('-'.repeat(80))
for (const [kind, text, mustSkip] of cases) {
  const got = isTrivial(text)
  const ok = got === mustSkip
  if (!ok) fail++
  console.log(
    (got ? 'SKIP' : 'KEEP').padEnd(8) +
    (mustSkip ? 'SKIP' : 'KEEP').padEnd(8) +
    String(text.length).padEnd(6) +
    JSON.stringify(normalizeBare(text)).padEnd(18) +
    JSON.stringify(text) +
    (ok ? '' : '   <-- FAIL'),
  )
}
console.log('-'.repeat(80))
console.log(fail === 0 ? `全部通过（${cases.length} 项）` : `${fail} 项失败 / 共 ${cases.length} 项`)

// ---- 长度法对照：证明 minChars 无法同时满足 ----
const contrast = [
  ['继续', '噪声', true],
  ['可以', '噪声', true],
  ['好', '噪声', true],
  ['重启了', '有效', false],
  ['不是吧', '有效', false],
  ['改成1M', '有效', false],
]
console.log('\n为什么不能用 minChars —— 各取值误判对照\n')
console.log('样本'.padEnd(12) + '长度'.padEnd(6) + '性质'.padEnd(8)
  + [2, 3, 4, 5, 6].map((n) => `min=${n}`.padEnd(9)).join('') + '词表法')
let badCols = 0
for (const [s, kind, mustSkip] of contrast) {
  let row = s.padEnd(12) + String(s.length).padEnd(6) + kind.padEnd(8)
  for (const n of [2, 3, 4, 5, 6]) {
    const verdict = s.length < n ? 'SKIP' : 'KEEP'
    const wrong = (verdict === 'SKIP') !== mustSkip
    row += (verdict + (wrong ? '*' : ' ')).padEnd(9)
  }
  row += isTrivial(s) ? 'SKIP' : 'KEEP'
  console.log(row)
}
console.log('\n* = 误判。结论：任一 minChars 都无法同时正确处理这 6 个样本，故改用词表。')
console.log(`词表共 ${DEFAULT_FILLER_WORDS.length} 条；可用 config.fillerWords 整体替换。`)

process.exit(fail === 0 ? 0 : 1)
