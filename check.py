#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check.py — 装完跑一下，确认这个 server 在你机器上是活的。

    python check.py

退出码 0 = 全通，1 = 有问题。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parent
EXPECT_TOOLS = 15
ok, bad = [], []

# Windows 控制台默认 cp1252，直接 print 中文会 UnicodeEncodeError。
# CI 上 windows-latest 就是这么挂的，先兜住再说话。
if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def t(name: str, cond: bool, detail: str = "") -> None:
    (ok if cond else bad).append(name)
    print(f"  {'✓' if cond else '✗'} {name}{'  ' + detail if detail else ''}")


def main() -> int:
    print("skill-router 自检\n")

    print("[1] 依赖")
    try:
        import mcp

        v = getattr(mcp, "__version__", "") or "未知"
        t("mcp 已安装", True, f"版本 {v}")
    except ImportError:
        t("mcp 已安装", False, '跑 pip install "mcp>=1.19"')
        print("\nmcp 没装，后面不用测了。")
        return 1

    print("\n[2] 加载 server")
    try:
        import server
        t("server.py 可导入", True, f"HOME={server.HOME}")
    except Exception as e:
        t("server.py 可导入", False, f"{type(e).__name__}: {e}")
        return 1

    print("\n[3] 工具注册")
    try:
        tools = asyncio.run(server.mcp.list_tools())
        names = sorted(x.name for x in tools)
        t(f"工具数 = {EXPECT_TOOLS}", len(tools) == EXPECT_TOOLS, f"实际 {len(tools)}")
        t("skill_plan 存在", "skill_plan" in names)
        t("skill_hint 存在", "skill_hint" in names)
        t("skill_exp 存在", "skill_exp" in names)
    except Exception as e:
        t("工具注册", False, f"{type(e).__name__}: {e}")

    print("\n[4] 数据")
    idx = HOME / "index.json"
    if idx.is_file():
        d = json.loads(idx.read_text(encoding="utf-8"))
        n = sum(len(v.get("skills", [])) for v in d.get("repos", {}).values())
        t("索引可读", n > 0, f"{len(d.get('repos', {}))} 仓库 / {n} skill")
    else:
        t("索引可读", False, "没有 index.json，先调 skill_sync()")

    exp = HOME / "experience.json"
    if exp.is_file():
        e = json.loads(exp.read_text(encoding="utf-8"))
        t("经验库可读", len(e.get("entries", [])) > 0, f"{len(e.get('entries', []))} 条")
    else:
        t("经验库可读", False, "缺 experience.json")

    print("\n[5] 检索")
    try:
        import experience

        experience.search("warm up")
        st = time.perf_counter()
        hits = experience.search("VMP 脱壳 IAT 修复失败", topk=3)
        ms = (time.perf_counter() - st) * 1000
        t("经验检索命中", len(hits) > 0, f"{len(hits)} 条 / {ms:.2f} ms")
    except Exception as e:
        t("经验检索命中", False, f"{type(e).__name__}: {e}")

    print("\n[6] 索引检索")
    try:
        r = server.skill_plan("脱这个 vmp 的壳")
        t("skill_plan 返回计划", "作战计划" in r or "阶段" in r, r.split("\n")[0][:40])
    except Exception as e:
        t("skill_plan 返回计划", False, f"{type(e).__name__}: {e}")

    print(f"\n{'=' * 50}")
    if bad:
        print(f"✗ {len(bad)} 项失败：{', '.join(bad)}")
        return 1
    print(f"✓ 全部通过 {len(ok)} / {len(ok)}")
    print("\n下一步：把 rules/AGENTS.md 的内容贴到你的项目里，")
    print("否则「卡住自动翻经验库」这层不会生效。")
    print("\n遇到问题：极跃Ai交流群 606140489")
    return 0


if __name__ == "__main__":
    sys.exit(main())
