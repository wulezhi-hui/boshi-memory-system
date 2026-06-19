"""
伯仕语音识别服务 v1
使用 FunASR + SenseVoice 模型，本地离线中文语音转文字
"""

import os, sys, json, asyncio, time, threading
from pathlib import Path

# 音频临时目录
AUDIO_DIR = Path("D:/boshi_audio")
AUDIO_DIR.mkdir(exist_ok=True)

model = None
model_lock = threading.Lock()

def init_model():
    """初始化语音识别模型"""
    global model
    try:
        from funasr import AutoModel
        model = AutoModel(
            model="iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            vad_model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
            punc_model="iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
            disable_update=True,
        )
        return True
    except Exception as e:
        print(f"模型加载失败: {e}", flush=True)
        return False


def init_model_sensevoice():
    """使用更轻量的 SenseVoice 模型"""
    global model
    try:
        from funasr import AutoModel
        model = AutoModel(
            model="iic/SenseVoiceSmall",
            disable_update=True,
        )
        return True
    except Exception as e:
        print(f"SenseVoice 模型加载失败: {e}", flush=True)
        return False


def recognize_file(audio_path: str) -> str:
    """识别音频文件，返回文字"""
    global model
    if model is None:
        with model_lock:
            if model is None:
                if not init_model_sensevoice():
                    if not init_model():
                        return "【语音模型未加载】"
    
    try:
        result = model.generate(input=audio_path)
        if isinstance(result, list) and len(result) > 0:
            text = result[0].get("text", "")
            # 过滤 SenseVoice 内部标签 <|xxx|>
            import re
            text = re.sub(r'<\|[^>]+\|>', '', text).strip()
            return text
        return str(result)
    except Exception as e:
        return f"识别失败: {e}"


def recognize_raw(audio_data: bytes, sample_rate: int = 16000) -> str:
    """识别原始音频数据"""
    global model
    if model is None:
        with model_lock:
            if model is None:
                if not init_model_sensevoice():
                    if not init_model():
                        return "【语音模型未加载】"
    
    try:
        import numpy as np
        arr = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
        result = model.generate(input=arr)
        if isinstance(result, list) and len(result) > 0:
            return result[0].get("text", "")
        return str(result)
    except Exception as e:
        return f"识别失败: {e}"


if __name__ == "__main__":
    # 测试模式
    if len(sys.argv) > 1:
        audio_file = sys.argv[1]
        if os.path.exists(audio_file):
            print(f"识别: {audio_file}", flush=True)
            text = recognize_file(audio_file)
            print(f"结果: {text}", flush=True)
        else:
            print(f"文件不存在: {audio_file}", flush=True)
    else:
        print("伯仕语音识别服务 v1", flush=True)
        print("用法: python voice_service.py <音频文件>", flush=True)
