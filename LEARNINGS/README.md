# LEARNINGS

> 踩过的坑 / 发现的 trick / 硬件与第三方 API 的隐藏行为。
> 凡是「下次遇到同样情况 30 秒能查到」比「重新 debug 半小时」划算的，都写在这里。
>
> Pitfalls, tricks, and undocumented behaviour of hardware and third-party APIs.
> If looking it up in 30 seconds beats re-debugging it for half an hour, it belongs here.

**本目录刻意与 `jumpoAi/team-brain` 的 `LEARNINGS/` 保持同构**（命名、frontmatter、
章节结构一致），将来若整体迁入 team-brain，是一次 `git mv` 的事，不需要重写。

This directory deliberately mirrors the schema of `jumpoAi/team-brain`'s `LEARNINGS/`,
so migrating later is a `git mv` rather than a rewrite.

## 命名 / Naming

```
YYYY-MM-DD-<short-topic>.md
```

日期前缀让 `ls` 天然按时间排序，`git grep` 找内容、文件名找时间线。
The date prefix gives you chronology from `ls` and content from `git grep`.

## 模板 / Template

见 [`_template.md`](./_template.md)。

## 什么时候写 / When to write one

**写 / Do write:**

- 一个报错你查了 >15 分钟才解决 / an error that took more than 15 minutes to resolve
- 某个 API 或 AT 指令的行为和文档不一致 / behaviour that contradicts the documentation
- 某个配置组合会导致 **silent failure** / a combination that fails silently
- 硬件症状与软件症状**长得一样**的情况（最值钱的一类）
  hardware and software faults that present identically — the highest-value kind

**不写 / Don't:**

- 架构层面的决定 → 写 [`docs/decisions/`](../docs/decisions/) 的 ADR
- 每次会话都成立的通用约定 → 写 [`CLAUDE.md`](../CLAUDE.md)
- 一次性的、不会再遇到的偶发问题

## 红线 / Red line

**这是公开仓库（Apache-2.0）。** 不写真实号码、IMEI/ICCID、API key、账户信息、
客户个人信息——写进去等于公开发布。需要记录这类内容时，放本地 `CLAUDE.local.md`
或私有的 team-brain。

**This is a public repo.** No real phone numbers, IMEI/ICCID, API keys, account
details or personal data — committing them here publishes them.
