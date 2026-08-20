"""FakeAgent：脚本化 VoiceAgent，实现 start/send_audio/say/stop。"""

from __future__ import annotations

from typing import Callable

from agentcall.agents.base import VoiceAgent


class FakeAgent(VoiceAgent):
    """say() 时按脚本推一段假 PCM 到 on_audio_out 并产生 agent 转写。"""

    # 假回复 PCM 用清晰可闻的振幅（0x1000=4096）：真实 TTS 语音必然过阈，
    # 全 1 之类的准静音样本会被轮首静音掐除（call_agent._trim_leading_silence）
    # 当垫子吃掉，不代表任何真实场景。
    def __init__(self, reply_pcm: bytes = b"\x00\x10" * 240) -> None:
        self.reply_pcm = reply_pcm
        self.started = False
        self.stopped = False
        self.received_audio: list[bytes] = []
        self.said: list[str] = []
        # 经 say_and_wait（等待音频投递完成）说出的话术，测试断言编排层
        # 用的是等待版而不是 fire-and-forget 的 say。
        self.waited_say: list[str] = []
        self._on_audio_out: Callable[[bytes], None] | None = None

    async def start(self, on_audio_out: Callable[[bytes], None]) -> None:
        self.started = True
        self._on_audio_out = on_audio_out

    async def send_audio(self, pcm: bytes) -> None:
        self.received_audio.append(pcm)

    async def say(self, instructions: str) -> None:
        self.said.append(instructions)
        self._emit_transcript("agent", instructions)
        if self._on_audio_out:
            self._on_audio_out(self.reply_pcm)

    async def say_and_wait(self, instructions: str, timeout: float = 6.0) -> bool:
        """同步版 say：Fake 的音频本来就同步吐完，等待语义天然满足。"""
        self.waited_say.append(instructions)
        await self.say(instructions)
        return True

    async def stop(self) -> None:
        self.stopped = True
