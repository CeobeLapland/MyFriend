"""任意 **OpenAI 兼容** 的第三方后端适配器（云端或自建）。

与 ``providers/lmstudio`` 的分工：那个包是**本地 LM Studio 专属**的，注释里记的是
本地实测出来的参数怪癖（``reasoning_effort=none`` 才能关思考、返回 32-bit float WAV
等等）。本包面向"任何声称 OpenAI 兼容的服务"，所以策略相反——**只发最小公共集**，
把有争议的参数交给配置开关控制。

覆盖三个能力：

======  ==================================  ==============================
能力    端点                                 实现
======  ==================================  ==============================
LLM     ``POST /chat/completions``           :class:`OpenAICompatibleLLMProvider`
STT     ``POST /audio/transcriptions``       :class:`OpenAICompatibleSTTProvider`
TTS     ``POST /audio/speech``               :class:`OpenAICompatibleTTSProvider`
======  ==================================  ==============================

**不做的事**（都写在 ADR 0032 里）：不做流式、不做 Azure 的 deployment/api-version
形状、不做 Anthropic 原生协议。它们各需要扩端口或扩配置面，而现在没有消费者。
"""
