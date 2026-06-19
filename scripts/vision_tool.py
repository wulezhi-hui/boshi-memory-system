#!/usr/bin/env python3
"""
伯仕视觉工具 — 截图+OCR+理解的完整管道
调用方式：python3 vision_tool.py <图片路径> [模式]

模式:
  ocr      — 仅OCR文字提取（默认）
  describe — 描述图片内容
  pipeline — 全流程：截图→OCR→理解

依赖：
  pip install pillow
  ollama 运行中（glm-ocr模型）
"""

import base64, json, os, sys, time, urllib.request
from PIL import Image, ImageGrab

# ─── 配置 ───
OLLAMA_URL = "http://localhost:11434/api/generate"
OCR_MODEL = "glm-ocr"
VISION_MODEL = "glm-ocr"  # 也可用其他视觉模型

# ─── 核心函数 ───

def screenshot(region=None, save_path=None):
    """截屏，region=(left, top, width, height)"""
    if region:
        img = ImageGrab.grab(bbox=(region[0], region[1], region[0]+region[2], region[1]+region[3]))
    else:
        img = ImageGrab.grab()
    if save_path:
        img.save(save_path)
        print(f"📷 截图已保存: {save_path} ({img.size})")
    return img

def img_to_b64(img_or_path):
    """图片转base64"""
    if isinstance(img_or_path, str):
        with open(img_or_path, 'rb') as f:
            return base64.b64encode(f.read()).decode()
    img_or_path.save("/tmp/_vision_temp.png")
    with open("/tmp/_vision_temp.png", 'rb') as f:
        return base64.b64encode(f.read()).decode()

def ollama_vision(image_b64, prompt, model=OCR_MODEL):
    """调用Ollama视觉模型"""
    payload = {
        'model': model,
        'prompt': prompt,
        'images': [image_b64],
        'stream': False,
        'options': {'temperature': 0}
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'}
    )
    t0 = time.time()
    resp = urllib.request.urlopen(req, timeout=120)
    result = json.loads(resp.read())
    elapsed = time.time() - t0
    return result.get('response', ''), elapsed

def ocr_text(img_or_path):
    """OCR提取图片中的文字"""
    b64 = img_to_b64(img_or_path)
    text, elapsed = ollama_vision(b64, "请识别图中所有文字，逐行输出")
    return text, elapsed

def describe_image(img_or_path):
    """描述图片内容"""
    b64 = img_to_b64(img_or_path)
    desc, elapsed = ollama_vision(b64, "请详细描述这张图片中的内容和文字")
    return desc, elapsed

def full_pipeline(region=None):
    """全流程：截图→OCR→描述"""
    img = screenshot(region)
    save_path = f"/tmp/vision_pipeline_{int(time.time())}.png"
    img.save(save_path)
    b64 = img_to_b64(save_path)
    size = img.size
    
    print("🔍 正在OCR识别文字...")
    text, t1 = ollama_vision(b64, "请识别图中所有文字，逐行输出")
    print(f"  ⏱ {t1:.1f}s")
    if text.strip():
        for line in text.strip().split('\n'):
            print(f"    📄 {line}")
    else:
        print("    (未识别到文字)")
    print()
    
    return {"text": text, "size": size}

# ─── CLI ───
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法:")
        print("  python3 vision_tool.py <图片路径> ocr      — 识别文字")
        print("  python3 vision_tool.py <图片路径> describe — 描述内容")
        print("  python3 vision_tool.py screenshot          — 截屏+识别")
        print("  python3 vision_tool.py screenshot [区域]    — 截取特定区域(0,0,800,600)")
        sys.exit(1)
    
    mode = sys.argv[2] if len(sys.argv) > 2 else "ocr"
    
    if sys.argv[1] == "screenshot":
        region = None
        if len(sys.argv) > 3:
            try:
                parts = list(map(int, sys.argv[3].split(',')))
                region = parts
            except: pass
        result = full_pipeline(region)
    else:
        img_path = sys.argv[1]
        if not os.path.exists(img_path):
            print(f"❌ 文件不存在: {img_path}")
            sys.exit(1)
        
        if mode == "ocr":
            text, elapsed = ocr_text(img_path)
            print(f"📝 OCR结果 (⏱{elapsed:.1f}s):\n{text}")
        elif mode == "describe":
            desc, elapsed = describe_image(img_path)
            print(f"🖼️ 图片描述 (⏱{elapsed:.1f}s):\n{desc}")
