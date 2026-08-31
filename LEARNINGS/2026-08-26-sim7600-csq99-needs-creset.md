---
title: SIM7600 射频卡死时 CSQ=99 与「天线掉了」完全同形，只有 AT+CRESET 能救
date: 2026-08-26
author: William Lou / Claude Code
status: confirmed
tags: [sim7600, modem, rf, false-diagnosis, at-commands]
stack: [SIMCom SIM7600G, LE20B05SIM7600M21-A_250919, macOS]
---

# SIM7600 射频卡死：CSQ=99 与天线断开无法区分，`AT+CRESET` 才能恢复
# SIM7600 stuck RF state: CSQ=99 is indistinguishable from a disconnected antenna; only `AT+CRESET` recovers it

## 现象 / Symptom

换 SIM 卡之后，模组能被识别、SIM 能读出来，但**完全没有信号**：

```
AT+CPIN?   ->  +CPIN: READY              ← SIM 正常
AT+CIMI    ->  310280xxxxxxxxx           ← IMSI 读得出（已脱敏）
AT+CSQ     ->  +CSQ: 99,99               ← 99 = 测不到（连采 6 次全是 99）
AT+CPSI?   ->  +CPSI: NO SERVICE,Online
AT+CFUN?   ->  +CFUN: 1                  ← 射频是开着的
AT+CEREG?  ->  +CEREG: 0,4               ← 4 = unknown
```

**关键点：这组读数与「天线接头掉了」的表现一模一样。** 同一块 dongle 四天前在同一位置
稳定在 CSQ 16~20，中间只做了换卡——所以「换卡时碰掉了 u.FL」是个极其合理、
而且**错误**的判断。

After a SIM swap the module enumerated fine and the SIM read fine, but there was no
signal at all. The reading set is identical to what a detached u.FL antenna produces —
which made "the antenna came loose during the swap" a very plausible and *wrong* call.

## 原因 / Root Cause

**不是硬件，是射频状态卡死。** 该模组在此之前经历过多次 `AT+CFUN=1,1` 软复位、
SIM 热插拔与 USB 重枚举；射频子系统停在一个「已上电但搜不到网」的状态里，
`CFUN` 查询照常返回 1，所以从 AT 层面看不出任何异常。

`CSQ` 测的是射频前端的实测结果，**与 SIM 是否激活无关**——所以「卡没激活」
也解释不了 99。这一点在排查时很容易混淆：当时正好在等运营商激活，
很容易把两件独立的事当成一件。

Not hardware — a stuck RF state. The module had been through several `AT+CFUN=1,1`
resets, SIM hot-swaps and USB re-enumerations. The RF subsystem parked in a
powered-but-blind state while `CFUN?` still reported 1, so nothing at the AT layer
looked wrong. Note `CSQ` is independent of SIM activation, so a pending activation
does *not* explain 99 — easy to conflate when you happen to be waiting on the carrier.

## 解法 / Fix

```bash
# CFUN=1,1（软复位）无效——试过，回来还是 99。
# 必须整机复位：
AT+CRESET
# 等约 45s 完整重启 + USB 重枚举，桥会自愈重建 PTY，再等 ~15s 让它搜网
```

复位后立刻恢复正常：

```
AT+CSQ    ->  +CSQ: 28,99                          ← 28/31，比故障前还好
AT+CPSI?  ->  +CPSI: LTE,Online,310-410,...,EUTRAN-BAND2,...
AT+CEREG? ->  +CEREG: 0,1                          ← 1 = 已注册
+CMTI: "SM",0..4                                    ← 运营商短信随即涌入
```

`CFUN=1,1` 只重启协议栈，射频子系统的这个卡死状态它清不掉；`CRESET` 是整机复位，
把射频一起带回初始态。

`CFUN=1,1` only restarts the protocol stack and does not clear this state;
`CRESET` is a full module reset that takes the RF subsystem down with it.

## 适用范围 / Scope

- **会遇到**：SIM7600 系列，尤其在多次软复位 / 频繁热插拔 SIM / USB 反复重枚举之后。
  上游 [tianye1999/callpilot#121](https://github.com/tianye1999/callpilot/issues/121)
  （AadYang 提）报告了同类现象：「CFUN / 应用笔记 =0 均无效，目前仅 CRESET 能恢复」，
  只是那条描述的是 USB Audio 端点劣化——**同一个复位层级差异，两种表现**。
- **不会遇到**：EC20/EG25（Quectel）路径未观察到此现象。
- **别误判**：如果 `CSQ` 一直是 99 **且 `CRESET` 之后仍是 99**，那才该怀疑天线/线材。
  先软后硬，顺序别反——反过来会拆一块本来没坏的板子。

## 排查顺序建议 / Suggested triage order

1. `AT+CPIN?` — SIM 在不在（`SIM not inserted` 是另一回事，重插卡）
2. `AT+CSQ` — 99 就继续往下；有读数说明射频通路是好的
3. `AT+CFUN?` — 确认射频没被关掉（应为 1）
4. **`AT+CRESET`** — 等 45s，再测 `CSQ`
5. 仍是 99 → 这时才查天线 u.FL（MAIN 焊盘）与线材

## References

- 上游 issue：[tianye1999/callpilot#121](https://github.com/tianye1999/callpilot/issues/121) — SIM7600 跨通 USB Audio 端点劣化，同样只有 CRESET 有效
- 桥的自愈逻辑：`scripts/sim7600_usb_pty.py`（CRESET 引发的 USB 重枚举会被等待-重连循环接住，PTY 自动重建）
- 本次实测记录：2026-08-26，AT&T SIM（MCC 310 / MNC 280），SIM7600G 固件 `LE20B05SIM7600M21-A_250919`
