# 功能与 API 配置

## 哪个文件负责什么

| 文件 | 用途 |
| --- | --- |
| `.env` | 本机密钥、模型 ID 和生成的管理密码；权限 0600 |
| `images.lock.json` | 官方镜像版本与不可变摘要 |
| `data/astrbot/cmd_config.json` | AstrBot 生效配置；含凭据引用和后台登录配置 |
| `data/astrbot/plugins/astrbot_plugin_qqbot_guard/routing.json` | 插件生效配置：群聊概率、冷却、并发、超时、可选 API |
| `plugins/astrbot_plugin_qqbot_guard/` | 插件发行源码；运行配置不会自动被源码覆盖 |
| `data/astrbot/plugin_data/qqbot_guard/` | 群模式、附件索引、近期图片和绘图任务等私密状态 |
| `data/napcat/` | QQ 登录状态、NapCat 和 OneBot 配置 |
| `system-prompt.txt` | 可自行修改的通用人设 |

修改 `.env` 后，Compose 环境变量需要**重建容器**才能生效；单纯 `restart` 不会加载新的容器环境变量。

## 切换聊天模型与预算

1. 先 `docker compose stop astrbot`。
2. 在服务器用编辑器修改 `.env`，保持每项单行，建议使用单引号。
3. `python3 scripts/configure.py apply`：重新检查 `/models` 和最小文本请求，备份旧配置后写入三个 Provider 及插件路由。
4. `docker compose up -d --force-recreate astrbot`。

日常、深度、分类三个模型可以共用 API，但它们有不同超时和输出限制。深度档由关键词/小模型选择；默认不加入厂商专用的 `thinking` 或 `reasoning_effort` 参数。需要更高思考预算时，请根据服务商**当前文档和模型实际能力**在 AstrBot Provider 的额外请求参数中配置，再用真实任务验证。

`CONTEXT_TOKENS` 控制本地上下文预算，默认 32768；若 API 已验证支持 200K，可设为 `204800`。这只是本地截断预算，不会改变服务商模型的实际窗口。框架最多 60 轮对话和 token 预算同时生效；群背景另有最近 100 条、每条 400 字、30 分钟有效期的限制。改大后请求费用和延迟也会增加。

`CHAT_MAX_TOKENS` 默认 1536，`DEEP_MAX_TOKENS` 默认 8192。兼容接口可能使用不同输出参数、限制或推理计费规则；不支持 `max_tokens` 的接口需要按官方契约改 Provider 和探测脚本，不能直接声称兼容。

分享版默认关闭流式发送，方便将完整公式/代码排版为图片。普通文本需要流式时，可先在 AstrBot 中测试；QQ 分段、图片转换与流式的组合需自行验证。

## 识图与 GIF

模型确实支持图片输入后，将 `.env` 的 `VISION_ENABLED=true`；意图判断小模型也支持图片时才设置 `CLASSIFIER_VISION_ENABLED=true`，随后按上节应用并重建。

GIF 会有限度抽取代表帧，控制像素和帧数，不逐帧推理。优先读取明确引用图，否则使用当前会话近期上传的材料；不同群、不同私聊不会共用材料。静态代表帧无法涵盖的快速动作需另行描述。

## 文件阅读

支持文本、代码、PDF、DOCX、XLSX 等有限格式。文件在受限子进程中解析，不执行宏、脚本或附件中的指令。

默认“先收文件，等用户提问”。`/文件自动回复 开` 可以改为收到文件后主动概括；关闭后发完文件再说“做一下这套题”即可。群里需明确叫机器人，并遵守本群回复模式。

ZIP 暂不支持直接解压读取，请先解压后发送其中的可读文件。一次失败只针对该附件提示；不会继续拦截普通聊天。关闭文件自动回复时，失败提示会等到实际追问文件时出现。

文件有大小、页数、字符数和时间限制；扫描 PDF 仅能对成功提取的页面图片交给视觉模型。文字提取会丢失部分公式或版面，不能保证完整读取所有扫描件。遇到截断会提示，可按页拆分上传。

## 按需绘图

API 必须支持 `/images/generations`，且能返回 `data[0].b64_json`。配置：

```dotenv
IMAGE_API_BASE_URL='https://image-api.example.com/v1'
IMAGE_API_KEY='在你本机填写'
IMAGE_MODEL='服务商实际支持的模型ID'
```

留空 Base URL / Key 时复用主 API。填入 `IMAGE_MODEL` 才启用。请求采用 `size=1024x1024`、`quality=standard`、`n=1`、`response_format=b64_json`；若服务商参数不同，请修改插件对应请求并实测。只返回远程 URL 的 API 暂不直接支持，不能自动认为接入成功。

修改后运行 `apply` 并重建 AstrBot。`apply` 验证聊天模型，**不会消耗生图额度验证图片 API**。请在私聊用 `/画图 一只橘猫坐在杯子旁边` 实测，再测试“重新画”“换成水彩风格”“重发图片”“只是聊画画，不用画”。只有明确意图才生成新图；重发命中缓存时不重复调用。

## QQ 语音转文字（可选）

需要真正支持 `POST /audio/transcriptions` 的 ASR API，采用 multipart 上传 WAV，返回 `{"text":"..."}`。

```dotenv
AUDIO_API_BASE_URL='https://audio-api.example.com/v1'
AUDIO_API_KEY='在你本机填写'
AUDIO_MODEL='服务商实际支持的转写模型ID'
```

Base URL / Key 留空时复用主 API；默认不启用。修改后 `apply` 并重建。QQ 音频通过平台原始 ID 下载、解码为有大小和时长限制的 WAV，再提交远程 API。最长 60 秒；静音、下载和解码失败会明确提示。

该功能的请求协议有模拟测试，但没有用你的 ASR 服务完成真实转写验证。歌曲、嘈杂录音、方言都可能识别错误，不能把返回了一段文字视为已经听对。

## 独立搜索 API 池

支持 TinyFish Search、Tavily 和 Exa 的独立 API；均需要你自己合法持有的凭据。免费额度和条款会变化，以各家当前控制台为准；**这里的本地计数不是服务商账单**。

在 `data/astrbot/plugin_data/qqbot_guard/search-accounts.json` 写入以下结构，设置 `chmod 600`。只添加真实免费计划凭据；同一个账号的不同 Key 使用同一个 `quota_group`，避免误算独立额度。

```json
{
  "accounts": [
    {
      "provider": "tavily",
      "id": "my-account",
      "quota_group": "my-account",
      "api_key": "在你本机填写",
      "enabled": true,
      "free_plan": true,
      "local_limit": 100
    }
  ]
}
```

然后在插件运行 `routing.json` 将 `web_search.enabled` 设为 `true`，重启 AstrBot。按 TinyFish、Tavily、Exa 的顺序选择可用凭据；限流、额度用尽或鉴权失败时有限次切换。最多 4 次尝试、总时限 45 秒，错误状态和计数持久化。没有配置凭据时不会假装完成搜索。

只轮换自己合法拥有且服务商允许使用的凭据；轮换不能绕过账号共享额度、封禁或付费限制。`local_limit` 请按实际免费余额保守设置；HTTP 429、额度耗尽会进入冷却，不无限重试。

默认回答不展示来源链接，来源仍按会话缓存；用户明确要求或 `/来源` 时再提供。网页和结果摘录只是外部材料，不得当成系统指令。

## 群聊模式与性能

修改插件的**运行** `routing.json` 后重启 AstrBot：

```json
{
  "group_reply": {"enabled": true, "probability": 0.05, "cooldown_seconds": 5, "context_messages": 100},
  "concurrency": {"max_concurrent": 1}
}
```

以上仅为需修改字段的节选，**不要用它覆盖完整文件**。每群模式开关由 `/主动回复` 管理并持久化；关闭时仅 @ 回复，管理命令仍可用。全局并发从 1 调到 2 或 3 前先观察可用内存、Swap、API 限额与响应时间；每个群始终最多一个回答，图片排版还会单独串行。

## 人设

编辑 `system-prompt.txt`，运行：

```bash
python3 scripts/configure.py persona --update
```

会先备份原人设。默认温和、简短、偶尔轻微拌嘴；没有内置任何真实个人的身份、学校或关系。姓名和唤醒词可以自行设置，但不要把昵称当作管理员身份凭证。
