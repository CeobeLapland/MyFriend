# 11 Security and Privacy

## 1. Threat Model

这是一个本地个人 Agent，但它一旦拥有：

- 文件访问
- 浏览器访问
- shell
- 麦克风
- 摄像头
- 屏幕截图

风险会从“聊天应用”迅速变成“高权限 Agent”。

因此权限必须逐步开放。

## 2. 默认原则

### 麦克风

默认关闭持续监听；只有用户启动 voice mode 后才采集。

### 摄像头

默认关闭。

### 屏幕

默认不截图；视觉模式必须明确开启。

### 文件系统

只允许访问配置的 allowlist 目录。

例如：

```yaml
filesystem:
  read_roots:
    - ./workspace
  write_roots:
    - ./workspace/output
```

### Shell

默认禁用。

不要因为 Agent 说“请执行这个命令”就执行任意 shell。

## 3. Secrets

禁止：

- 把 API key 写进源码。
- 把 token 放进日志。
- 把 `.env` 提交 Git。

## 4. Memory Privacy

用户必须能够：

- 查看。
- 修改。
- 删除。
- 导出。
- 全量清除。

删除 memory 时应同时处理 vector index。

## 5. Tool Permission Policy

建议：

```text
safe            -> execute
confirm         -> ask user
restricted      -> blocked by default
```

## 6. Logs

默认日志记录：

- event type
- latency
- success/failure
- provider name
- request ID

默认不记录：

- 原始麦克风音频
- 摄像头帧
- 完整敏感文件内容
- API tokens

## 7. Prompt Injection

任何外部内容都可能包含恶意指令，例如：

- 网页内容
- 文件内容
- 剪贴板
- OCR 文本
- 第三方 API

外部内容必须标记为 `untrusted_context`，不能自动升级为 system instruction。
