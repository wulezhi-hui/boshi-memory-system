"""
伯仕记忆 v5 — 自动实体/关系提取模块
每次对话结束后异步调用 LLM，从对话中提取结构化知识。
"""
import json
import logging
import requests
import sys

logger = logging.getLogger("伯仕提取")

# 默认模型
DEFAULT_MODEL = "deepseek-r1:7b"
OLLAMA_URL = "http://localhost:11434"

# 提取提示词（带 few-shot 示例）
EXTRACT_PROMPT = """你是一个实体提取助手。从对话中提取有意义的实体和关系。

实体类型（限以下）：
- engine: 游戏引擎、运行时
- framework: 技术框架、库、API
- tool: 工具、软件、平台
- hardware: 硬件设备
- person: 人物
- project: 项目、产品
- concept: 重要概念、方法论

规则：
1. 只提取值得长期记住的实体（技术栈、架构决策、关键人物/项目）
2. 忽略一次性细节（心情、客套、废话）
3. attr 属性只写有信息量的内容（版本号、用途、技术特点），不要写"已经跑通了""效果不错"这类废话
4. 关系是实体之间的语义连接，用 2-6 个字描述
5. 没有实体或关系就输出空数组

示例1：
对话：我们用UE5的Nanite做寺院建筑，效果比传统低模好太多了
输出：
{"entities": [{"name": "UE5", "type": "engine", "attr": ""}, {"name": "Nanite", "type": "framework", "attr": "虚拟几何体系统"}], "relations": [{"from": "Nanite", "to": "UE5", "relation": "属于"}]}

示例2：
对话：今天我试了一下Ollama跑qwen模型，速度还可以
输出：
{"entities": [{"name": "Ollama", "type": "tool", "attr": "本地模型运行工具"}, {"name": "qwen", "type": "framework", "attr": "大语言模型"}], "relations": [{"from": "qwen", "to": "Ollama", "relation": "运行于"}]}

示例3：
对话：早啊，今天天气不错
输出：
{"entities": [], "relations": []}

对话：
{text}
"""


# 通用术语黑名单（不当作实体保存）
_BLACKLIST = {
    "用户", "系统", "记忆", "对话", "助手", "AI", "模型", "方案",
    "问题", "答案", "内容", "信息", "数据", "工具", "功能", "方法",
    "任务", "项目", "时间", "东西", "方式", "部分", "情况", "时候",
    "结果", "事情", "朋友", "同事", "地方",
}


def _filter_entities(entities: list) -> list:
    """过滤掉黑名单中的通用术语"""
    return [
        e for e in entities
        if e.get("name", "").strip() not in _BLACKLIST
    ]


def _cleanup_response(raw: str) -> str:
    """清理模型输出中的 think 标签和 markdown 包裹"""
    if not raw:
        return ""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        raw = raw.rsplit("```", 1)[0]
    if "<think>" in raw:
        raw = raw.split("</think>", 1)[-1] if "</think>" in raw else raw.split("<think>", 1)[0]
    return raw.strip()


def _normalize_relation_keys(relations: list) -> list:
    """统一关系字段名为 from/to/relation"""
    normalized = []
    for rel in relations:
        entry = dict(rel)
        if "source" in entry and "from" not in entry:
            entry["from"] = entry.pop("source")
        if "target" in entry and "to" not in entry:
            entry["to"] = entry.pop("target")
        if "from" in entry and "to" in entry and "relation" in entry:
            normalized.append(entry)
    return normalized


def extract_facts(text: str, model: str = DEFAULT_MODEL) -> dict:
    """
    从文本中提取实体和关系。
    
    参数：
        text: 要分析的文本
        model: Ollama 模型名
    
    返回：
        {"entities": [...], "relations": [...]}
        失败时返回空结构
    """
    if not text or len(text.strip()) < 5:
        return {"entities": [], "relations": []}
    
    prompt = EXTRACT_PROMPT.replace("{text}", text[:800])  # 限制输入长度，避免 .format 冲突
    
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 500}
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json().get("response", "").strip()
        
        # 清理模型输出
        raw = _cleanup_response(raw)
        
        # 解析 JSON
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {"entities": [], "relations": []}
        
        entities = result.get("entities", [])
        relations = result.get("relations", [])
        
        if not isinstance(entities, list):
            entities = []
        if not isinstance(relations, list):
            relations = []
        
        # 过滤掉空的实体名
        entities = [e for e in entities if e.get("name")]
        # 过滤掉黑名单通用术语
        entities = _filter_entities(entities)
        
        # 归一化关系字段名
        relations = _normalize_relation_keys(relations)
        
        return {
            "entities": entities[:10],
            "relations": relations[:10],
        }
    
    except requests.exceptions.Timeout:
        logger.warning(f"实体提取超时（{model}）")
        return {"entities": [], "relations": []}
    except requests.exceptions.ConnectionError:
        logger.warning(f"Ollama 连接失败")
        return {"entities": [], "relations": []}
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.debug(f"实体提取解析失败: {e}")
        return {"entities": [], "relations": []}
    except Exception as e:
        logger.error(f"实体提取异常: {e}")
        return {"entities": [], "relations": []}


if __name__ == "__main__":
    # CLI 测试入口
    text = sys.argv[1] if len(sys.argv) > 1 else "你好，今天我们来聊聊PCG框架"
    result = extract_facts(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
