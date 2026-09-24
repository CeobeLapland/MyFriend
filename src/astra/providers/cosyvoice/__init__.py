"""CosyVoice3 雨落版整合包的 provider 实现（M4）。

后端不是 REST API，而是一个 **Gradio 应用**（``http://127.0.0.1:7860``）。
本包用**纯 httpx** 调它的 ``/gradio_api/call/*`` 端点，**刻意不引入
``gradio_client``**——实测那条路只需要三步（POST 拿 ``event_id``、
GET SSE 拿结果、GET 文件拿字节），三十行 httpx 就能覆盖，而 ``gradio_client``
会拖进 ``huggingface_hub`` / ``aiofiles`` 等一串与本项目无关的传递依赖。

这样做的另一个好处是**天然 provider-agnostic**：将来换成第三方 HTTP API
（用户已明确可能这么做），只需新写一个 provider 包，端口与上层代码都不用动。
"""
