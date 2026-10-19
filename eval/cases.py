# -*- coding: utf-8 -*-
"""A5/A6/A7 共用的 gold 评测集：(query, gold_id8, anchor)。

anchor 是该 gold 记忆必须逐字包含的字面串，用于自检 gold 没有记错
（eval/check_gold.py 会 import 它并逐条核对）。
"""
CASES = [
    ("218 router 启动清单 17 条 一个模型两个条目", "70cd3d35", "17 条"),
    ("P4 中医知识库 tensor 32B-VL n_slots 4 验收", "55cc3251", "n_slots=4"),
    ("禁用 QQ 右键菜单 DisabledShellExtensions 共几项", "4b17de7c", "DisabledShellExtensions"),
    ("Xeon E5-2696 v4 GRD 软件编码 远程桌面 对比", "38ff6ff1", "E5-2696"),
    ("embedding 服务 11436 隐藏窗口 自动拉起", "a5ad3de9", "11436"),
    ("218 vLLM v0.2.2-post2 驱动 595.91.07 CUDA13.0 复刻", "abb74ac7", "0.2.2-post2"),
    ("ComfyUI Manager 启动 9 次报错 model-list.json", "c6db5b3f", "model-list.json"),
    ("DSH 550K 上下文 改的是 models.dev limit.context", "e9264528", "550K"),
    ("辞典 E26 主件漏 768 词条 九界", "4062f4c8", "768"),
    ("agnes-ai 7 个视觉模型哪几个能看图", "d2ccef88", "7 个"),
    ("Qwen3.8-Flash-Next MTP Q8_0 4.14GB 自包含", "3691a63e", "4.14"),
    ("本机驱动 610.47 CUDA UMD 13.3 能跑哪个预编译包", "fd922a5f", "610.47"),
    ("llama 20.4 tok/s 内核 7.0 gdm 停 595.84", "e5b61c3d", "20.4"),
    ("wsl_update.msi 删除 拒绝访问 脚本假阳性", "c08f0272", "wsl_update.msi"),
    ("MTP N=4 N≥7 有损 DFlash2 无损 md5 逐字相同", "bf3e2bc0", "DFlash2"),
    ("195 个月 缴到退休 够不够 25 年 要不要补", "91492b69", "195"),
    ("218 加载 occamy-1.0-Q6_K 34.6B 代理到 53653", "1f682f5d", "occamy"),
    ("电源睡眠 900 秒 0x384 电池 0x2D0", "dd6c29c4", "0x384"),
    ("PLC 巴汉辞典 dila TEI 庄春江 本机有没有中巴方向", "224e4305", "dila"),
    ("格桑舞 ref2va 参考图 首帧 灰斑 贴脸融合失败", "5775c9f8", "灰斑"),
    ("Qwythos-9B 加载失败 ctx 512 根因", "7d944c53", "Qwythos"),
    ("192K 和 64K 对比 30K 上下文 llama-server timing", "66b6de8f", "192K"),
    ("opencode v2.0.16 不提供 @opencode/plugin 服务端插件", "0a07f5a4", "v2.0.16"),
    ("/data 机械盘顺序读 109 MB/s tcm 144 MB/s", "e79e3fe9", "109 MB/s"),
    ("思考模式会不会降低 tok/s", "0ce918be", "思考模式"),
    ("218 双 2080Ti 各 22528 MiB 总显存 44G", "90161ac0", "22528"),
    ("strata expert arena 39 GiB 2 MiB 连续内存块", "08e4fa8e", "39 GiB"),
    ("自在吟 MV 19 镜 5165 帧 215.25s 定稿", "59fdee0a", "5165"),
    ("llm-compressor compressed-tensors 在 vLLM 原生支持吗", "6859b441", "llm-compressor"),
    ("strata 0.1.40.4 双卡 layer split across GPUs", "9335d44b", "0.1.40.4"),
    ("218 strata 0.1.43.1 升到 0.1.44.1 全过程", "3462e8fb", "0.1.44.1"),
    ("opencode 免费模型调不通 重启路由器 换公网出口 IP 恢复", "e83a096c", "路由器"),
    ("魔搭 HF 断点续传 状态文件 upload_state", "162c7454", "upload_state"),
    ("Qwen-Image-2.1-Turbo 8 步 CFG 必须 1.0", "1b7390dd", "Turbo"),
    ("Qwen3.5-27B 和 Qwen3.6-27B 架构 64 层 48 层", "94f91246", "48 层"),
]
