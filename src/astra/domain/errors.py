"""领域错误：稳定的错误码闭集 + 异常类型。

`docs/04` §5 与 `docs/01` §4 要求：工具/依赖失败时要"自然地告诉用户发生了什么，
而不是编造成功结果"。错误码是这个承诺的机读部分——客户端按 `code` 分支，
用户读 `message`/`reply`。

`ErrorCode` 是**闭集**：新增错误码属于协议变更，需要同步 `docs/` 与 ADR。
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """对外暴露的稳定错误码。"""

    LLM_UNAVAILABLE = "llm_unavailable"
    LLM_TIMEOUT = "llm_timeout"
    LLM_DISABLED = "llm_disabled"
    LLM_INVALID_RESPONSE = "llm_invalid_response"
    #: M4：STT/TTS 后端不可达。**都不让 turn 失败**——STT 失败时本轮根本没开始，
    #: TTS 失败时文本回复照常送达（`docs/10` §4「TTS failure」）。
    STT_UNAVAILABLE = "stt_unavailable"
    TTS_UNAVAILABLE = "tts_unavailable"
    AUDIO_FORMAT_ERROR = "audio_format_error"
    CONVERSATION_NOT_FOUND = "conversation_not_found"
    INVALID_INPUT = "invalid_input"
    INTERNAL_ERROR = "internal_error"


class AstraError(Exception):
    """本项目所有受控异常的基类。"""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    #: 是否值得原样重试一次（由 Orchestrator 消费，见 ADR 0011 的重试矩阵）
    retryable: bool = False

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class LLMUnavailableError(AstraError):
    """Provider 不可达（连接失败 / 拒绝 / DNS 失败）。重试无意义。"""

    code = ErrorCode.LLM_UNAVAILABLE
    retryable = False


class LLMTimeoutError(AstraError):
    """Provider 超时。值得重试一次。"""

    code = ErrorCode.LLM_TIMEOUT
    retryable = True


class LLMDisabledError(AstraError):
    """`ASTRA_LLM_PROVIDER=none`，本阶段没有配置任何 LLM。"""

    code = ErrorCode.LLM_DISABLED
    retryable = False


class LLMResponseError(AstraError):
    """Provider 有响应但内容不可用（截断、非法 JSON、reply 为空）。"""

    code = ErrorCode.LLM_INVALID_RESPONSE
    retryable = True


class ConversationNotFoundError(AstraError):
    """显式给出的 conversation_id 不存在或已结束。"""

    code = ErrorCode.CONVERSATION_NOT_FOUND
    retryable = False


class STTUnavailableError(AstraError):
    """STT 后端不可达 / 返回了不可用的结果（M4）。

    不做重试：语音识别是用户主动发起的一次操作，重试要用户重新说一遍才有意义。
    """

    code = ErrorCode.STT_UNAVAILABLE
    retryable = False


class TTSUnavailableError(AstraError):
    """TTS 后端不可达 / 合成失败（M4）。

    **抛它不会让 turn 失败**：调用方（``Orchestrator``）捕获后只发一条
    ``tts.completed{status:"failed"}``，文本回复已经送达（ADR 0014 的降级模式）。
    """

    code = ErrorCode.TTS_UNAVAILABLE
    retryable = False


class AudioFormatError(AstraError):
    """客户端声明的音频格式不受支持（M4）。

    边界层（WS）应当先拦掉它；这里保留是为了让音频入口面对任何客户端
    都能给出稳定的错误码而不是抛栈。
    """

    code = ErrorCode.AUDIO_FORMAT_ERROR
    retryable = False


class InvalidInputError(AstraError):
    """用户输入为空。

    边界层（HTTP / WS）应当先拦掉它；这里保留是为了让 Orchestrator 面对任何
    客户端都能给出诚实结果而不是抛栈。
    """

    code = ErrorCode.INVALID_INPUT
    retryable = False
