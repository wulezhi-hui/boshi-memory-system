# -*- coding: utf-8 -*-

"""

伯仕工作台 v6 —— 直连 Gateway + 汇聚模式

"""



import os, sys, json, asyncio, logging, socket, time

from datetime import datetime



_REQUIRED = ['aiohttp']

_missing = [p for p in _REQUIRED if os.system(

    f'{sys.executable} -c "import {p}" >nul 2>&1') != 0]

if _missing:

    os.system(f'{sys.executable} -m pip install {" ".join(_missing)} -q')



import aiohttp

from aiohttp import web



HOST = "0.0.0.0"

PORT = 7681

BOSHI_DIR = os.path.expanduser("~/.boshi")

DATA_DIR = os.path.join(BOSHI_DIR, "workstation", "data")

LOG_FILE = os.path.join(BOSHI_DIR, "workstation", "boshi_workstation.log")

CONVERGE_FILE = os.path.join(BOSHI_DIR, "memory", "converge_mode.json")

SHARED_DIR = os.path.join(os.path.expanduser("~"), ".openclaw", "shared")

SHARED_HISTORY_FILE = os.path.join(SHARED_DIR, "converge_history.json")

import sys as _sys; _sys.path.insert(0, os.path.expanduser("~/.boshi")); from converge_db import write_message as db_write, read_recent_messages as db_read_recent; del _sys

GATEWAY_URL = "http://127.0.0.1:8642"

CONVERGE_SESSION_ID = "boshi-converge-weixin"

MEMORY_DIR = os.path.join(BOSHI_DIR, "memory")

CROSS_HISTORY_FILE = os.path.join(MEMORY_DIR, "cross_platform_history.json")



os.makedirs(DATA_DIR, exist_ok=True)



logging.basicConfig(

    level=logging.INFO,

    format="%(asctime)s [%(levelname)s] %(message)s",

    handlers=[

        logging.FileHandler(LOG_FILE, encoding="utf-8"),

        logging.StreamHandler(),

    ],

)

log = logging.getLogger("伯仕工作台")



connected_ws = set()

converge_enabled = False





def load_converge_mode():

    global converge_enabled

    try:

        if os.path.exists(CONVERGE_FILE):

            with open(CONVERGE_FILE, 'r') as f:

                cfg = json.load(f)

            converge_enabled = cfg.get("enabled", False)

        else:

            converge_enabled = False

            os.makedirs(os.path.dirname(CONVERGE_FILE), exist_ok=True)

            with open(CONVERGE_FILE, 'w') as f:

                json.dump({"enabled": False, "updated_at": datetime.now().isoformat()}, f)

    except Exception as e:

        log.error(f"读取汇聚模式失败: {e}")





def save_converge_mode(enabled: bool):

    global converge_enabled

    converge_enabled = enabled

    try:

        with open(CONVERGE_FILE, 'w') as f:

            json.dump({"enabled": enabled, "updated_at": datetime.now().isoformat()}, f)

    except Exception as e:

        log.error(f"保存汇聚模式失败: {e}")





async def push_to_ws(msg_type, text="", extra=None):

    payload = {"type": msg_type, "text": text, "time": datetime.now().isoformat()}

    if extra:

        payload.update(extra)

    for ws in list(connected_ws):

        try:

            if not ws.closed:

                await ws.send_str(json.dumps(payload, ensure_ascii=False))

        except:

            pass





async def broadcast_to_weixin(message: str):

    """汇聚模式下，通过 Gateway 的 send API 把消息推送到微信"""

    try:

        async with aiohttp.ClientSession() as client:

            async with client.post(

                f"{GATEWAY_URL}/api/send",

                json={"platform": "weixin", "message": f"〔汇聚〕{message[:2000]}"},

                timeout=aiohttp.ClientTimeout(total=10),

            ) as resp:

                if resp.status == 200:

                    log.info("汇聚广播 → 微信 ✅")

                else:

                    log.warning(f"汇聚广播 → 微信 失败: {resp.status}")

    except Exception as e:

        log.error(f"汇聚广播异常: {e}")





def load_shared_history():

    """从汇聚数据库读取微信端的对话历史"""

    return db_read_recent(limit=80, channels=["weixin", "workstation", "terminal", "system"], for_hermes=True)



def append_shared_history(role, content):

    """写入一条消息到汇聚数据库（汇聚模式开启时，仅写工作台来源）"""

    if not converge_enabled:

        return

    db_write("workstation", role, content)



def load_cross_history():

    """读取跨平台历史记录"""

    try:

        if os.path.exists(CROSS_HISTORY_FILE):

            with open(CROSS_HISTORY_FILE, 'r', encoding='utf-8') as f:

                data = json.load(f)

            return data.get("messages", [])

    except Exception as e:

        log.error(f"读取历史失败: {e}")

    return []





def append_to_cross_history(role, content):

    """追加一条消息到跨平台历史"""

    try:

        os.makedirs(os.path.dirname(CROSS_HISTORY_FILE), exist_ok=True)

        if os.path.exists(CROSS_HISTORY_FILE):

            with open(CROSS_HISTORY_FILE, 'r', encoding='utf-8') as f:

                data = json.load(f)

        else:

            data = {"session_id": "boshi-cross-platform", "messages": []}

        messages = data.setdefault("messages", [])

        # 只保留最近 200 条，避免无限膨胀

        if len(messages) > 200:

            messages = messages[-200:]

        messages.append({"role": role, "content": content[:10000]})

        data["messages"] = messages

        with open(CROSS_HISTORY_FILE, 'w', encoding='utf-8') as f:

            json.dump(data, f, ensure_ascii=False, indent=2)

    except Exception as e:

        log.error(f"追加历史失败: {e}")





async def chat_with_gateway(content, ws):

    """通过 Gateway API 发送消息并流式接收回复，自动携带全量上下文"""

    try:

        await push_to_ws("status", "", {"status": "processing"})



        # ── 读取跨平台历史 + 共享历史，构建全量 messages ──

        history = load_cross_history()

        shared = load_shared_history()

        # 合并：微信来的共享消息在前（旧），本地历史在后（新）

        all_history = shared + history

        messages = all_history + [{"role": "user", "content": content}]



        # 截断到最近 100 条避免 token 超限

        if len(messages) > 100:

            messages = messages[-100:]



        payload = {

            "model": "hermes-agent",

            "messages": messages,

            "stream": True,

            "max_tokens": 8192,

        }

        headers = {"Content-Type": "application/json", "Authorization": "Bearer test123"}



        async with aiohttp.ClientSession() as client:

            async with client.post(

                f"{GATEWAY_URL}/v1/chat/completions",

                json=payload,

                headers=headers,

                timeout=aiohttp.ClientTimeout(total=300),

            ) as resp:

                full = ""

                reasoning_text = ""

                async for line in resp.content:

                    line = line.decode("utf-8").strip()

                    if not line.startswith("data: "):

                        continue

                    raw = line[6:]

                    if raw == "[DONE]":

                        break

                    try:

                        data = json.loads(raw)

                    except:

                        continue



                    choices = data.get("choices")

                    if not choices:

                        continue

                    delta = choices[0].get("delta", {})

                    finish = choices[0].get("finish_reason")



                    if "reasoning" in delta and delta["reasoning"]:

                        reasoning_text += delta["reasoning"]

                        await push_to_ws("terminal", delta["reasoning"], {"category": "thinking"})



                    if "content" in delta and delta["content"]:

                        full += delta["content"]

                        await push_to_ws("terminal", delta["content"])



                    if finish == "stop" and full:

                        if ws and not ws.closed:

                            await ws.send_str(json.dumps({

                                "type": "assistant_reply",

                                "content": full,

                            }, ensure_ascii=False))

                        await push_to_ws("content_done", full)

                        await push_to_ws("status", "", {"status": "ready"})

                        log.info(f"回复完成: {full[:50]}...")



                        # ── 写入跨平台历史 ──

                        append_to_cross_history("user", content)

                        append_to_cross_history("assistant", full)



                        # ── 写入共享历史（微信端能读到） ──

                        append_shared_history("user", content)

                        append_shared_history("assistant", full)



                        # ── 汇聚广播：把回复同步到微信 ──

                        if converge_enabled:

                            try:

                                broadcast = {

                                    "source": "workstation",

                                    "user_msg": content[:500],

                                    "assistant_msg": full[:500],

                                    "timestamp": time.time(),

                                }

                                bf = os.path.join(MEMORY_DIR, "live_turns.json")

                                if os.path.exists(bf):

                                    with open(bf, 'r', encoding='utf-8') as f:

                                        turns = json.load(f)

                                else:

                                    turns = {}

                                turns["boshi-workstation-latest"] = broadcast

                                with open(bf, 'w', encoding='utf-8') as f:

                                    json.dump(turns, f, ensure_ascii=False, indent=2)

                                log.info("汇聚广播已写入 live_turns.json ✅")

                                # 也把伯仕的回复推送到微信

                                msg = full[:1000]

                                if msg:

                                    await broadcast_to_weixin(f"伯仕: {msg}")

                            except Exception as _e:

                                log.error(f"广播写入失败: {_e}")



    except Exception as e:

        log.error(f"Gateway 请求失败: {e}")

        await push_to_ws("error", f"连接伯仕失败: {e}")

        await push_to_ws("status", "", {"status": "ready"})





async def websocket_handler(request):

    ws = web.WebSocketResponse()

    await ws.prepare(request)

    connected_ws.add(ws)

    log.info(f"WebSocket 连接 ({len(connected_ws)} 个)")



    await ws.send_str(json.dumps({

        "type": "converge_mode",

        "enabled": converge_enabled,

    }))



    try:

        async for msg in ws:

            if msg.type == aiohttp.WSMsgType.TEXT:

                data = json.loads(msg.data)

                cmd = data.get("type")



                if cmd == "chat":

                    content = data.get("content", "")

                    asyncio.create_task(chat_with_gateway(content, ws))



                elif cmd == "set_converge":

                    enabled = data.get("enabled", True)

                    save_converge_mode(enabled)

                    await ws.send_str(json.dumps({"type": "converge_mode", "enabled": enabled}))

                    log.info(f"汇聚模式切换为: {'开启' if enabled else '关闭'}")



                elif cmd == "toggle_converge":

                    """前端一键切换汇聚/独立模式"""

                    new_state = not converge_enabled

                    save_converge_mode(new_state)

                    await ws.send_str(json.dumps({"type": "converge_mode", "enabled": new_state}))

                    log.info(f"一键切换汇聚模式为: {'开启' if new_state else '关闭'}")

                    # 通知所有连接的客户端

                    await push_to_ws("converge_mode", "", {"converge_enabled": new_state})



                elif cmd == "get_converge":

                    """前端查询当前汇聚模式状态"""

                    await ws.send_str(json.dumps({"type": "converge_mode", "converge_enabled": converge_enabled}))



                elif cmd == "ping":

                    await ws.send_str(json.dumps({"type": "pong"}))



                elif cmd == "new_image":

                    path = data.get("path", "")

                    if path:

                        # 通知所有连接的客户端（包括自己）有新图片

                        await push_to_ws("image_notify", f"新图片: {path}", {"path": path})



                elif cmd == "load_history":

                    """加载历史对话到左侧面板（增量模式：只推送新消息）"""

                    try:

                        last_id = data.get("last_id", 0)

                        from converge_db import read_recent_messages as db_read

                        import sqlite3

                        conn = sqlite3.connect(os.path.expanduser("~/.openclaw/shared/converge.db"))

                        c = conn.cursor()

                        rows = c.execute("SELECT id, channel, role, content, timestamp FROM conversations WHERE id > ? AND channel IN ('weixin', 'workstation') ORDER BY id ASC", (last_id,)).fetchall()

                        conn.close()

                        

                        if not rows:

                            # 没新消息，只推一个空心跳

                            await ws.send_str(json.dumps({

                                "type": "history_heartbeat",

                                "last_id": last_id,

                            }))

                            return

                        

                        for msg_id, channel, role, content, ts in rows:

                            role_icon = "🧑" if role == "user" else "🤖"

                            line = f"{role_icon} {content[:200]}"

                            await ws.send_str(json.dumps({

                                "type": "terminal",

                                "text": line,

                                "category": "history",

                                "role": role,

                                "msg_id": msg_id,

                            }))

                        

                        await ws.send_str(json.dumps({

                            "type": "history_done",

                            "last_id": rows[-1][0],

                            "count": len(rows),

                        }))

                        log.info(f"增量推送 {len(rows)} 条 (从ID {last_id} 开始)")

                    except Exception as e:

                        log.error(f"加载历史失败: {e}")

                        await ws.send_str(json.dumps({

                            "type": "terminal",

                            "text": f"⚠️ 加载历史失败: {e}",

                            "category": "error",

                        }))



    except Exception as e:

        log.error(f"WebSocket 错误: {e}")

    finally:

        connected_ws.discard(ws)

        log.info(f"WebSocket 断开 ({len(connected_ws)} 个)")





async def index_handler(request):

    html = """<!DOCTYPE html>

<html lang="zh-CN">

<head>

<meta charset="UTF-8">

<meta name="viewport" content="width=device-width, initial-scale=1.0">

<title>伯仕工作台</title>

<style>

*{margin:0;padding:0;box-sizing:border-box}

body{font-family:-apple-system,'Segoe UI',sans-serif;background:#0d1117;color:#c9d1d9;height:100vh;display:flex;flex-direction:column}

.header{background:#161b22;border-bottom:1px solid #30363d;padding:10px 20px;display:flex;align-items:center;gap:8px;flex-shrink:0}

.header h1{font-size:18px;color:#58a6ff}

.mode-btn{padding:4px 10px;border:1px solid #30363d;border-radius:4px;cursor:pointer;font-size:12px;background:#21262d;color:#8b949e}

.mode-btn.active{border-color:#58a6ff;color:#58a6ff;background:#1a2538}

.status{font-size:12px;color:#8b949e;margin-left:auto}

.main{display:flex;flex:1;min-height:0;gap:1px;background:#30363d}

.main.chat-mode{display:flex;flex-direction:column}

.terminal-panel{display:flex;flex:0 0 45%;background:#0d1117;overflow:hidden;flex-direction:column;min-width:200px;max-width:50%}

.chat-mode .terminal-panel{display:none}

.terminal-line{white-space:pre-wrap;word-break:break-word;overflow-wrap:break-word;width:100%;max-width:100%}

.terminal-line.thinking{color:#8b949e;font-style:italic}

.terminal-line.tool-call{color:#d2a8ff}

.terminal-line.separator{border-top:1px solid #21262d;margin:8px 0}

.terminal-line.history{color:#c9d1d9;font-size:12px;padding:2px 0}

.terminal-line.history.user{color:#58a6ff}

.terminal-line.history.assistant{color:#7ee787}

.panel-split{display:flex;flex:1;flex-direction:column;min-height:0;overflow:hidden}

.panel-half{flex:1;overflow-y:auto;padding:8px 12px;min-height:0}

.panel-half.top{border-bottom:1px solid #30363d;flex:0 0 auto;max-height:50%}

.panel-half.bottom{flex:1;min-height:0}

.panel-header{font-size:12px;color:#8b949e;padding:8px 12px 4px;border-bottom:1px solid #30363d;font-weight:600;flex-shrink:0}

.chat-panel{display:flex;flex:0 0 55%;background:#0d1117;flex-direction:column;min-height:0}

.chat-mode .chat-panel{flex:1;width:100%;height:100%}

.chat-messages{flex:1;overflow-y:auto;padding:12px;min-height:0}

.message{margin-bottom:12px;display:flex;flex-direction:column}

.message.user{align-items:flex-end}

.message.assistant{align-items:flex-start}

.message .bubble{max-width:80%;padding:8px 14px;border-radius:12px;font-size:14px;line-height:1.5}

.message.user .bubble{background:#1f6ebf;color:#fff;border-bottom-right-radius:4px}

.message.assistant .bubble{background:#21262d;color:#c9d1d9;border-bottom-left-radius:4px}

.input-area{border-top:1px solid #30363d;padding:10px 12px;display:flex;gap:8px;background:#161b22;align-items:flex-start}

.input-area textarea{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:8px 12px;color:#c9d1d9;font-size:14px;resize:none;outline:none}

.input-area textarea:focus{border-color:#58a6ff}

.img-btn{padding:6px 10px;background:#21262d;color:#8b949e;border:1px solid #30363d;border-radius:6px;cursor:pointer;font-size:14px;line-height:1}

.img-btn:hover{border-color:#58a6ff;color:#58a6ff}

.send-btn{padding:8px 20px;background:#238636;color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:14px;white-space:nowrap}

.send-btn:disabled{background:#21262d;color:#484f58;cursor:not-allowed}

.send-btn:hover:not(:disabled){background:#2ea043}

.toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:#238636;color:#fff;padding:10px 20px;border-radius:8px;font-size:14px;z-index:999;animation:fadeInOut 3s forwards}

@keyframes fadeInOut{0%{opacity:0;transform:translateX(-50%) translateY(20px)}15%{opacity:1;transform:translateX(-50%) translateY(0)}85%{opacity:1}100%{opacity:0}}

</style>

</head>

<body>

<div class="header">

  <h1>伯仕工作台</h1>

  <button class="mode-btn active" id="modeBtn" onclick="toggleMode()">分屏</button>

  <button class="mode-btn" id="convergeBtn" onclick="toggleConverge()">汇聚</button>

  <span class="status" id="status">连接中...</span>

</div>

<div class="main" id="mainContainer">

  <div class="terminal-panel">

    <div class="panel-header">💬 微信历史</div>

    <div class="panel-half top" id="historyPanel"></div>

    <div class="panel-split">

      <div class="panel-header" style="border-top:1px solid #30363d">🤖 伯仕思考</div>

      <div class="panel-half bottom" id="thinkingPanel"></div>

    </div>

  </div>

  <div class="chat-panel">

    <div class="chat-messages" id="chatMessages">

    </div>

    <div class="input-area">

      <button class="img-btn" id="voiceBtn" title="语音输入" onclick="startVoice()">🎤</button>

      <input type="file" id="imgInput" accept="image/*" style="display:none" onchange="uploadImage(this.files)">

      <textarea id="inputBox" rows="2" placeholder="输入消息... (Shift+Enter 换行; Ctrl+V 贴图)" onkeydown="onKeydown(event)" onpaste="onPaste(event)"></textarea>

      <button class="img-btn" id="imgBtn" title="上传图片" onclick="document.getElementById('imgInput').click()">🖼️</button>

      <button class="send-btn" id="sendBtn" onclick="sendMessage()">发送</button>

    </div>

  </div>

</div>

<script>

let HISTORY=document.getElementById('historyPanel');

let THINKING=document.getElementById('thinkingPanel');

const CHAT=document.getElementById('chatMessages');

const INP=document.getElementById('inputBox');

const BTN=document.getElementById('sendBtn');

const STS=document.getElementById('status');

const MODE=document.getElementById('modeBtn');

const MAIN=document.getElementById('mainContainer');

let WS, processing=false, splitMode=localStorage.getItem('workstation_splitMode')==='true', convergeOn=false, historyLastId=0;

// 加载持久化的模式

if(splitMode){MODE.textContent='分屏';MODE.className='mode-btn active';MAIN.className='main'}else{MODE.textContent='聊天';MODE.className='mode-btn';MAIN.className='main chat-mode'}



function toggleMode(){

  splitMode=!splitMode;

  localStorage.setItem('workstation_splitMode', splitMode);

  MODE.textContent=splitMode?'分屏':'聊天';

  MODE.className='mode-btn'+(splitMode?' active':'');

  MAIN.className=splitMode?'main':'main chat-mode';

}



function connect(){

  const p=location.protocol==='https:'?'wss:':'ws:';

  WS=new WebSocket(p+'//'+location.host+'/ws');

  WS.onopen=()=>{

    STS.textContent='已连接';STS.style.color='#3fb950';

    // 按汇聚模式设置决定是否拉历史

    WS.send(JSON.stringify({type:'get_converge'}));

    if(!window._histTimer){window._histTimer=setInterval(()=>{

      if(WS.readyState===1 && convergeOn){WS.send(JSON.stringify({type:'load_history',last_id:historyLastId}))}

    },5000)}};

  WS.onclose=()=>{STS.textContent='断开';STS.style.color='#f85149';if(window._histTimer){clearInterval(window._histTimer);window._histTimer=null};setTimeout(connect,3000)};

  WS.onmessage=e=>{

    const d=JSON.parse(e.data);

    if(d.type==='terminal'){

      let panel;

      if(d.category==='history'){

        panel=HISTORY;

      }else{

        panel=THINKING;

      }

      const div=document.createElement('div');

      let cls='terminal-line';

      if(d.category){

        if(d.category==='history'){

          cls+=' history';

          if(d.role==='user') cls+=' user';

          else if(d.role==='assistant') cls+=' assistant';

        }else{

          cls+=' '+d.category;

        }

      }

      div.className=cls;

      div.textContent=d.text;

      panel.appendChild(div);

      panel.scrollTop=panel.scrollHeight

    }

    else if(d.type==='clear_history'){HISTORY.innerHTML=''}

    else if(d.type==='history_done'){historyLastId=d.last_id}

    else if(d.type==='history_heartbeat'){/* 没有新消息，心跳保持 */}

    else if(d.type==='assistant_reply'){addChat(d.content,'assistant');processing=false;BTN.disabled=false;BTN.textContent='发送'}

    else if(d.type==='status'){if(d.status==='processing'){processing=true;BTN.disabled=true;BTN.textContent='思考中...';STS.textContent='思考中...'}else{processing=false;BTN.disabled=false;BTN.textContent='发送';STS.textContent='就绪'}}

    else if(d.type==='error'){addChat('错误: '+d.text,'assistant');processing=false;BTN.disabled=false;BTN.textContent='发送';STS.textContent='错误'}

    else if(d.type==='content_done'){processing=false;BTN.disabled=false;BTN.textContent='发送';STS.textContent='就绪'}

    else if(d.type==='converge_mode'){convergeOn=d.converge_enabled!==undefined?d.converge_enabled:d.enabled;document.getElementById('convergeBtn').textContent=convergeOn?'汇聚':'独立';document.getElementById('convergeBtn').className='mode-btn'+(convergeOn?' active':'')}

  };

}



function toggleConverge(){

  convergeOn=!convergeOn;

  document.getElementById('convergeBtn').textContent=convergeOn?'汇聚':'独立';

  document.getElementById('convergeBtn').className='mode-btn'+(convergeOn?' active':'');

  WS.send(JSON.stringify({type:'toggle_converge'}));

  toast(convergeOn?'汇聚模式：对话双向同步':'独立模式：工作台对话不广播');

}



function addChat(t,r){

  const div=document.createElement('div');div.className='message '+r;

  const b=document.createElement('div');b.className='bubble';b.textContent=t;div.appendChild(b);

  CHAT.appendChild(div);CHAT.scrollTop=CHAT.scrollHeight

}



function onKeydown(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage()}}

function sendMessage(){const t=INP.value.trim();if(!t||processing)return;INP.value='';addChat(t,'user');WS.send(JSON.stringify({type:'chat',content:t}))}



function toast(t){const d=document.createElement('div');d.className='toast';d.textContent=t;document.body.appendChild(d);setTimeout(()=>d.remove(),3500)}



async function uploadImage(files){

  if(!files.length)return;

  const img=files[0];

  if(!img.type.startsWith('image/')){toast('只支持图片文件');return}

  if(img.size>20*1024*1024){toast('图片太大，请压缩后上传（限20MB）');return}

  const fd=new FormData();fd.append('image',img);

  try{

    const r=await fetch('/upload',{method:'POST',body:fd});

    const j=await r.json();

    if(j.ok){addChat('📷 已上传图片','user');WS.send(JSON.stringify({type:'new_image',path:j.path}));toast('图片已发送，伯仕正在查看...')}

    else toast('上传失败: '+j.error)

  }catch(e){toast('上传失败')}

}



function onPaste(e){

  const items=e.clipboardData.items;

  for(let i=0;i<items.length;i++){

    if(items[i].type.startsWith('image/')){

      e.preventDefault();

      const blob=items[i].getAsFile();

      uploadImage([blob]);

      return

    }

  }

}



// 🎤 语音输入

let mediaRecorder = null;

let audioChunks = [];

let isRecording = false;

const VOICE_BTN = document.getElementById('voiceBtn');



function startVoice(){

  if(isRecording){

    // 停止录音

    stopRecording();

    return;

  }

  // 检查浏览器支持

  if(!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia){

    toast('您的浏览器不支持语音输入，请用 Chrome/Edge');

    return;

  }

  // 开始录音

  navigator.mediaDevices.getUserMedia({audio:true}).then(stream=>{

    mediaRecorder = new MediaRecorder(stream);

    audioChunks = [];

    isRecording = true;

    VOICE_BTN.textContent = '🔴';

    VOICE_BTN.style.borderColor = '#f85149';

    VOICE_BTN.style.color = '#f85149';

    toast('🎤 录音中... 说完点一下停止');



    mediaRecorder.ondataavailable = e => audioChunks.push(e.data);

    mediaRecorder.onstop = async () => {

      VOICE_BTN.textContent = '🎤';

      VOICE_BTN.style.borderColor = '';

      VOICE_BTN.style.color = '';

      isRecording = false;

      stream.getTracks().forEach(t=>t.stop());



      // 上传音频

      const blob = new Blob(audioChunks, {type:'audio/webm'});

      if(blob.size < 100){toast('录音太短'); return}

      toast('正在识别语音...');

      const fd = new FormData();

      fd.append('audio', blob, 'voice.webm');

      try{

        const r = await fetch('/stt', {method:'POST', body:fd});

        const j = await r.json();

        if(j.ok && j.text){

          INP.value = j.text;

          toast('✅ 识别完成，按发送');

          INP.focus();

        } else {

          toast('识别失败: ' + (j.error||'未知错误'));

        }

      }catch(e){toast('语音识别失败')}

    };



    mediaRecorder.start();

  }).catch(e=>{

    if(e.name==='NotAllowedError') toast('请允许麦克风权限');

    else toast('麦克风错误: '+e.message);

  });

}



function stopRecording(){

  if(mediaRecorder && mediaRecorder.state !== 'inactive'){

    mediaRecorder.stop();

  }

}

connect();

</script>

</body>

</html>"""

    return web.Response(text=html, content_type="text/html")





async def upload_handler(request):

    """接收图片上传，保存到 D:\\boshi_images\\"""

    try:

        reader = await request.multipart()

        field = await reader.next()

        if not field or field.name != "image":

            return web.json_response({"ok": False, "error": "未找到图片字段"})



        # 读取图片数据

        data = await field.read()

        if len(data) > 20 * 1024 * 1024:

            return web.json_response({"ok": False, "error": "图片超过20MB限制"})



        # 生成唯一文件名，保留原扩展名

        ext = os.path.splitext(field.filename or "image.png")[1] or ".png"

        fname = f"boshi_img_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time())}{ext}"

        save_dir = "D:\\boshi_images"

        os.makedirs(save_dir, exist_ok=True)

        save_path = os.path.join(save_dir, fname)



        with open(save_path, "wb") as f:

            f.write(data)



        log.info(f"图片已保存: {save_path} ({len(data)} bytes)")



        # 通知工作台有新图片

        await push_to_ws("image_notify", f"新图片: {save_path}", {"path": save_path})



        return web.json_response({"ok": True, "path": save_path, "filename": fname})

    except Exception as e:

        log.error(f"上传失败: {e}")

        return web.json_response({"ok": False, "error": str(e)})





async def stt_handler(request):

    """接收音频，调 FunASR 语音识别"""

    try:

        reader = await request.multipart()

        field = await reader.next()

        if not field or field.name != "audio":

            return web.json_response({"ok": False, "error": "未找到音频"})



        data = await field.read()

        if len(data) < 100:

            return web.json_response({"ok": False, "error": "音频太短"})



        # 保存音频文件

        fname = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time())}.webm"

        audio_dir = "D:\\boshi_audio"

        os.makedirs(audio_dir, exist_ok=True)

        audio_path = os.path.join(audio_dir, fname)



        with open(audio_path, "wb") as f:

            f.write(data)



        log.info(f"音频已保存: {audio_path} ({len(data)} bytes)")



        # 调 FunASR 识别

        text = await asyncio.get_event_loop().run_in_executor(

            None, lambda: run_stt(audio_path)

        )



        log.info(f"语音识别结果: {text}")

        return web.json_response({"ok": True, "text": text, "path": audio_path})

    except Exception as e:

        log.error(f"语音识别失败: {e}")

        return web.json_response({"ok": False, "error": str(e)})





def run_stt(audio_path):

    """在子线程中运行语音识别（避免阻塞事件循环）"""

    try:

        sys.path.insert(0, os.path.expanduser("~/.boshi/stt"))

        from voice_service import recognize_file

        text = recognize_file(audio_path)

        # 二次过滤，确保标签被清理

        import re

        text = re.sub(r'<\|[^>]+\|>', '', text).strip()

        return text

    except Exception as e:

        return f"【识别错误: {e}】"





async def push_history_handler(request):

    """外部 API：推送一条微信消息到左边栏"""

    try:

        raw = await request.read()

        body = json.loads(raw.decode('utf-8'))

        role = body.get("role", "user")

        content = body.get("content", "")

        if not content:

            return web.json_response({"ok": False, "error": "content required"})

        

        role_icon = "🧑" if role == "user" else "🤖"

        line = f"{role_icon} {content[:200]}"

        await push_to_ws("terminal", line, {"category": "history", "role": role})

        log.info(f"微信→工作台 推送: {content[:50]}...")

        return web.json_response({"ok": True})

    except Exception as e:

        log.error(f"推送历史失败: {e}")

        return web.json_response({"ok": False, "error": str(e)})





async def run_app():

    app = web.Application()

    app.router.add_get('/', index_handler)

    app.router.add_get('/ws', websocket_handler)

    app.router.add_post('/upload', upload_handler)

    app.router.add_post('/stt', stt_handler)

    app.router.add_post('/push_history', push_history_handler)

    runner = web.AppRunner(app)

    await runner.setup()

    site = web.TCPSite(runner, HOST, PORT)

    await site.start()

    log.info(f"工作台 v6 启动 http://127.0.0.1:{PORT}")

    log.info(f"汇聚模式: {'开启' if converge_enabled else '关闭'}")

    await asyncio.Event().wait()





if __name__ == '__main__':

    try:

        s = socket.socket()

        s.settimeout(0.5)

        if s.connect_ex(('127.0.0.1', PORT)) == 0:

            s.close()

            result = os.popen(f'netstat -ano | findstr :{PORT} | findstr LISTEN').read()

            if result:

                pid = int(result.strip().split()[-1])

                os.system(f'taskkill /f /pid {pid} 2>nul')

                time.sleep(1)

        else:

            s.close()

    except:

        pass

    load_converge_mode()

    asyncio.run(run_app())

