# ADR 0009 — External Voice and Avatar Constraints

## Decision

记录两条来自实际环境、会影响 M4/M5 设计的外部约束。

### 1. CosyVoice 后端不支持流式

TTS/STT 使用第三方整合包「CosyVoice3 雨落版」（B站 UP主 雨落实战）。
它暴露的是一个 **Gradio 应用**（`http://127.0.0.1:7860`，28 个端点，
通过 `gradio_client` 调用），不是 REST API。

关键端点：

| 端点 | 用途 | 返回 |
|---|---|---|
| `/rainfall_gen_zero_shot` | TTS + 音色克隆（≤30s 参考音频） | `.wav` 文件路径 |
| `/rainfall_gen_sft` | TTS + 预训练音色 | `.wav` 文件路径 |
| `/rainfall_gen_instruct` | TTS + 指令控制语气/方言 | `.wav` 文件路径 |
| `/prompt_wav_recognition` | STT（SenseVoiceSmall） | 文本 |

**接口是文件级的，不是流式的**：TTS 返回一个生成完毕的 `.wav` 路径，
STT 需要传入完整音频文件。

### 2. Unity 侧可以复用旧工程壳，只复用 VRM 模型资源

不复用 `E:\UnityProject\VirtualTuber` 里的任何脚本（那是一年前的实验品，
与本项目架构无关）。只用 `Celandine.vrm` 及其表情资源。

## Reason

**关于流式**：`07_AUDIO_AND_AVATAR.md` §3 要求
`TTSProvider.synthesize_stream -> AsyncIterator[bytes]`，目标是"首包即播"。
该后端做不到，接口设计必须按**真实能力**来，不能假装能流式。

**关于 Unity**：旧工程用的是 UDP 每帧原始动捕数据（骨骼四元数 + blendshape），
与文档定义的语义事件协议（`avatar.expression.set` / `avatar.action.play`）
是两套东西。复用脚本会把旧架构的假设带进新项目。

## Consequences

- `TTSProvider` 端口按**非流式**设计：`synthesize(request) -> AudioArtifact`。
  可选提供 `supports_streaming` 能力标记，而不是强制实现 `AsyncIterator`。
- 折中方案是**按句切分**：LLM 流式输出时每凑够一个完整句子就立刻发给 TTS，
  第一句话的音频能提前开始播，体感接近"边说边播"。
- `voice.style` 字段可以映射到 `/rainfall_gen_instruct` 的 `instruct_text`。
- M4 必须重新评估显存预算：LM Studio 的 LLM 与 CosyVoice 的 torch 会争抢
  RTX 4060 Laptop 的 8GB。候选方案：改用更小的 LLM
  （`nvidia/nemotron-3-nano-4b`）、LLM 部分 offload、或 TTS 走 CPU。
- M4 之前 CosyVoice 保持关闭，避免与 M0–M3 的开发环境争内存。
