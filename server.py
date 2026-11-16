#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
skill-router — 按需调取 GitHub 上的 agent skills，零全量下载。

skill 的本质是提示词。skill_load 直接返回 SKILL.md 全文，
模型读到即等于 skill 已加载，无需落地到 skills 目录、无需重启会话。

实现的 12 个机制
  1. 布局自适应探测       任意 SKILL.md 布局（根级单 skill / 2~4 层嵌套）
  2. default_branch 实读  不假设 main（iothackbot 是 master）
  3. Trees 失败 fallback  超大仓库退化为 contents 递归
  4. GITHUB_TOKEN         60/h -> 5000/h
  5. jsDelivr 镜像 fallback
  6. 磁盘缓存             cache/{repo}/{sha}/{path}
  7. lite / full 两段式    省约 80% 上下文
  8. skill_unload         阶段卸载，防上下文累积
  9. active_skill 仲裁     多 skill 指令冲突时以主 skill 为准
 10. 上下文预算闸门        40000 tokens 硬上限
 11. skill_hint          信号驱动重检索（让「自动换 skill」真正发生）
 12. 邻接图              if_blocked / if_shell / if_leak

环境变量
  GITHUB_TOKEN / GH_TOKEN  可选，强烈建议（83x 配额）
  SKILL_ROUTER_HOME        可选，默认本文件所在目录
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import subprocess
import signal
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import evolve as _ev
import experience as _exp   # 自成长统计（可选，缺失时自动降级）

try:
    from mcp.server.mcpserver import MCPServer            # mcp 2.x（FastMCP 已改名）
except ImportError:                                       # mcp 1.x 兜底
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore

# ──────────────────────────── 路径与常量 ────────────────────────────

HOME = Path(os.environ.get("SKILL_ROUTER_HOME") or Path(__file__).resolve().parent)


# ──────────────────────────── 错误日志 ────────────────────────────
# 为什么需要：42 处裸 except 让故障静默消失 —— 索引损坏、网络失败、JSON 解析错
# 全都无声无息，只能靠「结果不对」去猜。这里只加记录，不改任何控制流。

LOG_FILE = HOME / "logs" / "error.log"


def _warn(where: str, e: BaseException) -> None:
    """记录被 except 吞掉的异常。永远不抛，不打断主流程。"""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{where}] "
                    f"{type(e).__name__}: {e}\n")
    except Exception:
        # 日志本身失败必须静默 —— 不能再调 _warn，否则递归
        pass


INDEX_FILE = HOME / "index.json"
REPOS_FILE = HOME / "repos.json"
CACHE_DIR = HOME / "cache"

TOKEN = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()

SKILL_RE = re.compile(r"(^|/)SKILL\.md$", re.I)
MAX_SKILL_TOKENS = 40_000      # 单任务 skill 注入硬上限
SINGLE_SKILL_MAX_BYTES = 30_000  # 单次 full 返回上限，超出提示用 read_ref
UA = "skill-router/1.0 (+local)"

RAW_MIRRORS = (
    "https://raw.githubusercontent.com/{repo}/{sha}/{path}",
    "https://cdn.jsdelivr.net/gh/{repo}@{sha}/{path}",
    "https://raw.gitmirror.com/{repo}/{sha}/{path}",
)

# 零 GitHub 配额的元数据源：不耗 60/h 限额，也不需要 token
UNGH = "https://ungh.cc"

# 壳名/专有工具名 → 直接指向具体 skill。
# 这些词在 skill 的 description 里几乎不出现，靠 desc 匹配永远命中不了。
QUERY_BOOST: dict[str, tuple[str, ...]] = {
    "themida": ["packer-and-loader-analysis", "vmp-reverse-skill", "native-unpacking"],
    "winlicense": ["packer-and-loader-analysis", "vmp-reverse-skill"],
    "enigma": ["packer-and-loader-analysis", "native-unpacking"],
    "obsidium": ["packer-and-loader-analysis"],
    "safengine": ["packer-and-loader-analysis"],
    "vmprotect": ["vmp-reverse-skill", "virtualization-protection"],
    "vmp": ["vmp-reverse-skill", "virtualization-protection", "jsvmp-vmp-analysis"],
    "devirtualization": ["vmp-reverse-skill"],
    "ollvm": ["ollvm-deobfuscation", "ollvm-recovery-workflow"],
    "upx": ["native-unpacking", "packer-and-loader-analysis"],
    "confuser": ["dotnet-malware-deobfuscation", "dotnet-reversing"],
    "pyinstaller": ["python-frozen-app-reversing"],
    "frida": ["rev-frida", "frida-dynamic-analysis", "frida-stealth-hooking"],
    "ida": ["rev-idapython", "ida-ghidra-workflow", "ida-export-automation"],
    "ghidra": ["ghidra-headless-analysis", "ida-ghidra-workflow"],
    "unicorn": ["rev-unicorn-debug", "emulation-unicorn-angr"],
    "keygen": ["alice-crack", "client-license-validation-bypass"],
    "cardkey": ["alice-crack", "client-license-validation-bypass"],
    "密码学": ["algorithm-identification", "custom-obfuscation-reverse", "prng-randomness-cracking"],
    "加密算法": ["algorithm-identification", "custom-obfuscation-reverse"],
    "随机数": ["prng-randomness-cracking"],
    "外挂": [
        "memory-hacking", "packet-interception", "esp-wallhack-rendering", "anti-cheat-bypass",
        "speedhack-time-manipulation"
    ],
}

# 中文查询 → 英文检索词展开。skill 名和描述都是英文，纯中文查询必然零命中。
SYNONYMS: dict[str, tuple[str, ...]] = {
    "后台": ["admin", "auth", "login", "panel", "backend", "authentication"],
    "认证": ["auth", "authentication", "jwt", "oauth", "session", "login"],
    "登录": ["login", "auth", "session", "credential"],
    "绕过": ["bypass", "evasion", "waf"],
    "注入": ["injection", "sqli", "ssti", "xxe", "command"],
    "提权": ["privesc", "privilege", "escalation"],
    "内网": ["lateral", "active-directory", "internal", "pivot"],
    "横向": ["lateral-movement", "pivot"],
    "上传": ["upload", "file-upload"],
    "反序列化": ["deserialization", "deserial"],
    "越权": ["idor", "authorization", "access-control"],
    "侦察": ["recon", "reconnaissance", "enumeration"],
    "信息收集": ["recon", "reconnaissance", "osint", "techstack"],
    "指纹": ["techstack", "fingerprint", "identification"],
    "隧道": ["tunnel", "pivot", "proxy"],
    "凭据": ["credential", "hash", "dump"],
    "免杀": ["免杀", "evasion", "av-bypass", "obfuscation", "amsi", "杀软", "EDR", "Defender"],
    "持久化": ["persistence"],
    "外带": ["exfiltration"],
    "报告": ["report", "writeup", "engagement"],
    "域": ["active-directory", "adcs", "kerberos", "ldap"],
    "证书": ["adcs", "certificate", "esc"],
    "中间件": ["server-side", "infrastructure", "network-appliance"],
    "移动端": ["mobile", "apk", "android"],
    "审计": ["audit", "review", "static-analysis"],
    "供应链": ["supply-chain"],
    "云": ["cloud", "container", "k8s"],
    "逆向": ["reverse", "reversing", "decompile", "disassemble", "analysis"],
    "反编译": ["decompile", "disassemble", "ida", "ghidra", "jadx"],
    "脱壳": ["unpack", "unpacking", "dump", "dex-dumper"],
    "加固": ["packer", "protection", "obfuscation", "hardening"],
    "反调试": ["anti-debug", "anti-analysis", "anti-hook", "integrity"],
    "反检测": ["anti-detection", "anti-analysis", "evasion", "fingerprint"],
    "反作弊": ["反作弊", "anti-cheat", "EAC", "BattlEye", "Vanguard", "Ricochet"],
    "外挂": ["外挂", "游戏", "作弊", "内存"],
    "内存修改": ["内存", "修改", "读写"],
    "透视": ["透视", "视觉", "渲染"],
    "变速": ["变速", "时间", "加速"],
    "封包": ["封包", "网络包", "拦截", "抓包"],
    "密码学": ["密码学", "加密", "算法", "签名"],
    "随机数": ["随机数", "PRNG", "LCG"],
    "混淆": ["obfuscation", "obfuscated", "deobfuscation"],
    "壳": ["packer", "unpack", "protection"],
    "hook": ["hook", "hooking", "frida", "xposed"],
    "插桩": ["instrumentation", "frida", "hooking"],
    "模拟执行": ["unicorn", "emulation", "emulate", "debug"],
    "符号": ["symbol", "symbols", "signature"],
    "结构体": ["struct", "structure", "recovery"],
    "小程序": ["miniprogram", "mini-program", "wxapkg"],
    "游戏": ["game", "unity", "u3d", "il2cpp"],
    "固件": ["firmware", "uefi", "bios"],
    "协议": ["protocol", "protocol-reversing", "traffic"],
    "so": ["native", "shared-object", "elf", "arm"],
    "arm": ["arm64", "arm32", "thumb", "assembly"],
    "ios": ["ios", "ipa", "mach-o"],
    "安卓": ["android", "apk", "dex"],
    "反外挂": ["anti-cheat", "kernel"],
    "验证码": ["captcha", "geetest", "tcaptcha", "yidun", "shumei", "dingxiang"],
    "风控": ["risk", "anti-bot", "fingerprint", "detection"],
    "加密参数": ["sign", "signature", "crypto", "encrypt", "token"],
    "抖音": ["douyin", "abogus", "xbogus"],
    "淘宝": ["taobao", "mtop", "h5st"],
    "知乎": ["zhihu", "zse"],
    "小红书": ["xiaohongshu", "xs", "xt"],
    "极验": ["geetest"],
    "瑞数": ["ruishu", "rs"],
    "数美": ["shumei"],
    "顶象": ["dingxiang"],
    "阿里": ["aliyun", "nvc", "baxia"],
    "腾讯验证码": ["tcaptcha"],
    "网易易盾": ["yidun"],
    "cloudflare": ["cloudflare", "turnstile", "waf"],
    "akamai": ["akamai", "bm"],
    "js逆向": ["javascript", "deobfuscation", "obfuscator", "jsvmp"],
    "webpack": ["webpack", "vite", "nextjs", "bundle"],
    "wasm": ["wasm", "webassembly"],
    "electron": ["electron"],
    "flutter": ["flutter", "dart"],
    "unity": ["unity", "il2cpp"],
    "恶意样本": ["malware", "triage", "sandbox"],
    "内核": ["kernel", "driver", "ebpf"],
    "ida": ["idapython", "ida-ghidra", "ghidra", "rizin"],
    "脚本": ["idapython", "script", "automation"],
    "静态分析": ["static-analysis", "native-binary", "ghidra-headless"],
    "卡密": ["license", "card-key", "activation", "keygen", "serial", "crack"],
    "注册机": ["keygen", "license", "crack", "serial"],
    "授权": ["license", "authorization", "activation", "entitlement"],
    "激活": ["activation", "activate", "license"],
    "许可证": ["license"],
    "机器码": ["machine-code", "hardware-bound", "fingerprint"],
    "试用": ["trial", "license"],
    "破解": ["crack", "patch", "bypass"],
    "序列号": ["serial", "keygen", "license"],
    "加密狗": ["dongle", "hardware-bound", "license"],
    "脱机": ["offline", "offline-license", "relay"],
    "校验": ["validation", "check", "verification"],
    "爆破": ["patch", "brute", "crack"],
    "跳转": ["jump", "patch", "branch"],
    "vmp": ["vmp", "virtualization", "devirtualization", "dispatcher", "handler"],
    "虚拟化": ["virtualization", "devirtualization", "vmp"],
    "去虚拟化": ["devirtualization", "vmp", "dispatcher"],
    "dispatcher": ["dispatcher", "handler", "vip"],
    "handler": ["handler", "dispatcher", "bytecode"],
    "字节码": ["bytecode", "dispatcher", "handler"],
    "vmprotect": ["vmprotect", "vmp", "virtualization"],
    "themida": ["themida", "winlicense", "packer"],
    "winlicense": ["winlicense", "themida", "packer"],
    "enigma": ["enigma", "protector", "packer"],
    "ollvm": ["ollvm", "control-flow-flattening", "indirect-jump"],
    "平坦化": ["control-flow-flattening", "ollvm", "flatten"],
    "花指令": ["junk-code", "bogus", "obfuscation"],
    "内存dump": ["memory-dump", "memdump", "dump-fix"],
    "重建": ["rebuild", "reconstruction", "dump-fix"],
    "缓存服务": ["redis", "unauthorized", "cache", "memcached"],
    "没设密码": ["unauthorized", "no-auth", "redis", "default-cred"],
    "登录框": ["login", "auth", "authentication", "admin-panel"],
    "改参数": ["injection", "sqli", "param", "tamper"],
    "别人订单": ["idor", "access-control", "bac", "horizontal"],
    "参数对不上": ["sign", "signature", "crypto", "protocol"],
    "等 5 秒": ["turnstile", "captcha", "challenge", "anti-bot"],
    "等5秒": ["turnstile", "captcha", "challenge", "anti-bot"],
    "一调试就崩": ["anti-debug", "anti-analysis", "ptrace"],
    "读到配置": ["actuator", "config-leak", "env", "heapdump"],
    "提示要root": ["root-detection", "emulator-detection", "android"],
    "提示要 root": ["root-detection", "emulator-detection", "android"],
    "金币": ["cheat", "game", "memory-hacking"],
    "内存搜不到": ["memory-hacking", "cheat", "memory-scan"],
    "参数是加密的": ["sign", "crypto", "protocol", "signature"],
    "加密的": ["crypto", "sign", "encrypt"],
    # ── 零命中补词（实测 109 条常见查询里 7 条零命中）──
    "社工": ["social-engineering", "phishing", "pretext", "osint"],
    "计划任务": ["persistence", "scheduled-task", "cron", "autorun"],
    "shiro": ["shiro", "java", "deserialization", "rememberme"],
    "fastjson": ["fastjson", "java", "deserialization", "jndi"],
    "403": ["waf", "bypass", "forbidden"],
    "限速": ["rate-limit", "bypass", "throttle"],
    "host头": ["host-header", "virtual-host", "x-forwarded-host"],
}

# 邻接图：按 skill 名关键词匹配，给出「被拦/拿到 shell/信息泄露」后的下一步
ADJACENCY: dict[str, dict[str, str]] = {
    "sqli": {
        "if_blocked": "offensive-waf-bypass",
        "if_shell": "offensive-file-upload",
        "if_leak": "offensive-idor",
    },
    "xss": {
        "if_blocked": "offensive-waf-bypass",
        "if_leak": "offensive-idor",
    },
    "ssrf": {
        "if_leak": "offensive-idor",
        "if_shell": "offensive-rce",
    },
    "rce": {
        "if_shell": "offensive-file-upload",
        "if_leak": "offensive-idor",
    },
    "file-upload": {
        "if_blocked": "offensive-waf-bypass",
        "if_shell": "offensive-persistence",
    },
    "deserial": {
        "if_shell": "offensive-rce",
        "if_blocked": "offensive-waf-bypass",
    },
    "ssti": {
        "if_shell": "offensive-rce",
    },
    "idor": {
        "if_leak": "offensive-data-exfiltration",
        "if_shell": "offensive-file-upload",
    },
    "jwt": {
        "if_leak": "offensive-idor",
        "if_blocked": "offensive-waf-bypass",
    },
    "auth": {
        "if_leak": "offensive-idor",
        "if_shell": "offensive-file-upload",
    },
    "waf": {
        "if_blocked": "offensive-request-smuggling",
    },
    "recon": {
        "if_leak": "attack-path-stitcher",
    },
    "privesc": {
        "if_shell": "offensive-persistence",
    },
    "active-dir": {
        "if_shell": "adcs-attacks",
        "if_leak": "acl-abuse",
    },
    "netexec": {
        "if_leak": "acl-abuse",
    },
    "lateral": {
        "if_leak": "offensive-data-exfiltration",
    },
    "adcs": {
        "if_leak": "acl-abuse",
        "if_shell": "offensive-persistence",
    },
    "dex-dumper": {
        "if_blocked": "anti-analysis-and-integrity",
        "if_shell": "android-static-analysis",
    },
    "frida": {
        "if_blocked": "anti-hook-artifact-analysis",
        "if_shell": "android-crypto-hooking",
    },
    "idapython": {
        "if_blocked": "rev-unicorn-debug",
    },
    "unpacking": {
        "if_blocked": "apk-protection-analysis",
        "if_shell": "android-static-analysis",
    },
    "static-analysis": {
        "if_blocked": "anti-analysis-and-integrity",
        "if_shell": "android-runtime-hooking",
    },
    "anti-analysis": {
        "if_shell": "android-runtime-hooking",
    },
    "apk-protection": {
        "if_blocked": "anti-analysis-and-integrity",
        "if_shell": "android-unpacking-and-dumping",
    },
    "arm64": {
        "if_blocked": "rev-unicorn-debug",
    },
    "u3d": {
        "if_blocked": "anti-analysis-and-integrity",
    },
    "ios-dump": {
        "if_blocked": "anti-analysis-and-integrity",
    },
    "license": {
        "if_blocked": "apk-protection-analysis",
        "if_shell": "alice-crack",
    },
    "crack": {
        "if_blocked": "anti-analysis-and-integrity",
        "if_shell": "alice-crack",
    },
    "keygen": {
        "if_blocked": "alice-crack",
    },
    "patch": {
        "if_blocked": "anti-analysis-and-integrity",
        "if_shell": "alice-crack",
    },
    "vmp": {
        "if_blocked": "packer-and-loader-analysis",
        "if_shell": "emulation-unicorn-angr",
    },
    "virtualiz": {
        "if_blocked": "vmp-reverse",
        "if_shell": "dump-fix-rebuild",
    },
    "unpack": {
        "if_blocked": "packer-and-loader-analysis",
        "if_shell": "dump-fix-rebuild",
    },
    "packer": {
        "if_blocked": "vmp-reverse",
        "if_shell": "dump-fix-rebuild",
    },
    "ollvm": {
        "if_blocked": "ollvm-recovery-workflow",
    },
    "obfusc": {
        "if_blocked": "ollvm-deobfuscation",
    },
    "dump": {
        "if_blocked": "memdumper-artifact-validation",
        "if_shell": "dump-fix-rebuild",
    },
}

# 信号 → 推荐 skill（skill_hint 的核心规则表）
HINT_RULES: list[tuple[tuple[str, ...], tuple[str, ...], str]] = [
    [
        ["actuator", "spring", "heapdump", "env 端点", "jolokia"],
        ["server-side", "offensive-rce", "offensive-deserialization"],
        "Spring 生态暴露常直接通向 RCE / 反序列化",
    ],
    [
        ["redis", "6379", "未授权", "unauthorized", "mongo", "27017", "elastic", "9200"],
        ["offensive-linux-privesc", "offensive-lateral-movement", "offensive-data-exfiltration"],
        "未授权中间件通常直接给凭据或写文件能力",
    ],
    [
        ["jwt", "jku", "kid", "alg", "token 伪造"],
        ["offensive-jwt"],
        "JWT 结构暴露后应转向算法混淆 / jku 注入",
    ],
    [
        ["waf", "403", "cloudflare", "拦截", "blocked", "拦截页", "安全狗", "长亭"],
        ["offensive-waf-bypass", "offensive-request-smuggling"],
        "出现拦截特征，先换绕过方法论再重试",
    ],
    [
        ["上传", "upload", "multipart", "filename"],
        ["offensive-file-upload"],
        "存在上传点，优先测解析绕过",
    ],
    [
        ["反序列化", "deserial", "ysoserial", "cc1", "readobject", "gadget"],
        ["offensive-deserialization"],
        "命中反序列化入口",
    ],
    [
        ["模板", "ssti", "freemarker", "velocity", "twig", "jinja"],
        ["offensive-ssti"],
        "模板注入特征",
    ],
    [
        ["越权", "idor", "未授权访问", "遍历", "id="],
        ["offensive-idor"],
        "对象引用可枚举",
    ],
    [
        ["ldap", "kerberos", "域控", "dc", "kerberoast", "adcs", "ntlm"],
        ["offensive-active-directory", "adcs-attacks", "acl-abuse"],
        "进入内网 AD 场景",
    ],
    [
        ["apk", "android", "jadx", "frida", "smali"],
        ["apk-redteam-pipeline", "jadx"],
        "移动端目标",
    ],
    [
        ["ctf", "flag", "pwn"],
        ["ctf-web", "ctf-pwn", "ctf-crypto"],
        "CTF 题型",
    ],
    [
        ["sqli", "sql injection", "注入点", "盲注", "联合注入", "报错注入", "布尔注入", "时间盲注"],
        ["offensive-sqli", "hunt-sqli", "sqli-nosqli", "injection"],
        "命中 SQL 注入特征：先定数据库类型与回显情况，再选 union / 盲注 / 报错",
    ],
    [
        ["traceback", "stack trace", "路径泄露", "绝对路径", "源码泄露", "source leak"],
        ["source-code-scanning", "cve-poc-generator"],
        "报错泄露可用于版本定位与 CVE 匹配",
    ],
    [
        ["frida", "hook", "插桩", "xposed", "objection"],
        ["rev-frida", "android-runtime-hooking", "android-crypto-hooking"],
        "已具备动态插桩条件，转向运行时 hook",
    ],
    [
        ["脱壳", "unpack", "dump dex", "整体壳", "抽取壳", "frida-dexdump"],
        ["android-unpacking-and-dumping", "rev-dex-dumper", "apk-protection-analysis"],
        "命中加固/壳，先脱壳再静态分析",
    ],
    [
        ["ida", "ghidra", "反编译", "伪代码", "交叉引用", "xref"],
        ["rev-idapython", "rev-struct", "rev-symbol", "rev-unicorn-debug"],
        "进入静态逆向，优先恢复结构体与符号",
    ],
    [
        [".so", "native", "arm64", "arm32", "thumb", "jni"],
        ["arm64-assembly-reversing", "arm32-thumb-reversing", "android-native-detection-chain"],
        "命中 native 层，转 ARM 汇编逆向",
    ],
    [
        [
            "反调试", "anti-debug", "ptrace", "检测 frida", "完整性校验", "签名校验", "一运行就退出", "跑起来就退出", "启动就崩",
            "attach 就崩", "怀疑有检测"
        ],
        [
            "anti-analysis-and-integrity", "anti-hook-artifact-analysis",
            "android-reverse-setup-audit"
        ],
        "目标有反分析，先处理检测点",
    ],
    [
        ["反作弊", "ace", "tp", "tenprotect", "内核模块", "驱动"],
        ["anti-cheat-kernel-analysis", "anonymous-executable-memory"],
        "命中反作弊/内核层，转内核与匿名内存分析",
    ],
    [
        ["unity", "il2cpp", "u3d", "global-metadata", "mono"],
        ["rev-u3d-dump"],
        "Unity 游戏，走 il2cpp metadata dump",
    ],
    [
        ["ios", "ipa", "mach-o", "砸壳"],
        ["rev-ios-dump"],
        "iOS 目标，走砸壳流程",
    ],
    [
        ["小程序", "wxapkg", "miniprogram", "支付宝小程序"],
        ["alipay-miniprogram-reversing", "rev-dex-dumper"],
        "小程序包，走解包还原",
    ],
    [
        ["unicorn", "模拟执行", "emulate", "so 调试"],
        ["rev-unicorn-debug"],
        "需要脱离真机模拟执行",
    ],
    [
        ["协议", "抓包", "加密参数", "sign", "签名算法"],
        ["ble-usb-protocol-reversing", "android-network-stack", "android-crypto-hooking"],
        "加密参数/协议还原",
    ],
    [
        ["加固", "packer", "apk 加固", "dex 加固", "梆梆", "爱加密", "360 加固", "腾讯乐固"],
        ["apk-protection-analysis", "anti-analysis-and-integrity"],
        "Android 加固厂商特征，先识别加固类型再走对应脱壳链",
    ],
    [
        ["抖音", "abogus", "xbogus"],
        ["web-douyin-abogus-xbogus"],
        "抖音 a_bogus / X-Bogus 签名参数",
    ],
    [
        ["淘宝", "mtop", "h5st"],
        ["web-taobao-mtop-h5st"],
        "淘宝 mtop / h5st 签名",
    ],
    [
        ["知乎", "zse"],
        ["web-zhihu-zse"],
        "知乎 zse 加密参数",
    ],
    [
        ["小红书", "xiaohongshu"],
        ["web-xiaohongshu-xs-xt"],
        "小红书 X-S / X-T 签名",
    ],
    [
        ["极验", "geetest", "滑块验证"],
        ["vendor-geetest-captcha"],
        "极验验证码",
    ],
    [
        ["瑞数", "ruishu", "动态混淆", "无限 debugger"],
        ["vendor-ruishu-rs"],
        "瑞数动态防护",
    ],
    [
        ["数美", "shumei"],
        ["vendor-shumei-captcha"],
        "数美风控",
    ],
    [
        ["顶象", "dingxiang"],
        ["vendor-dingxiang-captcha"],
        "顶象验证码",
    ],
    [
        ["阿里云盾", "baxia", "nvc", "无痕验证"],
        ["vendor-aliyun-nvc-baxia"],
        "阿里云盾 / 霸下",
    ],
    [
        ["turnstile", "5秒盾", "cf 盾", "cloudflare"],
        ["vendor-cloudflare-turnstile", "vendor-cloudflare-waf"],
        "Cloudflare 防护",
    ],
    [
        ["akamai", "sensor_data"],
        ["vendor-akamai-bm"],
        "Akamai Bot Manager",
    ],
    [
        ["jsvmp", "js 虚拟机", "vmp 保护"],
        ["jsvmp-vmp-analysis"],
        "JS VMP 保护",
    ],
    [
        ["wasm", "webassembly"],
        ["wasm-reversing", "wasm-crypto-signature"],
        "WASM 加密逻辑",
    ],
    [
        ["卡密", "注册机", "激活码", "授权码", "机器码", "试用期", "加密狗", "card-key"],
        ["alice-crack", "client-license-validation-bypass", "android-elf-license-crack"],
        "卡密/授权校验场景，先判定权威在本地还是服务端",
    ],
    [
        ["license 校验", "activation", "序列号校验", "keygen", "许可文件", "license file"],
        ["client-license-validation-bypass", "alice-crack"],
        "客户端许可校验，先判断能否伪造成立还是只能中继",
    ],
    [
        ["改跳转", "nop", "爆破", "暴力破解", "patch 校验", "绕过校验点", "je jne"],
        ["rev-idapython", "runtime-patch-validation", "alice-crack"],
        "已定位到校验点，准备打补丁",
    ],
    [
        ["服务端校验", "联网验证", "心跳", "远程授权", "必须联网"],
        ["android-elf-license-crack", "alice-crack"],
        "服务端强制依赖，走中继或完全脱机化",
    ],
    [
        [
            "vmp", "vmprotect", "虚拟化保护", "devirtualiz", "dispatcher", "handler", "vip 寄存器",
            "一层套一层", "分发器", "虚拟机解释"
        ],
        ["vmp-reverse", "virtualization-protection", "jsvmp-vmp-analysis"],
        "命中代码虚拟化：先静态定位 dispatcher 与锚点寄存器，再 trace 切 handler，别急着看伪代码",
    ],
    [
        ["themida", "winlicense", "enigma", "obsidium", "safengine", "商业壳"],
        ["packer-and-loader-analysis", "vmp-reverse", "native-unpacking"],
        "商业壳：先定壳版本，Themida/WinLicense 2.x-3.x 用 unlicense / Magicmida",
    ],
    [
        [
            "ollvm", "控制流平坦化", "间接跳转", "mba", "花指令", "bogus flow", "全是跳转", "跳转看不懂", "逻辑看不懂",
            "看不懂逻辑", "没意义的指令", "无意义指令"
        ],
        ["ollvm-deobfuscation", "ollvm-recovery-workflow"],
        "OLLVM 系列：走控制流还原，不要逐条硬啃",
    ],
    [
        ["脱壳", "加壳", "加了壳", "有壳", "上壳", "套壳", "unpack", "脱不掉", "dump 不出来", "壳检测"],
        ["packer-and-loader-analysis", "native-unpacking", "dump-fix-rebuild"],
        "先识别壳类型，再决定 dump + 修复路径",
    ],
    [
        ["dump 修复", "iat 修复", "重定位", "入口点修复", "重建 pe"],
        ["dump-fix-rebuild", "packed-so-elf-rebuild", "memdumper-artifact-validation"],
        "dump 后修复三件套：IAT / 重定位 / 入口点，产物必须自检",
    ],
]

# ──────────────────────────── HTTP 层 ────────────────────────────

_RL_HINT = "GitHub API 配额耗尽。设置环境变量 GITHUB_TOKEN 可提升到 5000 req/h（当前 60 req/h）。"


def _http(url: str, token: str = "", timeout: int = 20) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = b""
        try:
            body = e.read()
        except Exception as _e:
            _warn('_http', _e)
            pass
        if e.code in (403, 429) and b"rate limit" in body.lower():
            raise RuntimeError(_RL_HINT) from e
        if e.code == 404:
            return None
        raise RuntimeError(f"HTTP {e.code} {url}") from e
    except Exception as _e:
        _warn('_http', _e)
        return None


def _api(path: str) -> Any:
    raw = _http(f"https://api.github.com{path}", TOKEN, timeout=25)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception as _e:
        _warn('_api', _e)
        return None


def _fetch_skill_text(repo: str, sha: str, path: str) -> str:
    """拉取 skill 正文。本地源（sha=local）直接读盘，远程走缓存 + 三源 fallback。"""
    if sha == "local":
        p = Path(path)
        if p.is_file():
            return p.read_text(encoding="utf-8", errors="replace")
        raise RuntimeError(f"本地文件不存在：{path}")

    cache = CACHE_DIR / repo.replace("/", "__") / sha[:12] / path
    if cache.is_file():
        return cache.read_text(encoding="utf-8", errors="replace")

    last_err = ""
    for tpl in RAW_MIRRORS:
        url = tpl.format(repo=repo, sha=sha, path=path)
        data = _http(url, "", timeout=25)
        if data and len(data) > 32:
            text = data.decode("utf-8", errors="replace")
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(text, encoding="utf-8")
            except Exception as _e:
                _warn('_fetch_skill_text', _e)
                pass
            return text
        last_err = url
    raise RuntimeError(f"三个源都拉不到 {path}（最后尝试 {last_err}）")


# ──────────────────────────── 索引构建 ────────────────────────────


def _derive_name(repo: str, path: str) -> str:
    """布局自适应：根级 SKILL.md 用仓库名，其余用父目录名。"""
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    if not parent:
        return repo.split("/")[-1]
    return parent.rsplit("/", 1)[-1]


def _repo_meta(repo: str) -> dict:
    """拿 default_branch + stars。优先零配额源，避免为了一个分支名烧 GitHub 配额。"""
    raw = _http(f"{UNGH}/repos/{repo}", "", timeout=20)
    if raw:
        try:
            r = json.loads(raw).get("repo") or {}
            if r.get("defaultBranch"):
                return {"branch": r["defaultBranch"], "stars": int(r.get("stars") or 0), "via": "ungh"}
        except Exception as _e:
            _warn('_repo_meta', _e)
            pass

    # ungh 失败（新仓库/小仓库常 403）：用 jsDelivr 探测 main / master，仍然零配额
    for b in ("main", "master"):
        raw = _http(f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{b}?structure=flat", "", timeout=25)
        if raw:
            try:
                if json.loads(raw).get("files"):
                    return {"branch": b, "stars": 0, "via": "jsdelivr"}
            except Exception as _e:
                _warn('_repo_meta', _e)
                pass

    info = _api(f"/repos/{repo}") or {}
    return {"branch": info.get("default_branch") or "main",
            "stars": int(info.get("stargazers_count") or 0), "via": "api"}


def _head_sha(repo: str, branch: str) -> str:
    """拿 commit sha 用于 pin。三级降级：git ls-remote → GitHub API → branch 名。

    为什么优先 git ls-remote：它零配额、不需要 GITHUB_TOKEN，而 API 匿名只有 60 次/小时。
    实测 16 个 repo 全部因为 API 失败回退成了 branch 名（index.json 里 sha="main"），
    导致无法判断上游有没有更新 —— pin 形同虚设。
    """
    # 源 1：git ls-remote（零配额）
    try:
        r = subprocess.run(
            ["git", "ls-remote", f"https://github.com/{repo}.git", f"refs/heads/{branch}", "HEAD"],
            capture_output=True, text=True, timeout=25)
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{40}", parts[0]):
                    if parts[1].endswith(f"refs/heads/{branch}") or parts[1] == "HEAD":
                        return parts[0]
    except Exception as _e:
        _warn('_head_sha', _e)
        pass
    # 源 2：GitHub API
    try:
        data = _api(f"/repos/{repo}/commits/{branch}")
        if isinstance(data, dict) and data.get("sha"):
            return str(data["sha"])[:40]
    except Exception as _e:
        _warn('_head_sha', _e)
        pass
    # 源 3：降级为 branch（接受漂移，但不阻塞同步）
    return branch


def _fetch_tree(repo: str, branch: str) -> list[tuple[str, int]] | None:
    """文件树三源：ungh.cc → jsDelivr data API → GitHub Trees API → contents 递归。
    前两个零配额；jsDelivr 对超大仓库会截断文件数，只作兜底。"""
    # 源 1：ungh.cc
    raw = _http(f"{UNGH}/repos/{repo}/files/{branch}", "", timeout=30)
    if raw:
        try:
            files = json.loads(raw).get("files") or []
            got = [(str(x["path"]).lstrip("/"), int(x.get("size") or 0))
                   for x in files if x.get("path")]
            if got:
                return got
        except Exception as _e:
            _warn('_fetch_tree', _e)
            pass

    # 源 2：jsDelivr data API（零配额；大仓库可能不全，故排在 ungh 之后）
    raw = _http(f"https://data.jsdelivr.com/v1/packages/gh/{repo}@{branch}?structure=flat", "", timeout=30)
    if raw:
        try:
            files = json.loads(raw).get("files") or []
            got = [(str(x.get("name") or "").lstrip("/"), int(x.get("size") or 0))
                   for x in files if x.get("name")]
            if got:
                return got
        except Exception as _e:
            _warn('_fetch_tree', _e)
            pass

    # 源 3：GitHub Trees API（耗配额）
    data = _api(f"/repos/{repo}/git/trees/{branch}?recursive=1")
    if isinstance(data, dict) and "tree" in data:
        return [(i["path"], int(i.get("size") or 0)) for i in data["tree"] if i.get("type") == "blob"]
    return None


def _walk_contents(repo: str, branch: str, prefix: str = "", depth: int = 0) -> list[tuple[str, int]]:
    """Trees API 对超大仓库返回空时的 fallback：contents 逐层递归。"""
    if depth > 6:
        return []
    data = _api(f"/repos/{repo}/contents/{prefix}?ref={branch}" if prefix else f"/repos/{repo}/contents?ref={branch}")
    out: list[tuple[str, int]] = []
    if not isinstance(data, list):
        return out
    for it in data:
        t = it.get("type")
        if t == "dir":
            out.extend(_walk_contents(repo, branch, it["path"], depth + 1))
        elif t == "file":
            out.append((it["path"], int(it.get("size") or 0)))
    return out


def _parse_frontmatter(text: str) -> dict[str, str]:
    """解析 YAML frontmatter。支持块标量（description: | 后面缩进的多行内容）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    lines = text[3:end].splitlines()
    meta: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            k = k.strip().lower()
            v = v.strip().strip("\"'")
            if v in ("|", ">", "|-", ">-", "|+", ">+"):
                # YAML 块标量：收集后续缩进行作为值
                block: list[str] = []
                i += 1
                while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                    if lines[i].strip():
                        block.append(lines[i].strip())
                    i += 1
                meta[k] = " ".join(block).strip()
                continue
            meta[k] = v
        i += 1
    return meta


def _clean_desc(d: str) -> str:
    """过滤纯符号/过短的描述（如 YAML 块标量解析残留的 '|'）。"""
    d = " ".join((d or "").split())
    if len(d) < 12:
        return ""
    if not any(c.isalnum() or "\u4e00" <= c <= "\u9fff" for c in d):
        return ""
    return d[:220]


# ── 结构化边界抽取 ──
# 为什么需要：索引里的 desc 被截断到 220 字符，把 "Use when ..." 这类
# 最关键的适用条件切掉了。而 64.9% 的 skill 写了前置条件、50.2% 写了适用场景 ——
# 这些材料一个字都没进索引，skill_fit 因此没有任何判断依据。

_USE_PAT = re.compile(
    r"(?:Use when|Use this (?:skill )?when|use for|适用于|用于|适用场景)[\s:：]*"
    r"(.{6,200}?)(?:\.\s|\.$|。|$)", re.I)
_SEC_USE = re.compile(
    r"(?ms)^#{2,3}\s*(?:When to Use|When To Use|适用场景|使用场景|使用条件|Scope)\s*\n"
    r"(.{10,600}?)(?=\n#{2,3}\s|\Z)", re.I)
_SEC_REQ = re.compile(
    r"(?ms)^#{2,3}\s*(?:Prerequisites?|Requirements?|前提条件|前置条件|环境要求|依赖)\s*\n"
    r"(.{10,500}?)(?=\n#{2,3}\s|\Z)", re.I)
_AVOID_PAT = re.compile(
    r"(?:不要|禁止|绝不|严禁|never|do not|don't|avoid)\s*([^。\n.]{8,120})", re.I)
_TOOL_PAT = re.compile(
    r"(?:pip install|apt(?:-get)? install|brew install|npm i(?:nstall)?|go install)\s+([\w\-.@/]+)", re.I)


def _dedup_cap(items: list[str], cap: int = 5, maxlen: int = 130) -> list[str]:
    out: list[str] = []
    for x in items:
        x = " ".join(x.split()).strip(" -·*:：")
        if len(x) < 6:
            continue
        x = x[:maxlen]
        if x not in out:
            out.append(x)
        if len(out) >= cap:
            break
    return out


def _extract_meta(text: str) -> dict[str, list[str]]:
    """抽取 skill 的适用边界：什么时候用 / 需要什么 / 不许做什么 / 依赖什么工具。

    这四类信息决定了 skill 是否「该用」——比 desc 里的技术栈罗列重要得多。
    """
    meta: dict[str, list[str]] = {"use_when": [], "requires": [], "avoid": [], "tools": []}
    if not text:
        return meta
    fm = _parse_frontmatter(text[:6000])
    full_desc = fm.get("description", "") or ""
    for mm in _USE_PAT.finditer(full_desc):
        meta["use_when"].append(mm.group(1))
    for mm in _SEC_USE.finditer(text):
        meta["use_when"].append(re.sub(r"\s+", " ", mm.group(1)))
    for mm in _SEC_REQ.finditer(text):
        meta["requires"].append(re.sub(r"\s+", " ", mm.group(1)))
    for mm in _AVOID_PAT.finditer(text):
        meta["avoid"].append(mm.group(0))
    for mm in _TOOL_PAT.finditer(text):
        meta["tools"].append(mm.group(1))
    for k in meta:
        meta[k] = _dedup_cap(meta[k])
    return meta


_CN_RE = re.compile(r"[\u4e00-\u9fff]")


def _est_tokens(text: str) -> int:
    """按字符类型估算 token：中文约 1.5 字/token，其余约 4 字符/token。
    纯 len//4 对中文会低估 1.4~1.9 倍，导致预算闸门失效。"""
    if not text:
        return 1
    cn = len(_CN_RE.findall(text))
    return max(1, int(cn / 1.5 + (len(text) - cn) / 4))


def _split_sections(text: str, max_bytes: int) -> list[str]:
    """按 markdown 二级标题切段，单段不超过 max_bytes。
    顺序：先按 ## 切 → 超大块先硬切 → 再合并相邻小块填满。
    这样既不会切断章节，也不会出现只有几 KB 的碎片段。"""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]

    soft = int(max_bytes * 0.92)   # 硬切留余量，便于后续合并

    # 1. 按二级标题切
    raw = re.split(r"(?m)^(?=## )", text)

    # 2. 超过上限的块先按行硬切
    chunks: list[str] = []
    for s in raw:
        if len(s.encode("utf-8")) <= max_bytes:
            chunks.append(s)
            continue
        buf: list[str] = []
        n = 0
        for ln in s.splitlines(keepends=True):
            lb = len(ln.encode("utf-8"))
            if buf and n + lb > soft:
                chunks.append("".join(buf))
                buf, n = [], 0
            buf.append(ln)
            n += lb
        if buf:
            chunks.append("".join(buf))

    # 3. 合并相邻小块，尽量填满一段
    out: list[str] = []
    cur = ""
    for c in chunks:
        if cur and len((cur + c).encode("utf-8")) > max_bytes:
            out.append(cur)
            cur = c
        else:
            cur += c
    if cur:
        out.append(cur)
    return out or [text]


def _sync_one(entry: dict, enrich: bool, pin: bool = True) -> dict:
    repo = entry["repo"]
    trust = entry.get("trust", "trusted")
    domain = entry.get("domain", "misc")
    overrides: dict[str, str] = entry.get("domain_overrides") or {}

    meta = _repo_meta(repo)
    branch = meta["branch"]           # 机制 2：实读，不假设 main
    stars = meta["stars"]

    tree = _fetch_tree(repo, branch)  # 机制 1+3
    if not tree:
        tree = _walk_contents(repo, branch)

    sha = _head_sha(repo, branch) if pin else branch   # 机制 12：pin sha 防漂移

    skills = []
    for path, size in tree:
        if not SKILL_RE.search(path):
            continue
        nm = _derive_name(repo, path)
        skills.append({
            "repo": repo, "name": nm, "path": path,
            "sha": sha, "branch": branch, "size": size, "desc": "",
            "trust": trust, "domain": overrides.get(nm, domain),
            "meta": {"use_when": [], "requires": [], "avoid": [], "tools": []},
        })

    if enrich and skills:
        def _fill(s: dict) -> None:
            try:
                full_txt = _fetch_skill_text(repo, sha, s["path"])
                head_txt = full_txt[:2500]
                fm = _parse_frontmatter(head_txt)
                d = _clean_desc(fm.get("description") or "")
                if not d:
                    body = head_txt.split("\n---", 1)[-1] if head_txt.startswith("---") else head_txt
                    for ln in body.splitlines():
                        t = ln.strip()
                        if t and not t.startswith(("#", "|", ">", "```", "-", "*", "---")) and len(t) > 20:
                            d = _clean_desc(t)
                            if d:
                                break
                s["desc"] = d
                # 同步抽取适用边界 —— 只靠 220 字符的 desc 会丢掉 "Use when..." 和铁律
                s["meta"] = _extract_meta(full_txt)
            except Exception as _e:
                _warn('_fill', _e)
                pass

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(_fill, skills))

    return {"repo": repo, "branch": branch, "sha": sha, "stars": stars,
            "trust": trust, "domain": domain, "synced_at": int(time.time()), "skills": skills}


def _sync_local(entry: dict) -> dict:
    """扫描本地源。kind='skill' 只认 SKILL.md；kind='kb' 认全部 .md（方法论文章）。
    sha 固定为 'local'，读取时直接走文件系统，零网络零延迟。"""
    root = Path(entry["path"])
    if not root.is_dir():
        raise RuntimeError(f"目录不存在：{root}")
    domain = entry.get("domain", "local")
    overrides: dict[str, str] = entry.get("domain_overrides") or {}
    rules: list[dict] = entry.get("domain_rules") or []
    kind = entry.get("kind", "skill")
    repo_id = entry.get("id") or f"local/{root.name}"

    def _desc_of(text: str) -> str:
        fm = _parse_frontmatter(text[:2500])
        d = _clean_desc(fm.get("description") or "")
        if d:
            return d
        body = text.split("\n---", 1)[-1] if text.startswith("---") else text
        for ln in body.splitlines():
            t = ln.strip()
            if t and not t.startswith(("#", "|", ">", "```", "-", "*", "---")) and len(t) > 20:
                d = _clean_desc(t)
                if d:
                    return d
        return ""

    skills: list[dict] = []

    if kind == "kb":
        for f in sorted(root.rglob("*.md")):
            if not f.is_file() or f.name.upper() in ("README.MD", "INDEX.MD"):
                continue
            rel = f.relative_to(root).as_posix()
            nm = re.sub(r"^\d+[-_]", "", f.stem) or f.stem
            d = domain
            for r in rules:
                if r.get("contains") and r["contains"] in rel:
                    d = r.get("domain", d)
                    break
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as _e:
                _warn('_sync_local', _e)
                text = ""
            skills.append({
                "repo": repo_id, "name": nm, "path": str(f).replace("\\", "/"),
                "sha": "local", "branch": "local", "size": f.stat().st_size,
                "desc": _desc_of(text), "trust": entry.get("trust", "trusted"),
                "domain": overrides.get(nm, d), "local": True, "kb": True, "rel": rel,
                "meta": _extract_meta(text),
            })
    else:
        for f in sorted(root.rglob("*")):
            if not f.is_file() or f.name.lower() != "skill.md":
                continue
            name = f.parent.name
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except Exception as _e:
                _warn('_sync_local', _e)
                text = ""
            skills.append({
                "repo": repo_id, "name": name, "path": str(f).replace("\\", "/"),
                "sha": "local", "branch": "local", "size": f.stat().st_size,
                "desc": _desc_of(text), "trust": entry.get("trust", "trusted"),
                "domain": overrides.get(name, domain), "local": True,
                "meta": _extract_meta(text),
            })

    # KB 里不同目录常有同名文件（01-xxx.md），冲突时用父目录名消歧
    seen: set[str] = set()
    for s in skills:
        if s["name"] in seen:
            parent = Path(s["path"]).parent.name
            cand = f"{parent}--{s['name']}"
            s["name"] = cand if cand not in seen else f"{cand}~{abs(hash(s['path'])) % 9999}"
        seen.add(s["name"])

    return {"repo": repo_id, "branch": "local", "sha": "local", "stars": 0,
            "trust": entry.get("trust", "trusted"), "domain": domain,
            "local": True, "synced_at": int(time.time()), "skills": skills}


_INDEX_CACHE: dict[str, Any] = {"data": None, "mtime": -1.0}


def _load_index() -> dict:
    """读索引。按 mtime 缓存，避免每次检索都重读 200KB JSON。"""
    try:
        mtime = INDEX_FILE.stat().st_mtime
        if _INDEX_CACHE["data"] is not None and mtime == _INDEX_CACHE["mtime"]:
            return _INDEX_CACHE["data"]
    except Exception as _e:
        _warn('_load_index', _e)
        mtime = -1.0

    data: dict = {"version": 1, "repos": {}}
    if INDEX_FILE.is_file():
        try:
            data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
        except Exception as _e:
            _warn('_load_index', _e)
            pass
    _INDEX_CACHE["data"] = data
    _INDEX_CACHE["mtime"] = mtime
    return data


def _all_skills(idx: dict) -> list[dict]:
    out: list[dict] = []
    for r in idx.get("repos", {}).values():
        out.extend(r.get("skills", []))
    return out


# ──────────────────────────── 会话状态 ────────────────────────────

LOADED: dict[str, int] = {}     # key -> tokens
ACTIVE: dict[str, str] = {"key": ""}

# 上一次 skill_hint 的推荐，用于「自动推断结果」——
# 模型不会主动调 skill_feedback，所以成功/失败必须由工具自己推断：
#   推荐了 X 且之后 load 了 X        → 记 success（弱权重 0.3）
#   推荐了 X 但一个都没 load 又来 hint → 记 fail
_LAST_HINT: dict = {"signal": "", "candidates": [], "ts": 0}
# 分段加载的明细：key -> {part: tokens}。
# 为什么需要：大 skill 分 19 段读，每段都真进了上下文，但 LOADED[key] 只存最后一次
# 的 est —— 读 3 段实际 14703 tokens，_used_tokens() 只报 6742，预算闸门形同虚设。
_LOADED_PARTS: dict[str, dict[int, int]] = {}
# 当前任务描述，供 skill_load 记任务特征（第 5 阶任务级记忆）
_ACTIVE_TASK: dict = {"task": ""}




# ──────────────────── 卡住检测（自动触发经验库的入口）────────────────────
# 为什么需要：rules 里写着「失败 2 次必须 hint」，但实测模型会偷懒不调
# （2026-10-20 那次 12 组类型矩阵全试遍，一次 hint 都没触发）。
# 所以「是否卡住」必须由工具自己判断，不能靠模型自觉。

_STUCK_TRACE: dict = {"history": [], "streak": 0}


def _track_stuck(signal: str) -> int:
    """记录一次 signal 并返回卡住等级 0-3。

    判「同类」两条路径，任一成立就算继续：
      1. 与最近 3 次的最大 token 重叠 ≥ 0.35，且共同词 ≥ 2
      2. 含明确失败词（不行 / 无效 / 还是不对 …）

    第 2 条不能省。实测：模型第 4 次说「换了另一个 Context 也不行，IR 依然错位」，
    与前面的 token 重叠不足 0.35，但语义上明显还在同一个坑里 —— 只看重叠会
    把 streak 重置成 1，经验库就永远不触发。

    等级：
      0 = 正常  1 = 可疑（首次带失败词）  2 = 明确卡住  3 = 死循环
    """
    toks = _exp._tokens(signal) - _exp._STOP
    low = (signal or "").lower()
    has_fail = any(k.lower() in low for k in STUCK_SIGNALS)
    hist = _STUCK_TRACE["history"]

    best, best_common = 0.0, 0
    for h in hist[-3:]:
        common = len(toks & h)
        if common < 2:
            continue
        ov = common / max(1, min(len(toks), len(h)))
        if ov > best:
            best, best_common = ov, common

    if hist and toks and (best >= 0.35 or has_fail):
        _STUCK_TRACE["streak"] = _STUCK_TRACE["streak"] + 1
    else:
        _STUCK_TRACE["streak"] = 1

    hist.append(toks)
    if len(hist) > 6:
        hist.pop(0)
    _STUCK_TRACE.setdefault("raw", []).append(signal or "")
    if len(_STUCK_TRACE["raw"]) > 6:
        _STUCK_TRACE["raw"].pop(0)

    streak = _STUCK_TRACE["streak"]
    if streak >= 3:
        return 3
    if streak >= 2:
        return 2
    return 1 if has_fail else 0


def _stuck_query(signal: str) -> str:
    """卡住时的检索词 = 最近几次 signal 拼起来。

    为什么不能只用当前这一句：卡住是累积状态，单次措辞往往信息不全。
    实测「换了另一个 Context 也不行，IR 依然错位」单独检索零命中，
    但拼上前面两次（「所有 IR 全对不上」「IR 全部错位，跨进程重跑对不上」）
    就能精确命中 A2（inputs[0] 是随机化 ram 标记）。
    """
    raws = _STUCK_TRACE.get("raw") or []
    tail = raws[-3:] if len(raws) >= 3 else raws
    parts = [x for x in tail if x]
    if signal and signal not in parts:
        parts.append(signal)
    return " ".join(parts)


def _stuck_banner(level: int, signal: str) -> str:
    """卡住时的强制横幅 + 经验。放在输出最顶部，模型不可能漏看。"""
    streak = _STUCK_TRACE["streak"]
    exps = _exp.search(_stuck_query(signal), topk=3)
    if level >= 3:
        head = (f"# 🔴 已连续 {streak} 次同类尝试 —— 停止换参数，换方法\n\n"
                f"**你在同一类方法上试了 {streak} 次。** 再试第 {streak+1} 种变体"
                f"（改参数 / 改写法 / 改编码）不会改变结果。\n")
    else:
        head = (f"# ⚠️ 已连续 {streak} 次同类尝试\n\n"
                f"**同一类方法试了 {streak} 次还没过。** 建议先看下面的经验，"
                f"再决定要不要换赛道。\n")

    if exps:
        body = ("## 📚 经验库命中（症状匹配，不是关键词猜测）\n\n"
                + _exp.render(exps, max_action=2) + "\n")
        # 记录「经验被展示过」，供后续统计命中率
        for e in exps:
            try:
                _exp.record_hit(e.get("id", ""), None)
            except Exception:
                pass
    else:
        body = ("## 📚 经验库无匹配\n\n"
                "说明这个现象在库里没有记录。此时**必须补具体信息**：\n"
                "报错原文 / 函数名 / 端口号 / 版本号 / 具体返回值。\n"
                "现象太抽象时，任何库都帮不上忙。\n")

    tail = ("\n---\n"
            "**下一步二选一**：\n"
            "1. 上面经验里有对路的 → 按它做，别自己再试变体\n"
            "2. 全不对路 → `skill_fit(当前skill, 观察到的现象)` 判断是否该换 skill\n")
    return head + "\n" + body + tail


def _key(repo: str, name: str) -> str:
    return f"{repo}::{name}"


def _remember_hint(signal: str, candidates: list[str]) -> None:
    """记下本次 hint 的推荐，供下次自动推断成功/失败。"""
    _LAST_HINT["signal"] = signal
    _LAST_HINT["candidates"] = [c for c in candidates if "::" in c][:5]
    _LAST_HINT["ts"] = int(time.time())


def _used_tokens() -> int:
    return sum(LOADED.values())


def _suggest_mode(s: dict) -> str:
    """按体积给加载建议 —— 实测成本差异极大，统一走 full 是最贵的做法。

    <3KB   ：全文约 700 tokens，直接 full 比「meta→lite→full」三次调用还省
    3-30KB ：全文 2000-6000 tokens，先 lite 看目录，确认相关再 full
    >30KB  ：必须分段（part=N），先用 skill_cmd 看有没有现成命令

    实测三条路径成本：hint+cmd 250 / hint+meta+lite 856 / +full 3630 tokens
    —— 对命令型 skill，轻量路径省 93%。
    """
    sz = s.get("size") or 0
    kb = sz / 1024
    if kb < 3:
        return f"直接 full（{kb:.1f}KB ≈{max(1, sz // 3):,}t，比三次调用还省）"
    if kb < 30:
        return f"先 lite 再 full（{kb:.0f}KB ≈{max(1, sz // 3):,}t）"
    parts = -(-sz // SINGLE_SKILL_MAX_BYTES)
    return f"先 cmd/lite，再分段 full（{kb:.0f}KB / {parts} 段）"


def _budget_line() -> str:
    return f"上下文预算 {_used_tokens()}/{MAX_SKILL_TOKENS} tokens，已加载 {len(LOADED)} 个 skill"


def _stamp_budget(payload: str, est: int) -> str:
    """把 payload 里的 budget 行替换成「加载后」的真实状态。

    payload 渲染时 LOADED 还没写入本次的 est，所以原文里的预算行必然是旧快照。
    不回填的话，模型看到「0/40000」会以为还能随便 load，预算闸门等于没有预警。
    """
    line = (f"budget: 本次 +{est} tokens，当前 {_budget_line()}"
            f"（余 {MAX_SKILL_TOKENS - _used_tokens()}）")
    out, done = [], False
    for ln in payload.splitlines():
        if not done and ln.startswith("budget:"):
            out.append(line)
            done = True
        else:
            out.append(ln)
    if not done:
        out.append(line)
    # splitlines + join 会丢掉末尾换行，导致后续 `payload += "primary: ..."` 粘连
    return "\n".join(out) + ("\n" if payload.endswith("\n") else "")


# ──────────────────────────── 邻接图 ────────────────────────────



# ── 现象本体（Phenomena Ontology）──
# 解决「用户描述行为、索引里全是术语」的字面不交问题。
# 实测：全新未补词的行为描述命中率 20% → 100%。
# 维护规则：只收「用户会说的白话」，不收术语（术语走 SYNONYMS）。
PHENOMENA: dict[str, tuple[str, ...]] = {
    "xss": ('弹窗', '弹框', '原样显示', '原样返回', '输入回显', '注入脚本', '脚本执行', '跨站', '别人打开页面', '我的输入被显示', '显示我输入的'),
    "ssrf": ('服务器主动', '服务端访问', '外部地址', '内网探测', '回连', '请求我给的', '让服务器去', '服务端请求', '打内网'),
    "sqli": ('数据库报错', '单引号', '拼接查询', '特殊字符', '数据库语法', '注入点', '盲注', '联合查询', '时间盲注', '改参数报错', '参数报错', '加引号报错',
             # 中文口语里「就」「后」这类插入词极多，只收固定短语会漏掉一半表述
             '改参数就报错', '改参数后报错', '改了参数就报错', '参数一改就报错',
             '输入就报错', '一提交就报错', '报数据库错误', 'sql 报错'),
    "idor": ('改id', '换个id', '别人的数据', '越权', '权限不足', '能操作别人', '遍历用户', '对象引用', '看别人订单', '别人的账号', '看到别人', '改别人密码', '改别人', '别人的信息', '替别人'),
    "lfi": ('任意文件', '读取文件', '文件包含', '路径穿越', '目录穿越', '下载任意', '读配置文件', 'etc/passwd'),
    "rce": ('命令执行', '执行命令', '弹shell', '反弹shell', '系统命令', '传命令', '代码执行'),
    "logic": ('重复使用', '重复用', '重复领', '多次领', '验证码复用', '验证码可以重复', '绕过限制', '负数', '并发', '业务逻辑', '条件竞争', '零元', '0元',
              '改价格', '改个价格', '修改价格', '一分钱', '0.01', '改金额', '修改金额', '支付金额',
              '优惠券', '领好几次', '领多次', '无限领', '重复下单', '积分兑换', '改数量', '退款', '多退'),
    "auth": ('没校验', '未校验', '不验', '绕过登录', '免登录', '认证绕过', '默认密码', '弱口令'),
    "upload": ('上传脚本', '传个脚本', '上传后能执行', '图片马', '绕过后缀', '上传点'),
    "deserialization": ('反序列化', 'gadget', '序列化数据', 'ysoserial', 'readobject'),
    "ssti": ('模板注入', '模板渲染', '模板表达式', '模板拼接'),
    "xxe": ('xml 实体', '外部实体', 'doctype', '实体注入'),
    "csrf": ('跨站请求', '伪造请求', 'csrf', '自动提交表单'),
    "open-redirect": ('跳转到我给的', '任意跳转', '跳转链接', 'redirect'),
    "info-leak": ('配置泄露', '密钥泄露', '源码泄露', '备份文件', '环境变量', '读到配置'),
    "unpack": ('加了壳', '上壳', '套壳', '脱壳', '打不开', '什么壳', '脱不掉'),
    "vmp": ('虚拟机保护', '虚拟化保护', 'handler', 'dispatcher', 'vmprotect', '一层套一层', '分发器', '看不懂逻辑'),
    "obfuscation": ('混淆', '全是跳转', '控制流平坦化', '花指令', '没意义的指令', 'ollvm'),
    "anti-debug": ('一调试就崩', '反调试', '检测调试', '一运行就退出', '启动就崩', 'attach 就崩', '有检测'),
    "frida": ('hook', '插桩', '运行时修改', 'frida', '注入进程'),
    "license": ('卡密', '激活码', '授权码', '机器码', '注册机', '试用期', '序列号', '加密狗', '换台电脑不能用', '输错就退出',
                '改时间', '系统时间', '时间校验', '到期', '过期', '免费使用', '破解版', '试用到期', '剩余天数'),
    "anti-cheat": ('反作弊', '游戏检测', '内存改不了', '封号', 'eac', 'battleye', 'vanguard'),
    "js-sign": ('签名参数', '加密参数', '参数对不上', 'sign', '风控', '无限 debugger'),
    "memory": ('改血量', '改金币', '内存修改', '搜不到数值', '改数值'),
    "ad": ('域控', '域内', 'kerberos', 'adcs', '内网域', '拿域管'),
    "lateral": ('横向', '跳板', '其他机器', '内网漫游', '打内网其他'),
    "privesc": ('提权', '权限提升', 'root 权限', 'system 权限', '拿到机器往上'),
    "crypto": ('加密算法', '密文', '密钥', '解密', 'aes', 'rsa', '随机数'),
    "phishing": ('钓鱼', '仿站', '话术', '诱导点击'),
    "wifi": ('wifi', '无线', '握手包', '蓝牙', '抓包破解'),
    "iot": ('固件', 'iot', 'uart', 'jtag', '路由器', '摄像头'),
    "api": ('graphql', 'swagger', 'rest api', '接口文档', '接口未授权', 'api 遍历', 'openapi', '接口越权'),
    "data-leak": ('手机号', '身份证', '身份证号', '泄露', '别人的信息', '别人手机号',
                  '敏感数据', '数据库泄露', '返回了别人', '拖库', '用户数据'),
    "techstack": ('什么框架', '用了什么', '什么cms', '什么中间件', '指纹', '识别技术栈',
                  '技术栈', '版本识别', '什么服务器', '用的什么'),
    "redos": ('卡死', '卡住不动', 'cpu 爆满', 'cpu 100', 'cpu 跑满', '正则', '回溯',
              '请求超时变慢', '接口变慢', '拒绝服务'),
    "subdomain": ('子域', '子域名', '资产发现', '旁站', 'c 段', '同 ip 站点'),
    "cors": ('跨域', 'cors', 'origin 头', '跨域资源共享'),
    "cache-poisoning": ('缓存投毒', '缓存欺骗', 'cdn 缓存', '缓存键', 'web cache'),
    "prototype-pollution": ('原型链', 'prototype', '__proto__', 'constructor 污染'),
    "host-header": ('host 头', 'host header', 'x-forwarded-host', '虚拟主机'),
    "crlf": ('crlf', '换行注入', '响应头注入', 'header 注入'),
    "websocket": ('websocket', 'ws 协议', 'socket 通信'),
    "cloud": ('云元数据', '169.254.169.254', 'oss', 's3 桶', 'bucket 权限', '云函数', 'iam 权限', '对象存储'),
    "container": ('容器逃逸', 'k8s', 'kubernetes', 'docker', 'pod', '集群提权'),
    "supply-chain": ('供应链', '依赖投毒', 'npm 包', 'pypi', '构建链', '投毒包'),
    "android": ('apk', '安卓', 'dex', 'smali', 'androidmanifest', '重打包'),
    "ios": ('ipa', 'ios 应用', 'mach-o', '砸壳', '越狱'),
    "miniprogram": ('小程序', 'wxapkg', '微信小程序', '支付宝小程序'),
    "kernel": ('内核', '驱动', 'ring0', '内核模块', 'kernel panic'),
    "shellcode": ('shellcode', '载荷', 'payload 编码', '反弹代码', '位置无关'),
    "dotnet": ('.net', 'dotnet', 'c# 反编译', 'il 代码', 'dnspy', 'confuserex'),
    "forensics": ('取证', '内存取证', '日志分析', '流量分析', '应急响应', '证据固定'),
    "malware": ('样本分析', '病毒分析', '木马分析', '恶意代码', 'c2 通信', '家族归因',
                '样本', '可疑文件', '可疑程序', '可疑进程', '免杀测试'),
    "osint": ('信息收集', '资产测绘', '指纹识别', '社工库', '人员画像', '企业信息'),
    "ddos": ('拒绝服务', 'ddos', '压测', '流量攻击', '资源耗尽'),
    "email": ('邮件伪造', 'spf', 'dkim', 'dmarc', '钓鱼邮件', '邮件网关'),
    "game-cheat": ('外挂', '透视', '自瞄', '飞天', '无敌', '游戏辅助', '变速',
                   '穿墙', '瞬移', '锁血', '自动打怪', '脚本挂机', '改数值',
                   '血量', '金币', '钻石', '内存修改', '人物加速'),
}

# 注入 payload 特征：这类 query 的成分基本是 or/and/1/'/--，会被停用词过滤吃掉，
# 必须单独识别后注入 sqli 术语，否则「OR 1=1」这种最经典的探针反而零命中。
_SQLI_PAYLOAD = re.compile(
    r"(?:'\s*(?:or|and)\s*'?[\d'\"]"
    r"|\bunion\s+(?:all\s+)?select\b"
    r"|\bsleep\s*\(|\bbenchmark\s*\("
    r"|\b(?:or|and)\s+\d+\s*=\s*\d+"
    r"|(?:--|#)\s*$)",
    re.I,
)

# 英文虚词：在 949 个英文命名的 skill 里到处都是，参与计分只会制造噪声。
_STOPWORDS = frozenset({
    "a", "an", "the", "of", "in", "on", "at", "to", "for", "is", "are", "be",
    "and", "or", "not", "with", "by", "as", "it", "this", "that", "from",
    "we", "you", "i", "my", "me", "how", "what", "why", "can", "do", "does",
})

# 现象标签 → skill 名/desc 的匹配别名（标签和 skill 命名习惯不同时用）。
PHENOMENA_ALIAS: dict[str, tuple[str, ...]] = {
    "cache-poisoning": ('cache-poison', 'cache-deception', 'web-cache'),
    "cloud": ('cloud-', '-cloud', 'cloud_', 'clouds', 'aws-', 'gcp-', 'azure-'),
    "info-leak": ('info-leak', 'disclosure', 'leak'),
    "prototype-pollution": ('prototype',),
    "open-redirect": ('redirect',),
    "game-cheat": ('cheat', 'memory-hacking', 'anti-cheat'),
    "api": ('api-', 'graphql', 'swagger', 'openapi'),
    "crypto": ('crypto', 'cipher', 'hash'),
    "data-leak": ('data-leak', 'info-leak', 'disclosure', 'exfil', 'user-data', 'pii'),
    "techstack": ('techstack', 'fingerprint', 'recon', 'identification'),
    "redos": ('redos', 'regex', 'exhaustion', 'timing'),
    # 裸 'ad' 太短：会命中 ad-environment / adaptive / admin 之类前缀，
    # 实测「域控」的 Top1 被 ad-environment-constraints 抢走（它只是 AD 环境方法论）。
    "ad": ('active-directory', 'adcs', 'kerberos', 'domain', 'windows-ad'),
}

# 「我卡住了」类元信号：不指向具体 skill，而是要求做适配自检。
STUCK_SIGNALS: tuple[str, ...] = ('失败', '不奏效', '不行', '没用', '无效', '卡住', '卡在', '走不通', '试了几次', '试了多种', '换了几种', '打不进去', '过不去', '绕不过', '连不上', '跑不起来', '挂掉', '崩溃', '退出', 'fail', 'failed', 'not working', "doesn't work", 'stuck', 'no luck')

# 口语现象 → 标准术语对照表。工具不做语义理解，只给模型「该怎么翻译」的参照。
TERM_TRANSLATION = """| 你观察到的口语现象 | 该翻译成的标准术语（用它重试） |
| :--- | :--- |
| 缓存服务没设密码 / 连上去能读数据 / 不用认证就能执行 | `redis unauthorized` `6379` `mongo 27017` |
| 每次发包返回挑战页 / 浏览器能过脚本过不了 / 等 5 秒 | `cloudflare turnstile` `反爬 挑战` `waf bypass` |
| 请求全被挡回来 / 对面有防护 / 一直 403 | `waf bypass` `403 blocked` `拦截页` |
| 有个端点能读到配置 / 读到数据库密码 / 环境变量 | `actuator` `env 端点` `heapdump` `配置泄露` |
| 改了几种写法都不行 / payload 被过滤 | `waf bypass` `sqli bypass` `过滤 绕过` |
| 数据库不是 MySQL / ORA 报错 / 存储过程 | `oracle` `plsql` `数据库 指纹` |
| 页面能点但不能发包 / 有签名 / 参数对不上 | `sign` `加密参数` `签名算法` `协议 抓包` |
| 加了壳打不开 / 提示要授权 / 一运行就退出 | `packer` `壳检测` `license 校验` `反调试` |
| 代码看不懂 / 全是跳转 / 逻辑混乱 | `ollvm` `控制流平坦化` `vmp` `混淆` |
| 检测到我在调试 / 一 attach 就崩 | `反调试` `anti-debug` `ptrace` `检测 frida` |"""

_NEG_RE = re.compile(
    r"(?:没有|没|无|不存在|不是|未发现|看不到|没发现|不涉及|非|非但|并没有|未曾|不具备)\s*$")




# ──────────────────────────── 现象本体 / 否定匹配 ────────────────────────────


def _detect_phenomena_scored(query: str) -> dict[str, int]:
    """识别查询描述的是哪几类现象，返回 {技术标签: 命中词数}。

    两处比朴素子串匹配更强的处理：
    1. **否定词排除**：「页面没有弹窗」不该识别为 xss。检查命中词前 6 个字符
       是否有否定词（没有/不存在/不是/未发现…），有则跳过。
       实测修正前否定句 5/5 全误判。
    2. **证据计数**：命中 5 个现象词比命中 1 个更可信。返回计数而非布尔，
       让调用方按证据量放大权重。
    """
    ql = query.lower()
    out: dict[str, int] = {}
    for tech, kws in PHENOMENA.items():
        cnt = 0
        for k in kws:
            kl = k.lower()
            start = 0
            while True:
                pos = ql.find(kl, start)
                if pos < 0:
                    break
                prefix = ql[max(0, pos - 6):pos]
                if not _NEG_RE.search(prefix):
                    cnt += 1
                    break
                start = pos + len(kl)
        if cnt:
            out[tech] = cnt
    return out


def _detect_phenomena(query: str) -> list[str]:
    """兼容接口：只要标签列表。"""
    return list(_detect_phenomena_scored(query).keys())


def _match_affirmed(term: str, hay: str) -> bool:
    """在 hay 里找 term，但跳过被否定的出现。

    「不存在越权」「页面没有弹窗」不该被当成目标特征 —— 这类描述是
    在说「这里没问题」，把它当信号会推向完全相反的方向。
    """
    if not term:
        return False
    tl = term.lower()
    hl = hay.lower()
    if len(tl) <= 3:
        for mm in re.finditer(rf"(?<![a-z0-9]){re.escape(tl)}(?![a-z0-9])", hl):
            if not _NEG_RE.search(hl[max(0, mm.start() - 6):mm.start()]):
                return True
        return False
    start = 0
    while True:
        pos = hl.find(tl, start)
        if pos < 0:
            return False
        if not _NEG_RE.search(hl[max(0, pos - 6):pos]):
            return True
        start = pos + len(tl)


def _looks_untranslated(sig: str) -> bool:
    """判断信号是否像「还没翻译的口语」：纯中文、没有英文技术词、没有端口/版本号。"""
    if re.search(r"[a-zA-Z]{2,}", sig):
        return False
    if re.search(r"\b\d{2,5}\b", sig):
        return False
    if re.search(r"[/._]{1}\w", sig):
        return False
    return bool(re.search(r"[\u4e00-\u9fff]{4,}", sig))


def _stuck_response(signal: str, matched: list[str]) -> str:
    """「卡住」类元信号的响应：不假装知道答案，直接把判断权交回模型。

    但**经验库必须先查** —— 「卡住」正是最需要「这个坑别人怎么过」的时候。
    以前这里只给 skill_fit 引导，等于让模型自己去想，浪费了经验库。
    """
    _exps = []
    try:
        _exps = _exp.search(signal, topk=3)
    except Exception as _e:
        _warn('_stuck_response', _e)
    _exp_block = ""
    if _exps:
        _exp_block = ("\n## 📚 经验库命中（卡住时最该先看这个）\n\n"
                      + _exp.render(_exps, max_action=2) + "\n")
        for _e2 in _exps:
            try:
                _exp.record_hit(_e2.get("id", ""), None)
            except Exception:
                pass
    return "\n".join([
        f"# 你描述的是「卡住」，不是技术特征：{signal}",
        "",
        f"命中的元信号：{' / '.join(matched)}",
        "",
        "**本工具是关键词查表，不理解语义。** 你的描述里没有可识别的技术特征，",
        "所以我给不出「该换哪个 skill」—— **这一步的判断权在你，不在工具。**",
        "",
        _exp_block,
        "## 现在按顺序做三件事",
        "",
        "1. `skill_fit(current, observed)` —— 传「当前 skill 名」+「你实际观察到的现象」，",
        "   它会用现象重新排序，告诉你当前 skill 还排第几、Top1 是谁。",
        "2. 若 skill_fit 也判排不上号 → 先 `skill_unload()` 卸掉旧的，再 load 新的。",
        "3. 若 skill_fit 也排不出东西 → 现象描述太抽象，必须补具体信息：",
        "   报错原文 / 函数名 / 端口号 / 版本号 / 抓到的包 / 具体返回内容。",
        "",
        "## 反面做法",
        "",
        "❌ 继续试第 3 种写法（同一类方法试 2 次就该换赛道，不是换参数）",
        "❌ 硬套当前 skill 的方法论，用「再试一次」当进展",
        "❌ 把失败归因于「环境问题」然后放弃换方法",
    ])


def _meta_block(s: dict, full: bool = False) -> str:
    """把索引里抽取好的适用边界渲染成注入文本。"""
    mt = s.get("meta") or {}
    rows: list[str] = []
    if mt.get("use_when"):
        rows.append("适用：" + "；".join(mt["use_when"][:3 if full else 2]))
    if mt.get("avoid"):
        n = 5 if full else 3
        rows.append("铁律/禁止：" + "；".join(mt["avoid"][:n]))
    if mt.get("requires"):
        rows.append("前置：" + "；".join(mt["requires"][:2]))
    if mt.get("tools"):
        rows.append("工具：" + "、".join(mt["tools"][:6]))
    if not rows:
        return ""
    head = "## 适用边界（从正文自动抽取，非人工标注）" if full else "boundary"
    body = "\n".join(f"- {r}" for r in rows)
    return f"\n{head}\n{body}\n" if full else f"{head}: " + " | ".join(rows) + "\n"


def _avoid_conflict(s: dict, observed: str) -> list[str]:
    """检查观察到的现象是否撞上了 skill 自己写的禁止条款。

    比 skill_fit 的重排序更硬：skill 明确说「不要 X」而你在做 X，
    这不是「排名靠后」，是「用错了」。
    """
    mt = s.get("meta") or {}
    avoid = mt.get("avoid") or []
    if not avoid or not observed:
        return []
    low = observed.lower()
    hits: list[str] = []
    for a in avoid:
        core = re.sub(r"^(?:不要|禁止|绝不|严禁|never|do not|don't|avoid)\s*", "", a, flags=re.I)
        toks = [t for t in re.findall(r"[a-zA-Z][\w\-]{3,}|[\u4e00-\u9fff]{2,6}", core)][:4]
        if toks and sum(1 for t in toks if _match(t.lower(), low)) >= 2:
            hits.append(a)
    return hits[:3]


def _pitfall_lines(repo: str, name: str) -> str:
    """把已知踩过的坑渲染成提示（自成长模块记录）。"""
    try:
        pits = _ev.pitfalls(repo, name, limit=3)
    except Exception as _e:
        _warn('_pitfall_lines', _e)
        return ""
    if not pits:
        return ""
    lines = ["known_pitfalls:"]
    for p in pits:
        alt = ""
        if p.get("alts"):
            alt = "  →  " + ", ".join(a.split("::")[-1] for a in p["alts"])
        lines.append(f"  - [{p['hits']} 次] {p['signal']}{alt}")
    lines.append("  （遇到相同现象别再试同一条路，直接换上面的替代或调 skill_hint）")
    return "\n".join(lines) + "\n"





# ──────────────────────────── 命令抽取 ────────────────────────────
# 为什么需要：31% 的 skill 含可执行 shell 命令（样本 150 个就有 376 条）。
# 想看某条命令必须 mode="full" 拉全文（3000+ tokens），99% 的内容是白读的。
# 这里只抽命令 + 出处行号，200 tokens 拿到同样的信息。

_CODE_BLOCK = re.compile(r"```(\w*)\n(.*?)```", re.S)
_SHELL_LANGS = {"bash", "sh", "shell", "console", "zsh", "ps1",
                "cmd", "bat", "terminal", "shell-session", ""}
_TOOL_HEAD = re.compile(
    r"^\s*(?:\$|#|>|PS[> ])?\s*(?:sudo\s+(?:-\w+\s+)*)?(?:\./)?"
    r"(nmap|sqlmap|ffuf|gobuster|dirsearch|feroxbuster|wfuzz|curl|wget|"
    r"python3?|pip3?|msfconsole|msfvenom|nuclei|subfinder|httpx|katana|gau|waybackurls|"
    r"hashcat|john|hydra|medusa|patator|"
    r"frida|objection|adb|jadx|apktool|aapt|dex2jar|"
    r"r2|radare2|rabin2|objdump|readelf|nm|strings|xxd|hexdump|"
    r"nc|ncat|netcat|socat|ssh|scp|rsync|telnet|"
    r"impacket-[\w-]+|secretsdump|psexec|wmiexec|smbclient|crackmapexec|nxc|"
    r"bloodhound|sharphound|rubeus|certipy|"
    r"aircrack-ng|airodump-ng|aireplay-ng|airmon-ng|hcxdumptool|hcxtools|"
    r"volatility|vol3|vol2|yara|"
    r"git|docker|kubectl|helm|terraform|aws|gcloud|"
    r"pwsh|certutil|bitsadmin|reg|sc|schtasks|wmic|"
    r"gdb|gdbserver|ltrace|strace|perf|tcpdump|tshark|"
    r"unidbg|unicorn|qemu|ida64|idat|ghidra|ghidraRun)\b", re.I)


def _extract_commands(text: str, pattern: str = "") -> list[dict]:
    """从正文抽可执行命令。返回 [{line, cmd, note}]，line 是正文绝对行号。"""
    out: list[dict] = []
    pat = re.compile(re.escape(pattern), re.I) if pattern else None
    for mm in _CODE_BLOCK.finditer(text):
        lang = (mm.group(1) or "").lower()
        if lang not in _SHELL_LANGS:
            continue
        body = mm.group(2)
        base_line = text[:mm.start(2)].count("\n") + 1
        note = ""
        for i, raw in enumerate(body.splitlines()):
            line = raw.strip()
            if not line:
                note = ""
                continue
            if line.startswith(("#", "//", ";")):
                note = line.lstrip("#/; ").strip()[:100]
                continue
            if not _TOOL_HEAD.match(line):
                continue
            if pat and not (pat.search(line) or pat.search(note)):
                note = ""
                continue
            out.append({"line": base_line + i, "cmd": line[:300], "note": note})
            note = ""
    return out


def _extract_payloads(text: str, pattern: str = "") -> list[dict]:
    """从非 shell 代码块里抽 payload / 片段（注入串、JSON、伪代码）。"""
    out: list[dict] = []
    pat = re.compile(re.escape(pattern), re.I) if pattern else None
    for mm in _CODE_BLOCK.finditer(text):
        lang = (mm.group(1) or "").lower()
        if lang in _SHELL_LANGS - {""}:
            continue
        body = mm.group(2)
        base_line = text[:mm.start(2)].count("\n") + 1
        for i, raw in enumerate(body.splitlines()):
            line = raw.strip()
            if not line or len(line) < 4:
                continue
            if _TOOL_HEAD.match(line):
                continue
            if pat and not pat.search(line):
                continue
            out.append({"line": base_line + i, "cmd": line[:200], "note": lang or "payload"})
    return out


def _resolve_skill(repo: str, name: str):
    """按 (repo, name) 找 skill。repo 空时全库找，多个命中返回候选说明。"""
    idx = _load_index()
    pool = _all_skills(idx)
    if repo:
        hit = next((s for s in pool if s["name"] == name and s["repo"] == repo), None)
        if hit:
            return hit, ""
        near = [s["name"] for s in pool if name.lower() in s["name"].lower()][:5]
        return None, f"{repo} 内无 skill「{name}」。相近：{near}"
    hits = [s for s in pool if s["name"] == name]
    if not hits:
        near = [s["name"] for s in pool if name.lower() in s["name"].lower()][:5]
        return None, f"全库无 skill「{name}」。相近：{near}"
    if len(hits) > 1:
        return None, ("⚠️ 全库有 %d 个同名 skill「%s」，请用 repo 指定：\n" % (len(hits), name)
                      + "\n".join('  - repo="%s"  `%s`' % (s["repo"], s["path"]) for s in hits[:8]))
    return hits[0], ""


_DOMAIN_SIBS: dict[str, list[dict]] = {}


def _domain_siblings(domain: str, exclude: str = "", hint: str = "",
                     limit: int = 3) -> tuple[list[str], bool]:
    """同 domain 里与当前 skill 最相关的兄弟。

    排序依据（按名字长度排是错的 —— 实测 `transilience-report-style` 会推荐
    `dfir / ssrf / ssti`，因为它只是名字短，跟「报告」毫无关系）：
      1. 名字 token 重叠（`offensive-reporting` 与 `report-style` 共享 report）×3
      2. desc 里的关键词重叠 ×1
      3. trust=trusted 加分
    """
    global _DOMAIN_SIBS
    if not _DOMAIN_SIBS:
        try:
            for s in _all_skills(_load_index()):
                d = s.get("domain") or ""
                if d:
                    _DOMAIN_SIBS.setdefault(d, []).append(s)
        except Exception as _e:
            _warn('_domain_siblings', _e)

    cands = _DOMAIN_SIBS.get(domain, [])
    if not cands:
        return [], False

    # 名字与名字比，desc 与 desc 比 —— 混在一起算会让「名字重叠」这个判定失效：
    # desc 有几百个通用词（system / testing / analysis），任何候选都能「重叠」。
    me_name = {t for t in re.findall(r"[a-z0-9]+", exclude.lower())
               if len(t) >= 3 and t not in _STOPWORDS}
    me_desc = {t for t in re.findall(r"[a-z0-9]+", hint.lower())
               if len(t) >= 3 and t not in _STOPWORDS}

    scored: list[tuple[int, str, int]] = []
    for s in cands:
        nm = s["name"]
        if nm == exclude:
            continue
        nm_tok = set(re.findall(r"[a-z0-9]+", nm.lower()))
        nm_overlap = len(me_name & nm_tok)
        ds_tok = set(re.findall(r"[a-z0-9]+", (s.get("desc") or "").lower())) & me_desc
        sc = nm_overlap * 3 + min(len(ds_tok), 4)
        if s.get("trust") == "trusted":
            sc += 2
        # 完全不重叠的也留着兜底（sc=0），保证覆盖率
        scored.append((sc, nm, nm_overlap))
    # 排序主键必须是「名字重叠数」：desc 重叠只做次键。
    # 否则名字里明明有 report 的 skill 会被「desc 里恰好出现 4 个通用词」的挤掉。
    scored.sort(key=lambda x: (-x[2], -x[0], len(x[1]), x[1]))
    top = [nm for _, nm, _ in scored[:limit]]
    # 相关性门槛：top1 分数为 0 说明同 domain 里没有一个名字/desc 与当前 skill
    # 有词重叠（如 transilience-report-style 落在 pentest 域，域内没有 report 类 skill）。
    # 这时仍给候选，但必须标明「仅同版块」，否则模型会误以为这是精确关联。
    # 只看名字重叠：desc 里全是 system / testing / analysis 这类通用词，
    # 用它判「相关」会把一切都判成相关（实测 transilience-report-style 被判相关，
    # 推荐的却是 system / ai-threat-testing）。
    related = bool(scored) and scored[0][2] > 0
    return top, related


def _adjacency(name: str, hit: dict | None = None) -> dict[str, str]:
    """邻接提示：手工规则优先，无规则时退回「同 domain 兄弟 skill」。

    手工规则只有 38 条，实测只覆盖 146/949 = 15% —— 剩下 803 个 skill 的
    full payload 里全是「related: （无规则命中，按 skill_hint 判断）」，等于没有提示。
    兜底按 domain 推荐兄弟，覆盖率拉到 100%，且不需要人工维护 949 条。
    """
    low = name.lower()
    # 长 key 优先：`upload-to-rce` 同时含 rce 和 file-upload，应命更具体的
    for kw in sorted(ADJACENCY, key=len, reverse=True):
        if kw in low:
            return ADJACENCY[kw]
    if hit:
        sibs, related = _domain_siblings(hit.get("domain") or "", exclude=name,
                                         hint=(hit.get("desc") or ""))
        if sibs:
            key = "related_same_domain" if related else "same_domain_fallback"
            return {key: ", ".join(sibs)}
    return {}


def _match(term: str, hay: str) -> bool:
    """短词（<=3）用词边界，避免 'ida' 命中 'frida'、'rs' 命中 'reverse'。"""
    if not term:
        return False
    if len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", hay) is not None
    return term in hay


def _hint_lookup(signal: str) -> list[tuple[str, str]]:
    """信号 → 规则匹配。

    - 用 _match_affirmed：跳过被否定的出现（「不存在越权」不该命中 idor）
    - 多条命中时按「具体度」排序：命中词数量×2 + 总长度，越具体越靠前
      （否则 "vmp vmprotect dispatcher" 会被只命中 'vmp' 的 Android 规则抢第一）
    """
    s = signal.lower()
    scored: list[tuple[int, int, str, str]] = []
    for order, (kws, targets, why) in enumerate(HINT_RULES):
        matched = [k for k in kws if _match_affirmed(k.lower(), s)]
        if matched:
            spec_score = len(matched) * 2 + sum(len(k) for k in matched)
            scored.append((spec_score, -order, ",".join(targets), why))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [(t, w) for _, _, t, w in scored]


# ──────────────────────────── MCP Server ────────────────────────────

INSTRUCTIONS = """skill-router：949 个 skill 的按需技能库。不预下载，命中才注入。索引比训练记忆新。

【第一动作】
安全 / 渗透 / 逆向 / 脱壳 / 破解 / JS逆向 / 样本 / CTF 类任务，
第一个工具调用必须是 skill_plan(task)。在它返回前不要输出任何方法论。

【执行序列：按需升级，不要一路 full】
实测成本：hint+cmd 250t ｜ hint+meta+lite 856t ｜ +full 3630t
skill_plan → 按每行的「加载建议」走 → skill_unload
- 「直接 full」= <3KB，一步到位比三次调用还省
- 「先 lite 再 full」= 先看目录，确认相关才 full
- 「先 cmd/lite，再分段」= 先 skill_cmd 找现成命令，没有才 lite
只要命令就用 skill_cmd（省 93%），别拉 full。

【边界优先于 desc】
每个 skill 从正文抽取了「什么时候用 / 前置条件 / 禁止条款 / 工具依赖」
（avoid 覆盖 62% / use_when 32%）。skill_load 自动带 boundary 段。
skill_fit 检测「你在做的事是否撞上它自己写的禁令」——撞上判 🔴，那是前提不成立。

【实战经验库（自动触发）】
skill 是方法论，经验库是「这个坑别人怎么过」。
- skill_load 自动附「本领域已知坑」4 条
- 连续 2 次同类尝试 → skill_hint 顶部强制插入经验（🔴 3 次时必须换方法，不是换参数）
- 主动查：skill_exp("报错原文") / skill_exp(phase="ir")
经验带置信度，<0.85 会标注「未验证」，版本不同坑也不同，别当铁律。

【翻译是硬前置】
skill_hint / skill_search 都是关键词查表，不做语义理解。
- 返回「⚠️ 未翻译」→ 你在传大白话。必须翻译成「标准术语 + 端口/报错码/框架名」后重调一次
- 不许直接用它的「仅供参考」候选；纯中文大白话 = 没翻译
- 返回「你描述的是卡住」→ 去做 skill_fit
翻译是你的活，不是工具的。

【现象本体：口语也能听懂】
内置 31 类现象本体（xss / ssrf / sqli / idor / lfi / rce / logic / auth / upload /
unpack / vmp / obfuscation / anti-debug / license / anti-cheat / js-sign / ad / privesc …）。
描述「行为」而非术语也能识别：「页面把我输入原样显示」→ 识别为 xss 并加权检索。
- 返回「现象已识别：xxx」→ 工具已听懂，候选可靠，可 mode="lite" 直接确认
- 返回「⚠️ 未翻译」→ 连现象都认不出，必须翻译后重试
所以先直接描述你看到的现象，比硬凑术语更有效。

【禁止】
- 凭记忆列举工具名 / payload / 命令 / 版本号
- 跳过 skill_plan 直接 skill_search
- 一次 full 加载超 2 个 skill（预算 40000）
- skill_fit 判「排不上号」后继续用旧 skill
- 把 skill 正文复述当交付 —— 交付的是执行结果
- 直接用「未翻译 / 仅供参考」的候选

【大文件 / 同名】
>30KB 用 part=N 分段读；同名用 path 精确指定。
只有 2.5% 的 skill 带 whenToUse，不适配不会自动提示，必须主动 skill_fit。

【自成长：不用你反馈，工具自己记】
- hint 推荐了 X 且你 load 了 X → 自动记弱成功（+0.3）
- hint 推荐了 X 但你没 load 又来 hint → 自动记失败
- 历史分数会微调检索排序（全成功 ×1.3 / 无记录 ×1.0 / 全失败 ×0.7）
- 同类任务的 skill 组合会自动记忆，下次 skill_plan 会提示
所以：**正常用就行，不需要调 skill_feedback**；想强制标记时才用它。
`skill_stats` 可查看积累情况。"""

_MCP_BASE: dict[str, Any] = {
    "name": "skill-router",
    "instructions": INSTRUCTIONS,
}
try:
    # mcp 2.x：MCPServer 支持 title / description / version 元数据
    mcp = MCPServer(
        title="Skill Router",
        description="按需调取 GitHub 上的 agent skills，零全量下载，支持信号驱动重检索。",
        version="1.0.0",
        **_MCP_BASE,
    )
except TypeError:
    # mcp 1.x：FastMCP 构造只认 name / instructions，
    # 传 title 会直接 TypeError 起不来 —— 少这三个参数不影响任何功能。
    # 副作用：1.x 下 serverInfo.version 会显示 mcp 包版本而非 1.0.0，纯展示字段。
    mcp = MCPServer(**_MCP_BASE)



@mcp.tool()
def skill_sync(repos: str = "", enrich: bool = False, pin: bool = True) -> str:
    """建立或刷新 skill 索引。默认走 ungh.cc，不需要 GITHUB_TOKEN。

    Args:
        repos: 逗号分隔的 owner/repo；留空则同步 repos.json 里的全部仓库。
        enrich: 是否用 raw 拉每个 SKILL.md 前 2KB 补描述（不耗 API 配额，但慢约 15s）。
        pin: 是否记录 commit sha 防版本漂移。True 时每仓库多 1 次 GitHub API 请求（8 仓库 = 8 请求）；
             False 则完全零 API 调用，但 skill 内容会随上游分支变化。
    """
    cfg = {"repos": []}
    if REPOS_FILE.is_file():
        try:
            cfg = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
        except Exception as _e:
            _warn('skill_sync', _e)
            pass

    wanted = [r.strip() for r in repos.split(",") if r.strip()]

    targets: list[tuple[str, dict]] = []
    for e in cfg.get("repos", []):
        if not wanted or e["repo"] in wanted:
            targets.append(("remote", e))
    for e in cfg.get("local", []):
        rid = e.get("id") or f"local/{Path(e['path']).name}"
        if not wanted or rid in wanted:
            targets.append(("local", e))

    if not targets:
        avail = ([e["repo"] for e in cfg.get("repos", [])] +
                 [(e.get("id") or f"local/{Path(e['path']).name}") for e in cfg.get("local", [])])
        return f"没有匹配的仓库。repos.json 现有：{avail}"

    idx = json.loads(json.dumps(_load_index()))   # 深拷贝，避免改写污染 mtime 缓存
    lines = ["# skill_sync 结果", ""]
    total = 0
    for kind, e in targets:
        try:
            r = _sync_local(e) if kind == "local" else _sync_one(e, enrich, pin)
            # 继承旧索引的 description：sync 不 enrich 时不应把已有描述清空
            old = (idx.get("repos", {}).get(r["repo"]) or {}).get("skills", [])
            if old:
                old_desc = {s["path"]: s.get("desc", "") for s in old}
                old_meta = {s["path"]: s.get("meta") for s in old}
                for s in r["skills"]:
                    if not s.get("desc"):
                        s["desc"] = old_desc.get(s["path"], "")
                    if not any((s.get("meta") or {}).values()) and old_meta.get(s["path"]):
                        s["meta"] = old_meta[s["path"]]
            idx.setdefault("repos", {})[r["repo"]] = r
            total += len(r["skills"])
            if kind == "local":
                lines.append(f"- ✅ {r['repo']}  [{r['domain']}]  本地目录  "
                             f"skill={len(r['skills'])}")
            else:
                ver = f"sha={r['sha'][:8]}" if len(r["sha"]) == 40 else "未 pin"
                lines.append(f"- ✅ {r['repo']}  [{r['domain']}]  branch={r['branch']}  {ver}  "
                             f"skill={len(r['skills'])}  star={r['stars']}")
        except Exception as ex:
            lines.append(f"- ❌ {e.get('repo') or e.get('path')}  {ex}")

    idx["updated_at"] = int(time.time())
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")

    lines += ["", f"索引合计 {len(_all_skills(idx))} 个 skill，本次同步 {total} 个。",
              f"索引文件：{INDEX_FILE}"]
    if not TOKEN:
        lines.append("ℹ️ 未配置 GITHUB_TOKEN —— 不影响使用。文件树走 ungh.cc，正文走 raw，均不消耗 GitHub 配额。")
    return "\n".join(lines)


def _expand_terms(query: str) -> list[str]:
    """把查询展开成检索词列表：分词 → 同义词映射 → 去重。

    两处关键处理（都是踩过的坑）：
    1. **反向词归一化**：「未授权」里的「授权」会让 SYNONYMS 的 license 组误命中，
       把渗透场景最常见的「未授权访问」翻译成卡密授权。先替换成 unauthorized。
    2. **多词短语必须在完整 query 上匹配**：先分词再匹配会让「SQL 注入」「等 5 秒」
       「提示要 root」永久失效 —— 它们被空格切成单字，任何含空格的 key 都命中不了。
    """
    # 全角归一化：实测「ｓｑｌｉ」「ＳＱＬＩ」全部零命中 —— 中文输入法下
    # 全角字符极常见，不归一化等于整类查询失效。
    query = unicodedata.normalize("NFKC", query)

    terms = [t for t in re.split(r"[\s,，]+", query.strip().lower()) if t]
    if not terms:
        return []
    # 停用词过滤：实测裸 "a" 命中 271 条、"or" 命中 280 条 —— 单字符与英文虚词
    # 在 949 个英文命名 skill 里到处都是，不滤掉会淹没真实信号。
    _filtered = [t for t in terms if t not in _STOPWORDS and not (len(t) == 1 and t.isascii())]
    # `OR 1=1` 这类注入 payload 的成分全是停用词/单字符，全滤掉会变成空查询。
    # 命中 payload 特征时不仅保留原词，还直接注入 sqli 术语；
    # 不命中就是纯虚词（如裸 "a" / "the"），没有检索价值，直接返回空。
    if not _filtered:
        if _SQLI_PAYLOAD.search(query):
            terms = terms + ["sqli", "injection"]
        else:
            return []
    else:
        terms = _filtered
        if _SQLI_PAYLOAD.search(query):
            terms = terms + ["sqli", "injection"]

    expanded: list[str] = []
    q_full = query.strip().lower()
    q_norm = q_full.replace("未授权", " unauthorized ").replace("没授权", " unauthorized ")
    q_compact = re.sub(r"\s+", "", q_norm)

    # ① 完整 query 级匹配（覆盖多词短语）
    for cn, ens in SYNONYMS.items():
        if cn.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(cn)}(?![a-z0-9])", q_full):
                expanded.extend(ens)
        elif cn in q_norm or cn in q_compact:
            expanded.extend(ens)

    # ② 单词级匹配（覆盖单字映射）
    for t in terms:
        expanded.append(t)
        t_norm = t.replace("未授权", " unauthorized ").replace("没授权", " unauthorized ")
        compact = re.sub(r"\s+", "", t_norm)
        for cand in {t_norm.strip(), compact}:
            if not cand:
                continue
            for cn, ens in SYNONYMS.items():
                hit = (cn == cand) if cn.isascii() else (cn in cand)
                if hit:
                    expanded.extend(ens)

    seen: set[str] = set()
    return [t for t in expanded if not (t in seen or seen.add(t))]


def _score_one(s: dict, terms: list[str], phen: dict[str, int],
               alias_targets: set[str], boosts: set[str]) -> int:
    """给单个 skill 打分。返回 0 表示不命中，会被调用方过滤掉。

    分四层：字面匹配 → 别名直连 → 现象加权 → 域/历史加权。
    现象加权是 54 类现象本体的落点：name 命中 200、desc 命中 90，
    每多命中一个现象词 +50（上限 +100）。
    """
    name = s["name"].lower()
    desc = (s.get("desc") or "").lower()
    path = s["path"].lower()
    score = 0
    for t in terms:
        if t == name:
            score += 120
        elif _match(t, name):
            score += 55
        if _match(t, desc):
            # 按词频加权，区分「提了一句」与「整篇在讲」
            score += 25 + min(desc.count(t), 6) * 3
        if _match(t, path):
            score += 12
        if _match(t, s["repo"].lower()):
            score += 6
    if s["name"] in alias_targets:
        score += 150

    phen_bonus = 0
    for p, cnt in phen.items():
        terms_p = PHENOMENA_ALIAS.get(p, (p,))
        in_name = any(t in name for t in terms_p)
        in_desc = any(t in desc for t in terms_p)
        base = 200 if in_name else (90 if in_desc else 0)
        if base:
            phen_bonus += base + min(cnt - 1, 2) * 50
    score += phen_bonus
    # 技术域纯度：识别出明确现象时，完全不含该现象标签的 skill 衰减 15%
    if phen and phen_bonus == 0:
        score = int(score * 0.85)

    if score > 0:
        if s.get("trust") == "trusted":
            score += 8
        if s.get("domain") in boosts:
            score += 80
        # 第 5 阶：历史分数加权（0.7 ~ 1.3）
        try:
            _evs = _ev.boost(s["repo"], s["name"])
            score = int(score * (0.7 + 0.6 * _evs))
        except Exception as _e:
            _warn('_score_one', _e)
    return score


@mcp.tool()
def skill_search(query: str, limit: int = 8, repo: str = "", domain: str = "") -> str:
    """按关键词检索 skill 索引。返回 name / repo / 描述，不返回正文。

    Args:
        query: 关键词，支持空格分隔多个词（命中任一即计分）。
        limit: 返回条数上限，默认 8。
        repo: 只在该仓库内检索，留空则全库。
        domain: 只在该版块内检索。可选：offensive / pentest / bugbounty / audit / reversing / ad / iot / ctf / src。
    """
    # limit 负数会让切片变成「去掉末尾 N 条」，0 会退化成默认值 —— 都不可预期。
    # 统一 clamp 到 [1, 50]。
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 8
    idx = _load_index()
    skills = _all_skills(idx)
    if not skills:
        return "索引为空。先执行 skill_sync()。"

    if domain:
        avail = sorted({s.get("domain") for s in skills if s.get("domain")})
        if domain not in avail:
            return f"domain「{domain}」不存在。可选：{avail}"

    terms = _expand_terms(query)
    if not terms:
        # 区分「真·空」和「全是虚词」：后者用户明明传了内容，说「为空」会让人困惑
        if query.strip():
            return (f"「{query.strip()[:40]}」全是停用词/单字符，没有检索价值。\n"
                    "补一个具体术语再查：技术名 / 端口号 / 报错码 / 框架名 / 漏洞类型。")
        return "query 为空。"

    # 领域引导：查询命中某版块特征词时给该版块加权，避免被同名通用 skill（如 waf-bypass）抢位
    DOMAIN_BOOST: dict[str, tuple[str, ...]] = {
        "license": ("卡密", "注册机", "授权", "激活", "license", "机器码", "试用", "序列号", "加密狗", "card-key"),
        "unpack": ("vmp", "vmprotect", "虚拟化", "去虚拟化", "脱壳", "加壳", "themida", "winlicense",
                   "enigma", "obsidium", "ollvm", "混淆", "加固", "壳", "dump", "devirtualiz"),
        "reversing": ("逆向", "反编译", "hook", "frida", "ida", "ghidra", "反调试", "反作弊",
                      "二进制", "汇编", "android", "apk", "ios"),
    }
    q_low = query.lower()
    q_boost = q_low.replace("未授权", " unauthorized ").replace("没授权", " unauthorized ")
    boosts = {d for d, kws in DOMAIN_BOOST.items() if any(k in q_boost for k in kws)}

    # 现象本体：识别「用户描述的是哪类行为」，给对应技术域加权。
    # 用 scored 版本：命中现象词越多越可信（1 个 200 / 2 个 250 / 3+ 个 300）。
    phen = _detect_phenomena_scored(query)

    # 壳名/工具名 → 具体 skill 直连（这些词不出现在 desc 里，必须单独加权）
    alias_targets: set[str] = set()
    for t in terms:
        alias_targets.update(QUERY_BOOST.get(t, ()))

    scored: list[tuple[int, dict]] = []
    for s in skills:
        if repo and s["repo"] != repo:
            continue
        if domain and s.get("domain") != domain:
            continue
        score = _score_one(s, terms, phen, alias_targets, boosts)
        if score > 0:
            scored.append((score, s))

    if not scored:
        return (f"未命中「{query}」。\n"
                f"提示：skill 名与描述以英文为主，中文词会经同义词表展开（如 后台→admin/auth/login）。\n"
                f"可尝试：更短的词、英文关键词，或 skill_sync() 补仓库。")

    scored.sort(key=lambda x: (-x[0], x[1]["name"]))
    top = scored[:max(1, min(limit, 30))]

    out = [f"# 检索「{query}」命中 {len(scored)} 条，显示前 {len(top)}", ""]
    for i, (sc, s) in enumerate(top, 1):
        dom = s.get("domain") or ""
        tag = f"·{dom}" if dom else ""
        out.append(f"{i}. **{s['name']}**  [{s['repo']}]{tag}  score={sc}")
        if s.get("desc"):
            out.append(f"   {s['desc']}")
        out.append(f"   `{s['path']}`")
    out += ["", '下一步：skill_load(repo, name, mode="lite") 先看目录。']
    return "\n".join(out)


def _resolve_load_target(idx: dict, repo: str, name: str, path: str):
    """定位要加载的 skill。返回 (repo, hit, err)。

    repo 留空时全库找：唯一命中自动补全 repo，多个命中列出候选 ——
    以前这里直接报「仓库  未索引」，同名 skill 用户完全不知道该怎么办。
    """
    if not repo:
        hits_all = [s for s in _all_skills(idx) if s["name"] == name]
        if not hits_all:
            near = [s["name"] for s in _all_skills(idx) if name.lower() in s["name"].lower()][:6]
            return "", None, f"全库无 skill「{name}」。相近：{near}"
        if len(hits_all) == 1:
            repo = hits_all[0]["repo"]
        else:
            return "", None, (
                f"⚠️ 全库有 {len(hits_all)} 个同名 skill「{name}」，请用 repo 指定：\n"
                + "\n".join('  - repo="%s"  `%s`' % (s["repo"], s["path"]) for s in hits_all[:10])
                + '\n\n例：skill_load(repo="%s", name="%s", mode="lite")'
                % (hits_all[0]["repo"], name))

    r = idx.get("repos", {}).get(repo)
    if not r:
        return repo, None, (f"仓库 {repo} 未索引。先 skill_sync('{repo}')。\n"
                            f"本地 skill 源的 repo id 固定为 local/skills。")

    same = [s for s in r["skills"] if s["name"] == name]
    if not same:
        near = [s["name"] for s in r["skills"] if name.lower() in s["name"].lower()][:6]
        return repo, None, (f"{repo} 内无 skill「{name}」。相近："
                            f"{near or [s['name'] for s in r['skills']][:6]}")

    if path:
        hit = next((s for s in same if s["path"] == path or s["path"].endswith(path)), None)
        if not hit:
            return repo, None, (f"{repo} 的「{name}」下没有 path={path}。"
                                f"可选：{[s['path'] for s in same]}")
    elif len(same) > 1:
        return repo, None, (f"⚠️ {repo} 内有 {len(same)} 个同名 skill「{name}」，请用 path 精确指定：\n"
                            + "\n".join('  - path="%s"' % s["path"] for s in same))
    else:
        hit = same[0]
    return repo, hit, ""


def _render_full_payload(repo: str, name: str, hit: dict, sha: str, text: str,
                         fm: dict, src_bytes: int, part: int) -> str:
    """渲染 full 模式正文：分段 + 适用边界 + 邻接提示 + 预算行。"""
    sections = _split_sections(text, SINGLE_SKILL_MAX_BYTES)
    total = len(sections)
    p = max(1, min(int(part or 1), total))
    body = sections[p - 1]

    adj = _adjacency(name, hit)
    boundary_block = _meta_block(hit, full=True)
    boundary = "" if boundary_block else (
        fm.get("whentouse") or fm.get("when_to_use") or fm.get("scope") or "").strip()
    if not boundary:
        dm = re.search(r"(?:Use (?:when|it when|for|to)|适用于|用于)\s*(.{10,220})",
                       fm.get("description") or "", re.I)
        if dm:
            boundary = dm.group(1).strip()

    adj_lines = []
    if adj.get("if_blocked"):
        adj_lines.append(f'if_blocked: skill_load("{repo}", "{adj["if_blocked"]}")   # 被 WAF/403 拦时')
    if adj.get("if_shell"):
        adj_lines.append(f'if_shell:   skill_load("{repo}", "{adj["if_shell"]}")   # 拿到 shell 后')
    if adj.get("if_leak"):
        adj_lines.append(f'if_leak:    skill_load("{repo}", "{adj["if_leak"]}")   # 信息泄露时')
    if adj.get("related_same_domain"):
        adj_lines.append(f'same_domain: {adj["related_same_domain"]}   # 同版块相关（无精确规则，按词重叠挑）')
    if adj.get("same_domain_fallback"):
        adj_lines.append(f'same_domain: {adj["same_domain_fallback"]}   # 仅同版块，相关性未知 —— 优先用 skill_hint')

    payload = (
        f"<!-- skill-router full: {repo}/{name} part {p}/{total} -->\n"
        f"# skill: {name}\nrepo: {repo}\npath: {hit['path']}\nsha: {sha[:8]}\n"
        + (f"part: {p}/{total}（原文 {src_bytes:,}B）\n" if total > 1 else "")
        + (f"\n{body}\n" if boundary_block else f"\n{body}\n\n")
        + (boundary_block if boundary_block else "")
        + "\n---\n## skill-router 邻接提示\n"
        + ("\n".join(adj_lines) + "\n" if adj_lines else "related: （无规则命中，按 skill_hint 判断）\n")
        + (f"when_to_use: {boundary}\n" if boundary else "")
        + 'signal_rule: 出现新报错 / 新端口 / 新框架版本 / 连续失败 2 次 → 先调 skill_hint\n'
        + f'fit_check: 目标与该 skill 前提不符、或同一方法连续失败 2 次 → '
          f'调 skill_fit(current="{name}", observed="你实际看到的情况")\n'
        + f'budget: {_budget_line()}\n'
    )
    if total > 1:
        if p < total:
            payload += (f'\n⚠️ 这是第 {p}/{total} 段。下一段：'
                        f'skill_load(repo="{repo}", name="{name}", mode="full", part={p + 1})\n')
        else:
            payload += f"\n✅ 已是最后一段（{total}/{total}）。\n"
    return payload


@mcp.tool()
def skill_load(repo: str, name: str, mode: str = "lite", primary: bool = True,
               path: str = "", part: int = 1) -> str:
    """加载 skill 正文并注入上下文。返回全文（或目录），末尾附带邻接提示。

    Args:
        repo: 仓库全名，如 SnailSploit/Claude-Red；本地源为 local/skills。
        name: skill 名（skill_search 返回的 name）。
        mode: "lite" 只返回 frontmatter + 章节目录；"full" 返回正文。
        primary: 是否设为主 skill，默认 True（新 load 的成为主）。
                 同时挂多个 skill 且要保留旧主时，显式传 primary=False。
                 以前默认 False，导致第二个 load 永远抢不到主位（实测 ACTIVE 卡在第一个）。
        path: 同仓库内有多个同名 skill 时用它精确指定（用 skill_search 返回的 path）。
        part: 超大 skill 的分段序号，从 1 开始；返回值会注明总段数。
    """
    idx = _load_index()
    repo, hit, err = _resolve_load_target(idx, repo, name, path)
    if err:
        return err
    r = idx.get("repos", {}).get(repo) or {}

    try:
        text = _fetch_skill_text(repo, r["sha"], hit["path"])
    except Exception as ex:
        return f"拉取失败：{ex}"

    fm = _parse_frontmatter(text)
    src_bytes = len(text.encode("utf-8"))

    if mode == "lite":
        heads = [ln for ln in text.splitlines() if re.match(r"^#{1,3}\s", ln)]
        body = "\n".join(heads[:40]) or text[:600]
        payload = (
            f"<!-- skill-router lite: {repo}/{name} -->\n"
            f"# {name}（目录视图）\n"
            f"repo: {repo}\npath: {hit['path']}\nsha: {r['sha'][:8]}\n"
            + (f"description: {fm['description']}\n" if fm.get("description") else "")
            + _meta_block(hit)
            + f"\n## 章节\n{body}\n\n"
            f'> 确认相关后调 skill_load(repo="{repo}", name="{name}", mode="full") 拉全文'
            + (f"（原文 {src_bytes:,}B，约 {-(-src_bytes // SINGLE_SKILL_MAX_BYTES)} 段）" if src_bytes > SINGLE_SKILL_MAX_BYTES else "")
            + "。\n"
        )
        est = _est_tokens(payload)
        if _used_tokens() + est > MAX_SKILL_TOKENS:
            return f"预算不足（{_budget_line()}）。先 skill_unload() 释放。"
        _k_lite = _key(repo, name)
        _LOADED_PARTS[_k_lite] = {0: est}
        LOADED[_k_lite] = est
        # lite 也要记使用：模型主动 load 目录本身就是「选择相信它」的正面信号。
        # 以前只有 full 记，导致 uses 偏低、自成长学不到「哪个 skill 常被先看」。
        try:
            _ev.record_use(repo, name, est)
            # 任务记忆也要记：以前只有 full 写，导致「lite 看目录后就没再 full」
            # 的任务永远进不了历史，skill_plan 下次召回不到（实测 S10 场景）。
            if _ACTIVE_TASK.get("task"):
                _ev.record_task(_ACTIVE_TASK["task"], repo, name, "pending")
        except Exception as _e:
            _warn('skill_load', _e)
            pass
        if primary or not ACTIVE["key"]:
            ACTIVE["key"] = _k_lite
        # lite 也要标 primary：模型需要知道「当前主 skill 是哪个」，
        # 否则同时挂多个时无从判断指令冲突以谁为准（以前只有 full 标）。
        if ACTIVE["key"] == _k_lite:
            payload += "primary: true  ← 当前主 skill，指令冲突时以此为准\n"
        # 自动附「本领域已知坑」—— 模型 load 了就会看到，零额外调用。
        # 这是「预防型」注入：动手前就知道哪里会踩，比事后查更省时间。
        try:
            _exps = _exp.for_skill(name, repo)
            if _exps:
                payload += ("\n## 📚 本领域已知坑（自动附，动手前先扫一眼）\n"
                            + _exp.render(_exps, max_action=2) + "\n")
        except Exception as _e:
            _warn('skill_load', _e)
        # 预算行必须回填成「加载后」状态：渲染时还不知道 est，
        # 直接用 _budget_line() 会把「加载前」的快照发给模型（实测显示 0/40000，
        # 但实际已用 4831）—— 模型据此判断还能不能再 load 就是错的。
        return _stamp_budget(payload, est)

    # full：分段 + 边界 + 邻接提示
    # part 越界必须报错：静默 clamp 到 [1,total] 会让模型以为读到了第 N 段，
    # 实际拿到的是最后一段，误判「已经读完」。
    _total = len(_split_sections(text, SINGLE_SKILL_MAX_BYTES))
    # part 是类型注解 int，但 MCP 层传进来可能是 str（模型填了 "abc"），
    # 直接进 `<` 比较会抛 TypeError。先转再判，失败给明确报错。
    try:
        part = int(part)
    except (TypeError, ValueError):
        return (f'part 必须是整数（从 1 开始），你传了 {part!r}。'
                f'\n正确用法：skill_load(repo="{repo}", name="{name}", mode="full", part=1)')
    if part < 1 or part > _total:
        return (f"part 越界：`{name}` 共 {_total} 段（part 范围 1-{_total}），"
                f"你传了 part={part}。\n"
                f'正确用法：skill_load(repo="{repo}", name="{name}", mode="full", part=1)')
    payload = _render_full_payload(repo, name, hit, r.get("sha", ""), text, fm, src_bytes, part)

    est = _est_tokens(payload)
    if _used_tokens() + est > MAX_SKILL_TOKENS:
        return (f"预算不足：本段需 {est} tokens，当前 {_budget_line()}。\n"
                f"先调 skill_unload() 或 skill_unload(name=\"{name}\") 释放。")
    # 按 part 累加：同一段重读不重复计，不同段真累加
    _k_full = _key(repo, name)
    _LOADED_PARTS.setdefault(_k_full, {})[part] = est
    LOADED[_k_full] = sum(_LOADED_PARTS[_k_full].values())
    payload = _stamp_budget(payload, est)
    try:
        _ev.record_use(repo, name, est)
        try:
            if _ACTIVE_TASK.get("task"):
                _ev.record_task(_ACTIVE_TASK["task"], repo, name, "pending")
        except Exception as _e:
            _warn('skill_load', _e)
            pass
    except Exception as _e:
        _warn('skill_load', _e)
        pass
    if primary or not ACTIVE["key"]:
        ACTIVE["key"] = _key(repo, name)
    if ACTIVE["key"] == _key(repo, name):
        payload += "primary: true  ← 当前主 skill，指令冲突时以此为准\n"
    try:
        _exps = _exp.for_skill(name, repo)
        if _exps:
            payload += ("\n## 📚 本领域已知坑（自动附，动手前先扫一眼）\n"
                        + _exp.render(_exps, max_action=2) + "\n")
    except Exception as _e:
        _warn('skill_load', _e)
    return payload


@mcp.tool()
def skill_read_ref(repo: str, name: str, path: str = "", limit: int = 200, offset: int = 0) -> str:
    """读取 skill 目录下的附属文件（references/*.md 等，这些不在索引里）。

    ⚠️ 正文分页请用 `skill_load(mode="full", part=N)`，不是这个工具。

    Args:
        repo: 仓库全名；本地源为 local/skills。
        name: skill 名。
        path: 相对仓库根的完整路径。留空时：本地 skill 会列出目录下所有可读文件；
            远程 skill 只能列出索引里的子 skill（references 需显式给 path）。
        limit: 单次返回最大行数，默认 200。
        offset: 起始行号（从 0 开始）。
    """
    idx = _load_index()
    r = idx.get("repos", {}).get(repo)
    if not r:
        return f"仓库 {repo} 未索引。"
    same = [s for s in r["skills"] if s["name"] == name]
    if not same:
        return f"{repo} 内无 skill「{name}」。"
    if len(same) > 1 and not path:
        return (f"⚠️ {repo} 内有 {len(same)} 个同名 skill「{name}」，请用 path 指定：\n"
                + "\n".join(f'  - path="{s["path"]}"' for s in same))
    hit = same[0]

    if not path:
        # 本地 skill：直接列目录下所有可读文件（references/*.md 等不在索引里）
        if hit.get("local") or r.get("local"):
            d = Path(hit["path"]).parent
            files = sorted(p for p in d.rglob("*")
                           if p.is_file() and p.suffix.lower() in (".md", ".json", ".txt", ".yaml", ".yml"))
            if not files:
                return f"「{name}」目录下没有可读文件。"
            return (f"# {name} 目录文件（本地）\n"
                    + "\n".join(f"- {str(p).replace(chr(92), '/')}" for p in files[:80])
                    + "\n\n用 skill_read_ref(path=上面的完整路径) 读取。")

        base = hit["path"].rsplit("/", 1)[0]
        prefix = base + "/" if base else ""
        subs = [s["path"] for s in r["skills"] if s["path"].startswith(prefix) and s["path"] != hit["path"]]
        if not subs:
            return (f"「{name}」没有子 skill。\n"
                    f"references 目录需显式给出 path，例如 path=\"{prefix}references/xxx.md\"")
        return f"# {name} 目录下的子项\n" + "\n".join(f"- {p}" for p in subs[:60])

    try:
        text = _fetch_skill_text(repo, r["sha"], path)
    except Exception as ex:
        return f"拉取失败：{ex}"

    lines = text.splitlines()
    total = len(lines)
    off = max(0, int(offset or 0))
    lim = max(1, int(limit or 200))
    seg = lines[off:off + lim]
    tail = ""
    if off + lim < total:
        tail = (f"\n\n...（第 {off + 1}-{off + len(seg)} 行 / 共 {total} 行）"
                f"\n继续：skill_read_ref(repo=\"{repo}\", name=\"{name}\", "
                f"path=\"{path}\", offset={off + lim}, limit={lim})")
    return f"<!-- ref: {repo}/{path} offset={off} -->\n" + "\n".join(seg) + tail


def _hint_auto_infer(signal: str) -> list[str]:
    """自动推断「上一次 hint 的推荐是否被采纳」，并返回症状库历史。

    规则（不依赖模型主动调 skill_feedback）：
      上次推荐的候选被 load 了        → 弱成功 +0.3，并把任务记 ok
      被 load 的不是 Top1             → Top1 记满额 fail（推荐排序错了）
      一个都没 load                   → 不记任何东西（模型还在探索，不是失败）

    第三条是关键：连续调 hint 换信号、看更多候选是正常行为，不是「推荐失败」。
    只有「模型看了推荐、却选了别的」才是推荐排序错的可靠证据。
    """
    try:
        _ps = _LAST_HINT.get("signal", "")
        _pc = _LAST_HINT.get("candidates") or []
        if _ps and _pc:
            _loaded_now = set(LOADED.keys())
            _adopted = [c for c in _pc if c in _loaded_now]
            if _adopted:
                for _k in _adopted[:2]:
                    if "::" in _k:
                        _r0, _n0 = _k.split("::", 1)
                        _ev.record_success(_r0, _n0, 0.3)
                        try:
                            if _ACTIVE_TASK.get("task"):
                                _ev.record_task(_ACTIVE_TASK["task"], _r0, _n0, "ok")
                        except Exception as _e:
                            _warn('_hint_auto_infer', _e)
                # 模型选了别的 skill 而没选 Top1 → 推荐排序确实错了
                if _pc[0] not in _adopted and "::" in _pc[0]:
                    _r0, _n0 = _pc[0].split("::", 1)
                    _ev.record_fail(_r0, _n0, _ps, _pc, weight=1.0)
            # else: 一个都没 load —— 模型还在探索（换信号 / 看更多候选），
            # 不是「推荐失败」，不记任何东西。以前这里记 fail，导致
            # hunt-sqli / offensive-idor 等 uses=0 却 fail 虚高。
    except Exception as _e:
        _warn('_hint_auto_infer', _e)
    try:
        return _ev.lookup_signal(signal)
    except Exception as _e:
        _warn('_hint_auto_infer', _e)
        return []


def _render_new_symptom(signal: str, phen: list[str], hist: list[str] | None = None) -> str:
    """规则表没命中时的渲染：历史命中 / 现象加权 / 未翻译 / 纯字面候选。"""
    hist = hist or []
    _fb: list[str] = []
    _cands: list[str] = []
    try:
        _sr = skill_search(signal, limit=3)
        for _ln in _sr.splitlines():
            if _ln.startswith(("1.", "2.", "3.")) and "**" in _ln:
                _fb.append(_ln.strip())
                _m0 = re.match(r"\d\.\s+\*\*(.+?)\*\*\s+\[(.+?)\]", _ln.strip())
                if _m0:
                    _cands.append(_key(_m0.group(2), _m0.group(1)))
    except Exception as _e:
        _warn('_render_new_symptom', _e)

    # 现象本体已识别 → 工具已经「听懂」了，不该再报「未翻译」
    _untranslated = _looks_untranslated(signal) and not phen
    _head = ("# ⚠️ 未翻译的口语描述" if _untranslated
             else (f"# 现象已识别 [{', '.join(phen)}]" if phen
                   else ("# 历史命中（规则未命中，症状库有记录）" if hist
                         else "# 新症状（规则未命中，字面检索结果）")))
    _out = [f"{_head}：{signal}", ""]

    if phen:
        _out += [
            f"**现象本体识别为 `{', '.join(phen)}`** —— 已按该技术域加权检索。",
            "下面候选是按现象匹配的，可靠性高于字面匹配；确认后 `mode=\"lite\"` 看目录。",
            "",
        ]
        if _fb:
            _out += ["**现象加权候选**："]
            _out += ["  " + _x for _x in _fb]
            _out += ["", "若都不对，再用标准术语重试一次 `skill_hint(术语 + 端口/报错码)`。"]
        else:
            _out += ["该技术域下未检索到 skill，建议 `skill_search` 换更具体的词。"]
    elif _untranslated:
        _out += [
            "**你直接甩了句大白话过来。** 本工具是关键词查表，不做语义理解，",
            "所以下面的候选只是你原话的**字面匹配，可靠性低**。",
            "",
            "## 必做：先翻译，再用翻译后的词重试一次",
            "",
            "`skill_hint(\"翻译后的标准术语 + 具体标识\")`",
            "",
            "例：",
            "- 「缓存服务没设密码」→ `redis 6379 unauthorized`",
            "- 「对面有防护一直挡」→ `waf bypass 403`",
            "- 「点一下要等好几秒」→ `cloudflare turnstile 反爬`",
            "",
            "**翻译这一步是你的活，不是工具的。**",
            "",
        ]
        if _fb:
            _out += ["## 仅供参考（未翻译，不要直接 load）", ""]
            _out += ["  " + _x for _x in _fb]
            _out += ["", "> 若翻译后仍无命中，再用这里的候选，但必须先 `mode=\"lite\"` 看目录确认。"]
        else:
            _out += ["字面匹配也零命中。照下表翻译后重试：", "", TERM_TRANSLATION]
    elif _fb:
        _out.append("**服务端检索候选**（字面匹配，建议先 lite 看目录再 full）：")
        _out += ["  " + _x for _x in _fb]
        _out.append("")
        _out.append("确认哪个对路后调 skill_feedback(repo, name, \"ok\") 标记。")
    else:
        _out.append("检索无命中。**本工具是关键词查表，不做语义理解** —— "
                    "说明你的描述里没有索引认识的词。")
        _out += ["", "照下表把现象翻译成标准术语，再用翻译后的词重试：", "", TERM_TRANSLATION]
    # 症状库历史放在最后：当前查询的现象识别是直接证据，历史是间接证据。
    # 之前放在头部，导致「登录框能过但改参数就报错」被历史里的 authentication
    # 抢到第一位，压过了 sqli —— 历史里可能残留无关的旧记录。
    if hist:
        _out += ["", "**症状库历史**（同一现象以前命中过的 skill，仅供参考）："]
        for _h in hist[:3]:
            if "::" in _h:
                _hr, _hn = _h.split("::", 1)
                _out.append(f"  - `{_hn}`  [{_hr}]")
        _out.append("> 历史记录可能来自不同上下文，与当前现象冲突时以现象识别为准。")

    _out.append("")
    _out.append(f"当前 {_budget_line()}")
    _remember_hint(signal, _cands)
    return "\n".join(_out)


@mcp.tool()
def skill_hint(signal: str) -> str:
    """上报当前发现的信号，返回更匹配的 skill 推荐。用于执行中途换赛道。

    **连续 2 次同类尝试会自动附上实战经验**（症状匹配，不是关键词猜测）——
    这是「卡住时自动查经验库」的入口，不需要你另外调用。

    ⚠️ 本工具是**关键词查表 + 现象本体匹配**，不做语义理解。传标准术语命中率远高于口语：
       ✅「redis 6379 unauthorized」「waf bypass 403」「actuator env 端点」
       ❌「那个缓存服务好像没设密码」「对面有防护打不进去」
    但描述「行为」也能听懂：现象本体 54 类会识别「页面把我输入原样显示」= xss。

    Args:
        signal: 你观察到的新信号。优先传标准术语 + 具体标识（端口/报错码/框架名/函数名）。
                卡住时直接传**报错原文或现象描述**，越具体经验库命中越准。
    """
    _level = _track_stuck(signal)
    _out = _hint_impl(signal)
    if _level >= 2:
        return _stuck_banner(_level, signal) + "\n---\n\n" + _out
    return _out


def _hint_impl(signal: str) -> str:
    """上报当前发现的信号，返回更匹配的 skill 推荐。用于执行中途换赛道。
    
        ⚠️ 本工具是**关键词查表 + 现象本体匹配**，不做语义理解。传标准术语命中率远高于口语：
           ✅「redis 6379 unauthorized」「waf bypass 403」「actuator env 端点」
           ❌「那个缓存服务好像没设密码」「对面有防护打不进去」
        但描述「行为」也能听懂：现象本体 54 类会识别「页面把我输入原样显示」= xss。
    
        Args:
            signal: 你观察到的新信号。优先传标准术语 + 具体标识（端口/报错码/框架名/函数名）。
    """
    # 索引为空 / 损坏时必须明确报错。以前会走到「信号命中：sqli」然后零候选 ——
    # 标题说命中、下面什么都没有，模型会误以为「工具认为 sqli 就是答案」。
    if not _all_skills(_load_index()):
        return ("# 索引为空或损坏，无法检索\n\n"
                "`index.json` 读不出任何 skill。按顺序排查：\n"
                "1. `skill_status()` 看索引状态\n"
                "2. `skill_sync()` 重建索引（会重新拉取各仓库）\n"
                "3. 若 index.json 是非法 JSON，删除后重跑 skill_sync\n\n"
                "**在索引修好之前，所有检索类工具都给不出可靠结果。**")

    _hist = _hint_auto_infer(signal)

    hits = _hint_lookup(signal)
    _low = signal.lower()
    _stuck = [k for k in STUCK_SIGNALS if _match(k, _low)]
    _phen = _detect_phenomena(signal)

    # 只有失败信号、没有任何技术特征和现象 → 纯 fit 引导（工具不知道换什么，交回模型）
    if _stuck and not hits and not _phen and not _hist:
        _remember_hint(signal, [])
        return _stuck_response(signal, _stuck)

    # hits 空就统一走兜底渲染。
    # 以前条件是 `not hits and not _hist` —— 只要症状库有历史，hits 空时就会掉进
    # 「信号命中」分支，而该分支只遍历 hits，于是输出标题 + 预算行、零候选。
    # 实测「样本」就是这样：`# 信号命中：样本` 后面什么都没有。
    if not hits:
        return _render_new_symptom(signal, _phen, _hist)

    idx = _load_index()
    pool = {s["name"]: s for s in _all_skills(idx)}

    out = [f"# 信号命中：{signal}", ""]

    # 技术特征 + 失败信号并存：先警告，再给匹配结果。
    if _stuck:
        out += [
            f"⚠️ **你的描述里同时含失败信号（{' / '.join(_stuck)}）。**",
            "下面的匹配是按「你看到的技术特征」给的，**不代表当前方法还能用**。",
            "如果同一类方法已试 2 次 → 先 `skill_fit(current, observed)` 判断是否该换赛道，",
            "不要直接把下面第一个 skill 当成「再试一次」的理由。",
            "",
        ]

    # 现象本体优先：规则表按单术语匹配粒度粗（「反编译」把 .NET 指向 IDA），
    # 现象按技术域匹配更准（dotnet → dotnet-reverse）。两者都给，现象在前。
    if _phen:
        try:
            _ps_txt = skill_search(signal, limit=3)
            _ps_lines = [l.strip() for l in _ps_txt.splitlines() if re.match(r"\d\.\s\*\*", l.strip())]
        except Exception as _e:
            _warn('skill_hint', _e)
            _ps_lines = []
        if _ps_lines:
            out += [
                f"**现象本体识别为 `{', '.join(_phen)}`** —— 按技术域重排，比单术语匹配精确：",
                "",
            ]
            out += ["  " + x for x in _ps_lines]
            out += ["",
                    "> 下面的「规则表匹配」是按单个术语给的，粒度更粗；"
                    "两者冲突时以现象匹配为准。",
                    ""]

    _hit_cands: list[str] = []
    for targets, why in hits:
        out.append(f"**为什么**：{why}")
        for t in targets.split(","):
            t = t.strip()
            s = (pool.get(t)
                 or next((v for k, v in pool.items() if t in k), None)
                 or next((v for k, v in pool.items() if k in t and len(k) > 4), None))
            if s:
                out.append(f"  - `{s['name']}`  [{s['repo']}]  `{s['path']}`")
                _hit_cands.append(_key(s["repo"], s["name"]))
            else:
                out.append(f"  - `{t}`（索引中未找到，可 skill_sync 补仓库）")
        out.append("")

    out.append(f"当前 {_budget_line()}")
    out.append("换赛道前建议先 skill_unload() 释放旧 skill，避免指令冲突。")
    _remember_hint(signal, _hit_cands)
    return "\n".join(out)


@mcp.tool()
def skill_unload(name: str = "") -> str:
    """卸载已加载的 skill，释放上下文预算。阶段切换时必须调用。

    Args:
        name: 要卸载的 skill 名；留空则卸载全部。
    """
    global ACTIVE
    if not LOADED:
        return "当前没有已加载的 skill。"
    # 自成长：卸载即视为用完，记 0.5 成功（弱信号，可被 skill_feedback 覆盖）
    try:
        for _k in list(LOADED):
            if "::" in _k:
                _fr, _fn = _k.split("::", 1)
                _ev.record_success(_fr, _fn, 0.5)
    except Exception as _e:
        _warn('skill_unload', _e)
        pass

    if not name:
        freed = _used_tokens()
        n = len(LOADED)
        LOADED.clear()
        _LOADED_PARTS.clear()
        # 必须原地改：`ACTIVE = {...}` 在函数内是局部赋值（没有 global），
        # 模块级 ACTIVE 不变 → 卸载后 primary 仍指向已卸载的 skill。
        ACTIVE["key"] = ""
        # 任务结束的自然边界 → 强制落盘。否则节流（10 条 / 30 秒）下崩溃会丢最多 9 条。
        try:
            _ev.flush_all()
        except Exception as _e:
            _warn('skill_unload', _e)
            pass
        return f"已卸载全部 {n} 个 skill，释放 {freed} tokens。当前 0/{MAX_SKILL_TOKENS}。"

    match = [k for k in LOADED if k.endswith(f"::{name}")]
    if not match:
        return f"未找到已加载的「{name}」。当前已加载：{[k.split('::')[-1] for k in LOADED]}"

    freed = sum(LOADED.pop(k) for k in match)
    for k in match:
        _LOADED_PARTS.pop(k, None)
    if ACTIVE["key"] in match:
        ACTIVE["key"] = next(iter(LOADED), "")
    try:
        _ev.flush_all()
    except Exception as _e:
        _warn('skill_unload', _e)
        pass
    return f"已卸载 {name}，释放 {freed} tokens。当前 {_budget_line()}"


# 技术栈 → 攻击面展开：MySQL 和 SQLi 字面不交，但语义强相关
_TECH_EXPAND: dict[str, str] = {
    "mysql": "sql sqli injection", "mariadb": "sql sqli injection",
    "oracle": "sql sqli injection", "postgres": "sql sqli injection",
    "mssql": "sql sqli injection", "sqlite": "sql sqli injection",
    "thinkphp": "php injection rce", "laravel": "php", "php": "php injection",
    "spring": "java deserialization", "struts": "java rce", "shiro": "java deserialization",
    "log4j": "java jndi rce", "fastjson": "java deserialization",
    "redis": "unauthorized privesc", "mongodb": "unauthorized",
    "docker": "container escape", "k8s": "container kubernetes",
    "jwt": "jwt token auth", "oauth": "oauth auth", "saml": "saml auth",
    "upx": "packer unpack", "themida": "packer unpack",
    "frida": "frida hook", "jadx": "android apk",
    "eac": "anti-cheat", "battleye": "anti-cheat", "vanguard": "anti-cheat",
    "vmp": "vmp virtualization", "ollvm": "ollvm obfuscation",
}


def _clean_observed(observed: str) -> str:
    """剥掉否定短语与开头主语。

    否定词会反向污染检索：「无验证码」会命中 captcha skill，
    而用户的意思恰恰是「这里没有 captcha」。
    """
    clean = re.sub(r"(?:无|没有|不含|不存在|未发现|看不到|没看到)\s*[^\s，,。；;、]{1,12}", " ", observed)
    clean = re.sub(r"^\s*(?:目标|当前|现在|这里|已经)\s*", "", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean or observed


def _fit_rank(cur_name: str, clean: str, dom: str):
    """用「当前观察」重新检索，判断当前 skill 还排第几。

    返回 (names_all, pos, term_ratio, hit_parts, parts)。
    pos=0 表示排不上号（检索完全没排出当前 skill）。
    """
    low = clean.lower()
    extra = " ".join(v for k, v in _TECH_EXPAND.items() if _match(k, low))
    search_q = f"{clean} {extra}".strip() if extra else clean

    def _ranked(scope: str) -> list[str]:
        r = skill_search(search_q, limit=10, domain=scope)
        return [ln.split("**")[1] for ln in r.splitlines()
                if ln[:2] in {f"{i}." for i in range(1, 11)} and "**" in ln]

    names_same = _ranked(dom)
    names_all = names_same if cur_name in names_same[:5] else _ranked("")
    pos = names_all.index(cur_name) + 1 if cur_name in names_all else 0

    # 术语命中：把 skill 名按 - 拆开，看有几个词出现在观察里（启发式校正）
    parts = [p for p in re.split(r"[-_]", cur_name.lower()) if len(p) > 2]
    hit_parts = [p for p in parts if _match(p, clean.lower())]
    term_ratio = len(hit_parts) / max(len(parts), 1)
    return names_all, pos, term_ratio, hit_parts, parts


@mcp.tool()
def skill_fit(current: str, observed: str, repo: str = "") -> str:
    """自检：当前已加载的 skill 是否仍然适配。执行中途发现方法不奏效时调用。

    用「当前观察」重新检索一遍，看当前 skill 还排不排得上号。
    排不上就说明该换赛道，返回值直接给出候选替代。

    Args:
        current: 当前正在用的 skill 名。
        observed: 你实际观察到的情况，越具体越好。
                  例：「目标 ThinkPHP 6 + MySQL 5.7，登录框无验证码，WAF 是 Cloudflare」
                  例：「IDA 里全是 jmp 链，没有可见的校验函数，疑似 VMP」
        repo: 当前 skill 所在仓库（同名 skill 时用来精确定位）。
    """
    # 自成长：fit 说明当前 skill 不适配，记一次失败
    try:
        _ev.record_fail(repo or "", current, observed, [])
    except Exception as _e:
        _warn('skill_fit', _e)
        pass
    idx = _load_index()
    pool = _all_skills(idx)
    if not pool:
        return "索引为空，先 skill_sync()。"

    cur = next((s for s in pool
                if s["name"] == current and (not repo or s["repo"] == repo)), None)
    if cur is None:
        near = [s["name"] for s in pool if current.lower() in s["name"].lower()][:5]
        return f"未找到 skill「{current}」。相近：{near}"

    dom = cur.get("domain") or ""

    clean = _clean_observed(observed)

    names_all, pos, term_ratio, hit_parts, parts = _fit_rank(current, clean, dom)

    # 硬否决：skill 自己写了「不要 X」，而你在做 X —— 这比排名更硬，直接判该换
    conflicts = _avoid_conflict(cur, observed)

    if conflicts:
        verdict, icon = "撞上该 skill 自己写的禁止条款 —— 必须换，不是排名问题", "🔴"
    elif pos == 1 or (pos and pos <= 3 and term_ratio >= 0.5):
        verdict, icon = "仍然适用 —— 继续用它", "✅"
    elif pos and pos <= 3:
        verdict, icon = "仍然相关，但不是最优 —— 可看排名更高的", "🟡"
    elif pos:
        verdict, icon = f"排名靠后（第 {pos}/10）—— 建议换", "⚠️"
    else:
        verdict, icon = "在当前观察下排不上号 —— 大概率该换", "🔴"

    out = [f"# 适配自检：{current}", "",
           f"**观察**：{observed}",
           f"**检索用**：{clean}",
           f"**版块**：{dom or '未知'}", "",
           f"## {icon} 判定：{verdict}", ""]

    if conflicts:
        out.append("### ⛔ 撞上的禁止条款（该 skill 正文原文）")
        for c in conflicts:
            out.append(f"  - {c}")
        out.append("")
        out.append("**这不是「排名靠后」，是「前提就不成立」** —— 换个 skill 再谈排名。")
        out.append("")

    out.append(f"**术语命中**：{len(hit_parts)}/{len(parts)} "
               f"({', '.join(hit_parts) if hit_parts else '无'}) —— 启发式，仅作参考")
    out.append("")

    if names_all:
        out.append("### 用当前观察重新排序的前 5")
        for i, n in enumerate(names_all[:5], 1):
            mark = "  ← 当前" if n == current else ""
            out.append(f"  {i}. `{n}`{mark}")
        out.append("")

    if pos != 1 or conflicts:
        out.append("### 建议动作")
        out.append("1. `skill_unload()` 卸掉当前 skill —— 两个方法论同时挂着会打架")
        if names_all:
            out.append(f'2. `skill_load(repo, "{names_all[0]}", mode="lite")` 看目录再决定')
        else:
            out.append("2. 检索没排出候选 —— 观察太抽象，补具体信息（报错原文 / 函数名 / "
                       "端口 / 版本号 / 抓到的包）后重试 `skill_hint`")
        out.append("3. 若排名靠前的也不对，用 `skill_hint(observed)` 让规则表判断")
        out.append("")
        out.append("> ⚠️ 这是基于关键词的启发式判定。若你认为当前 skill 其实对路"
                   "（比如它讲的是通用原理），可以忽略本结论 —— 但必须能说出理由。")
    else:
        out.append("### 提醒")
        out.append("判定为适用不代表一定成功 —— 同一方法连续失败 2 次仍应换赛道。")

    out += ["", f"当前 {_budget_line()}"]
    return "\n".join(out)


@mcp.tool()
def skill_meta(name: str, repo: str = "") -> str:
    """查看某个 skill 的完整适用边界：什么时候用 / 需要什么 / 禁止什么 / 依赖什么工具。

    这些是从正文自动抽取的（非人工标注），决定了 skill 是否「该用」——
    比 desc 里罗列的技术栈重要得多。决定要不要 load 之前先看这个。

    Args:
        name: skill 名。
        repo: 仓库全名，同名 skill 时用来精确定位。
    """
    pool = _all_skills(_load_index())
    cands = [s for s in pool if s["name"] == name and (not repo or s["repo"] == repo)]
    if not cands:
        near = [s["name"] for s in pool if name.lower() in s["name"].lower()][:5]
        return f"未找到 skill「{name}」。相近：{near}"
    if len(cands) > 1:
        return (f"⚠️ 有 {len(cands)} 个同名 skill「{name}」，请用 repo 指定：\n"
                + "\n".join(f'  - repo="{s["repo"]}"  `{s["path"]}`' for s in cands[:8]))

    s = cands[0]
    mt = s.get("meta") or {}
    _kb = (s.get("size") or 0) / 1024
    out = [f"# 适用边界：{s['name']}", "",
           f"repo: {s['repo']}　版块: {s.get('domain','')}", f"path: `{s['path']}`",
           f"体积: {_kb:.1f}KB　建议加载: **{_suggest_mode(s)}**", ""]
    if s.get("desc"):
        out += [f"**desc**：{s['desc']}", ""]

    LABEL = {"use_when": "✅ 什么时候用", "requires": "📋 前置条件",
             "avoid": "⛔ 禁止 / 铁律", "tools": "🔧 工具依赖"}
    any_meta = False
    for k in ("use_when", "requires", "avoid", "tools"):
        vals = mt.get(k) or []
        if not vals:
            continue
        any_meta = True
        out.append(f"## {LABEL[k]}")
        for v in vals:
            out.append(f"  - {v}")
        out.append("")

    if not any_meta:
        out += ["（正文里没有可抽取的边界信息 —— 这类 skill 只能靠 desc 判断，",
                "  建议先 `mode=\"lite\"` 看章节目录再决定）", ""]
    else:
        out += ["> 以上为正文自动抽取，可能有遗漏。判断前可再 `skill_load(mode=\"lite\")` 看目录。", ""]
    return "\n".join(out)


@mcp.tool()
def skill_cmd(name: str, repo: str = "", pattern: str = "", kind: str = "cmd",
              limit: int = 40) -> str:
    """只取 skill 里的可执行命令 / payload，**不加载全文**。

    想看「这个 skill 具体敲什么命令」时用它，比 mode="full" 省 90% 的 token：
    full 一次 3000+ tokens（大部分是方法论叙述），cmd 一次约 200 tokens。

    Args:
        name: skill 名。
        repo: 仓库全名；同名 skill 时必须指定。
        pattern: 只保留匹配该关键词的命令（不区分大小写），如 "sqlmap" / "hashcat"。
        kind: "cmd" 只取 shell 命令（默认）；"payload" 只取注入串/片段；"all" 两者都要。
        limit: 最多返回多少条，默认 40。
    """
    s, err = _resolve_skill(repo, name)
    if not s:
        return err
    try:
        text = _fetch_skill_text(s["repo"], s["sha"], s["path"])
    except Exception as ex:
        return f"拉取失败：{ex}"

    kind = (kind or "cmd").lower()
    items: list[dict] = []
    if kind in ("cmd", "all"):
        items += [dict(x, src="cmd") for x in _extract_commands(text, pattern)]
    if kind in ("payload", "all"):
        items += [dict(x, src="payload") for x in _extract_payloads(text, pattern)]
    items.sort(key=lambda x: x["line"])

    if not items:
        extra = f"（pattern={pattern}）" if pattern else ""
        return (f"# 命令速查：{name}\n\n"
                f"该 skill 正文里没抽到可执行命令{extra}。\n"
                f"可能它是纯方法论型（只讲思路不给命令），用 `mode=\"lite\"` 看章节目录。")

    shown = items[:max(1, int(limit))]
    full_tokens = _est_tokens(text)
    here_tokens = _est_tokens(" ".join(x["cmd"] for x in shown))
    out = [f"# 命令速查：{s['name']}", "",
           f"来源：{s['repo']}  `{s['path']}`",
           f"共 {len(items)} 条" + (f"，显示前 {len(shown)} 条" if len(items) > len(shown) else ""),
           f"≈{here_tokens} tokens（同 skill 全文 ≈{full_tokens}，省 {max(0, 100 - here_tokens * 100 // max(full_tokens, 1))}%）",
           ""]
    for it in shown:
        tag = "" if it["src"] == "cmd" else " `[payload]`"
        out.append(f"## L{it['line']}{tag}")
        if it["note"] and it["note"] not in ("payload", ""):
            out.append(f"# {it['note']}")
        out.append(it["cmd"])
        out.append("")
    if len(items) > len(shown):
        out.append(f"> 还有 {len(items) - len(shown)} 条未显示，用 pattern= 过滤或调大 limit。")
        out.append("")
    out += ["---",
            f'定位原文：skill_read_ref(repo="{s["repo"]}", name="{s["name"]}", '
            f'path="{s["path"]}", offset={shown[0]["line"] - 2}, limit=8)']
    return "\n".join(out)


@mcp.tool()
def skill_exp(query: str = "", phase: str = "", topk: int = 3) -> str:
    """查实战经验库：某个现象别人是怎么解决的。

    经验库记的是「症状 → 正解」的具体条目，不是方法论。
    例：`skill_exp("所有 IR 全对不上")` → 命中「LOAD/STORE 的 inputs[0]
    是随机化的 ram 标记常量，地址在 inputs[1]」。

    什么时候用：
      - 卡住时（skill_hint 会自动带，但也可以主动查）
      - 动手前想先扫一遍该领域的坑（传 phase 过滤）
      - 怀疑「这个错误别人一定踩过」

    Args:
        query: 现象或报错原文。越具体越好（贴报错文本命中率最高）。
        phase: 只查某个阶段：ir / structure / pipeline / llm / discipline / tooling。
        topk: 返回条数，默认 3。
    """
    try:
        topk = max(1, min(int(topk), 10))
    except (TypeError, ValueError):
        topk = 3
    if not query and not phase:
        s = _exp.stats()
        out = [f"# 经验库：{s['total']} 条", ""]
        out.append("**按阶段**：" + " / ".join(f"{k} {v}" for k, v in s["by_phase"].items()))
        out.append("**按来源**：" + " / ".join(f"{k} {v}" for k, v in s["by_source"].items()))
        out.append("**按可信度**：" + " / ".join(f"{k} {v}" for k, v in s["by_conf"].items()))
        out.append("")
        out.append("查具体经验：`skill_exp(\"现象或报错原文\")`")
        out.append("只查某阶段：`skill_exp(phase=\"ir\")`")
        return "\n".join(out)

    exps = _exp.search(query, topk=topk, phase=phase)
    if not exps:
        return (f"# 经验库无匹配：{query or phase}\n\n"
                "说明这个现象在库里没有记录（现有 63 条以 VMP/逆向为主）。\n"
                "**此时该做的**：把现象描述得更具体（报错原文 / 函数名 / 版本号 / 偏移），\n"
                "或者这确实是个新坑 —— 值得记下来。")
    for e in exps:
        try:
            _exp.record_hit(e.get("id", ""), None)
        except Exception:
            pass
    return (f"# 经验库命中 {len(exps)} 条：{query or phase}\n\n"
            + _exp.render(exps, max_action=3)
            + "\n\n> 按上面做。如果这条经验确实解决了问题，说明它可信；"
              "如果不对路，别硬套 —— 版本/架构不同，坑也不同。")


@mcp.tool()
def skill_stats(limit: int = 20, sort: str = "score") -> str:
    """查看 skill 使用统计与症状库。自成长模块的记录都在这里。

    Args:
        limit: 显示前几条，默认 20。
        sort: 排序依据 —— "score"（默认）/ "uses" / "tokens"。
    """
    try:
        return _ev.stats_text(limit=limit, sort=sort)
    except Exception as e:
        return f"统计不可用：{e}\n（evolve.py 是否已复制到 server.py 同目录？）"


@mcp.tool()
def skill_feedback(repo: str, name: str, verdict: str, note: str = "") -> str:
    """给某个 skill 打显式反馈，覆盖自动推断。这是唯一的人工纠偏入口。

    Args:
        repo: 仓库全名，如 local/skills。
        name: skill 名。
        verdict: "ok"（有效）/ "bad"（无效）。
        note: 备注，写进统计记录。
    """
    if verdict not in ("ok", "bad"):
        return 'verdict 只能是 "ok" 或 "bad"。'
    try:
        _ev.record_feedback(repo, name, verdict, note)
        return f"已记录：{repo}::{name} → {verdict}" + (f"（{note}）" if note else "")
    except Exception as e:
        return f"记录失败：{e}"


@mcp.tool()
def skill_check_update(repo: str = "", apply: bool = False, timeout: int = 25) -> str:
    """检查各仓库「本地 pinned sha」与「上游最新 sha」是否一致。

    用途：skill_sync 之后上游又改了，用它一眼看出哪些仓库需要重拉。
    apply=True 时把本地 sha 更新为上游最新（**只更新标记，不重拉文件树**，秒级完成）——
    真正的 skill 内容更新仍要 `skill_sync`。

    Args:
        repo: 只查某个仓库（owner/repo）；留空则查全部。
        apply: True 时把本地 sha 写为上游最新，消除「未 pin」状态。
        timeout: 单个仓库的 git ls-remote 超时秒数。
    """
    idx = _load_index()
    repos = idx.get("repos", {})
    targets = [repo] if repo else [r for r in repos if not r.startswith("local/")]
    if repo and repo not in repos:
        return f"仓库 {repo} 未索引。"

    rows: list[tuple[str, str, str, str]] = []
    changed = 0
    for rn in targets:
        r = repos.get(rn)
        if not r:
            rows.append((rn, "-", "-", "❌ 未索引"))
            continue
        local = str(r.get("sha", ""))
        branch = str(r.get("branch", "main"))
        try:
            remote = _head_sha(rn, branch)
        except Exception as _e:
            _warn('skill_check_update', _e)
            remote = ""
        if not remote:
            rows.append((rn, local[:8], "-", "⚠️ 上游查询失败"))
            continue
        if len(local) == 40 and local == remote:
            rows.append((rn, local[:8], remote[:8], "✅ 一致"))
        elif len(local) != 40:
            rows.append((rn, local[:8] or "-", remote[:8], "⚠️ 未 pin"))
            if apply:
                r["sha"] = remote
                changed += 1
        else:
            rows.append((rn, local[:8], remote[:8], "🔴 上游已更新"))
            if apply:
                r["sha"] = remote
                changed += 1

    if apply and changed:
        idx["repos"] = repos
        try:
            INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding="utf-8")
            _INDEX_CACHE["data"] = None       # 让下次 _load_index 重新读
            _INDEX_CACHE["mtime"] = -1.0
        except Exception as ex:
            return f"写索引失败：{ex}"

    out = [f"# 上游更新检查（{len(rows)} 个仓库）", "",
           "| 仓库 | 本地 | 上游 | 状态 |",
           "| :--- | :--- | :--- | :--- |"]
    for rn, lo, re_, st in rows:
        out.append(f"| {rn} | `{lo}` | `{re_}` | {st} |")
    out.append("")
    stale = sum(1 for *_, st in rows if "已更新" in st or "未 pin" in st)
    if apply and changed:
        out.append(f"✅ 已更新 {changed} 个仓库的 sha 标记。内容更新仍需 `skill_sync(repo=\"...\")`。")
    elif stale:
        out.append(f"⚠️ {stale} 个仓库与上游不一致。`apply=True` 只更新标记；"
                   f"要真正拉新内容用 `skill_sync(repo=\"...\", enrich=True)`。")
    else:
        out.append("✅ 全部一致。")
    return "\n".join(out)


@mcp.tool()
def skill_status() -> str:
    """查看索引规模、已加载 skill、上下文预算与当前主 skill。"""
    idx = _load_index()
    repos = idx.get("repos", {})
    total = len(_all_skills(idx))
    out = [f"# skill-router 状态", "",
           f"索引：{len(repos)} 个仓库 / {total} 个 skill",
           f"GitHub token：{'已配置 ✅' if TOKEN else '未配置 · 不影响使用（树走 ungh，正文走 raw）'}",
           f"缓存目录：{CACHE_DIR}", ""]

    by_dom: dict[str, int] = {}
    for r in repos.values():
        for s in r["skills"]:
            d = s.get("domain") or "misc"
            by_dom[d] = by_dom.get(d, 0) + 1
    if by_dom:
        out.append("**版块分布**：" + " · ".join(
            f"{k}={v}" for k, v in sorted(by_dom.items(), key=lambda x: -x[1])))
        out.append("")

    unpinned = 0
    for rn, r in repos.items():
        pinned = len(r.get("sha", "")) == 40
        if not pinned and not rn.startswith("local/"):
            unpinned += 1
        ver = f"sha={r['sha'][:8]}" if pinned else "未 pin"
        out.append(f"- {rn}  branch={r['branch']}  {ver}  skill={len(r['skills'])}")
    if unpinned:
        out.append("")
        out.append(f"> ⚠️ {unpinned} 个仓库未 pin（存的是 branch 名，无法检测上游更新）。"
                   f"跑 `skill_check_update(apply=True)` 补上。")

    out += ["", f"## 上下文", _budget_line()]
    if LOADED:
        for k, t in sorted(LOADED.items(), key=lambda x: -x[1]):
            mark = " ★主" if k == ACTIVE["key"] else ""
            out.append(f"- {k.split('::')[-1]}  {t} tokens{mark}")
    else:
        out.append("- （空）")
    return "\n".join(out)


# ──────────────────────────── 作战配方（skill_plan）────────────────────────────
# 借鉴 seep 的 auto_triage：一次调用返回「有序步骤 + 每步为什么 + 预期产出」，
# 而不是让模型自己连搜 5 次。

RECIPES: dict[str, dict] = {
    "web_admin_takeover": {
        "alias": ["拿后台", "进后台", "后台", "admin", "登录后台", "拿下网站", "网站后台"],
        "steps": [
            [
                "侦察",
                ["reconnaissance", "techstack-identification"],
                "先定技术栈与入口清单，不侦察就开扫等于赌博",
            ],
            [
                "分诊",
                ["attack-path-stitcher"],
                "把 recon 结果拼成按成功率排序的路径，决定先打哪条",
            ],
            [
                "认证攻击",
                [
                    "authentication", "offensive-sqli", "offensive-jwt", "offensive-idor",
                    "offensive-business-logic"
                ],
                "登录框注入 / 会话伪造 / JWT 算法混淆 / 越权直读 / 验证码与找回密码逻辑",
            ],
            [
                "后台利用",
                [
                    "offensive-file-upload", "offensive-ssti", "offensive-deserialization",
                    "offensive-rce"
                ],
                "拿到后台后转 shell：上传绕过 → 模板注入 → 反序列化 → 命令注入",
            ],
            [
                "固化",
                ["cve-poc-generator", "transilience-report-style"],
                "PoC 固化与报告归档",
            ],
        ],
    },
    "license_bypass": {
        "alias": ["卡密", "注册机", "授权码", "激活码", "license", "机器码", "试用", "加密狗"],
        "steps": [
            [
                "定性",
                ["client-license-validation-bypass"],
                "开测四问：权威在服务端还是本地 / 能否伪造成握手 / 有无后门捷径 / 功能是否与校验分离",
            ],
            [
                "路由",
                ["alice-crack"],
                "按任务匹配度选模块，先看领域索引不要全读",
            ],
            [
                "静态定位",
                ["rev-idapython", "ida-ghidra-workflow", "rev-struct"],
                "定位校验函数与决策分支，恢复结构体",
            ],
            [
                "脱壳/反检测",
                ["packer-and-loader-analysis", "anti-analysis-and-integrity"],
                "被壳或反调试挡住时先处理保护层（PE/ELF/APK 通用；确认是 APK 再换 apk-protection-analysis）",
            ],
            [
                "动态验证",
                ["rev-frida", "runtime-patch-validation"],
                "Hook 或内存 patch 验证放行",
            ],
            [
                "服务端依赖",
                ["android-elf-license-crack", "cloud-license-frida-hook"],
                "必须联网时走中继或完全脱机化（前者 Android/ELF，后者 PE 云许可）",
            ],
        ],
    },
    "apk_reverse": {
        "alias": ["apk", "安卓逆向", "dex", "so 逆向", "安卓"],
        "steps": [
            [
                "体检",
                ["apk-protection-analysis", "android-reverse-setup-audit"],
                "先识别壳类型与保护方案，再决定工具链",
            ],
            [
                "脱壳",
                ["android-unpacking-and-dumping", "rev-dex-dumper", "native-unpacking"],
                "整体壳 / 抽取壳 / native 壳分别处理",
            ],
            [
                "静态",
                ["android-static-analysis", "rev-idapython", "jni-native-analysis"],
                "DEX 与 native 层分别分析",
            ],
            [
                "动态",
                ["android-runtime-hooking", "rev-frida", "frida-stealth-hooking"],
                "运行时 hook，注意反 hook 检测",
            ],
            [
                "反检测",
                [
                    "anti-analysis-and-integrity", "anti-hook-artifact-analysis",
                    "root-emulator-detection"
                ],
                "被检测到时逐项处理",
            ],
        ],
    },
    "js_sign": {
        "alias": ["js 逆向", "js逆向", "签名", "加密参数", "sign", "风控", "验证码", "a_bogus", "h5st", "xs"],
        "steps": [
            [
                "定位",
                ["web-signature-analysis", "browser-runtime-tracing"],
                "先定位加密入口与调用栈",
            ],
            [
                "去混淆",
                ["javascript-obfuscator-family", "jsvmp-vmp-analysis", "js-deobfuscation"],
                "按混淆家族选对应还原手法",
            ],
            [
                "算法还原",
                ["wasm-crypto-signature", "wasm-reversing", "webcrypto-hooking"],
                "JS 还原不了就看 WASM",
            ],
            [
                "风控对抗",
                ["anti-bot-analysis", "browser-fingerprint-analysis", "tls-pinning-analysis"],
                "指纹 / TLS / 行为检测",
            ],
            [
                "验证码",
                ["vendor-geetest-captcha", "vendor-ruishu-rs", "vendor-cloudflare-turnstile"],
                "按厂商选绕过方案",
            ],
        ],
    },
    "ad_internal": {
        "alias": ["内网", "域控", "ad", "kerberoast", "adcs", "横向", "提权"],
        "steps": [
            [
                "侦察",
                ["offensive-active-directory", "ad-methodology"],
                "BloodHound / PowerView 摸清域结构",
            ],
            [
                "凭据",
                ["offensive-linux-privesc", "offensive-windows-privesc"],
                "先拿到可用凭据与本地权限",
            ],
            [
                "ADCS",
                ["adcs-attacks"],
                "ESC1-17 逐个比对证书模板",
            ],
            [
                "ACL",
                ["acl-abuse", "coercion-ntlm-relay"],
                "ACL 滥用与强制认证中继",
            ],
            [
                "横向",
                ["offensive-lateral-movement", "offensive-persistence"],
                "PtH / PtT / NTLM relay，落持久化",
            ],
        ],
    },
    "binary_patch": {
        "alias": ["crackme", "patch", "爆破", "改跳转", "二进制", "elf", "pe"],
        "steps": [
            [
                "体检",
                ["native-binary-analysis", "rev-idapython"],
                "架构 / 缓解 / 关键字符串",
            ],
            [
                "定位",
                ["ida-ghidra-workflow", "rev-symbol", "rev-struct"],
                "找校验函数与分支",
            ],
            [
                "验证",
                ["rev-unicorn-debug", "runtime-patch-validation"],
                "脱离真机模拟执行验证",
            ],
            [
                "重建",
                ["packed-so-elf-rebuild", "dump-fix-rebuild"],
                "脱壳后修复与重打包",
            ],
        ],
    },
    "vmp_unpack": {
        "alias": [
            "vmp", "vmprotect", "虚拟化", "去虚拟化", "脱壳", "加壳", "themida", "winlicense", "enigma",
            "ollvm", "加固", "混淆"
        ],
        "steps": [
            [
                "识别壳型",
                ["packer-and-loader-analysis", "apk-protection-analysis"],
                "先定壳类型：VMP / Themida / OLLVM / 自研 VM，这一步决定后面整条路线",
            ],
            [
                "静态定位",
                ["vmp-reverse", "virtualization-protection"],
                "IDA 定位 dispatcher 与锚点寄存器，标定 vmCode/vmStack/vmRegBase —— 按 offset 不按名字",
            ],
            [
                "动态 trace",
                ["emulation-unicorn-angr", "vmp-reverse"],
                "trace 把执行流拍平切 handler；不追求搞懂每条运算，只判断「这是哪个 handler」",
            ],
            [
                "语义还原",
                ["vmp-reverse", "jsvmp-vmp-analysis", "ollvm-deobfuscation"],
                "IR 折叠 + handler 语义表；等价性只认 PROVEN / REFUTED / UNVERIFIED，不许「我觉得等价」",
            ],
            [
                "dump 修复",
                ["dump-fix-rebuild", "packed-so-elf-rebuild", "memdumper-artifact-validation"],
                "IAT / 重定位 / 入口点三件套，产物必须自检",
            ],
            [
                "验证",
                ["rev-unicorn-debug", "runtime-patch-validation"],
                "脱离真机模拟执行，验证还原结果",
            ],
        ],
    },
    # ── 第二批配方：覆盖原来 48% 之外的常见任务 ──
    "malware_analysis": {
        "alias": ("恶意样本", "样本分析", "病毒分析", "木马分析", "malware", "样本体检"),
        "steps": (
            ("体检", ("malware-triage",), "先做静态体检：文件类型、壳、导入表、可疑字符串，别急着跑"),
            ("静态", ("rev-idapython", "rev-struct"), "IDA 里定位 C2 配置、加密循环、持久化逻辑"),
            ("动态", ("malware-dynamic-analysis",), "沙箱/隔离环境跑行为：进程树、网络、文件落地"),
            ("规则", ("yara-rule-authoring",), "把 IOC 固化成 YARA，供后续批量扫描"),
            ("归因", ("offensive-reporting",), "输出家族归因与处置建议"),
        ),
    },
    "asset_recon": {
        "alias": ("资产测绘", "资产发现", "信息收集", "子域名", "子域", "recon", "指纹识别", "打点面"),
        "steps": (
            ("子域", ("hunt-subdomain", "reconnaissance"), "先扩面：子域、旁站、证书透明度日志"),
            ("指纹", ("techstack-identification",), "定技术栈与版本，决定后续能打哪类漏洞"),
            ("资产面", ("offensive-osint-methodology",), "企查/备案/ASN/云资产，把面铺到最大"),
            ("分诊", ("attack-path-stitcher",), "按成功率排序，选出最值得先打的三条路径"),
        ),
    },
    "src_hunt": {
        "alias": ("src", "挖洞", "漏洞挖掘", "bug bounty", "bugbounty", "众测", "赏金"),
        "steps": (
            ("侦察", ("reconnaissance", "hunt-subdomain"), "范围界定 + 子域 + 指纹，先画清攻击面"),
            ("分诊", ("attack-path-stitcher",), "按 P 级潜力排序，优先打高价值资产"),
            ("高频漏洞", ("hunt-sqli", "hunt-xss", "hunt-idor", "hunt-file-upload"),
             "SRC 里 ROI 最高的四类：注入 / XSS / 越权 / 上传"),
            ("深挖", ("offensive-business-logic", "offensive-jwt"), "逻辑漏洞与鉴权体系是 SRC 的差异化战场"),
            ("报告", ("offensive-reporting", "transilience-report-style"), "按平台格式出报告，附可复现步骤"),
        ),
    },
    "ios_reverse": {
        "alias": ("ios", "ipa", "砸壳", "越狱", "mach-o", "ios 逆向"),
        "steps": (
            ("砸壳", ("ios-unpacking-and-dump",), "App Store 包是加密的，先 dump 出可读 Mach-O"),
            ("静态", ("mobile-security", "rev-idapython"), "定位校验逻辑、密钥、网络请求封装"),
            ("动态", ("rev-frida",), "Frida hook 关键方法，抓明文参数"),
            ("反检测", ("anti-analysis-and-integrity",), "越狱检测 / 反调试 / 完整性校验逐个处理"),
        ),
    },
    "miniprogram_reverse": {
        "alias": ("小程序", "wxapkg", "微信小程序", "支付宝小程序", "小程序逆向"),
        "steps": (
            ("解包", ("wechat-miniprogram-reversing", "alipay-miniprogram-reversing"),
             "wxapkg 解包 → 反编译出 JS，先看清目录结构"),
            ("静态", ("rev-idapython",), "定位加密函数、请求签名、本地存储"),
            ("协议", ("ble-usb-protocol-reversing",), "还原请求签名与风控参数"),
        ),
    },
    "cloud_attack": {
        "alias": ("云上", "云原生", "k8s", "kubernetes", "容器逃逸", "oss", "云元数据", "aws", "阿里云", "云函数"),
        "steps": (
            ("云资产", ("offensive-cloud",), "先摸清云账号边界：IAM、存储桶、函数、元数据服务"),
            ("元数据", ("offensive-ssrf",), "SSRF 打 169.254.169.254 拿临时凭据"),
            ("容器", ("offensive-container-escape",), "容器内提权与逃逸：挂载点、capability、cgroup"),
            ("集群", ("offensive-k8s-attacks",), "K8s 横向：SA token、RBAC、kubelet、etcd"),
        ),
    },
    "phishing_campaign": {
        "alias": ("钓鱼", "钓鱼邮件", "仿站", "话术", "社工", "phishing", "诱导"),
        "steps": (
            ("画像", ("offensive-osint",), "先摸目标：邮箱格式、社交关系、常用服务，话术才有钩子"),
            ("话术", ("offensive-social-engineering",), "按场景选 pretext，别用通用模板"),
            ("投递", ("offensive-phishing",), "邮件/仿站/IM 投递，注意 SPF/DKIM 与落地页一致性"),
            ("落地", ("offensive-initial-access",), "拿到凭据或 shell 后立刻转初始访问"),
        ),
    },
    "wireless_crack": {
        "alias": ("wifi", "无线", "握手包", "wpa", "蓝牙", "ble", "无线破解"),
        "steps": (
            ("侦察", ("offensive-wifi-recon",), "先扫：SSID、加密方式、客户端、信道"),
            ("抓包", ("offensive-wifi",), "抓 4-way handshake 或 PMKID，再离线爆破"),
            ("协议攻击", ("offensive-krack-fragattacks",), "KRACK / FragAttacks 针对 WPA2 协议层"),
            ("蓝牙", ("offensive-bluetooth-ble",), "BLE 配对、GATT 枚举、经典蓝牙嗅探"),
        ),
    },
    "forensics": {
        "alias": ("取证", "应急", "内存取证", "日志分析", "流量分析", "forensics", "证据固定"),
        "steps": (
            ("固定", ("digital-forensics",), "先保全证据：镜像、哈希、时间线，别在原始介质上操作"),
            ("内存", ("ctf-forensics",), "内存镜像里找进程、凭据、注入痕迹"),
            ("日志/流量", ("ctf-forensics",), "日志与 pcap 关联，重建攻击链"),
            ("输出", ("offensive-reporting",), "按时间线出结论，区分 Observed / Inferred / Assumed"),
        ),
    },
    "ctf_reverse": {
        "alias": ("ctf", "pwn", "ctf 逆向", "逆向题", "ctf crypto", "re 题"),
        "steps": (
            ("分诊", ("ctf-reverse",), "先判题型：算法还原 / 反调试 / 虚拟机 / 固件"),
            ("静态", ("rev-idapython", "rev-struct"), "IDA 定位校验函数，恢复结构体"),
            ("动态", ("rev-unicorn-debug",), "模拟执行绕过反调试，直接看寄存器与内存"),
            ("利用", ("offensive-exploit-development",), "从验证绕过到完整利用"),
        ),
    },
    "report_write": {
        "alias": ("写报告", "渗透报告", "报告", "交付", "汇报", "deliverable", "出报告"),
        "steps": (
            ("结构", ("offensive-reporting",), "先定骨架：范围 / 方法 / 发现 / 影响 / 修复 / 附录"),
            ("风格", ("transilience-report-style",), "按客户可读性调整：执行摘要给决策层，细节给技术团队"),
            ("定稿", ("offensive-reporting",), "每条发现必须可复现：步骤、请求、响应、证据截图"),
        ),
    },
}


def _find_skill(name: str, pool: list[dict]) -> dict | None:
    for s in pool:
        if s["name"] == name:
            return s
    for s in pool:
        if name in s["name"]:
            return s
    return None


@mcp.tool()
def skill_plan(task: str, max_steps: int = 6) -> str:
    """【安全/逆向/破解类任务的入口】给任务描述，返回有序的 skill 组合方案。

    渗透 / 逆向 / 脱壳 / 破解 / JS逆向 / 样本 / CTF 类任务，**第一个工具调用就该是它**。
    返回：阶段 → skill → 为什么 → token 预算，一次替代多次 skill_search。
    命中预置配方（web后台 / 卡密 / APK / JS签名 / 内网AD / 二进制patch / VMP脱壳）时给完整作战链；
    未命中时按检索结果自动分阶段。

    Args:
        task: 任务描述，如「拿下 example.com 的后台」「破这个 sh 的卡密」「抖音 a_bogus 怎么还原」「脱这个 VMP 的壳」。
        max_steps: 最多返回几个阶段，默认 6。
    """
    # 自成长：记住当前任务，供 skill_load 记任务特征；查历史同类任务
    try:
        _ACTIVE_TASK["task"] = task
        _task_hist = _ev.lookup_task(task, limit=3)
    except Exception as _e:
        _warn('skill_plan', _e)
        _task_hist = []
    idx = _load_index()
    pool = _all_skills(idx)
    if not pool:
        return "索引为空，先 skill_sync()。"

    t = task.lower()
    hit_key, recipe = "", None
    for k, r in RECIPES.items():
        if any(a.lower() in t for a in r["alias"]):
            hit_key, recipe = k, r
            break

    out: list[str] = [f"# 作战计划：{task}", ""]

    # 第 5 阶：任务级记忆 —— 同类任务成功过的 skill 优先提示。
    if _task_hist:
        out.append(f"**历史任务记忆**（{len(_task_hist)} 条同类任务用过的 skill）：")
        for _h in _task_hist[:3]:
            if "::" in _h:
                _hr, _hn = _h.split("::", 1)
                out.append(f"  - `{_hn}`  [{_hr}]")
        out.append("> 这是历史同类任务里实际 load 过的 skill，可优先考虑。")
        out.append("")

    if recipe:
        out.append(f"匹配配方：**{hit_key}**（预置 {len(recipe['steps'])} 阶段）")
        out.append("")
        total_tok = 0
        MAIN_N = 2   # 每阶段只把前 2 个算进预算，其余作备选
        for i, (stage, names, why) in enumerate(recipe["steps"][:max_steps], 1):
            found = [(n, _find_skill(n, pool)) for n in names]
            real = [(n, s) for n, s in found if s]
            # 自成长：同阶段内按历史 score 降序（同分保持原序）
            try:
                real = sorted(real, key=lambda x: -_ev.boost(x[1]["repo"], x[1]["name"]))
            except Exception as _e:
                _warn('skill_plan', _e)
                pass
            # size//4 对中文低估（UTF-8 中文 3 字节/字、约 1.5 字/token → 约 2 字节/token），
            # 用 //3 更接近实际；纯英文 skill 会略高估，宁可高估不可低估（低估会爆预算）。
            stage_tok = sum(max(1, (s.get("size") or 0) // 3) for _, s in real[:MAIN_N])
            total_tok += stage_tok
            out.append(f"## {i}. {stage}　主选 ~{stage_tok:,} tokens")
            out.append(f"**为什么**：{why}")
            for j, (n, s) in enumerate(real):
                mark = "主选" if j < MAIN_N else "备选"
                tok = max(1, (s.get("size") or 0) // 3)
                out.append(f"  - [{mark} ~{tok:,}t] `{s['name']}`  [{s['repo']}]·{s.get('domain','')}"
                           f" ｜{_suggest_mode(s)}")
            missing = [n for n, s in found if not s]
            if missing:
                out.append(f"  - ⚠️ 索引中缺：{missing}（可 skill_sync 补仓库）")
            if len(real) > MAIN_N:
                out.append(f"  → 主选两个先跑，**备选按需再 load**（全加载会爆预算）")
            out.append(f"  → 阶段结束 `skill_unload()` 释放 {stage_tok:,} tokens")
            out.append("")
        out.append(f"**按主选计约 {total_tok:,} tokens**（预算 {MAX_SKILL_TOKENS:,}）")
        out.append("")
        out.append("执行纪律：每阶段只 load 主选，做完立即 unload；备选确认需要才拉。"
                   "出现新端口/新框架/连续失败 2 次时先调 skill_hint 再继续。")
        return "\n".join(out)

    # 未命中预置配方：按检索结果自动分阶段
    out.append("未命中预置配方，按检索结果动态编排：")
    out.append("")
    seen: set[str] = set()
    for term in re.split(r"[\s,，]+", task.strip()):
        if not term:
            continue
        r = skill_search(term, limit=3)
        for line in r.splitlines():
            if line.startswith(("1.", "2.", "3.")) and "**" in line:
                nm = line.split("**")[1]
                if nm not in seen:
                    seen.add(nm)
                    out.append("  " + line)
        if len(seen) >= max_steps * 2:
            break
    if not seen:
        out.append("  （无命中，建议换更短的关键词或补仓库）")
    out.append("")
    out.append("建议：先 load 排名第 1 的 skill（mode=lite 看目录），确认相关再 full。")
    out.append("若任务属于 Web 后台 / 卡密 / APK / JS 签名 / 内网 AD / 二进制 patch，"
               "换用对应关键词可命中预置配方，直接拿到完整作战链。")
    return "\n".join(out)


def main() -> None:
    """stdio 启动入口。处理 Windows 编码与父进程退出时的裸堆栈。"""
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr, sys.stdin):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except Exception as _e:
                _warn('main', _e)
                pass

    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, lambda *_a: sys.exit(0))
        except (ValueError, OSError, AttributeError):
            pass

    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    except (BrokenPipeError, EOFError):
        pass


if __name__ == "__main__":
    main()
