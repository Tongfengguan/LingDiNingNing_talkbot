# 🌸 NingNing-Bot（绫地宁宁 AI 助手）

基于 Python 3.13、FastAPI、DeepSeek 和 NapCatQQ 的个人 QQ 私聊机器人。支持角色对话、按用户隔离的长期记忆、共享知识库、图片理解、B 站热门、Codeforces 查询与需要确认的邮件草稿。

## 安装与升级

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

已有安装直接运行第二条命令。升级前停止旧进程并备份 `data/`；新版本会在首次写入时把旧向量数据升级为带来源、章节、页码、分组与检索分数的版本 3 格式，旧代码不能读取该格式。不要同时启动多个实例或设置多个 Uvicorn workers：向量缓存、消息顺序、主动任务与邮件草稿由单个进程管理。

本次更新不修改已有 `.env` 或 NapCat 配置。安全设置未完成时程序会拒绝启动。

## 必需配置

参考 [.env.example](.env.example)，在项目根目录配置 `.env`。已有文件请补齐字段，不要覆盖密钥。

| 变量 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 对话模型 API 密钥 |
| `DASHSCOPE_API_KEY` | 可选；向量检索、知识库索引与视觉模型需要它 |
| `ADMIN_QQ` | 管理员 QQ；邮件确认与定时推送仅对该用户开放 |
| `ALLOWED_QQ` | 逗号分隔的授权 QQ。省略时仅允许 `ADMIN_QQ`；显式留空则拒绝所有用户 |
| `WEBHOOK_SECRET` | 必需，至少 32 字符；与 NapCat **HTTP 客户端的 token** 一致 |
| `NAPCAT_URL` | NapCat HTTP 服务端地址，默认 `http://localhost:3000` |
| `NAPCAT_TOKEN` | 与 NapCat **HTTP 服务端的 token** 一致，用于发送 QQ 消息 |
| `DASHBOARD_TOKEN` | 可选，至少 32 字符；留空关闭控制台 |
| `BOT_HOST` / `BOT_PORT` | 默认 `127.0.0.1:8080` |

各用途使用独立随机密钥，可分别执行下面的命令生成，然后写入对应配置：

```powershell
.\venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
```

邮件配置可选：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`RECEIVER_EMAIL`。465 使用 TLS，其他配置端口使用 STARTTLS；证书校验开启。邮件只投递到固定的 `RECEIVER_EMAIL`。

## NapCat 与运行

1. 在 NapCat 开启 **HTTP 服务端**，建议监听 `127.0.0.1:3000`，设置 token 并填入项目的 `NAPCAT_TOKEN`。
2. 添加 **HTTP 客户端**事件上报，消息格式选择 `array`，关闭自身消息上报，将 token 设置为项目的 `WEBHOOK_SECRET`。
3. 同机部署时，上报 URL 可直接使用 `http://127.0.0.1:8080/qq/webhook`，不需要公网隧道。
4. 启动机器人：

```powershell
.\venv\Scripts\python.exe main.py
```

NapCat 在 HTTP 客户端设置 token 后，会为正文生成 `X-Signature: sha1=...`。本项目按原始请求字节验证 HMAC-SHA1，并校验授权用户、事件时间与消息 ID。可核对 [NapCat 官方 HTTP 客户端实现](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/network/http-client.ts)。

确需跨机器回调时，可使用 `start.bat` 启动 Cloudflare 隧道；先调整脚本中的 `CF_PATH`，再将 HTTPS 域名加 `/qq/webhook` 填入 NapCat。脚本读取 `BOT_PORT` 并在安全配置检查通过后启动。控制台即使经过隧道也需要独立登录；建议在边缘代理进一步限制只公开回调路径。公网访问必须使用 HTTPS。

健康检查：`GET /healthz`，就绪后返回 200。控制台：`http://127.0.0.1:8080/dashboard/`，浏览器登录用户名为 `admin`，密码为 `DASHBOARD_TOKEN`。控制台可以管理知识文档、对话与长期记忆、图片资源和主动任务。所有修改接口都需要登录和同源管理请求标记；删除操作在页面中会再次确认。

## 管理控制台

- **运行概览**：查看消息数、文档和记忆片段数、图片占用、任务队列与已启用任务。
- **知识库**：上传、重新索引、分组或删除文档。上传成功后立即索引，后台仍会每五分钟检查磁盘上的变更。
- **对话与记忆**：按用户、类型和关键词筛选，查看原始对话，删除单条记录或清空某个用户的聊天数据。
- **图片库**：预览图片，修改机器人调用名称，清理不再使用的图片。
- **主动任务**：设置时区、免打扰时段、每日执行时间、最小发送间隔、提醒内容，并可立即试运行。

控制台采用单页界面，桌面和手机浏览器均可使用。它面向受信任的管理员，仍应只监听本机或放在带 HTTPS 和访问控制的反向代理之后。

## 功能与指令

| 指令 / 操作 | 行为 |
| --- | --- |
| 直接私聊 | 对话；按意图搜索或查知识库，并检索当前用户自己的长期记忆 |
| `/help`、`/h` | 帮助 |
| `/cf 用户名` | 查询 Codeforces |
| 随图片发送 `/学这个 名字` | 下载、解码检查并保存表情；名称禁止路径字符 |
| 发送图片 | 安全下载后交给视觉模型分析 |
| 询问 B 站热门 | 视频数据、可安全下载的封面与点评 |
| “发邮件给我……” | 管理员生成包含收件人、主题和全文的草稿，尚不发送 |
| `/确认邮件 确认码` | 10 分钟内确认当前草稿；每份草稿只尝试投递一次 |
| `/取消邮件` | 取消当前草稿 |
| `/清除记忆` | 删除当前用户的 SQLite 聊天与向量记忆，并取消草稿；共享文档保留 |
| `/主动消息 状态` | 管理员查看日常问候、B 站推送、每日回顾和自定义提醒的状态 |
| `/主动消息 开启`、`/主动消息 关闭` | 管理员快速开启或关闭日常问候与 B 站推送；完整设置在控制台修改 |

SMTP 超时可能发生在邮件服务器已经接收之后，因此失败或不确定结果不会自动重发，应先检查收件箱。草稿仅保存在进程内，重启后失效。模型生成的旧 `ACTION` 或 `CQ` 字符串不能直接执行操作。

## 知识库、图片与记忆

- `assets/docs/` 支持 PDF、UTF-8 TXT/Markdown、DOCX、Python、JavaScript、TypeScript、JSON、YAML 和 CSV。DOCX 不做图片 OCR，扫描 PDF 需要先转为可提取文本。
- Markdown 按标题与段落、代码按类和函数、PDF 按页切分；检索结果保留文件名、章节、页码、分组和相关度，回答知识库问题时会一并提供这些来源信息。
- 检索综合向量相似度、BM25 风格关键词分数和轻量时效分。`DASHSCOPE_API_KEY` 未配置或向量服务暂时不可用时，文档和聊天记忆仍会建立关键词索引，不会中断本地检索。
- 文档在启动时和每隔五分钟同步，按内容哈希识别修改，并清理被删除文档的索引。控制台中的分组用于整理和检索过滤；当前 QQ 问答默认检索全部文档分组。
- 文档库对 **所有 ALLOWED_QQ 用户共享**。聊天记忆按 QQ 隔离；不要把只允许某个用户访问的材料放入共享目录。向量化文本、检索问题和图片会发送到配置的模型服务。
- 文档默认限制单个 20 MB、100 万字符，最多 200 个文件；每个用户保留最近最多 2000 个聊天向量块。SQLite 历史保留到主动清除，提示词另有条数和字符预算。
- 图片只下载公开 HTTP(S) 的 80/443 端口，不使用环境代理、不跟随跳转，固定连接已校验的公网 IP。限制下载 5 MB、1600 万像素，验证后转为 JPEG；新学习的动画只保留第一帧。
- 图片以名称哈希保存，兼容旧目录内的具名图片。共享图片库上限为 256 MB，满后拒绝新增；可手动清理不用的图片。
- `/清除记忆` 不处理外部模型服务留存、手工备份或旧版本已经写入的日志。新代码不记录聊天正文和视觉分析正文；日志保留七天。

## 主动任务

主动任务配置保存在 `data/automations.json`，通过临时文件原子替换，重启后继续生效。默认时区是 `Asia/Shanghai`，免打扰时段为 23:00–08:00；开始和结束相同时表示关闭免打扰。

内置四类任务：日常问候、B 站热门、每日回顾和自定义提醒。每项任务都可设置多个 `HH:MM` 执行时间和最小发送间隔。每日回顾只在过去 24 小时存在对话时生成；用户超过 3 天没有活动后，日常问候最多每天一次，超过 7 天后最多三天一次。控制台的“立即执行”会忽略开关、免打扰和最小间隔，便于验证内容。

消息队列最多等待 64 条，4 个工作任务，同一用户依次处理；单用户每分钟最多接收 12 条。回调超过 64 KB 或消息超过 8000 字符会拒绝。事件时间允许五分钟偏差，机器应保持时间同步。

重复事件记录写入 SQLite，但工作队列不做故障恢复。进程异常退出时，尚未完成的已接收消息可能丢失；发送失败也不会自动重复执行有副作用的操作。此版本面向个人单实例部署。

## 开发验证

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m pip check
.\venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\venv\Scripts\python.exe -m pip_audit --local
```

回归测试使用临时文件、假配置、模拟 HTTP/SMTP，不读取真实 `.env`，不调用付费模型或发送真实消息。GitHub Actions 配置了 Windows/Linux、Python 3.13 的测试任务。

## 项目结构

```text
engine/
  security.py       # 回调验签、控制台认证、请求体限制
  dispatcher.py     # 队列、限流、每用户顺序、事件去重
  aiEngine/         # 对话、参考资料、邮件草稿生成
  qqAdapter/        # OneBot 消息解析和结构化消息发送
  memoryManager/    # SQLite 历史和去重记录
  ragEngine/        # 隔离检索和原子向量存储
  pdfEngine/        # 文档提取与增量同步
  scheduleManager/  # 主动任务配置、持久化和动态调度
  imageUtils/       # 安全图片处理与视觉模型
  emailEngine/      # 待确认草稿和 SMTP
  dashboard/        # 需登录的控制台
tests/              # 离线回归测试
assets/             # 文档和图片
data/               # 本地数据，不提交 Git
config.py           # 配置与启动校验
main.py             # 服务生命周期和定时任务
```
