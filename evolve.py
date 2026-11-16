#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evolve.py v2 — skill-router 自成长模块

v1：使用统计 + 评分 + 症状库
v2 新增（四项）：
  ① 失败聚类    _add_signal 用质心合并，同义报错归一类，不再堆 500 条单记录
  ② 跨 skill 迁移 migrate()  A 失败 → 同 domain 的其他 skill（即使没被用过）
  ③ 任务特征记忆 record_task / lookup_task  「什么任务 → 什么 skill」，不只「什么症状」
  ④ TF-IDF 余弦  替代裸 Jaccard，纯标准库零依赖，效果接近 embedding

对外接口：
  record_use(repo, name, tokens)
  record_success(repo, name, weight=0.5)
  record_fail(repo, name, signal="", to=None)
  record_feedback(repo, name, verdict, note="")
  record_task(task, repo, name, verdict)
  lookup_task(task, limit=3) -> list[str]
  lookup_signal(signal) -> list[str]
  migrate(repo, name, candidates) -> list[tuple[str,str,float]]
  boost(repo, name) -> float
  score(repo, name) -> float
  stats_text(limit, sort) -> str
  reset(repo="", name="")
  flush_all()
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from pathlib import Path

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


USAGE_FILE = HOME / "usage.json"

_lock = threading.RLock()
_data: dict | None = None
_dirty = 0
_last_flush = 0.0
_last_curate = 0.0

FLUSH_EVERY = 10
FLUSH_SECS = 30.0
CURATE_SECS = 86400.0

CLUSTER_MERGE = 0.68     # >= 此值并入已有 cluster
HIT_MIN = 0.45           # 症状匹配阈值
HIT_MIN_TASK = 0.32      # 任务匹配阈值（任务描述更长、词更散，阈值放宽）
MAX_SIGNALS = 300
MAX_TASKS = 300


# ────────────────────────── 存取 ──────────────────────────

def _load() -> dict:
    global _data
    if _data is not None:
        return _data
    try:
        if USAGE_FILE.exists():
            _data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if not isinstance(_data, dict):
                raise ValueError("bad root")
        else:
            _data = {}
    except Exception as _e:
        _warn('_load', _e)
        _data = {}
    _data.setdefault("version", 2)
    _data.setdefault("skills", {})
    _data.setdefault("signals", [])
    _data.setdefault("tasks", [])
    return _data


def _flush(force: bool = False) -> None:
    global _dirty, _last_flush
    if not force and _dirty < FLUSH_EVERY and (time.time() - _last_flush) < FLUSH_SECS:
        return
    if _dirty == 0:
        return
    try:
        _data["updated_at"] = int(time.time())
        tmp = USAGE_FILE.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(_data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, USAGE_FILE)
        _dirty = 0
        _last_flush = time.time()
    except Exception as _e:
        _warn('_flush', _e)
        pass


def key_of(repo: str, name: str) -> str:
    return f"{repo}::{name}"


def _rec(repo: str, name: str) -> dict:
    d = _load()
    k = key_of(repo, name)
    r = d["skills"].get(k)
    if r is None:
        r = {"uses": 0, "success": 0.0, "fail": 0, "tokens": 0,
             "last_used": 0, "last_success": 0, "curated": False, "deprecated": False}
        d["skills"][k] = r
    return r


# ────────────────────────── 分词与相似度 ──────────────────────────

_WORD = re.compile(r"[a-z0-9]+")          # 不下划线连接：ptrace_peekdata → ptrace + peekdata
_HEX = re.compile(r"0x[0-9a-f]{2,}")
_NUM = re.compile(r"\b\d{3,}\b")
_ABBR = re.compile(r"\b[A-Z]{2,6}\b")     # 只认纯大写缩写（SEGV/ACL/RCE），不认 PTRACE_PEEKDATA
_CJK = re.compile(r"[\u4e00-\u9fff]{2,}")

STOP = {"error", "fail", "the", "and", "with", "for", "from", "not",
        "cannot", "can", "this", "that", "when", "while", "does"}

# 轻量词干：只处理最常见的几个后缀，避免误伤（process/address 这类不动）
_SUFFIX = (
    ("ations", "ate"), ("ation", "ate"), ("ures", "ure"), ("ure", "ure"),
    ("ings", ""), ("ing", ""), ("edly", ""), ("ed", ""), ("ies", "y"),
    ("es", ""), ("s", ""),
)


def _stem(w: str) -> str:
    if len(w) < 5:
        return w
    for suf, rep in _SUFFIX:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + rep
    return w


def normalize_signal(s: str) -> set[str]:
    """报错文本 → 可比对 token 集。中英混排都吃。"""
    s = (s or "").strip()
    low = s.lower()
    toks: set[str] = set()
    toks |= {_stem(t) for t in _WORD.findall(low)}
    toks |= {h.lower() for h in _HEX.findall(s)}
    toks |= set(_NUM.findall(s))
    toks |= {a.lower() for a in _ABBR.findall(s)}
    for seg in _CJK.findall(s):
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return {t for t in toks if t not in STOP and len(t) > 1}


def _idf(corpus: list[set[str]]) -> dict[str, float]:
    n = max(1, len(corpus))
    df: dict[str, int] = {}
    for toks in corpus:
        for t in toks:
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}


def _cos(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    common = a & b
    if not common:
        return 0.0
    num = sum(idf.get(t, 1.0) ** 2 for t in common)
    da = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in a))
    db = math.sqrt(sum(idf.get(t, 1.0) ** 2 for t in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def _overlap(a: set[str], b: set[str]) -> float:
    """重叠系数：|A∩B| / min(|A|,|B|)。

    对「短查询 vs 长描述」比余弦友好 —— 余弦会因长文档 token 多而严重稀释。
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _sim_mix(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """余弦 + 重叠系数各半。兼顾词频权重与长度差异。"""
    return 0.5 * _cos(a, b, idf) + 0.5 * _overlap(a, b)


# ────────────────────────── 评分 ──────────────────────────

def score(repo: str, name: str, now: int | None = None) -> float:
    try:
        r = _load()["skills"].get(key_of(repo, name))
        if not r:
            return 0.5
        now = now or int(time.time())
        uses = int(r.get("uses", 0))
        succ = float(r.get("success", 0))
        fail = float(r.get("fail", 0))
        # fail 封顶 uses*3：没用过就没有失败，用过 N 次也不可能失败 3N 次以上。
        # 超出部分必然来自「推荐了但没被选」的自动推断噪声（实测 fail 虚高到 25）。
        # 在这里封顶而不是清理数据 —— 一次性清理治不完，测试跑一次就重新污染。
        if uses <= 0:
            fail = 0.0
        else:
            fail = min(fail, uses * 3.0)
        # 拉普拉斯平滑：(正面 + 0.5) / (正面 + 半权重负面 + 1.0)
        # - 无记录 → 0.5/1.0 = 0.5（中性，不影响排序）
        # - 全成功 → 0.95+ ｜ 全失败 → 0.08-
        # fail 用半权重：自动推断的 fail 有噪声（推荐了没选 ≠ skill 差），
        # skill_fit 判定的才是硬失败，但两者混在一起，折中处理。
        base = (succ + 0.5) / (succ + 0.5 * fail + 1.0)
        last = r.get("last_success") or r.get("last_used") or now
        days = max(0.0, (now - last) / 86400.0)
        decay = 0.5 ** (days / 90.0)
        q = 0.0
        if r.get("curated"):
            q += 0.15
        if r.get("deprecated"):
            q -= 0.30
        return max(0.0, min(1.0, base * decay + q))
    except Exception as _e:
        _warn('score', _e)
        return 0.5


def boost(repo: str, name: str) -> float:
    return score(repo, name)


# ────────────────────────── 采集 ──────────────────────────

def record_use(repo: str, name: str, tokens: int = 0) -> None:
    global _dirty
    try:
        with _lock:
            r = _rec(repo, name)
            r["uses"] = r.get("uses", 0) + 1
            r["last_used"] = int(time.time())
            r["tokens"] = r.get("tokens", 0) + max(0, int(tokens))
            # load 本身就是正面信号 —— 模型主动选择了它，不是「尝试」。
            # 不记的话，用的次数越多分数越低（实测 load 4 次的 skill 只有 0.16）。
            r["success"] = round(float(r.get("success", 0)) + 0.15, 2)
            _dirty += 1
            _flush()
    except Exception as _e:
        _warn('record_use', _e)
        pass


def record_success(repo: str, name: str, weight: float = 0.5) -> None:
    global _dirty
    try:
        with _lock:
            r = _rec(repo, name)
            r["success"] = round(float(r.get("success", 0)) + weight, 2)
            r["last_success"] = int(time.time())
            _dirty += 1
            _flush()
    except Exception as _e:
        _warn('record_success', _e)
        pass


def record_fail(repo: str, name: str, signal: str = "", to: list[str] | None = None,
                weight: float = 1.0) -> None:
    """记一次失败。weight 用于「部分失败」—— 推荐列表里 Top2 没被选只是排名靠后，
    不等于推荐错了，所以记半权重；Top3 之后不记。

    为什么需要：一次 hint 推 3 个候选，模型只会 load 1 个。全部记满额 fail 会让
    Top2/Top3 被冤枉打死（实测 offensive-linux-privesc 从未被 load 却 fail=25，
    score 掉到 0.11，从此再也不出现在候选里）。
    """
    global _dirty
    try:
        with _lock:
            if repo and name:
                r = _rec(repo, name)
                r["fail"] = round(r.get("fail", 0) + max(0.0, weight), 2)
                _dirty += 1
            if signal:
                _add_signal(signal, key_of(repo, name) if repo else "", to or [])
                _dirty += 1
            _flush()
    except Exception as _e:
        _warn('record_fail', _e)
        pass


def record_feedback(repo: str, name: str, verdict: str, note: str = "") -> None:
    try:
        with _lock:
            r = _rec(repo, name)
            if verdict == "ok":
                r["success"] = round(float(r.get("success", 0)) + 1.0, 2)
                r["last_success"] = int(time.time())
            elif verdict == "bad":
                r["fail"] = r.get("fail", 0) + 1
            if note:
                r["note"] = note[:200]
            _flush(force=True)
    except Exception as _e:
        _warn('record_feedback', _e)
        pass


# ────────────────────────── ① 失败聚类 ──────────────────────────

def _add_signal(signal: str, from_key: str, to: list[str]) -> None:
    """把报错并入最相近的 cluster；没有够近的就新建。"""
    d = _load()
    sigs = d["signals"]
    toks = normalize_signal(signal)

    if sigs:
        corpus = [set(s.get("tokens") or normalize_signal(s.get("centroid", ""))) for s in sigs]
        idf = _idf(corpus + [toks])
        best_i, best_v = -1, 0.0
        for i, ct in enumerate(corpus):
            v = _sim_mix(toks, ct, idf)
            if v > best_v:
                best_i, best_v = i, v
        if best_i >= 0 and best_v >= CLUSTER_MERGE:
            s = sigs[best_i]
            s["hits"] = s.get("hits", 1) + 1
            s["ts"] = int(time.time())
            vs = s.setdefault("variants", [])
            if signal[:200] not in vs and len(vs) < 8:
                vs.append(signal[:200])
            if to and to != s.get("to"):
                s["resolved"] = False
                s["to"] = to
            for k in to:
                if k not in (s.get("to_all") or []):
                    s.setdefault("to_all", []).append(k)
            return

    sigs.append({
        "centroid": signal[:200],
        "variants": [],
        "tokens": sorted(toks)[:40],
        "from": from_key,
        "to": list(to),
        "to_all": list(to),
        "resolved": True,
        "hits": 1,
        "ts": int(time.time()),
    })
    if len(sigs) > MAX_SIGNALS:
        sigs.sort(key=lambda s: (s.get("hits", 1), s.get("ts", 0)))
        del sigs[:len(sigs) - MAX_SIGNALS]


def lookup_signal(signal: str, limit: int = 5) -> list[str]:
    try:
        d = _load()
        sigs = d["signals"]
        if not sigs:
            return []
        toks = normalize_signal(signal)
        corpus = [set(s.get("tokens") or normalize_signal(s.get("centroid", ""))) for s in sigs]
        idf = _idf(corpus + [toks])
        scored: list[tuple[float, str]] = []
        for s, ct in zip(sigs, corpus):
            v = _sim_mix(toks, ct, idf)
            if v < HIT_MIN:
                continue
            w = v * (1.0 + 0.15 * min(s.get("hits", 1) - 1, 5))
            for k in (s.get("to_all") or s.get("to") or []):
                scored.append((w, k))
        scored.sort(reverse=True)
        seen, out = set(), []
        for _, k in scored:
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out[:limit]
    except Exception as _e:
        _warn('lookup_signal', _e)
        return []


# ────────────────────────── ② 跨 skill 迁移 ──────────────────────────

def pitfalls(repo: str, name: str, limit: int = 3) -> list[dict]:
    """该 skill 的历史失败模式 + 当时的替代方案。

    用于把「坑」前置到 skill_plan / skill_load 的返回里 ——
    不等模型踩了才问，而是在它动手前就告诉它。
    """
    try:
        d = _load()
        k = key_of(repo, name)
        out = []
        for s in d["signals"]:
            if s.get("from") != k:
                continue
            alts = [x for x in (s.get("to_all") or []) if x != k]
            out.append({
                "signal": s.get("centroid", "")[:80],
                "hits": s.get("hits", 1),
                "alts": alts[:2],
                "resolved": s.get("resolved", True),
            })
        out.sort(key=lambda x: -x["hits"])
        return out[:limit]
    except Exception as _e:
        _warn('pitfalls', _e)
        return []


def migrate(repo: str, name: str, candidates: list[tuple[str, str]],
            limit: int = 3) -> list[tuple[str, str, float]]:
    """A 失败 → 同 domain 的其他 skill。

    candidates: [(repo, name), ...] 由调用方按 domain 过滤后传入。
    """
    try:
        out = []
        for r, n in candidates:
            if r == repo and n == name:
                continue
            out.append((r, n, score(r, n)))
        out.sort(key=lambda x: -x[2])
        return out[:limit]
    except Exception as _e:
        _warn('migrate', _e)
        return []


# ────────────────────────── ③ 任务特征记忆 ──────────────────────────

def record_task(task: str, repo: str, name: str, verdict: str) -> None:
    global _dirty
    try:
        with _lock:
            d = _load()
            toks = normalize_signal(task)
            if not toks:
                return
            tasks = d["tasks"]
            corpus = [set(t.get("tokens") or []) for t in tasks]
            idf = _idf(corpus + [toks]) if corpus else {}
            for t in tasks:
                if t.get("skill") != key_of(repo, name):
                    continue
                if _overlap(toks, set(t.get("tokens") or [])) >= 0.75:
                    t["verdict"] = verdict
                    t["ts"] = int(time.time())
                    t["hits"] = t.get("hits", 1) + 1
                    _dirty += 1
                    _flush()
                    return
            tasks.append({
                "task": task[:200],
                "tokens": sorted(toks)[:40],
                "skill": key_of(repo, name),
                "verdict": verdict,
                "hits": 1,
                "ts": int(time.time()),
            })
            if len(tasks) > MAX_TASKS:
                tasks.sort(key=lambda t: (t.get("hits", 1), t.get("ts", 0)))
                del tasks[:len(tasks) - MAX_TASKS]
            _dirty += 1
            _flush()
    except Exception as _e:
        _warn('record_task', _e)
        pass


def lookup_task(task: str, limit: int = 3) -> list[str]:
    try:
        d = _load()
        tasks = d["tasks"]
        if not tasks:
            return []
        toks = normalize_signal(task)
        corpus = [set(t.get("tokens") or []) for t in tasks]
        idf = _idf(corpus + [toks])
        scored: list[tuple[float, str]] = []
        for t, ct in zip(tasks, corpus):
            v = _overlap(toks, ct)
            if v < HIT_MIN_TASK:
                continue
            bonus = 1.3 if t.get("verdict") == "ok" else 0.6
            scored.append((v * bonus * (1.0 + 0.1 * min(t.get("hits", 1) - 1, 5)),
                           t.get("skill", "")))
        scored.sort(reverse=True)
        seen, out = set(), []
        for _, k in scored:
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out[:limit]
    except Exception as _e:
        _warn('lookup_task', _e)
        return []


# ────────────────────────── 自动升降级 ──────────────────────────

def autocurate() -> None:
    global _last_curate, _dirty
    now = time.time()
    if now - _last_curate < CURATE_SECS:
        return
    _last_curate = now
    try:
        with _lock:
            d = _load()
            for k, r in d["skills"].items():
                if "::" not in k:
                    continue
                repo, name = k.split("::", 1)
                s = score(repo, name, now=int(now))
                uses = r.get("uses", 0)
                if uses >= 10 and s >= 0.75 and not r.get("deprecated"):
                    r["curated"] = True
                elif uses >= 5 and s <= 0.25 and not r.get("curated"):
                    r["deprecated"] = True
                elif r.get("deprecated") and s >= 0.5:
                    r["deprecated"] = False
            _dirty += 1
            _flush(force=True)
    except Exception as _e:
        _warn('autocurate', _e)
        pass


# ────────────────────────── 输出 ──────────────────────────

def stats_text(limit: int = 20, sort: str = "score") -> str:
    try:
        d = _load()
        now = int(time.time())
        rows = []
        for k, r in d["skills"].items():
            repo, _, name = k.partition("::")
            rows.append({
                "repo": repo, "name": name,
                "score": score(repo, name, now),
                "uses": r.get("uses", 0),
                "ok": r.get("success", 0),
                "fail": r.get("fail", 0),
                "tokens": r.get("tokens", 0),
                "curated": r.get("curated", False),
                "deprecated": r.get("deprecated", False),
            })
        if sort == "uses":
            rows.sort(key=lambda x: -x["uses"])
        elif sort == "tokens":
            rows.sort(key=lambda x: -x["tokens"])
        else:
            rows.sort(key=lambda x: -x["score"])

        out = [f"# skill 使用统计（共 {len(rows)} 个有记录）", ""]
        if rows:
            out.append(f"{'#':>3} {'score':>6} {'use':>4} {'ok':>5} {'fail':>5} {'tokens':>8}  skill")
            for i, r in enumerate(rows[:limit], 1):
                flag = "★" if r["curated"] else ("✗" if r["deprecated"] else " ")
                out.append(f"{i:>3} {r['score']:>6.2f} {r['uses']:>4} {r['ok']:>5.1f} "
                           f"{r['fail']:>5} {r['tokens']:>8}  {flag} {r['name']}  [{r['repo']}]")
        else:
            out.append("（暂无记录。跑几次任务后回来看。）")

        sigs = sorted(d["signals"], key=lambda s: -s.get("hits", 1))[:10]
        if sigs:
            out.append("")
            out.append(f"# 症状库（{len(d['signals'])} 个 cluster，同义报错已合并）")
            for s in sigs:
                tgt = ", ".join((s.get("to_all") or s.get("to") or [])[:2]) or "（未解决）"
                mark = "✓" if s.get("resolved") else "✗"
                var = len(s.get("variants") or [])
                extra = f" (+{var} 变体)" if var else ""
                out.append(f"  {mark} {s.get('hits',1):>2} 次  {s.get('centroid','')[:56]}{extra}  →  {tgt}")

        tasks = sorted(d["tasks"], key=lambda t: -t.get("hits", 1))[:8]
        if tasks:
            out.append("")
            out.append(f"# 任务记忆（{len(d['tasks'])} 条，任务 → skill）")
            for t in tasks:
                mark = "✓" if t.get("verdict") == "ok" else "✗"
                out.append(f"  {mark} {t.get('hits',1):>2} 次  {t.get('task','')[:48]}  →  {t.get('skill','')}")

        return "\n".join(out)
    except Exception as e:
        return f"stats 失败：{e}"


def reset(repo: str = "", name: str = "") -> str:
    global _dirty
    try:
        with _lock:
            d = _load()
            if repo and name:
                d["skills"].pop(key_of(repo, name), None)
                _dirty += 1
                _flush(force=True)
                return f"已清除 {repo}::{name}"
            d["skills"].clear()
            d["signals"].clear()
            d["tasks"].clear()
            _dirty += 1
            _flush(force=True)
            return "已清空全部统计（skills / signals / tasks）"
    except Exception as e:
        return f"reset 失败：{e}"


def flush_all() -> None:
    try:
        _flush(force=True)
    except Exception as _e:
        _warn('flush_all', _e)
        pass
