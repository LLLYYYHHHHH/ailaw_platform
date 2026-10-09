# 多模型测试台

通用问题输入、多模型并行流式回答、SQLite 实验记录、历史查看、可配置人工评分、原文片段标记、JSON/CSV 导出。支持 DeepSeek、通义千问及兼容 Chat Completions 的服务，预留 OpenAI GPT 配置。

## 启动

在此目录打开终端，执行：

```powershell
python server.py
```

访问 http://127.0.0.1:8000 。关闭终端或按 Ctrl+C 停止。

如 8000 被旧版程序占用，可使用 `python server.py --port 8001`，访问 http://127.0.0.1:8001 。当前联调使用 `python server.py --port 8002`，访问 http://127.0.0.1:8002 。

## 配置真实模型

1. 将 `.env.example` 复制成同目录下的 `.env`。
2. 在 `.env` 的 `DEEPSEEK_API_KEY=` 和 `DASHSCOPE_API_KEY=` 后填写对应密钥，可只配置一家。密钥不用填入页面或发到聊天。
3. 检查 `models.json`：`model` 必须是服务商当前对你的账号开放的模型 ID；默认值为示例，不保证永久可用。`base_url` 应填基础地址，不包含 `/chat/completions`。
4. 如果原来的服务仍运行，停止后重新启动 `python server.py`。之后编辑配置或密钥，只需点击页面“刷新模型配置”。
5. 选择“真实模型”，勾选已配置模型，先用“请只回答：连接成功”进行小规模测试。

“已配置”仅表示本机配置齐全；第一次真实调用才能验证密钥、网络、权限和余额。失败显示在各自的回答卡片中。

### DeepSeek

示例地址 `https://api.deepseek.com`，密钥由服务商 API 平台获取。模型 ID 请以账号可用列表为准。

[DeepSeek 官方接入文档](https://api-docs.deepseek.com/)

### 通义千问

默认地址 `https://dashscope.aliyuncs.com/compatible-mode/v1` 是北京地域地址。密钥必须与接口地域一致；可按百炼控制台替换为业务空间专属地址。示例模型 `qwen-plus` 可按账号可用列表更换。

[百炼官方兼容接口文档](https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope)

### OpenAI GPT（可选）

在 `.env` 填写 `OPENAI_API_KEY`，在 `models.json` 设置账号可用模型 ID。地址为 `https://api.openai.com/v1`。兼容模型默认调用 `/chat/completions`；设置 `api: "responses"` 时调用 `/responses` 并整段返回正文。GPT 当前设置 `enabled: false`，不会出现在新实验中，也不能通过后端调用；历史实验仍可查看。未来恢复可将 `enabled` 改回 `true`。Responses 非流式接口读取超时 5 分钟。非流式模型的首字时延不提供，避免与流式模型的首字时延混淆。

API 使用独立的账号额度和计费。平台调用的是所配置的 API 模型；当前聊天内容、文件、工具和订阅不会随之传入。

[OpenAI API 入门](https://developers.openai.com/api/docs/quickstart) · [Chat Completions 文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)

### 参数与新增模型

每个模型的 `parameters` 单独配置，以便保存实际发送的参数。可设置 `temperature`、`top_p`、`max_tokens`、`max_completion_tokens`、`max_output_tokens`、`seed`、`enable_thinking`、`reasoning_effort`。仅设置服务商支持的参数；没有显式设置的值由服务商决定，不代表模型间参数完全一致。新模型在 `models.json` 增加一个对象，密钥放入其 `api_key_env` 指定的环境变量或 `.env` 项。本机 `.env` 项优先于系统环境变量；上线时不提供 `.env`，改用部署环境变量。

不支持 `stream_options` 的服务可设 `include_usage: false`。未返回用量时显示“用量未返回”，不估算为零。

## 保存与运行规则

每个模型独立并行调用，保存原始回答文本、输入、配置的模型 ID、服务商返回的模型标识、实际请求参数、状态、错误、首字时延、总时延、Token 用量与结束原因。首字时延从请求开始计算到第一段正文返回。响应中的模型标识不一定是可固定的版本号，严谨实验应在配置中使用服务商支持的版本 ID。

单次最多 5 个模型，同时最多 2 组实验。网络连接/单次读取超时 60 秒，整体生成检查上限 5 分钟。不会自动重试，避免重复计费。正文输出上限由每个模型参数控制；遇到 `length` 时页面提示可能截断。

刷新或关闭页面后后台仍继续调用，可以从历史查看已保存的内容；停止服务器会中断调用，下次启动将标记未完成实验。JSON 导出包括当前实验已收到的数据，运行完成后导出才是完整记录。历史列表显示最近 100 次，旧实验仍保存在数据库。

`.env` 不会通过静态文件路由提供，也不会写进实验或导出；错误提示不回显服务商错误正文。

## 当前范围与上线下一步

当前仍绑定 `127.0.0.1`，供本机联调，使用 Python 标准库，无额外依赖。上线前还需部署运行环境、登录权限、用户调用配额与服务器数据库方案。当前模块可作为迁移时的模型适配层。

尚未实现账号登录、复核状态流转、取消调用、自动成本估算、Responses 流式及 Anthropic 原生协议。

## 验证

```powershell
python -m unittest -v test_integration.py
```

测试使用本地接口替身验证并行、流式解析、失败隔离、记录保存与密钥隔离，不消耗真实服务商额度。真实账号连通性需完成上面的配置步骤后验证。

实验保存在本目录 experiments.sqlite3。当前仅供本机使用。

## 人工评价流程（0.3）

1. 输入问题，选择 DeepSeek 和千问，运行一次真实对比。
2. 结束后填写评价人昵称。昵称仅是本机记录，不是登录身份。
3. 默认评分项为正确性、完整性、清晰度，可用逗号分隔修改，点击“应用评分项”。每项 1–5 分，“未评分”不会记为零分；评价可只包含备注或标记。
4. 在回答中选中文字，或粘贴原文片段，填写问题类型和理由，点击“添加片段标记”。手动粘贴重复片段时定位到第一次出现的位置；精确定位重复片段请用鼠标选取。
5. 点击该模型的“保存评价”。每次提交生成一条新记录，保留此前评价。所有片段意见都标为待复核，当前没有专家复核流程。
6. 在实验档案搜索并回看记录，或导出 JSON/CSV。导出包含原始回答和已保存评价；草稿不会进入导出。

评价放在独立的 reviews 表中，模型回答更新不会覆盖评价。片段使用 Unicode 字符位置并校验引用文本，支持中文及 emoji；CSV 对可能被电子表格作为公式执行的单元格做文本保护。评分项只决定本次新评价，历史评分项随原记录保存。

所有测试题、演示评分和测试片段均标明功能联调用途，不作为模型能力评价证据。0.3 的自动测试覆盖模型调用、失败隔离、评价定位与保存、导出和禁用模型。
