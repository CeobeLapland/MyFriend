# ADR 0002 — Single Process First

## Decision

MVP 使用单个 Python backend 进程承载 API、Orchestrator、Memory、Event Bus；外部推理服务作为 provider 连接。

## Reason

这是个人单机应用。过早微服务化会引入额外部署、网络、日志和调试成本。

## Consequences

当出现以下情况再拆服务：

- STT/TTS 需要独立 GPU。
- Provider 崩溃影响主程序。
- 多客户端并发。
- 独立扩缩容。
