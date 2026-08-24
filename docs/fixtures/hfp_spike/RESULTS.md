# Phase 0 真机 spike 结论（2026-08-24）

硬件：Pixel 7（AT&T SIM）+ Windows 11 26200 + MediaTek MT79xx 蓝牙适配器。
手机零改动、未装任何 app。原始产物见同目录 JSON/WAV；号码一律打码（尾 2 位 91）。

## 计分板

| 项 | 结果 | 证据 |
|---|---|---|
| 0.1 音频端点 | **PASS** | `audio_test.json`：上行 peak 26836 / -22.7 dBFS、9.54s 无溢出、DTMF 注入无报错；实测协商 **16kHz mSBC** |
| 0.2a WinRT PhoneLine | **PASS** | `winrt_check.json` / `winrt_lines.json`：非打包 Python 拿到 PhoneCallStore，默认线路 Pixel 7（bluetooth, can_dial=True） |
| 0.2b 裸 RFCOMM | **FAIL（结论性）** | `rfcomm_connect.json`：WSAEADDRINUSE(10048)——Windows 自带栈独占 HFP，同 radio 不允许第二条 HF 连接 |
| 0.2c adb | 未做 | 0.2a 通过后不需要 |

## 接听/挂断/来电号码真机流转（2026-08-24 13:28，终端实录）

来电方为 Google Voice（发起方听到静音——符合预期：音频已路由到 PC，
探针未向下行喂声）：

```
[  9.64s] incoming  number='+1 510-***-**91' dir=INCOMING
  → accept_incoming() 已调用
[ 10.84s] talking
  → end() 已调用（--end-after 8）
[ 20.48s] ended
```

## 模组契约覆盖结论

| modem.py 契约 | 实现 | 状态 |
|---|---|---|
| `on_ring(caller)` | 轮询 `get_all_active_phone_calls` + `PhoneCallInfo.phone_number` | ✅ 实证 |
| `answer()` | `PhoneCall.accept_incoming()` | ✅ 实证 |
| `hangup()` | `PhoneCall.end()` | ✅ 实证 |
| `list_calls()`/CLCC | `PhoneLine.get_all_active_phone_calls()` | ✅ 实证 |
| `dial()` | `PhoneLine.dial*`（can_dial=True） | API 在，Phase 1 拨 611 验收 |
| `send_dtmf()` | `PhoneCall.send_dtmf_key()`（带外） | API 在，Phase 1 验收 |
| 通话音频 8kHz PCM | Windows 原生 HFP 端点（WDM-KS，回调式） | ✅ 实证（16k mSBC，需重采样） |
| `hold_toggle()` | `hold()` / `resume_from_hold()` | API 在 |

## 对 Phase 1 设计的三个直接影响

1. **不需要 AT over TCP 桥**。控制面直调 winsdk，照 `AndroidSmsGatewayModem`
   先例 duck-type 模组契约（`CallAgentService` 本就支持 `modem=` 注入）；
   原计划的 `HfpModem(SerialModem)` + `serial_for_url` 改动作废。
2. **音频桥需支持设备原生采样率**：端点是 16kHz mSBC，`ModemAudioBridge`
   写死的 `MODEM_RATE=8000` 要改为按端点采样率开流、重采样对接 Agent。
3. **来电号码走 `PhoneCallInfo.phone_number`**，不依赖 adb（且 adb 路线的
   脱敏风险不复存在）。

## 运维前提（写进用户文档）

- 蓝牙适配器需在设备管理器关掉「允许计算机关闭此设备以节约电源」
  （MediaTek 适配器不关会掉链）。
- 手机蓝牙设置里该 PC 的「通话」开关必须开启。
- 空闲时手机会断开与 PC 的链路——属正常；来电/拨号时链路自动重建
  （WinRT 层面线路一直可见）。
- PC 占用 HFP 期间，手机连不了车载/耳机（HFP 单实例）。
