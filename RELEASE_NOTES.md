# 发布说明 · Release Notes

> **批次 / Batch**：2026-08-02 → 2026-08-05
> **基线 / Base**：`c7b4546`（upstream/main，2026-07-23）
> **范围 / Scope**：30 commits（16 项实质改动 + merge）· 43 files · +5004 / −1022
> **测试 / Tests**：1288 passed · ruff · mypy · mypy --platform win32
>
> 本批次全部改动均有对应 Linear issue 与 GitHub `Refs`，并经 **Codex 独立代码评审**；
> 评审意见与逐条回应记录在各 Linear issue 上。
>
> Every change below maps to a Linear issue and a GitHub `Refs`, and passed
> **independent Codex review**; findings and responses are recorded on each issue.

---

## 一句话 / In one line

**修好了「AI 按了键但 IVR 没反应」这条主线，堵了一个高危安全洞，并把几处「静默失效」变成可见。**

**Fixed the core "AI pressed a key but the IVR never moved" defect, closed a
high-severity security hole, and turned several silent failures into visible ones.**

---

## 变更清单 / Change list

### 🔒 安全 / Security

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-45 | #41 | 本机网关 CSP 钉死配置的 LiveKit host，PWA 增加 exact-host 白名单。此前 `connect-src` 放行**全部** `wss:`，配合 fragment 入口，真域名上一条恶意链接即可把已配对手机的媒体连到攻击者服务器 | Gateway CSP now pins the configured LiveKit host; the PWA gained an exact-host allowlist. Previously `connect-src` allowed **all** `wss:`, so one malicious link could point a paired phone's media at an attacker's server |
| WIL-84 | #98 | 远程网关补**脱敏**访问日志。此前它不记录任何请求——一次成功的配对+拨号+双向通话在日志里没有任何痕迹 | Redacted access log for the remote gateway. It previously logged **nothing** — a successful pairing + dial + two-way call left no trace at all |

### 📞 IVR 按键导航 / IVR keypad navigation

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-49 | #45 | **按键护窗**：DTMF 期间丢弃 AI 下行，让双音独占上行。根因是双音与语音共用同一条上行队列，混叠后对端两者都识别不出 | **Keypress media guard**: AI downlink is dropped during DTMF so the tone owns the uplink. Root cause was the tone sharing a queue with speech — the peer could decode neither |
| WIL-72④ | #50 | 新增 `dtmf_outcome` 证据事件，把每次按键与对端下一句配对。本机 `result:success` 已被证实是**假阳** | New `dtmf_outcome` evidence event pairing each press with the peer's next utterance. Local `result:success` is a proven **false positive** |
| WIL-81 | #45 | 护窗内丢弃的语音落 `agent_audio_dropped` 事件，解释转写与录音的差异 | `agent_audio_dropped` event explains why the transcript and recording differ |
| WIL-82 | #74 | 真机对照定档：一级菜单上 inband 4/4、qvts 5/5 推进，无可测差异，**维持 `inband`** | Real-hardware A/B: inband 4/4 vs qvts 5/5 on the first-level menu — no measurable difference, **`inband` retained** |
| WIL-78 | #72 | 判官 single-flight 改按实例隔离，并发通话不再互相顶成伪超时（污染按键统计） | Per-instance judge single-flight; concurrent calls no longer fake-timeout each other and corrupt keypress stats |

### ☎️ 来电分诊 / Inbound triage

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-73 | #67 | 判官文本后端跟随 `AGENT_PROVIDER`，不再硬编码 openai——非 OpenAI 部署下判官此前拿不到任何裁决 | Judge text backend follows `AGENT_PROVIDER` instead of hardcoding OpenAI; non-OpenAI deployments previously got no verdict at all |
| WIL-80 | #76 | 分诊放行真正生效。此前 `continue_ai` 裁决**形同虚设**，且 provider 侧原本就没有中途改提示词的能力（本次补上） | `continue_ai` verdicts now take effect. Previously they did **nothing**, and no provider had a mid-call instruction-update capability at all (added here) |

### 🧾 预设与结果 / Presets & results

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-71 | #70 | 开场白上限由 40 字符改为按**显示宽度**计（上限 100），修复内置示例本身超限导致无法保存 | Opening-line cap now measured by **display width** (100), fixing shipped seed data that violated its own 40-char limit |
| WIL-77 | #75 | 预设号码匹配容忍国家码，`+8613…` 与裸号不再是两条互不命中的预设 | Preset lookup tolerates the country code; `+8613…` and the bare number are no longer separate profiles |
| WIL-74 | #68 | 结果校验要求证据与本次任务相关，营销短信不得冒充「已核实」的话费结果 | Result verification requires task relevance; marketing SMS can no longer pose as a verified bill result |

### 📱 App / PWA

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-64 | #60 | ①新配对码不再被旧 cookie 吞掉（此前用户扫了新码却**完全没有出路**）②本机媒体错误与 Edge 可达性分开呈现，不再把「没有麦克风」显示成「电脑端不可用」 | ① A fresh pairing code is no longer swallowed by a stale cookie (users previously had **no way forward**) ② Local media errors are separated from Edge reachability — "no microphone" no longer reads as "Edge unavailable" |

### ⚙️ 性能 · Provider · SIM

| Linear | Refs | 中文 | English |
| ------ | ---- | ---- | ------- |
| WIL-76 | #73 | 通话产物按 mtime 缓存 + `callId` 直接索引。此前每次请求全量重读所有通话目录，随历史增长退化为超时 | Content-sync mtime cache + `callId` index. Previously every request re-read every call directory, degrading to timeouts as history grew |
| WIL-75 | #69 | 豆包接上会话提示词；未实现的能力（function calling / 转写）**大声告警**而非静默失效 | Doubao now honours session instructions; unimplemented capabilities (function calling / transcripts) **warn loudly** instead of failing silently |
| WIL-48 | #44 | `CARRIER_HOTLINE` 人工覆盖。识别不到运营商时误拨保护此前**整个失效** | `CARRIER_HOTLINE` override. Misdial protection was previously **completely inert** when carrier detection failed |

---

## ⚠️ 升级须知 / Upgrade notes

### 1. 网关 CSP 现在 fail-closed

`LIVEKIT_URL` 配错或留空 → `connect-src` 只剩 `'self'`，**远程拨号会连不上**。

这是**刻意设计**：宁可给一个可见的故障，也不要静默放行任意 WSS 端点。

> If `LIVEKIT_URL` is wrong or empty, `connect-src` collapses to `'self'` and
> **remote dialing will fail**. This is deliberate — a visible failure beats
> silently permitting any WSS endpoint.

### 2. 新增配置 / New configuration

| Key | 默认 | 说明 / Notes |
| --- | ---- | ------------ |
| `DTMF_GUARD_MS` | `400` | 按键前后静音护窗（毫秒）；`0` = 关闭 / Keypress guard window (ms); `0` disables |
| `CARRIER_HOTLINE` | *(空)* | 留空 = 按 SIM 自动识别。**改后需重启** / Empty = auto-detect from SIM. **Requires restart** |

### 3. `events.jsonl` 新增事件 / New events

下游消费者可能需要适配 / Downstream consumers may need updating:

- `agent_audio_dropped` — 护窗内丢弃的 AI 语音 / AI speech dropped inside the guard window
- `dtmf_outcome` — 按键 ↔ 对端下一句的配对证据 / press ↔ peer's next utterance

### 4. 默认值未改 / No defaults changed

`DTMF_MODE` 仍为 `inband`，但现在有真机对照数据支撑，理由写进了 `config.py` 与 `.env.example`。

> `DTMF_MODE` remains `inband`, now backed by real-hardware A/B data with the
> rationale recorded in `config.py` and `.env.example`.

---

## ✅ 验证状态 / Verification status

**如实标注——并非全部跑过真机。**
**Stated honestly — not everything was verified on hardware.**

| 状态 / Status | Issues |
| ------------- | ------ |
| ✅ **真机验证** / Real-hardware verified | **WIL-49**（IVR 菜单实际推进）· **WIL-80**（真实来电触发放行）· **WIL-82**（10 通对照）· **WIL-45**（配对 + 双向通话）· **WIL-64 #98.3**（陈旧 cookie + 新配对码） |
| ⚙️ **单测 + 代码评审** / Unit tests + review | WIL-71 · WIL-73 · WIL-74 · WIL-76 · WIL-77 · WIL-78 · WIL-81 · WIL-84 · WIL-48 · WIL-72④ |
| ⚠️ **部分验证** / Partial | **WIL-64 #98.4** — 仅「权限拒绝」一例真机验证，且该分支**本就正确**；新增的三条映射在 iOS 上无法构造<br>**WIL-75** — 无豆包凭证，未做真机 |

### 关键真机证据 / Key real-hardware evidence

```
WIL-49  2026-08-03 14:22  拨 10086 / dialing 10086
  14:22:19.905  send_dtmf "1" → success
  14:22:28.790  [IVR] 正在为您转回广东10086,请稍候      ← 菜单实际推进 / menu advanced
  对照当日上午：同为 inband，4 次 success、菜单一次没动
  Compare that morning: same mode, 4× success, menu never moved

WIL-80  2026-08-04 17:13  真实来电 / real inbound call
  会话提示词已中途更新      ← ws.send() 成功后才打 / logged only after ws.send() succeeds
  分诊放行: 已解除限制话术
  7 对，0 失败；对照组（裁 transfer 的那通）0 次
  7 pairs, 0 failures; control call (transfer verdict) had 0
```

---

## 📌 遗留项 / Known follow-ups

已立案，未在本批次内 / Filed, not in this batch:

| Linear | 中文 | English |
| ------ | ---- | ------- |
| **WIL-83** | enforce 受限话术**不被模型遵从**——分诊定论前 AI 仍会承诺「会转告」。修法可能需把约束从提示词移到编排层 | The enforce-mode restricted prompt **is not obeyed**; the AI still promises to pass a message before triage decides. The fix likely moves the constraint from the prompt into the orchestration layer |
| **WIL-72③** | IVR Controller 的决策确定性化（Media Gate 与 Outcome 已具备，缺中间三段） | Deterministic IVR decision-making (Media Gate and Outcome now exist; the middle three stages remain) |
| **WIL-53** | 远程通话偶发零帧，未复现；插桩与取证协议已就位 | Intermittent zero-frame remote call, not reproduced; instrumentation and a capture protocol are in place |
| **WIL-85 / WIL-86** | 两份设计规格待评审：让 AI 听起来像人；机主上下文知识库 | Two design specs awaiting review: sounding human; owner context knowledge base |

---

## 🧭 复盘：本批次学到的三件事 / Three lessons from this batch

1. **绿测试 ≠ 正确。** 有两次改动的单测全绿，而功能实际是死的——一次是标志位没人读，一次是挂起点落在默认关闭的分支里，且测试用假对象把洞盖住了。
   **Green tests ≠ correct.** Twice, a change had fully green tests over a dead feature — once a flag nobody read, once a hook behind a default-off branch, with the test's own fake object hiding it.

2. **本机「成功」信号可以是假阳。** DTMF 的 `result:success` 与 IVR 是否推进完全脱节；判据必须取自对端。
   **Local "success" can be a false positive.** DTMF `result:success` is decoupled from whether the IVR advanced; the criterion must come from the peer.

3. **关键词表判读不可靠。** ASR 会输出繁体，简体关键词表直接漏判——这既印证了项目的非枚举硬原则，也是本批次多处改用「记证据、不下判断」的原因。
   **Keyword tables don't hold up.** The ASR emits Traditional Chinese and a Simplified keyword list silently misjudged it — vindicating the project's non-enumeration rule and the reason several changes record evidence rather than verdicts.
