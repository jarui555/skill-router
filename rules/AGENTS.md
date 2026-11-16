# skill-router 使用规则

收到下列任一领域任务时，**第一个工具调用必须是 `skill_plan`**。
在它返回之前，不要输出任何方法论、步骤、命令或 payload。

渗透 / 打点 / 漏洞利用 / 提权 / 内网
逆向 / 反编译 / IDA / Frida / hook / 反调试 / 反作弊
脱壳 / 加壳 / VMP / Themida / OLLVM / 混淆 / dump 修复
破解 / 卡密 / 授权 / 注册机 / 激活码 / license
JS 逆向 / 签名参数 / 风控 / 验证码 / 小程序 / 协议还原
样本 / 免杀 / EDR 绕过 / 取证
CTF / pwn / crypto / reverse

## 为什么

索引里的 skill 比模型的训练记忆新。凭记忆答 = 用过时方法论。

## 失败计数器

**同一类方法失败第 2 次，立刻 `skill_hint(<报错或现象原文>)`，不要试第 3 种变体。**

「同一类」= 手段相同，只是参数/写法不同：

- 读进程内存：`/proc/mem` → `process_vm_readv` → `ptrace PEEKDATA`
- 抓调用栈：`backtrace()` → `__builtin_return_address(N)` → 手动扫栈
- 注入方式：Frida spawn → Frida attach → LD_PRELOAD
- 猜协议字段：改 code → 改 msg → 改 time → 改 check

## 升级判据

| 路径 | tokens | 什么时候 |
| :--- | ---: | :--- |
| `hint` + `cmd` | 250 | 只要命令 |
| `hint` + `meta` + `lite` | 856 | 判断方向 |
| `+ full` | 3,630 | 要完整方法论 |

`lite` 目录里没有你要的那一节 → 才 full；`cmd` 返回「共 0 条」→ 直接 lite。

## 硬前置

`skill_hint` 返回「⚠️ 未翻译」= 你传的是大白话。必须补上**标准术语 + 端口/报错码/框架名**
后重调一次，不许直接用它的「仅供参考」候选。

## 经验库

- `skill_load` 自动附「本领域已知坑」4 条
- 连续 2 次同类 → `skill_hint` 顶部强制插经验
- 连续 3 次 → 🔴 必须换方法，不是换参数
- 主动查：`skill_exp("报错原文")` / `skill_exp(phase="ir")`
- 置信 `<0.85` 标「未验证」，必须自己验证再用
