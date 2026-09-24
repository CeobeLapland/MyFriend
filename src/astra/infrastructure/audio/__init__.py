"""音频基础设施：编解码与 VAD。

这一层是**实现细节**，不是端口——``providers`` 与 ``application`` 都可以用，
但它不认识任何具体后端。

分层注意：``application`` 层禁止 import httpx 等 HTTP 客户端（ADR 0010），
本包只用 stdlib，因此谁都能用。
"""
