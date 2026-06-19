"""
补丁管理器：Hermes 升级后自动重打工作台自启动补丁
=================================================
用法：
  python ~/.boshi/gateway_patch/apply_patches.py [--check]

--check：仅检查补丁是否已应用，不修改文件
不带参数：检查并应用补丁
"""

import os
import re
import sys


def get_run_py_path() -> str:
    """找到 run.py 的路径"""
    candidates = [
        os.path.join(os.environ.get('LOCALAPPDATA', ''),
                     'hermes', 'hermes-agent', 'gateway', 'run.py'),
        os.path.expanduser('~/AppData/Local/hermes/hermes-agent/gateway/run.py'),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[0]


# 补丁：在 return True 前加工作台启动调用的标记
PATCH_MARKER_START = "# ── 自动启动伯仕工作台 (7681)"
PATCH_METHOD_MARKER = 'async def _auto_launch_workstation'


def is_patched(run_py: str) -> bool:
    """检查补丁是否已应用"""
    if not os.path.isfile(run_py):
        return False
    with open(run_py, 'r', encoding='utf-8') as f:
        content = f.read()
    return PATCH_METHOD_MARKER in content


def apply_patch(run_py: str) -> bool:
    """应用补丁，返回是否成功"""
    if is_patched(run_py):
        print(f"✅ 补丁已存在: {run_py}")
        return True

    with open(run_py, 'r', encoding='utf-8') as f:
        content = f.read()

    # 补丁1: 在 return True 前加入工作台启动调用
    # 找第一个 '_launched_workstation'
    if PATCH_MARKER_START not in content:
        # 在最后一个 return True 前插入
        # 找 logger.info("Press Ctrl+C to stop") 后面的 return True
        old = 'logger.info("Press Ctrl+C to stop")\n        \n        return True'
        new = '''logger.info("Press Ctrl+C to stop")
        
        # ── 自动启动伯仕工作台 (7681) ──────────────────────────
        if not self._launched_workstation:
            self._launched_workstation = True
            asyncio.create_task(self._auto_launch_workstation())
        
        return True'''
        if old in content:
            content = content.replace(old, new, 1)

    # 补丁2: 加 _launched_workstation 属性
    if 'self._launched_workstation = False' not in content:
        old = 'self._background_tasks: set = set()'
        new = 'self._background_tasks: set = set()\n        \n        # Auto-launch workstation flag\n        self._launched_workstation = False'
        content = content.replace(old, new, 1)

    # 补丁3: 加 _auto_launch_workstation 方法（在 _active_profile_name 前）
    if PATCH_METHOD_MARKER not in content:
        method_code = '''
    async def _auto_launch_workstation(self) -> None:
        """自动启动伯仕工作台 (7681端口)"""
        import os
        import socket
        import subprocess
        import sys
        
        script = os.path.expanduser("~/.boshi/workstation/伯仕工作台.pyw")
        
        def _port_in_use(port: int) -> bool:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.5)
                result = s.connect_ex(("127.0.0.1", port))
                s.close()
                return result == 0
            except Exception:
                return False
        
        if _port_in_use(7681):
            logger.info("工作台 7681 已在运行")
            return
        
        if not os.path.isfile(script):
            logger.warning("工作台脚本不存在: %s", script)
            return
        
        python = sys.executable
        cmd = f'start /B "" "{python}" "{script}"'
        subprocess.Popen(cmd, shell=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        logger.info("工作台启动命令已提交: http://127.0.0.1:7681")
        
        for _ in range(6):
            await asyncio.sleep(0.5)
            if _port_in_use(7681):
                logger.info("工作台启动成功 ✅ http://127.0.0.1:7681")
                return
        logger.warning("工作台启动后未检测到 7681 端口")

    def _active_profile_name(self) -> str:'''
        content = content.replace(
            "def _active_profile_name(self) -> str:",
            method_code,
            1
        )

    try:
        with open(run_py, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"✅ 补丁已应用: {run_py}")
        return True
    except Exception as e:
        print(f"❌ 补丁应用失败: {e}")
        return False


if __name__ == '__main__':
    check = '--check' in sys.argv
    run_py = get_run_py_path()
    
    if not os.path.isfile(run_py):
        print(f"❌ 找不到: {run_py}")
        sys.exit(1)
    
    if check:
        if is_patched(run_py):
            print(f"✅ 补丁已应用: {run_py}")
        else:
            print(f"❌ 补丁未应用: {run_py}")
        sys.exit(0)
    
    apply_patch(run_py)
