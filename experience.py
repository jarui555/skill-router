"""实战经验库：检索 + 反馈 + 注入。

设计目标（用户核心需求）：
  **任务执行中，模型遇到难题时能自动查到「这个坑怎么过」，不需要自己想到去查。**

三个关键决策：

1. 为什么独立于 evolve.py 的症状库
   症状库记的是「信号 → 该用哪个 skill」（粗粒度，指向方法论）。
   经验库记的是「症状 → 具体怎么解」（细粒度，指向一个动作）。
   两者数据结构差 10 个字段，混在一起会互相污染。

2. 为什么按 symptom 匹配而不是按 topic 过滤
   模型描述问题用的是「现象」（「所有 IR 全对不上」），不是「领域」
   （「ir-foundations 的 A2 条」）。所以 symptom 必须是一等检索键。

3. 为什么 confidence 要分级且参与排序
   错误经验比没有更糟 —— 会让模型在错误方向上更自信。
   来源可信度：skill-reference 0.85-0.9 > awesome-index 0.85 > 散文抽取 0.7。
   被实战验证过的（success 计数）额外加权。
"""

import io
import json
import os
import re
import time
from pathlib import Path

# 现象本体：口语 → 技术域的映射。和 server.py 共用同一份（phenomena.py）。
# 没有它的时候，用户说「一打开调试器就退出」，条目里写「反调试检测」，
# 两边一个字都对不上，实测召回率只有 21%。
try:
    from phenomena import PHENOMENA as _PHEN
except Exception:
    _PHEN = {}

EXP_FILE = Path(__file__).with_name("experience.json")
_LOCK_LOADED: dict = {"data": None, "mtime": -1.0}
# 检索索引缓存。两个都要缓存，否则每次检索都重算：
#   df  → IDF 权重的分母（O(N) 遍历）
#   toks → 每条条目的分词结果（864 条 × 2 字段 × 每字段 ~200 个 2-gram）
# 实测：都不缓存 102ms / 只缓存 df 46ms / 两个都缓存 ~4ms。
_TOK_CACHE: dict = {"mtime": -1.0, "df": {}, "n": 0, "by_id": {}}


def _load() -> dict:
    """按 mtime 缓存，避免每次检索都读 JSON。"""
    try:
        mtime = EXP_FILE.stat().st_mtime
        if _LOCK_LOADED["data"] is not None and mtime == _LOCK_LOADED["mtime"]:
            return _LOCK_LOADED["data"]
    except Exception:
        mtime = -1.0
    data: dict = {"version": 1, "entries": []}
    if EXP_FILE.is_file():
        try:
            data = json.loads(EXP_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    _LOCK_LOADED["data"] = data
    _LOCK_LOADED["mtime"] = mtime
    return data


def save() -> None:
    d = _load()
    d["updated_at"] = int(time.time())
    tmp = EXP_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, EXP_FILE)
    _LOCK_LOADED["mtime"] = EXP_FILE.stat().st_mtime


def _tokens(text: str) -> set[str]:
    """归一化成检索 token。中英混合：英文按词、中文按 2-gram。"""
    t = (text or "").lower()
    toks: set[str] = set()
    for w in re.findall(r"[a-z0-9_][a-z0-9_\-.]{2,}", t):
        toks.add(w)
    cn = re.findall(r"[\u4e00-\u9fff]+", t)
    for seg in cn:
        for i in range(len(seg) - 1):
            toks.add(seg[i:i + 2])
    return toks


# 症状检索时的停用词（中文高频虚词，避免「一个问题」这种到处都命中）
_STOP = {"一个", "这个", "那个", "什么", "怎么", "就是", "可以", "不是", "没有",
         "但是", "因为", "所以", "如果", "已经", "还是", "然后", "现在", "问题",
         "时候", "发现", "看到", "一直", "都是", "不能", "无法", "不对", "不了"}


def search(query: str, topk: int = 3, phase: str = "",
           min_conf: float = 0.0) -> list[dict]:
    """按「症状」检索经验。返回按相关度 + 可信度排序的条目。

    query 通常是模型描述的现象（「所有 IR 全对不上」「按名字找寄存器漏掉 w23」）。
    """
    entries = _load().get("entries") or []
    if not entries:
        return []
    qt = _tokens(query) - _STOP

    # 口语 → 同类词扩展：命中某一类的任一口语词，就把这一类的全部词也加进来。
    # 「一打开调试器就退出」原本和「反调试检测」零重叠，扩展后共享「调试」「退出」。
    if _PHEN and qt:
        _low = (query or "").lower()
        _extra: set[str] = set()
        for _words in _PHEN.values():
            if any(_w in _low for _w in _words):
                for _w in _words:
                    _extra |= _tokens(_w)
        if _extra:
            qt |= (_extra - _STOP)

    if not qt:
        # 只给 phase 不给 query → 返回该阶段全部（按可信度排序）。
        # 场景：动手前想先扫一遍某层的坑（skill_exp(phase="ir")）。
        if phase:
            cand = [e for e in entries if e.get("phase") == phase
                    and float(e.get("confidence", 0)) >= min_conf]
            cand.sort(key=lambda e: -float(e.get("confidence", 0)))
            return cand[:max(topk, 8)]
        return []

    # ── IDF 加权（解决跨语言检索）──
    # 问题：apkseep 的条目是英文（`Stripping the whole META-INF/ breaks the app`），
    # 用户用中文问（「删了 META-INF 应用起不来」）。中文 2-gram 切出 5-6 个词，
    # 只有 `meta-inf` 一个能对上 → 覆盖率 1/6 = 0.167，被 0.18 阈值滤掉。
    # 但 `meta-inf` 是稀有词（全库几十条里才几条有），命中它比命中「应用」有意义得多。
    # 所以按「稀有度」加权：df 越低的词，命中权重越高。
    import math as _math
    try:
        _mt = EXP_FILE.stat().st_mtime
    except Exception:
        _mt = -1.0
    if _TOK_CACHE["mtime"] != _mt or not _TOK_CACHE["df"]:
        _df: dict[str, int] = {}
        _by_id: dict[str, tuple] = {}
        for e in entries:
            st = _tokens(e.get("symptom", "")) - _STOP
            at = _tokens(" ".join(e.get("action") or [])) - _STOP
            _by_id[e.get("id", "")] = (st, at)
            for t in (st | at):
                _df[t] = _df.get(t, 0) + 1
        _TOK_CACHE.update({"mtime": _mt, "df": _df, "n": len(entries), "by_id": _by_id})
    df = _TOK_CACHE["df"]
    N = max(1, _TOK_CACHE["n"])
    by_id = _TOK_CACHE["by_id"]

    def idf(t: str) -> float:
        return _math.log((N + 1) / (df.get(t, 0) + 1)) + 1.0

    # 查询词的权重和（分母）
    qw = sum(idf(t) for t in qt) or 1.0

    scored = []
    for e in entries:
        if phase and e.get("phase") != phase:
            continue
        conf = float(e.get("confidence", 0.5))
        if conf < min_conf:
            continue
        # 症状是主键，动作是次键（用户可能直接贴报错，而报错文本在 action 里）
        st, at = by_id.get(e.get("id", ""), (frozenset(), frozenset()))
        # IDF 加权覆盖率：命中稀有词得分高，命中「应用/问题」这种虚词得分低
        hit_s = qt & st
        hit_a = qt & at
        cov_s = sum(idf(t) for t in hit_s) / qw
        cov_a = sum(idf(t) for t in hit_a) / qw
        rel = max(cov_s, cov_a * 0.8)   # action 命中打折（不如症状命中直接）
        # 阈值降到 0.12：IDF 加权后 0.12 已经意味着「命中了有信息量的词」
        if rel < 0.12:
            continue
        # 实战验证过的加权：被用过且成功过的经验更可信
        hits = int(e.get("hits", 0))
        succ = int(e.get("success", 0))
        fail = int(e.get("fail", 0))
        proof = 1.0 + (0.15 * succ - 0.25 * fail) if hits else 1.0
        score = rel * conf * max(0.3, proof)
        scored.append((score, rel, e))

    scored.sort(key=lambda x: -x[0])
    return [e for _, _, e in scored[:topk]]


def for_skill(name: str, repo: str = "", topk: int = 4) -> list[dict]:
    """取某个 skill 关联的经验。

    关联方式：skill 名/描述里的技术词命中经验的 topic 或 symptom。
    这样 `skill_load("vmp-reverse-skill")` 会自动带出 VMP 相关的坑。
    """
    entries = _load().get("entries") or []
    if not entries:
        return []
    key = (name or "").lower()
    # 从 skill 名推领域词：vmp-reverse-skill → {vmp, reverse, skill}
    kt = {w for w in re.findall(r"[a-z0-9]{2,}", key)} - _STOP
    if not kt:
        return []
    # 「核心词」= 去掉通用词后的部分。vmp-reverse-skill → {vmp}
    # 只用核心词匹配 topic，否则「reverse」这种通用词会把 Electron / Android
    # 的经验也附到 VMP skill 上（实测踩到：load vmp-reverse-skill 附了
    # Electron 的 JSC 字节码经验）。
    GENERIC = {"skill", "skills", "reverse", "reversing", "analysis", "analyze",
               "tool", "tools", "tooling", "notes", "note", "guide", "checklist",
               "advanced", "basic", "pattern", "patterns"}
    core = kt - GENERIC
    scored = []
    for e in entries:
        topics = set(e.get("topic") or [])
        sym = _tokens(e.get("symptom", ""))
        # 核心词命中 topic 权重最高；只有通用词命中时降权
        core_hit = len(core & topics)
        gen_hit = len((kt - core) & topics)
        sym_hit = len(core & sym) if core else 0
        if core_hit == 0 and gen_hit == 0 and sym_hit == 0:
            continue
        # 核心词没命中 topic 的，只保留 symptom 里出现核心词的（降权）
        if core_hit == 0 and sym_hit == 0:
            continue
        ov = core_hit * 4 + sym_hit * 2 + gen_hit
        conf = float(e.get("confidence", 0.5))
        scored.append((ov * conf, e))
    scored.sort(key=lambda x: -x[0])
    return [e for _, e in scored[:topk]]


def record_hit(exp_id: str, ok: bool | None = None) -> None:
    """记录一次经验被使用。ok=True 表示这条经验确实解决了问题。

    这是经验库的自成长入口 —— 用过的经验排前面，没用的排后面。
    和 skill 的 score 不同，这里不做复杂衰减：经验是「事实」，
    不存在「过时」问题（VMP 1.x 的坑今天还是那个坑）。
    """
    for e in _load().get("entries") or []:
        if e.get("id") == exp_id:
            e["hits"] = int(e.get("hits", 0)) + 1
            if ok is True:
                e["success"] = int(e.get("success", 0)) + 1
            elif ok is False:
                e["fail"] = int(e.get("fail", 0)) + 1
            save()
            return


def stats() -> dict:
    entries = _load().get("entries") or []
    by_phase: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_conf: dict[str, int] = {"高 ≥0.85": 0, "中 0.7-0.85": 0, "低 <0.7": 0}
    for e in entries:
        by_phase[e.get("phase", "?")] = by_phase.get(e.get("phase", "?"), 0) + 1
        by_source[e.get("source", "?")] = by_source.get(e.get("source", "?"), 0) + 1
        c = float(e.get("confidence", 0))
        if c >= 0.85:
            by_conf["高 ≥0.85"] += 1
        elif c >= 0.7:
            by_conf["中 0.7-0.85"] += 1
        else:
            by_conf["低 <0.7"] += 1
    return {"total": len(entries), "by_phase": by_phase,
            "by_source": by_source, "by_conf": by_conf,
            "verified": sum(1 for e in entries if int(e.get("success", 0)) > 0)}


def render(entries: list[dict], max_action: int = 2) -> str:
    """把经验渲染成注入文本。紧凑格式 —— 经验的价值在内容不在排版。"""
    if not entries:
        return ""
    out = []
    for e in entries:
        conf = float(e.get("confidence", 0))
        # 低可信必须显式标注，否则模型会当铁律用
        mark = "" if conf >= 0.85 else f" [置信 {conf:.2f} 未验证]"
        sym = e.get("symptom", "")
        out.append(f"- **{sym[:110]}**{mark}")
        # 有些条目 symptom 与 action 是同一句话（technique-note 从散文抽的），
        # 重复渲染等于浪费一半 token，还会让模型以为是两个信息。
        shown = 0
        for a in (e.get("action") or [])[:max_action]:
            head = a[:30].strip("*` 　")
            if head and (head in sym or sym[:30].strip("*` 　") in a):
                continue
            out.append(f"  → {a[:280]}")
            shown += 1
        ev = e.get("evidence") or {}
        if ev.get("ref"):
            out.append(f"  （出处：{ev.get('source', '')} · {ev['ref']}）")
    return "\n".join(out)


if __name__ == "__main__":
    s = stats()
    print(f"经验库：{s['total']} 条")
    for k, v in s["by_phase"].items():
        print(f"  {k:12s} {v:>3}")
    print(f"  来源: {s['by_source']}")
    print(f"  可信度: {s['by_conf']}")
    print(f"  被验证过: {s['verified']}")
    print()
    print("=== 检索测试 ===")
    for q in ["所有 IR 全对不上", "按名字找寄存器漏掉 w23",
              "Hex-Rays 伪代码看着合理实际是错的", "handler 边界切不准"]:
        r = search(q, topk=2)
        print(f"\n[{q}] 命中 {len(r)} 条")
        print(render(r)[:400])
