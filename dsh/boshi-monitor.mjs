/**
 * 伯仕记忆系统监察插件
 * ======================
 * 监控伯仕在 DSH 中的集成效果，收集指标，提出改进建议
 * 
 * 监控指标：
 *   1. 注入成功率 - 每轮记忆保存是否成功
 *   2. 召回命中率 - 召回记忆是否与当前话题相关
 *   3. 响应延迟 - bridge 调用耗时
 *   4. 上下文膨胀 - 注入内容的 token 增长
 *   5. 记忆老化 - 热度衰减是否正常
 * 
 * 用法：
 *   - insert:
 *       - id: boshi-monitor
 *         name: './plugins/boshi-monitor.mjs'
 *         config:
 *           python: 'C:/Users/wulezhi/.boshi/venv/Scripts/python.exe'
 *           bridge: 'C:/Users/wulezhi/.boshi/boshi_bridge.py'
 *           reportDir: 'H:\\boshi-monitor'
 */
export default {
  inject: ['subprocess', 'systemPrompt', 'tools'],
  apply(ctx, config = {}) {
    const subprocess = ctx.subprocess
    const systemPrompt = ctx.systemPrompt
    const tools = ctx.tools

    // 配置
    const PYTHON = config.python || 'C:/Users/wulezhi/.boshi/venv/Scripts/python.exe'
    const BRIDGE = config.bridge || 'C:/Users/wulezhi/.boshi/boshi_bridge.py'
    const CWD = config.cwd || 'C:/Users/wulezhi/.boshi'
    const REPORT_DIR = config.reportDir || 'H:\\tmp\\boshi-monitor-reports'

    // 监控状态
    const metrics = {
      totalCalls: 0,
      successfulSaves: 0,
      failedSaves: 0,
      successfulRecalls: 0,
      failedRecalls: 0,
      totalLatencyMs: 0,
      injectionCounts: [],
      reportInterval: config.reportInterval || 100, // 每100轮生成报告
      lastReportRound: 0
    }

    // 内存缓存
    let profileText = ''
    let recallText = ''
    let sessionStats = {
      startTime: Date.now(),
      totalRounds: 0,
      saveAttempts: 0,
      recallAttempts: 0,
      errors: []
    }

    /**
     * 异步调用 bridge 并记录指标
     */
    function callBridgeWithMetrics(args, onResult) {
      const startMs = Date.now()
      metrics.totalCalls++
      
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
          const elapsed = Date.now() - startMs
          metrics.totalLatencyMs += elapsed
          
          const out = handle.collected.stdout?.readFrom(0)?.text?.trim() || ''
          try {
            const json = JSON.parse(out)
            if (json.error) {
              metrics.failedRecalls++
              sessionStats.errors.push({
                time: new Date().toISOString(),
                type: args[0],
                error: json.error
              })
            } else {
              if (args[0] === 'save') metrics.successfulSaves++
              else if (args[0] === 'search') metrics.successfulRecalls++
              onResult(json)
            }
          } catch (e) {
            metrics.failedRecalls++
            sessionStats.errors.push({
              time: new Date().toISOString(),
              type: args[0],
              error: `JSON parse failed: ${e.message}`
            })
          }
        }).catch((e) => {
          metrics.failedRecalls++
          sessionStats.errors.push({
            time: new Date().toISOString(),
            type: args[0],
            error: e.message
          })
        })
      } catch (e) {
        metrics.failedRecalls++
        console.error('boshi monitor spawn failed:', String(e))
      }
    }

    /**
     * 生成统计报告
     */
    function generateReport() {
      const elapsed = (Date.now() - sessionStats.startTime) / 1000
      const avgLatency = metrics.totalCalls > 0 ? metrics.totalLatencyMs / metrics.totalCalls : 0
      const saveRate = metrics.totalCalls > 0 ? (metrics.successfulSaves / metrics.totalCalls * 100).toFixed(1) : 0
      const recallRate = metrics.totalCalls > 0 ? (metrics.successfulRecalls / metrics.totalCalls * 100).toFixed(1) : 0

      const report = {
        timestamp: new Date().toISOString(),
        session_duration_seconds: elapsed.toFixed(1),
        total_operations: metrics.totalCalls,
        save_success_rate: `${saveRate}%`,
        recall_success_rate: `${recallRate}%`,
        avg_latency_ms: avgLatency.toFixed(1),
        errors_count: sessionStats.errors.length,
        recent_errors: sessionStats.errors.slice(-5)
      }

      // 获取当前记忆库状态
      callBridgeWithMetrics(['status'], (json) => {
        if (json) {
          report.memory_stats = {
            total_memories: json.total_memories,
            graph_nodes: json.knowledge_graph?.nodes,
            graph_edges: json.knowledge_graph?.edges
          }
        }
      })

      return report
    }

    /**
     * 提出改进建议
     */
    function generateSuggestions(report) {
      const suggestions = []

      if (report.save_success_rate < 95) {
        suggestions.push({
          severity: 'high',
          area: '写入稳定性',
          issue: `保存成功率仅 ${report.save_success_rate}%`,
          suggestion: '检查 boshi_bridge.py 进程状态，确认数据库连接正常'
        })
      }

      if (report.recall_success_rate < 90) {
        suggestions.push({
          severity: 'high',
          area: '召回质量',
          issue: `召回成功率仅 ${report.recall_success_rate}%`,
          suggestion: '检查 embedding 模型状态，考虑调整检索参数'
        })
      }

      if (report.avg_latency_ms > 500) {
        suggestions.push({
          severity: 'medium',
          area: '响应延迟',
          issue: `平均延迟 ${report.avg_latency_ms}ms`,
          suggestion: '考虑使用持久化连接替代 subprocess，或增加缓存层'
        })
      }

      if (report.errors_count > 10) {
        suggestions.push({
          severity: 'high',
          area: '错误频率',
          issue: `已发生 ${report.errors_count} 次错误`,
          suggestion: '查看最近错误日志，定位根本原因'
        })
      }

      if (report.memory_stats?.total_memories > 10000) {
        suggestions.push({
          severity: 'medium',
          area: '记忆规模',
          issue: `记忆库已达 ${report.memory_stats.total_memories} 条`,
          suggestion: '考虑启用自动清理策略，归档冷区记忆'
        })
      }

      return suggestions
    }

    // 注册监察工具
    tools.register({
      name: 'boshi_status',
      description: '查看伯仕记忆系统当前状态和监控报告',
      parameters: {
        type: 'object',
        properties: {
          detailed: {
            type: 'boolean',
            description: '是否显示详细报告',
            default: false
          }
        }
      },
      output: {
        schema: {
          type: 'object',
          properties: {
            report: { type: 'object' },
            suggestions: { type: 'array' },
            summary: { type: 'string' }
          }
        },
        render(data) {
          return `${data.summary}\n\n报告:\n${JSON.stringify(data.report, null, 2)}\n\n建议:\n${(data.suggestions || []).map(s => `- [${s.severity}] ${s.area}: ${s.suggestion}`).join('\n')}`
        }
      },
      async handler(args) {
        const report = generateReport()
        const suggestions = generateSuggestions(report)
        
        return {
          report,
          suggestions,
          summary: suggestions.length > 0 
            ? `发现 ${suggestions.length} 个问题需要关注` 
            : '系统运行正常'
        }
      }
    })

    // 注册快速健康检查工具
    tools.register({
      name: 'boshi_health',
      description: '快速检查伯仕系统健康状态（无需详细报告）',
      parameters: {},
      output: {
        schema: {
          type: 'object',
          properties: {
            ok: { type: 'boolean' },
            total_memories: { type: 'number' },
            graph_nodes: { type: 'number' },
            graph_edges: { type: 'number' },
            error: { type: 'string' }
          }
        },
        render(data) {
          if (data.ok) {
            return `伯仕系统正常 | 记忆数: ${data.total_memories} | 知识图谱节点: ${data.graph_nodes} | 边: ${data.graph_edges}`
          }
          return `伯仕系统异常: ${data.error}`
        }
      },
      async handler() {
        return new Promise((resolve) => {
          callBridgeWithMetrics(['status'], (json) => {
            if (json) {
              resolve({
                ok: true,
                total_memories: json.total_memories,
                graph_nodes: json.knowledge_graph?.nodes,
                graph_edges: json.knowledge_graph?.edges
              })
            } else {
              resolve({ ok: false, error: '无法获取状态' })
            }
          })
        })
      }
    })

    // 暴露给其他插件使用
    ctx.__boshiMonitor = {
      metrics,
      sessionStats,
      generateReport,
      generateSuggestions
    }

    return () => {}
  }
}
