# Security Policy

## 这个项目是什么

一个 MCP server，本地 stdio 运行，把 GitHub 上的 agent skills 和一份本地经验库按需注入给模型。

它**不**执行被加载的 skill 里的任何代码 —— skill 是 markdown 提示词，`skill_load` 只做一件事：把文件内容读出来返回给调用方。
`skill_cmd` 从正文里正则抽取命令行，也不执行。

## 风险面

| 面 | 说明 |
| :--- | :--- |
| 上游仓库内容 | 索引里的 skill 来自 14 个第三方仓库，内容未经审计。加载进来的提示词可能包含诱导性指令。 |
| 网络拉取 | `skill_sync` / `skill_load` 会访问 `raw.githubusercontent.com`、`cdn.jsdelivr.net`、`ungh.cc`。缓存写到本地 `cache/`。 |
| 本地文件读取 | `skill_read_ref` 读 skill 附带的 references 目录，路径限制在索引记录内。 |
| 经验库 | `experience.json` 是静态数据，来自公开技术文档与 GitHub issue 原文。 |

## 你该注意的

1. **加载的 skill 是不可信输入。** 第三方仓库的 SKILL.md 里如果写了「忽略之前的指令」，模型可能照做。
   这个 server 不负责给 skill 内容做安全过滤。
2. **别把 `GITHUB_TOKEN` 写进配置文件后提交。** 用环境变量。
3. **`SKILL_ROUTER_HOME` 指向的目录可写。** 别指到系统目录。

## 报告问题

发现以下类型的问题请开 issue（如果是敏感细节，先发邮件）：

- 路径穿越：`skill_read_ref` 或 `skill_load` 能读到索引之外的任意文件
- 命令注入：任何工具把参数拼进 shell
- 索引污染：能让 `skill_sync` 写入非预期仓库
- 缓存投毒：`cache/` 里的内容能被外部构造

普通的功能问题和误报不用走这里，直接开 issue 就行。
