# ADR 0032 — 第三方 OpenAI 兼容 provider 与密钥的"只写不读"

## Decision

### 1. 一个 `openai_compatible` 通用 provider 包，三个能力各一个实现

新增 `astra/providers/openai_compatible/`：``llm.py``（``/chat/completions``）、
``stt.py``（``/audio/transcriptions``）、``tts.py``（``/audio/speech``）、
``health.py``（共用探针）。**一个包覆盖所有声称 OpenAI 兼容的服务**，而不是每厂商
一个包——它们共享同一条 HTTP 形状，差异集中在"参数容忍度"上，那是配置而不是代码。

端口（``domain/ports.py``）**一个字节都没改**：这是 ADR 0022 §2 预告的那条路
（"将来换成第三方云 API，只需新写一个 provider 包"）。

### 2. LLM 的两个参数开关：默认**不发**

``reasoning_effort`` 与 ``response_format: json_schema`` 都是"发出去可能被 400 拒掉"
的参数。所以 ``openai_compatible`` 这条路径上：

| 配置 | 默认 | 行为 |
|---|---|---|
| ``ASTRA_LLM_SEND_REASONING_EFFORT`` | ``false`` | 不发 ``reasoning_effort`` |
| ``ASTRA_LLM_USE_JSON_SCHEMA`` | ``false`` | 退化为 ``{"type": "json_object"}`` |

**``lmstudio`` 路径不受这两项影响**，因为本地那套能力是实测出来的已知事实：
``reasoning_effort=none`` 是关掉 qwen3.5-9b 思考的**唯一**开关（15.4 s → 3.4 s），
而 json_schema 实测可用。把它做成"用户可关的开关"只会多一种把本地配置弄坏的方式。

### 3. TTS 默认请求 ``wav``，按**内容**分派解码

``pcm`` 是裸流、不带采样率，用它就得硬编码一个厂商专属的采样率；WAV 自带采样率，
对"任何兼容服务"这个目标才成立。``codec.decode_audio`` 因此**先看是不是 ``RIFF``**
——是就走那条零依赖的手写解析，否则交给 miniaudio 解 mp3。这样"厂商忽略了
``response_format``、照样回 mp3"也能正常工作。

**没有 opus 一档**：内置解码器（miniaudio）只认 WAV/FLAC/MP3/Vorbis。给一个做不到的
选项比不给更糟。

### 4. 新增运行时依赖 ``miniaudio``

"零新增依赖"是 M4 的选择（ADR 0022 §2），不是永久承诺——那时没有任何压缩音频需求。
miniaudio 自带单头 C 解码器、**不需要外部 ffmpeg 二进制**，并且只在 ``codec`` 里被
**延迟 import**：只用本地 WAV 后端的用户不为它付出导入成本。

### 5. STT 有独立的 ``stt_base_url``，cosyvoice 不拆

第三方语音几乎不可能是同一个服务，所以 STT 有自己的地址。但 **cosyvoice 的 STT 与
TTS 是同一个 Gradio 进程**，继续共用 ``tts_base_url``——把它的地址拆出来会让老配置
（改过 ``tts_base_url`` 的）静默走错端口。``tts_base_url`` 的语义因此明确为
"**TTS 后端地址**"，两个 provider 共用。

### 6. 密钥可以进 Studio，但走"只写不读"那条路

``SecretStr`` 字段（LLM / STT / TTS / embedding 各一把）从"完全不在白名单"改成
``kind="secret"`` 的字段：

- **读接口只报"已配置 / 未配置"**，``value`` 恒为空串、``default`` 恒为 ``None``；
- **留空保存 = 不改动**（否则每次点保存都会把已配好的密钥清成空串）；
- ``restart_pending`` 用**真值**在服务端比较，只把布尔结论发出去。

非密钥键的留空语义不变（照旧是"真的清空"，例如清掉参考音频路径）。

### 7. 第三方语音的健康探针**不校验模型名**

探 ``GET /models``：2xx + 鉴权通过 → ``OK``；LLM 那边还会校验模型在清单里
（不在 → ``DEGRADED``，那正是"名字写错"的信号）。语音**不校验**——大量兼容厂商
不把 whisper/tts 列进 ``/models``，拿它当依据会把"其实能用"报成降级。
语音后端回 404 时判 ``DEGRADED``（探不到 ≠ 挂了），LLM 那边同样的 404 判 ``DOWN``
（那意味着 ``base_url`` 写错了）。

### 8. 明确不做的事

- **Azure 的 URL 形状**（deployment 路径 + ``api-version`` 查询参数）。只换鉴权头
  解决不了它，做一半不如不做。
- **Anthropic 原生协议**、**流式**（ADR 0009/0022 已经定过）。
- **embedding 的 ``openai_compatible`` 一档**：``memory_vectors`` 的 ``float[768]``
  写在迁移 0003 里（不可改），换 1536 维的云端 embedding 需要新迁移 + 重建索引，
  那是独立的一件事。维度恰好是 768 的云端模型今天就能用（改
  ``ASTRA_EMBEDDING_BASE_URL`` 即可）。

## Reason

**为什么默认不发那些参数**：参数容忍度是服务端的静态属性，猜错一次就是整轮对话失败，
而且失败信息埋在 400 的 body 里、不好归因。让用户显式声明比自动探测诚实。
反过来，**默认开启才是不诚实的**——大多数第三方会直接拒掉。

**为什么响应解析比本地版本更宽**：``content`` 可能是字符串（常态）、对象数组
（``[{type: text, text: ...}]``）或 ``null``（推理模型把正文放在 ``reasoning`` 侧）。
本地 LM Studio 不会出现后两种，所以那边没处理，这里必须处理。

**为什么密钥要"只写不读"**：项目原本的做法是"密钥不进 UI"，代价是用户在 Studio 里
配不出可用配置。允许写但永不回读，既补上了这个缺口，又不引入"明文经过 HTTP 回到
浏览器"这条新路径。代价是留空必须解释成"不改动"——这条必须记清楚，否则一次保存
就会把密钥清空。

**为什么探针不检查语音的模型名**：ADR 0006 的原则是"诚实"——报 ``DEGRADED``
是"能用但有疑虑"，报 ``DOWN`` 是"不能用"。厂商不列模型这件事不足以支撑任何一个，
所以只报可达性与鉴权（那两条是真的）。

## Consequences

- ``/health`` 的 5 个 key 集合不变（ADR 0006），只是 value 有了新的来路。
- Studio 设置页多出 13 个键（provider 枚举、模型、地址、密钥、两个参数开关）；
  provider 枚举从 ``Literal`` 自动派生，不需要手抄（ADR 0026）。
- 新增依赖 ``miniaudio``（mypy 需要 override，它没有分发 py.typed）。
- 测试：`tests/unit/test_openai_compatible_{llm,audio,health}.py`、
  `tests/unit/test_provider_assembly.py`，以及一个 mp3 fixture
  （`tests/fixtures/tone_mono.mp3`，由 lameenc 一次性生成，见 `test_audio_codec.py` 注释）。
- **隐私边界变了**：启用第三方 provider 后，对话内容、记忆文本与语音音频会离开
  本机发往该服务商。README「已知限制」里写明了这一点。
- **已知局限**：不校验厂商对 ``json_schema`` / ``reasoning_effort`` 的真实支持，
  开关是用户声明；STT 不做 ``verbose_json``，所以 ``language`` / ``confidence``
  常常是空的；TTS 的音色名不做跨厂商映射。
