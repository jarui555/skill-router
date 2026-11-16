#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
install.py — skill-router 一键安装。

    python install.py

干三件事：装依赖 → 跑自检 → 把该贴进客户端的配置打印出来（路径已填好）。

为什么用 Python 写而不是 .ps1 / .sh：
  Windows 默认代码页 936，PowerShell 输出 UTF-8 中文会乱；
  bash 在 Git Bash / WSL / macOS 之间行为又不一致。
  Python 的 sys.stdout.reconfigure 一处解决，三个平台同一份代码。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"
CHECK = HERE / "check.py"

# 中文 Windows 控制台默认 cp936，直接 print 会 UnicodeEncodeError / 乱码
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

OK = "  ✓"
NO = "  ✗"


def line(ch: str = "-", n: int = 64) -> None:
    print(ch * n)


def main() -> int:
    print()
    print("skill-router 安装")
    print(f"目录：{HERE}")
    print()

    # ── 1. Python 版本 ────────────────────────────────────────
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        print(f"{NO} Python {v.major}.{v.minor} 太老，要 3.10+")
        print("    https://www.python.org/downloads/")
        return 1
    print(f"{OK} Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")

    if not SERVER.is_file():
        print(f"{NO} 找不到 server.py，你不在 skill-router 目录里？")
        return 1

    # ── 2. 依赖 ──────────────────────────────────────────────
    print("  装 mcp（约 10 秒）...")
    cmd = [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "mcp>=1.19"]
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print(f"{NO} pip 装失败，试 --user：")
        r = subprocess.run(cmd + ["--user"])
        if r.returncode != 0:
            print(f"{NO} 还是不行。手动跑：{sys.executable} -m pip install \"mcp>=1.19\"")
            return 1
    print(f"{OK} mcp 已装")

    # ── 3. 自检 ──────────────────────────────────────────────
    print()
    print("自检：")
    r = subprocess.run([sys.executable, str(CHECK)], cwd=str(HERE))
    if r.returncode != 0:
        print()
        print(f"{NO} 自检没过。把上面整段输出发到极跃Ai交流群 606140489")
        return 1

    # ── 4. 配置 ──────────────────────────────────────────────
    srv = SERVER.as_posix()
    py = sys.executable.replace("\\", "/")

    print()
    line("=")
    print("下一步：把配置贴进你的客户端")
    line("=")

    print()
    print("[ Claude Code ] 在终端跑这一条：")
    print(f'  claude mcp add skill-router -- "{py}" "{srv}"')

    print()
    print("[ Codex CLI ] 追加到 ~/.codex/config.toml：")
    print()
    print("  [mcp_servers.skill-router]")
    print(f'  command = "{py}"')
    print(f'  args = ["{srv}"]')

    print()
    print("[ Cursor / Cline / 其他 JSON 配置 ]：")
    print()
    conf = json.dumps(
        {"mcpServers": {"skill-router": {"command": py, "args": [srv]}}},
        ensure_ascii=False, indent=2,
    )
    for ln in conf.splitlines():
        print("  " + ln)

    print()
    line("-")
    print("最后一步（很多人漏）：把 rules/AGENTS.md 的内容贴进你项目根目录的")
    print("AGENTS.md 或 CLAUDE.md。不贴的话，AI 不会自动去查。")
    line("-")
    print()
    print("装完还是不对？极跃Ai交流群 606140489，把上面的输出贴上来。")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
