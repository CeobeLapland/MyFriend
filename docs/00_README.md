# AI Companion Project Spec

这是一个面向“个人本地 AI 伙伴”的工程规范包，目标是让另一个 Coding Agent 可以直接阅读这些文档，逐步实现项目，而不需要反复询问总体架构。

> 项目代号：`Astra`
>
> 定位：Local-first、长期记忆、有稳定人格、支持实时语音、具有可表达 3D 身体的 AI Companion。
>
> 第一阶段不做直播、不做游戏 Agent、不做大规模多用户服务。

## 1. 文档使用方式

建议把整个 `ai_partner_spec/` 目录复制到代码仓库的 `docs/`，然后把 `09_AGENT_INSTRUCTIONS.md` 的内容放进仓库根目录的 `AGENTS.md` 或类似 Agent 指令文件。

实现时遵循：

1. `00_README.md`：项目入口。
2. `01_REQUIREMENTS.md`：产品边界和需求。
3. `02_ARCHITECTURE.md`：系统架构与组件职责。
4. `03_DATA_MODEL.md`：SQLite 数据结构与对象模型。
5. `04_API_AND_EVENTS.md`：HTTP/WebSocket/Event Bus 协议。
6. `05_PERSONA_AND_PROMPTS.md`：人格、回复结构与 LLM Prompt 规范。
7. `06_MEMORY.md`：长期记忆算法与生命周期。
8. `07_AUDIO_AND_AVATAR.md`：STT/TTS/Unity/VRM 集成要求。
9. `08_IMPLEMENTATION_PLAN.md`：实施阶段和任务拆分。
10. `09_AGENT_INSTRUCTIONS.md`：给 Coding Agent 的执行规则。
11. `10_TESTING_AND_ACCEPTANCE.md`：测试和验收标准。
12. `11_SECURITY_AND_PRIVACY.md`：本地数据、工具权限和安全边界。
13. `schemas/`：JSON Schema，作为协议的机器可读来源。

## 2. 当前技术基线

推荐基线：

- LLM：Qwen3 系列，通过 LM Studio 本地 API 接入。
- LLM API：OpenAI-compatible `/v1/chat/completions`、`/v1/responses`；需要 Structured Output 与 Tool Calling 时优先走兼容接口。
- Backend：Python + FastAPI。
- Realtime：WebSocket。
- Database：SQLite。
- Vector Memory：第一阶段优先 sqlite-vec；若未来数据规模或检索需求增长，再抽象到 Qdrant。
- STT：FunASR / SenseVoice；faster-whisper 作为后备适配器。
- TTS：现有 CosyVoice 作为第一后端；Qwen3-TTS 作为第二后端和后续音色/风格候选。
- Avatar：Unity + VRM 1.0 + UniVRM。
- Avatar creation：VRoid Studio 或用户已有 VRM 模型。
- Content generation：ComfyUI 作为旁路内容生成服务，不进入主对话关键路径。

这些选型不是永久绑定。所有外部模型/服务必须通过 Adapter 接口接入。

## 3. 核心设计原则

### 3.1 LLM 不是系统本身

LLM 负责理解、生成、规划和建议，不直接拥有数据库、操作系统权限或 Unity 控制权。

### 3.2 人格、记忆、情绪、动作都是状态

不要把全部角色状态塞进一个长 System Prompt。角色状态应当有明确的数据结构，由程序管理，再按请求动态投影给 LLM。

### 3.3 LLM 产生“意图”，执行器决定“怎么做”

例如 LLM 输出 `emotion=happy`、`action=wave`，Unity 再将它映射为真正的动画参数。

### 3.4 记忆不是聊天记录

聊天历史是 Context；记忆是从事件中提取并长期保存的高价值信息。

### 3.5 默认本地、显式授权

本地数据默认不离开设备；高风险工具默认关闭；所有工具执行必须经过权限策略。

### 3.6 先实现可测试的文本核心，再接身体

第一里程碑应当能够在没有 Unity、没有麦克风的情况下跑通：`用户文本 -> LLM -> memory -> structured response`。

## 4. 当前明确非目标

暂不要求：

- 自动直播平台接入。
- 自动玩游戏。
- 多用户在线部署。
- 自主修改自身代码。
- 无提示地访问整个电脑文件系统。
- 无限制地执行 shell 命令。
- 复杂 3D 物理模拟。
- 模型微调作为 MVP 必需项。

## 5. 官方技术参考

- LM Studio Developer Docs: https://lmstudio.ai/docs/developer
- LM Studio OpenAI-compatible API: https://lmstudio.ai/docs/developer/openai-compat
- LM Studio Structured Output: https://lmstudio.ai/docs/developer/openai-compat/structured-output
- LM Studio Tool Use: https://lmstudio.ai/docs/developer/openai-compat/tools
- Qwen3: https://github.com/QwenLM/Qwen3
- Qwen3-TTS: https://github.com/QwenLM/Qwen3-TTS
- FunASR: https://github.com/modelscope/FunASR
- UniVRM: https://github.com/vrm-c/UniVRM
- FastAPI WebSocket: https://fastapi.tiangolo.com/advanced/websockets/
