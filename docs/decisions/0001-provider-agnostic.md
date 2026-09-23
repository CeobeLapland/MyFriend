# ADR 0001 — Provider Agnostic AI Core

## Decision

核心业务层不直接依赖 LM Studio、CosyVoice、FunASR 或 Unity；通过 Adapter 接口接入。

## Reason

本项目的核心价值是 Agent 行为、人格、记忆和状态，而不是某一个模型品牌。模型更新速度快，Provider 必须能够替换。

## Consequences

- 初期代码量稍高。
- 测试需要 mock providers。
- 未来切换云端/本地模型更容易。
