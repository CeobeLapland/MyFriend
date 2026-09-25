# 07 Audio and Avatar

## 1. Audio Pipeline

```text
Microphone
  ↓
VAD
  ↓
STT Adapter
  ↓
Transcript
  ↓
AI Core
  ↓
Text chunks
  ↓
TTS Adapter
  ↓
Audio chunks
  ↓
Audio Player
  ↓
Lip Sync / Avatar
```

## 2. STT Adapter

首选后端：FunASR / SenseVoice。

Adapter API：

```python
class STTProvider(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int,
        language_hint: str | None = None,
    ) -> Transcript:
        ...
```

`Transcript`：

```json
{
  "text": "你好呀",
  "language": "zh",
  "segments": [],
  "confidence": 0.97,
  "emotion_hint": "neutral"
}
```

注意：STT 的 emotion hint 只能作为辅助信号，不得直接覆写 AI 状态。

## 3. TTS Adapter

首选：现有 CosyVoice。

第二后端：Qwen3-TTS。

接口：

```python
class TTSProvider(Protocol):
    async def synthesize_stream(
        self,
        text: str,
        voice_id: str,
        style: str | None,
        speed: float = 1.0,
    ) -> AsyncIterator[bytes]:
        ...
```

## 4. 语音策略

### 不等待完整回答

LLM streaming 时，需要按“可播语句块”切分：

```text
第一句结束标点 -> TTS
第二句生成中...
```

目标是让 TTS 尽早开始。

### 可打断

用户开口后：

```text
stop TTS
cancel pending TTS chunks
cancel or mark LLM generation as interrupted
begin new turn
```

## 5. Unity Client

**M5 实际使用的版本**（见 [ADR 0024](decisions/0024-m5-avatar-runtime.md)）：

- **Unity 2022.3 LTS**（实测本机为 `2022.3.48f1c1`；原文写的 Unity 6 LTS 与
  实际环境冲突，本机没装、工程要迁移，2022.3 已经够用）。
- **UniVRM 0.131.0**（`com.vrmc.vrm` + `com.vrmc.gltf`，`displayName: VRM-1.0`）。
- **VRM 1.0**（vrmlib 的 `XYZ_RightUpForward_RH`，模型朝 **+Z**）。

Unity 端不要关心：

- Memory DB。
- LLM prompt。
- Tool permission。

Unity 只处理：

- Avatar loading。
- Expression。
- Animation。
- Lip Sync。
- Look-at。
- Camera。
- UI。

## 6. Avatar Action Model

Action 是有限集合，不允许 LLM 任意生成 Unity Animator 参数名称。

示例：

```text
idle
wave
nod
shake_head
think
laugh
shy
stretch
yawn
look_away
look_at_user
```

## 7. Expression Model

```text
neutral
happy
sad
angry
surprised
embarrassed
sleepy
excited
```

通过 intensity `0..1` 控制。

## 8. Layered Animation

动作应分层：

```text
Base Idle
    + Facial Expression
    + Upper Body Gesture
    + Head / Eye Look
    + Lip Sync
```

不要通过一堆互相抢 Animator Layer 的动画 clip 实现全部逻辑。

## 9. Lip Sync

MVP 可以先做 phoneme/volume based。后续再做更精细的 viseme 驱动。

输入：

```text
TTS audio amplitude / phoneme timing
```

输出：

```text
VRM mouth expression weights
```

## 10. Idle System

Idle 行为必须独立于 LLM。

状态机：

```text
Idle
├─ breathe
├─ blink
├─ eye_shift
├─ body_sway
├─ look_away
├─ yawn
└─ stretch
```

LLM 不需要为每次眨眼调用一次模型。

## 11. Avatar Event 示例

### Expression

```json
{
  "type": "avatar.expression.set",
  "version": 1,
  "payload": {
    "turn_id": "01J...",
    "emotion": "happy",
    "intensity": 0.72
  }
}
```

> **没有 `duration_ms`**：实现里没有这个字段（它曾经写在这份文档里）。
> 过渡时长由客户端自己定——M5 的 Unity 端用 0.25 s 插值（ADR 0024）。
> `emotion` 是**归一化后**的值，必定在 `/v1/config` 的 `emotions` 词表内。

### Action

```json
{
  "type": "avatar.action.play",
  "version": 1,
  "payload": {
    "name": "wave",
    "intensity": 0.55,
    "priority": "normal"
  }
}
```
