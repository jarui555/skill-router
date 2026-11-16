# skill-router

<div align="center">

### 💬 极跃Ai交流群：606140489

装不上 · 用不对 · 想投稿经验条目，直接进来问

[![极跃Ai交流群 606140489](https://img.shields.io/badge/%E6%9E%81%E8%B7%83Ai%E4%BA%A4%E6%B5%81%E7%BE%A4-606140489-12B7F5?style=for-the-badge)](#交流)

</div>

[![check](https://github.com/jarui555/skill-router/actions/workflows/ci.yml/badge.svg)](https://github.com/jarui555/skill-router/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

## 你的 AI 什么都知道一点，但没真的干过

你让它脱一个 VMP 壳、拿下一个站、破一个卡密 —— 它会从零开始推。

不是它笨，是它不知道**有人三年前就把这个坑填了**：

> 「Hex-Rays 的伪代码在 VMP 上不可信」
> 「trace 里 LOAD/STORE 的 inputs[0] 是随机化标记不是地址，地址在 inputs[1]」
> 「改 SELinux 没用，别浪费时间」

这些不是知识，是**经验**。知识在训练数据里，经验只在别人的日志里。

这个项目就是给 AI 装一个经验包。

```
659 个专业方法论      按症状查，不按目录翻
922 条实战踩坑记录    症状 → 动作 → 证据出处，带置信度
1.5 毫秒              纯本地检索，不用 GPU、不用向量库
```

**不是让 AI 变聪明，是让它别那么健忘。**

---

## 装之前 vs 装之后

| 你让它干什么 | 没有它 | 有它 |
| :--- | :--- | :--- |
| 脱 VMP 壳 | 从零推，试十几种方法 | 第一步就去查，直接看别人卡在哪 |
| 拿下某个站 | 想到哪打到哪 | 先跑「打点决策」，按优先级排，不浪费时间 |
| 破卡密 | 直接写 keygen，写三天 | 先判类型（本地 / 签名 / 硬件 / 云端），判错立刻止损 |
| 挖 SRC | 每个点试三个 payload，全是「疑似」 | 按「暴露面 × 技术栈年龄 × 功能复杂度」排序 |
| **AI 卡住了** | **一直换参数，越试越远** | **连续 2 次同类尝试自动插经验；3 次强制换方法** |

最后一条是这套东西最值钱的地方 —— **不是库大，是它会自己拦**。

---

## 它到底是什么

一个 **MCP 插件**。

MCP 是 AI 客户端（Claude Code / Codex / Cursor / Cline）的扩展协议。
装上去之后 AI 多了 15 个工具可以调用：查方法论、查踩坑记录、规划作战链、检查当前做法是否撞上禁令。

**它不是什么**（先排除误解）：

| 常见误解 | 实际 |
| :--- | :--- |
| 要显卡 / 要跑模型 | 不需要。里面没有任何 AI 模型，就是词表匹配 |
| 要下载几十 GB | 1.6 MB，一个 Python 脚本 + 两个 JSON |
| 要联网才能用 | 装完之后全程离线，只有第一次同步索引需要网络 |
| 要 Docker / 数据库 | 都不要。`pip install mcp` 就够了 |
| 是个能聊天的 AI | 不是。它是插件，得配合 Claude Code / Codex / Cursor 用 |

**它是什么**：一个 Python 脚本 + 两个 JSON 文件，`python install.py` 一条命令装完，
Windows / macOS / Linux 通用，Python 3.10+ 即可。

---

## 适合谁

| 适合 | 不适合 |
| :--- | :--- |
| 用 Claude Code / Codex / Cursor 干活的人 | 只用网页版 ChatGPT 的（网页版装不了插件） |
| 做逆向、渗透、CTF、安全研究 | 完全不碰这些领域的（装了也用不上） |
| 被 AI「反复犯同一个错」烦过 | 想要一个能直接对话的 AI（这是插件不是模型） |
| 想给团队沉淀经验的新人 | 期待开箱即用的成品工具（它给方法论，不给一键脚本） |

## 安装

### 一键（推荐）

```bash
git clone https://github.com/jarui555/skill-router.git
cd skill-router
python install.py
```

就这一条。脚本干三件事：装依赖 → 跑自检 → **把该贴进客户端的配置直接打印出来**（路径已填好），
复制粘贴就能用，不用自己拼路径。

Windows 用 PowerShell 也行，逻辑是同一个文件：

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

### 手动

```bash
pip install "mcp>=1.19"
python check.py
```

Python 3.10+。`mcp` 1.x 和 2.x 都支持，代码里做了双版本适配。

## 接进你的客户端

所有客户端都用同一种配置：**stdio + 一个 python 命令**。

### Claude Code

```bash
claude mcp add skill-router -- python /绝对路径/skill-router/server.py
```

### Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.skill-router]
command = "python"
args = ["/绝对路径/skill-router/server.py"]
```

### Cursor / Cline / 其他 JSON 配置

```json
{
  "mcpServers": {
    "skill-router": {
      "command": "python",
      "args": ["/绝对路径/skill-router/server.py"]
    }
  }
}
```

Windows 把 `python` 换成 `python.exe` 的绝对路径，路径分隔符用 `/` 或转义后的 `\\`。

## 首次使用

装完先自检，确认你机器上跑得起来：

```bash
python check.py
```

全通会打印 `✓ 全部通过 8 / 8`。它会验依赖、工具注册、索引、经验库、检索延迟。

如果带索引发布，clone 完就能用。想自己建索引：

```
skill_sync()                          # 同步 repos.json 里的全部仓库，约 3-5 分钟
skill_sync("owner/repo")              # 只同步一个
```

走 ungh.cc + jsDelivr + raw，**不消耗 GitHub API 配额**，不需要 token。
配了 `GITHUB_TOKEN` 只是让 enrich（补描述）快一些。

## 15 个工具

| 工具 | 用途 |
| :--- | :--- |
| `skill_plan(task)` | 任务 → 阶段链，每步带加载建议与 token 估算 |
| `skill_search(query)` | 关键词/现象检索 |
| `skill_hint(signal)` | 卡住时的重检索，**连续 2 次同类自动插经验** |
| `skill_load(repo, name, mode)` | 加载正文，`lite` 目录视图 / `full` 全文 / `part=N` 分段 |
| `skill_cmd(repo, name)` | 只抽命令 + 行号（250 tokens vs 3630） |
| `skill_meta(name)` | 边界：什么时候用 / 前置 / 禁止条款 |
| `skill_fit(current, observed)` | 自检「正在做的事是否撞上该 skill 的禁令」 |
| `skill_exp(query, phase)` | 查实战经验库 |
| `skill_unload()` | 释放上下文预算 |
| `skill_stats()` | 分数 / 症状库 / 任务记忆 |
| `skill_status()` | 索引与预算状态 |
| `skill_sync(repos)` | 建/刷新索引 |
| `skill_read_ref(...)` | 读 skill 附带的 references |
| `skill_check_update()` | 检查仓库更新 |
| `skill_feedback(...)` | 手动标成功/失败（平时不用，工具会自己推断） |

## 自动触发：三个条件，缺一个就退化成普通查询库

这是整套东西最关键的地方，也是最容易在别人的环境里失效的地方。

| 条件 | 谁负责 | 不满足会怎样 |
| :--- | :--- | :--- |
| ① 客户端把 server 的 `instructions` 注入系统提示 | 客户端 | 模型不知道有 `skill_plan` 这个东西 |
| ② 项目里有 rules / AGENTS.md 声明「第一动作是 skill_plan」 | 你 | 模型知道但不照做 |
| ③ 模型真的调 `skill_hint` | 模型 | 卡住判定永远不触发 |

server 启动时通过 MCP 标准字段 `instructions` 下发了完整规则（第一动作、升级判据、禁止清单）。
**但不同客户端对这个字段的处理不一致** —— 有的注入，有的忽略。

所以第二道保险必须自己加。把 `rules/AGENTS.md` 的内容贴到项目的 `AGENTS.md`
（Codex / 多数 CLI）或 `CLAUDE.md`（Claude Code），或客户端的 rules 目录。

## 目录

```
server.py          MCP 服务本体，15 个工具
experience.py      经验库检索（词表 + IDF + 现象本体，1.5ms）
evolve.py          自成长：按行为推断成功/失败，微调排序
experience.json    922 条经验
repos.json         仓库清单
index.json         索引（16 源 / 659 skill，sha 已 pin）
packs/             随包 skill，clone 就有，不依赖外部仓库
cache/             正文缓存，首次 load 时自动生成
logs/error.log     被 except 吞掉的异常
```

### packs/ 里有什么

| 目录 | 内容 |
| :--- | :--- |
| `packs/license/` | 网络授权 / 卡密 / keygen：机制分类、协议分析、云端替代、定位补丁、算法还原 |
| `packs/web-takeover/` | 渗透拿站：打点决策、WAF 绕过、shell 升级、隧道代理、后渗透 |

这两个目录在 `repos.json` 里对应 `local/license-pack` 和 `local/web-takeover`，
path 是相对路径，以 `SKILL_ROUTER_HOME` 为基准解析 —— 换台机器也能用，不需要改配置。

## 环境变量

| 变量 | 作用 |
| :--- | :--- |
| `SKILL_ROUTER_HOME` | 数据目录，默认本文件所在目录 |
| `GITHUB_TOKEN` / `GH_TOKEN` | 可选，只影响 enrich 速度 |

## 交流

极跃Ai交流群 **606140489** —— 装不上、用不对、想投稿经验条目，都来这里。

提问前先跑一遍 `python check.py`，把输出贴上来，比描述半天现象有用。

| 想干什么 | 怎么做 |
| :--- | :--- |
| 装完跑不起来 | 贴 `check.py` 输出 + Python 版本 + 系统 |
| 模型不会自己查 skill | 看上面「自动触发」那节，`rules/AGENTS.md` 贴了吗 |
| 投稿一条经验 | 照 `experience.json` 现有格式加，必须带 `evidence.source` |
| 某个 skill 内容有问题 | 开 issue，注明仓库名 + skill 名 |

## 边界

- 索引里 16 个源：14 个第三方仓库 + 2 个随包 pack，第三方的内容各自有 license
- 经验库带置信度，`<0.85` 会标「未验证」，版本不同坑也不同
- 预算硬上限 40000 tokens，一次 `full` 加载超过 2 个 skill 会顶到闸门
