# skill-router

<div align="center">

### 💬 交流群：QQ 606140489

装不上 · 用不对 · 想投稿经验条目，直接进来问

[![QQ群 606140489](https://img.shields.io/badge/QQ%E7%BE%A4-606140489-12B7F5?style=for-the-badge)](#交流)

</div>

[![check](https://github.com/jarui555/skill-router/actions/workflows/ci.yml/badge.svg)](https://github.com/jarui555/skill-router/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

按需调取 GitHub 上的 agent skills —— 零全量下载，命中才注入。

MCP server，15 个工具，纯 stdio，无外部依赖。不训练、不微调、不联网检索、不用向量库。

```
649 个 skill / 14 个源      按症状查，不按目录翻（可 skill_sync 继续扩）
864 条实战经验              症状 → 动作 → 证据出处，带置信度
检索 1.5 ms                 纯本地词表 + 现象本体，无 embedding
```

## 它解决什么

模型的瓶颈不是想不明白，是**不知道有这个东西**。

GitHub 上有大量高质量 skill（trailofbits、Claude-Red、CTF 战队自建库），但它们散在十几个仓库里，
模型不知道它们存在，也不会主动去查。这个 server 把它们做成索引，并且**在模型卡住时强制插到它面前**。

## 安装

```bash
git clone https://github.com/jarui555/skill-router.git
cd skill-router
pip install "mcp>=1.19"
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
experience.json    864 条经验
repos.json         仓库清单
index.json         索引（含 sha pin，可直接用）
cache/             正文缓存，首次 load 时自动生成
logs/error.log     被 except 吞掉的异常
```

## 环境变量

| 变量 | 作用 |
| :--- | :--- |
| `SKILL_ROUTER_HOME` | 数据目录，默认本文件所在目录 |
| `GITHUB_TOKEN` / `GH_TOKEN` | 可选，只影响 enrich 速度 |

## 交流

QQ 群 **606140489** —— 装不上、用不对、想投稿经验条目，都来这里。

提问前先跑一遍 `python check.py`，把输出贴上来，比描述半天现象有用。

| 想干什么 | 怎么做 |
| :--- | :--- |
| 装完跑不起来 | 贴 `check.py` 输出 + Python 版本 + 系统 |
| 模型不会自己查 skill | 看上面「自动触发」那节，`rules/AGENTS.md` 贴了吗 |
| 投稿一条经验 | 照 `experience.json` 现有格式加，必须带 `evidence.source` |
| 某个 skill 内容有问题 | 开 issue，注明仓库名 + skill 名 |

## 边界

- 索引里 14 个源都是第三方仓库，各自有 license，商用前自己看
- 经验库带置信度，`<0.85` 会标「未验证」，版本不同坑也不同
- 预算硬上限 40000 tokens，一次 `full` 加载超过 2 个 skill 会顶到闸门
