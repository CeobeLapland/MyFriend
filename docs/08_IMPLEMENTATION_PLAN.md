# 08 Implementation Plan

## 1. 阶段 0：工程骨架

目标：项目能启动、能测试、Agent 能持续工作。

任务：

- 初始化 Python 项目。
- 配置 `uv` / `pyproject.toml`。
- 建立 domain/application/infrastructure/providers。
- 建立 SQLite migration。
- 建立 pytest。
- 建立 lint/type check。
- 建立 `.env.example`。
- 建立 `/health`。

验收：

```text
pytest passes
health returns 200
empty DB can be initialized from scratch
```

## 2. 阶段 1：Text Brain

目标：完全不接 Unity，完成“会聊天的大脑”。

实现：

- LM Studio adapter。
- Conversation manager。
- Persona manager。
- Structured Response schema。
- Event Bus。
- SQLite persistence。

验收：

```text
text in
 -> LLM
 -> structured response
 -> text out
```

## 3. 阶段 2：Memory

实现：

- Memory candidate extractor。
- Importance scoring。
- Embedding adapter。
- sqlite-vec integration。
- Retrieval。
- Memory management API。

验收：

1. 第一次告诉 AI 一个稳定偏好。
2. 关闭程序。
3. 重新启动。
4. 新会话中能在相关语境检索出来。
5. 删除后不再检索到。

## 4. 阶段 3：State

实现：

- Emotion State。
- Relationship State。
- State transition rules。
- Decay。
- State snapshot。

验收：

```text
user praises companion
 -> happiness increases
 -> assistant response style changes
 -> avatar event can reflect the new state
```

## 5. 阶段 4：Audio

实现：

- Microphone capture。
- VAD。
- STT adapter。
- TTS adapter。
- audio streaming events。
- interruption。

验收：

```text
speak -> transcript -> response -> audio
```

同时键盘输入仍然可用。

## 6. 阶段 5：Avatar

实现：

- Unity project。
- VRM import。
- WebSocket client。
- expression controller。
- action controller。
- lip sync。
- idle system。

验收：

- 文本聊天会驱动表情。
- TTS 会驱动口型。
- action 能正确播放。
- Unity 断开不会影响 backend。

## 7. 阶段 6：Tools

先做安全工具：

- get_time。
- calculator。
- read_safe_notes。
- create_local_task。

之后再做：

- open_app。
- browser_search。
- file_read limited roots。
- file_write limited roots。

所有高风险工具都默认需要用户确认。

## 8. 阶段 7：主动行为

实现 Scheduler：

```text
cron-like tick
 -> evaluate context
 -> check cooldown
 -> check quiet hours
 -> decide whether proactive message is appropriate
```

不要让 LLM 每秒运行。

建议默认 tick 30~60 秒或事件驱动。

## 9. 阶段 8：视觉与 ComfyUI

最后接：

- screenshot capture。
- VLM。
- ComfyUI asynchronous generation。

视觉功能默认关闭，用户显式开启后使用。

## 10. 推荐任务粒度

让 Coding Agent 一次只拿一个可验证任务，例如：

```text
Implement the Conversation repository.
Requirements:
- Follow 03_DATA_MODEL.md.
- Add migration.
- Add unit tests.
- Do not implement API endpoints yet.
- Return a concise summary and test output.
```

不要直接给 Agent：

```text
“把整个项目做完。”
```

## 11. Definition of Done

每个任务至少满足：

- 代码通过 lint。
- 类型检查通过。
- 单元测试通过。
- 新增协议有 schema。
- 错误路径有测试。
- 不引入没有必要的新依赖。
- 文档与实现一致。

## 12. 开发者控制台（原计划外，见 ADR 0017）

原计划没有 UI 里程碑。M1 完成后补了一个**开发者控制台**（`GET /dev`），
理由是 M2/M3 产生的状态（记忆、emotion 数值）没有可视化就只能靠查库验收。

**它是开发工具，不是产品 UI**：

- 只在 `ASTRA_ENV=dev`（默认）时挂载；设成 `prod` 即可关掉。
- 单文件 HTML + 原生 JS，零构建、零 npm、零新增 Python 依赖。
- 展示：health 实时状态、原始事件流、对话（WS / 同步）、历史、协议词表、
  每轮的 stub / 重试 / 耗时。

**产品 UI**（角色形象、对话气泡、设置页）仍然属于 P1/P2，需要独立的技术选型
与设计，届时另开 ADR。两者看的东西不同，控制台在产品 UI 出来后依然有用。
