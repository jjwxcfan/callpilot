# Phase 0 真机 spike：安卓手机能否替换 SIM7600G

目的：在写任何实现代码之前，用真机把**唯一的关键未知数**测掉——
PC 侧谁来当蓝牙 HFP 的免提端。

背景与完整方案见计划文件（`用安卓手机替换 SIM7600G 模组`）。一句话架构：

```
安卓手机（SIM 在此，不装任何 app）
   │ 蓝牙 HFP：AT 控制通道 + SCO 8kHz 音频
   ▼
PC 侧免提端  ──AT──►  HfpModem(SerialModem) ──► call_agent.py 不动
   └────────8kHz PCM──►  ModemAudioBridge(uac) 不动
```

手机侧零改动的依据：蓝牙 HFP 的控制面**本身就是一条 AT 命令通道**，
安卓原生 HFP AG 已支持 `ATA` / `ATD` / `AT+CHUP` / `AT+CLCC` / `+CLIP` /
`AT+VTS` / `AT+CHLD` / `AT+COPS?`——正好覆盖 `modem.py` 需要的全部指令。

## 为什么先在 Windows 上测

Windows 与 macOS 的能力正好**镜像互补**：

| | 音频（SCO） | 控制（AT 通道） |
|---|---|---|
| Windows | ✅ 大概率原生可用（本机注册表已出现过成对的 `Hands-Free HF Audio` 端点） | ❌ 自带栈不把 AT 交给应用；Python 3.12 也开不了裸 RFCOMM（实测 `AF_BTH: False`） |
| macOS | ❌ 不实现 HFP-HF，Mac 不会对安卓机伪装成耳机 | ✅ `IOBluetooth` 开放 RFCOMM 给应用，PyObjC 可用 |

**SCO 音频是我们唯一写不出来的那一半**（两个 OS 都不开放 SCO 给应用），
所以先验 Windows 的音频。控制那一半在任一 OS 都能自己写，还有 adb 兜底。

## 前置准备（都要人工做）

1. **打开 Windows 蓝牙**——设置 → 蓝牙和其他设备 → 蓝牙开关打开。
   > 当前实测：适配器（MediaTek）在位且驱动正常，但**蓝牙是关着的**——
   > `BluetoothFindFirstRadio` 返回 `ERROR_NO_MORE_ITEMS(259)`。
2. **SIM 卡插进安卓手机**，确认能正常打电话。
3. **手机与这台 PC 配对**。配对后在 设置 → 设备 → 该手机 → 属性 → 服务，
   确认「免提电话 / Hands-free Telephony」已勾选。
4. （仅 0.2c 需要）手机开发者选项里打开 **USB 调试**，USB 连到 PC，
   并在手机上勾选「始终允许此计算机调试」。

> **硬约束**：真机外呼只拨 **611**（本机 AT&T 卡的免费客服号）。
> `probe_adb.py dial` 内置护栏，拨别的号会被拒。

## 执行顺序

所有命令用项目 venv 的 Python：`D:/Callpilot/.venv/Scripts/python.exe`

### 0.1 音频端点（最高优先级）

```bash
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_audio.py list
```

配对后先跑一次。**注意**：多数 Windows 只在通话建立（SCO 起来）后端点才可用，
所以这一步没看到候选是正常的，继续下一步。

然后跑测试命令——**先开脚本，再去手机上拨 611**。脚本会轮询等端点就绪
（默认等 120s），不用在通话中抢时间敲命令：

```bash
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_audio.py test --wait 120 --seconds 12 --dtmf 1
```

看到「等待 HFP 端点就绪」后拿起手机拨 611；接通瞬间脚本会自己开始，
录 12 秒上行、第 3 秒往下行注入一个 DTMF `1`。

> 轮询里每次都会强制 PortAudio 重新枚举设备（`sd._terminate()` / `sd._initialize()`）
> ——sounddevice 在进程内缓存设备表，不重来就永远看不见 SCO 起来后新出现的端点。

**判据**
- 上行 `peak > 500` → 录到了对端声音，上行通；
- 听 `docs/fixtures/hfp_spike/uplink_capture.wav` 确认是 611 的语音；
- 若 IVR 在注入后切了菜单 → **下行也通**（且证明 DTMF 能穿透 AMR 编码）。
- 记下报告里的 `samplerate`：`8000` 表示 CVSD 窄带，`audio_bridge.MODEM_RATE`
  写死的 8000 可直接用；`16000` 表示 mSBC 宽带，Phase 1 需要给 bridge 加重采样。

### 0.2 控制通道（三条并行，任一条通过即可）

**a. WinRT PhoneLine**（最干净，但可能已废弃）

```bash
D:/Callpilot/.venv/Scripts/pip.exe install winsdk
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_winrt.py check
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_winrt.py lines
```

**b. 裸 RFCOMM**（能成的话组合最理想）

```bash
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_rfcomm.py devices
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_rfcomm.py connect --watch 60
```

`connect` 会连手机的 HFP AG 并跑 SLC 握手（`AT+BRSF` → `AT+CIND` →
`AT+CMER` → `AT+CLIP=1`），`--watch 60` 期间从别的手机拨这张卡，
看能不能收到 `RING` + `+CLIP:` 带号码的主动上报。

> 核心问题：Windows 自带栈已经占着 HFP 连接（音频端点就是它建的），
> 安卓允不允许**第二条** HF 连接。被拒会报 `WSAECONNREFUSED(10061)`。
> 那种情况下试 `--headset`（HSP AG）当退路。

**c. adb**（兜底，与 OS 无关）

```bash
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py check
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py sim
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py watch --seconds 90
```

`watch` 期间从别的手机拨这张卡。**重点看响铃时 `incoming_number` 有没有值**
——新版 Android 常把它脱敏，这正是 a/b 相对 c 的主要价值。

拨号/接听/挂断（拨号有 611 护栏）：

```bash
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py dial
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py answer
D:/Callpilot/.venv/Scripts/python.exe scripts/hfp_spike/probe_adb.py hangup
```

`sim` 子命令还顺带验证了 PLMN → 运营商 → 免费客服号的解析链
（复用 `sim_identity` 的表，不另抄），这是 Phase 1 `identify_from_plmn()` 的原型。
已离线验证：`310410 → ('美国运营商', '611')`、`46000 → ('中国移动', '10086')`、
未知 PLMN → `('未知', '')` fail-closed。

## 真机实录（2026-08-24，Pixel 7 + Win11 26200 + MediaTek MT79xx 适配器）

0.1 **通过**：`audio_test.json` 上行 peak 26836 / -22.7 dBFS、9.54s 无溢出、
DTMF 注入无报错。踩出来的坑按顺序：

1. **PortAudio 阻塞 API 在 WDM-KS 上不可用**——HFP 端点只以 WDM-KS 形态出现，
   必须回调式（已改）。
2. **端点存在 ≠ 能开流**——KS pin 要 SCO 起来才实例化，没在通话时 `start()` 报
   `WdmSyncIoctl GLE=0x1`（已改成试开流重试）。
3. **「连上一下就掉」**两个原因叠加：MediaTek 适配器 USB 省电（设备管理器里
   取消「允许计算机关闭此设备」）+ 手机对 PC 的空闲链路本来就会被收掉——
   解法是**通话中再连**：先拨 611，通话中去蓝牙设置点 PC 名，SCO 立即建立
   并挂住链路。
4. **音频路由要手动选**：通话界面音频列表里选 PC 名（不是「扬声器」！）；
   列表里没有 PC = 蓝牙没连上或「通话」开关没开（手机蓝牙设置里该设备的齿轮）。
5. **配对多台手机时端点会混**（iPhone 抢先匹配），用 `--keyword pixel` 锁定。
6. **实测协商出 16kHz mSBC** 而非 8kHz CVSD → Phase 1 的 `ModemAudioBridge`
   要按设备原生采样率开流再重采样到 8k（`MODEM_RATE` 写死 8000 不能直接用）。
7. Windows 设置里的「移动设备」(Mobile devices/Phone Link) 走 Wi-Fi，与蓝牙
   HFP 无关，连上它没有任何用。

## 决策树

- 0.1 ✅ + 0.2 任一 ✅ → **走 Windows**，进 Phase 1
- 0.1 ✅ + 0.2 只有 c ✅ → Windows 出音频 + adb 出控制；来电号码若被脱敏，
  记为已知缺口（AI 接起时拿不到对端号码）
- 0.1 ❌ → 转 macOS 做 0.3（PyObjC + IOBluetooth 发布 HF 的 SDP record），
  音频退到「USB 声卡模拟耳机」，方案重估

## 产物

所有脚本把原始结果落到 `docs/fixtures/hfp_spike/`：

| 文件 | 来自 |
|---|---|
| `audio_devices.json` / `audio_test.json` / `uplink_capture.wav` | 0.1 |
| `winrt_check.json` / `winrt_lines.json` | 0.2a |
| `bt_devices.json` / `rfcomm_connect.json` | 0.2b |
| `adb_check.json` / `adb_sim.json` / `adb_watch.json` / `adb_sms.json` | 0.2c |

这些是方案决策依据，跑完后连同结论一起提交。
