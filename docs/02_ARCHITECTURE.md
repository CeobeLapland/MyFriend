# 02 Architecture

## 1. 总体架构

```text
┌─────────────────────────────────────────────────────────────────┐
│                         Client Layer                            │
│                                                                 │
│  Text UI / Voice UI / Unity Avatar / Settings / Memory UI      │
└──────────────────────────────┬──────────────────────────────────┘
                               │ WebSocket / HTTP
┌──────────────────────────────▼──────────────────────────────────┐
│                        AI Core (Python)                        │
│                                                                 │
│  Session Manager                                                │
│  Conversation Manager                                           │
│  Persona Manager                                                │
│  Memory Manager                                                 │
│  Emotion/Relationship State                                     │
│  Response Planner                                               │
│  Tool Registry + Permission Policy                              │
│  Event Bus                                                      │
└───────┬─────────────┬────────────┬──────────────┬──────────────┘
        │             │            │              │
        ▼             ▼            ▼              ▼
    LLM Adapter   Memory DB     Audio Hub      Tool Adapters
        │             │            │              │
        ▼             ▼            ▼              ├─ filesystem-safe
   LM Studio       SQLite         STT            ├─ browser-safe
   / other LLM     + vector       TTS            ├─ music
                                                └─ OS tools (gated)
        │
        ▼
   ComfyUI Adapter (optional, async)

Unity Client:
  VRM Runtime -> Expression Controller -> LipSync -> Animator -> Camera/UI
```

## 2. 组件职责

### 2.1 API Layer

负责：

- HTTP 健康检查。
- WebSocket 连接。
- Session 建立。
- 输入事件接收。
- 输出事件发送。

不负责：

- 拼 Prompt。
- 直接调用 LLM。
- 直接操作数据库表。

### 2.2 Orchestrator

负责一次 turn 的生命周期：

```text
input_received
 -> normalize_input
 -> retrieve_memory
 -> build_context
 -> ask_llm
 -> validate_response
 -> apply_state_delta
 -> persist_conversation
 -> enqueue_memory_extraction
 -> dispatch_avatar/audio events
```

### 2.3 Persona Manager

负责读取：

- 基本身份。
- 固定人格原则。
- 说话风格。
- 禁止行为。
- 动态人格变量。

### 2.4 Memory Manager

负责：

- memory candidate extraction。
- importance scoring。
- deduplication。
- embedding。
- retrieval。
- consolidation。
- forgetting/archiving。

### 2.5 State Manager

维护：

- mood。
- energy。
- relationship。
- current activity。
- attention target。

必须保证状态字段有上下界和衰减逻辑。

### 2.6 Tool Registry

所有工具统一描述：

```text
name
version
description
input_schema
risk_level
requires_confirmation
handler
```

LLM 只能选择 Tool Registry 中已经注册的工具。

### 2.7 Avatar Gateway

AI Core 不操作 Unity GameObject。

AI Core 只发布高层事件，例如：

```json
{
  "type": "avatar.expression.set",
  "emotion": "happy",
  "intensity": 0.75
}
```

Unity 自己负责把它映射成 Expression/Animator 参数。

## 3. Adapter 接口

推荐 Python Protocol 风格：

```python
class LLMProvider(Protocol):
    async def generate(self, request: LLMRequest) -> LLMResponse: ...

class STTProvider(Protocol):
    async def transcribe(self, audio: AudioInput) -> Transcript: ...

class TTSProvider(Protocol):
    async def synthesize(self, request: TTSRequest) -> AsyncIterator[AudioChunk]: ...

class MemoryStore(Protocol):
    async def add(self, memory: Memory): ...
    async def search(self, query: MemoryQuery) -> list[Memory]: ...

class AvatarClient(Protocol):
    async def publish(self, event: AvatarEvent): ...
```

## 4. 进程建议

MVP 可以单进程：

```text
backend/
  FastAPI
  Event Bus
  SQLite
  LM Studio adapter
```

P1 再拆出：

```text
ai-core
voice-service
unity-client
```

只有当资源隔离或部署需要时，才拆更多微服务。单机项目不要为了“架构漂亮”过早微服务化。

## 5. 依赖方向规则

允许：

```text
API -> Application -> Domain -> Infrastructure
```

不允许：

```text
Domain -> FastAPI
Domain -> Unity
Domain -> LM Studio SDK
Domain -> SQLite ORM specific implementation
```

这样后续更换基础设施时不会污染业务层。

## 6. 目录建议

```text
astra/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── .env.example
├── docs/
├── schemas/
├── apps/
│   ├── api/
│   ├── worker/
│   └── cli/
├── src/
│   └── astra/
│       ├── domain/
│       ├── application/
│       ├── infrastructure/
│       ├── providers/
│       └── settings.py
├── tests/
│   ├── unit/
│   ├── integration/
│   └── contract/
├── unity/
└── data/
```
