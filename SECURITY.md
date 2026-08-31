# Security Policy / 安全策略

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's [Security Advisories](../../security/advisories/new)
("Report a vulnerability" on the Security tab). If that is unavailable to you, open an
issue titled "Security contact request" with no technical detail, and a maintainer will
arrange a private channel.

Please include: what you found, how to reproduce it, the affected version or commit, and
the impact you believe it has. We aim to acknowledge within 5 working days.

Do not test against phone numbers, SIMs, carrier infrastructure, or cloud accounts you do
not own.

**请勿通过公开 issue 报告安全问题。** 请使用 GitHub Security Advisories 私下报告；
若无法使用，可开一个不含技术细节、标题为 "Security contact request" 的 issue，维护者会
安排私下沟通渠道。请附上问题描述、复现步骤、受影响版本/commit 与你判断的影响面。
不要对不属于你的号码、SIM 卡、运营商设施或云账号进行测试。

---

## What is in scope

This project drives a real cellular modem and places real phone calls. The areas where a
defect has the most serious consequences:

- **Misdial protection** — anything that lets an automated call reach a number other than
  the SIM carrier's own free service hotline
- **Credential handling** — API keys, LiveKit secrets, APNs keys, the cloud `ADMIN_TOKEN`,
  device enrollment credentials
- **Cloud control plane** — enrollment, pairing, device authorization, the `/v1` protocol
- **Call content** — recordings, transcripts, SMS bodies, and anything that could expose
  them beyond the local machine
- **Tool-call safety** — the gates around sending SMS, hanging up, and transferring a call
- **DTMF and verification codes** — anything logging or transmitting them in the clear

## What is out of scope

- Anything requiring physical access to an already-unlocked machine
- Carrier network or modem firmware defects (report those to the vendor; we will document
  them in `LEARNINGS/`)
- Missing hardening in code paths explicitly marked as unverified on real hardware

---

## For contributors

Secrets live only in the git-ignored `.env` and local config — never in code, tests,
fixtures, logs, screenshots, or issue text. **This repository is intended to become
public; git history is permanent.** If you believe a secret was committed, say so
immediately rather than quietly force-pushing — the credential must be rotated regardless
of whether the commit is removed, because it may already have been fetched.

Session archives (`agentcall-sessions/`) contain real phone numbers, IMEIs and API keys.
They are git-ignored and must stay that way.
