# 基于华为云 ECS 的微信公众号表情包机器人部署实践

> 本文是一份面向华为云论坛/开发者社区的案例分享稿，采用“场景介绍、技术方案、实操步骤、问题排查、经验总结”的写法。案例以 `/opt/wechat-bot` 项目为基础，演示如何将一个微信公众号表情包保存机器人部署到云服务器，并通过 Docker Compose 管理 Web 回调服务与后台抓取服务。

## 一、案例背景

日常使用微信时，很多有趣的表情包会散落在聊天记录或收藏表情里。手动保存、分类和转发这些图片并不方便，尤其是微信公众号接收图片、收藏表情和私信消息时，还会遇到不同消息类型、图片临时地址、登录态失效等问题。

本案例希望实现一个轻量级机器人：

- 用户把图片或表情发给公众号后，系统自动保存到服务器目录。
- 如果公众号收到的是“暂不支持的收藏表情”提示，后台任务会记录待处理消息，并尝试从公众号私信页面抓取原图。
- 保存成功后，机器人返回可访问的下载链接。
- 服务部署在云服务器上，使用容器方式运行，便于重启、迁移和排障。

从论坛案例分享角度看，这类项目适合作为“云上开发项目分享”或“上云技术实践”：业务场景足够小，但包含云服务器、容器部署、Webhook 回调、文件持久化、后台任务和运行监控等完整工程要素。

## 二、方案选型

### 1. 云上资源

本案例建议使用华为云弹性云服务器 ECS 承载应用。ECS 的定位是提供可按需创建和管理的云服务器，适合部署 Web 应用、后台任务、自动化脚本等轻量服务。

建议资源配置如下：

| 模块 | 建议配置 | 说明 |
| --- | --- | --- |
| 服务器 | 华为云 ECS，Linux 系统 | 用于运行 Flask 服务和后台抓取任务 |
| 公网访问 | 弹性公网 IP + 域名 | 微信公众号回调需要公网可访问地址 |
| 安全组 | 放通 80/443，按需放通 5000 | 生产环境建议由 Nginx/Caddy 反代到 5000 |
| 存储目录 | `/opt/wechat-bot/stickers` | 保存用户发送的图片或表情包 |
| 部署方式 | Docker Compose | 同时管理 Web 服务与后台 scraper 服务 |

### 2. 应用架构

项目当前由两个核心进程组成：

| 服务 | 入口 | 作用 |
| --- | --- | --- |
| `web` | `app.py` | 提供微信公众号服务器校验、消息接收、表情保存、管理状态接口 |
| `scraper` | `mp_private_scraper.py watch` | 监听待处理的收藏表情消息，登录公众号后台并尝试抓取原图 |

整体链路如下：

```text
微信用户
  -> 发送图片/表情到公众号
  -> 微信服务器回调 /wechat
  -> Flask 校验签名并解析 XML
  -> 图片下载到 /opt/wechat-bot/stickers
  -> 返回下载链接

收藏表情无法直接解析时：
微信回调
  -> 写入 pending_unsupported.jsonl
  -> scraper 后台轮询公众号私信
  -> 抓取图片并保存
  -> 更新状态文件
```

## 三、核心实现拆解

### 1. 微信回调校验

微信公众号服务器配置需要校验 `signature`、`timestamp`、`nonce` 和 `echostr`。项目中的 `check_sig()` 会按微信规则对 token、timestamp、nonce 排序后计算 SHA1，再和微信传入的签名做安全比较。

关键点：

- Token 必须和公众号后台配置一致。
- 回调路径建议使用 `/wechat`。
- 若启用安全模式，需要同时配置 `WX_APPID` 和 `WX_AES_KEY`。

示例环境变量：

```env
WX_TOKEN=替换为公众号后台Token
WX_APPID=替换为公众号AppID
WX_AES_KEY=替换为EncodingAESKey
BASE_URL=https://example.com/stickers
ADMIN_TOKEN=替换为管理接口Token
STICKER_DIR=/opt/wechat-bot/stickers
PENDING_FILE=/opt/wechat-bot/pending_unsupported.jsonl
MP_QR_FILE=/opt/wechat-bot/mp_login_qr.png
MP_STATUS_FILE=/opt/wechat-bot/mp_scraper_status.json
```

### 2. 图片和表情保存

当消息类型是 `image` 或 `emoji` 时，服务会读取图片地址并下载到 `STICKER_DIR`。保存时根据响应头或内容类型识别扩展名，常见格式包括 JPG、PNG、GIF、WEBP。

返回给用户的是一个公开访问链接，链接前缀来自：

```text
BASE_URL + "/" + 文件名
```

这里需要注意两点：

- `BASE_URL` 必须是外网可访问地址，否则用户收到链接后无法下载。
- `stickers` 目录需要做持久化挂载，避免容器重建后文件丢失。

### 3. 收藏表情的异步处理

部分收藏表情并不会直接以图片消息形式进入公众号回调，可能只收到“不支持的消息类型”文本。项目的处理方式是：

1. 将这类消息写入 `pending_unsupported.jsonl`。
2. `scraper` 服务周期性读取待处理消息。
3. 通过 Playwright 登录公众号后台私信页面。
4. 从私信 DOM 和图片请求中识别候选图片。
5. 下载成功后写入状态文件，方便排查。

这个设计的优点是不会阻塞公众号回调。即使后台抓取需要等待登录态、页面加载或重试，Web 服务仍然可以快速响应微信服务器。

## 四、部署步骤

### 1. 准备服务器目录

登录 ECS 后创建项目目录：

```bash
mkdir -p /opt/wechat-bot/stickers /opt/wechat-bot/verify
cd /opt/wechat-bot
```

将代码放入该目录后，复制环境变量模板：

```bash
cp .env.example .env
```

然后编辑 `.env`，至少配置：

- `WX_TOKEN`
- `WX_APPID`
- `WX_AES_KEY`
- `BASE_URL`
- `ADMIN_TOKEN`

生产环境不要把真实 `.env`、`private.env`、登录态文件和 access token 缓存提交到代码仓库。

### 2. 启动容器

项目已经提供 `docker-compose.yml`，可以直接构建并启动：

```bash
docker-compose build
docker-compose up -d
```

启动后查看服务状态：

```bash
docker-compose ps
docker-compose logs -f web
docker-compose logs -f scraper
```

Web 服务默认监听容器内 5000 端口，并映射到宿主机：

```yaml
ports:
  - "5000:5000"
```

如果生产环境使用 Nginx 或 Caddy，建议将公网 HTTPS 流量反向代理到 `127.0.0.1:5000`。

### 3. 配置微信公众号服务器地址

在公众号后台配置服务器地址：

```text
https://你的域名/wechat
```

同时填入：

- Token：对应 `.env` 中的 `WX_TOKEN`
- EncodingAESKey：对应 `.env` 中的 `WX_AES_KEY`
- 消息加解密方式：按实际需求选择明文、兼容或安全模式

保存配置时，可以观察 `web` 服务日志。如果签名校验通过，会看到类似：

```text
VERIFY OK
```

### 4. 登录公众号后台供 scraper 使用

后台抓取服务依赖公众号后台登录态。项目提供了管理接口查看二维码和状态：

```text
GET /admin/mp-qr?token=你的ADMIN_TOKEN
GET /admin/mp-status?token=你的ADMIN_TOKEN
```

如果 `scraper` 提示需要登录，可以访问二维码接口，用管理员微信扫码。登录成功后，状态会写入：

```text
/opt/wechat-bot/mp_state.json
/opt/wechat-bot/mp_scraper_status.json
```

这些文件同样需要持久化保存。

## 五、常见问题与处理

### 1. 微信公众号校验失败

现象：

```text
VERIFY FAIL
POST VERIFY FAIL
```

排查方向：

- 检查公众号后台 Token 是否和 `.env` 的 `WX_TOKEN` 一致。
- 检查公网 URL 是否真实访问到当前服务。
- 检查反向代理是否转发了 query string。
- 如果启用安全模式，确认 `WX_APPID`、`WX_AES_KEY` 是否完整。

### 2. 用户收到链接但打不开

排查方向：

- `BASE_URL` 是否设置为公网域名，而不是内网地址。
- `/stickers/<filename>` 是否能直接访问。
- 安全组、防火墙、反向代理是否放通对应路径。
- 容器内保存路径和宿主机挂载路径是否一致。

### 3. 收藏表情没有抓取成功

排查方向：

- `pending_unsupported.jsonl` 是否有新增记录。
- `scraper` 容器日志是否提示登录过期。
- `/admin/mp-qr` 是否能看到最新二维码截图。
- `mp_scraper_status.json` 中的 `updated_at` 是否持续更新。
- 公众号后台页面结构可能变化，DOM 识别逻辑需要随页面调整。

### 4. 容器重启后文件丢失

需要确认 `docker-compose.yml` 中已经挂载持久化目录和状态文件：

```yaml
volumes:
  - ./stickers:/opt/wechat-bot/stickers
  - ./mp_state.json:/opt/wechat-bot/mp_state.json
  - ./mp_scraper_state.json:/opt/wechat-bot/mp_scraper_state.json
  - ./mp_scraper_status.json:/opt/wechat-bot/mp_scraper_status.json
  - ./pending_unsupported.jsonl:/opt/wechat-bot/pending_unsupported.jsonl
```

## 六、实践效果

完成部署后，用户侧的体验比较直接：

1. 给公众号发送图片或表情。
2. 机器人自动保存文件。
3. 公众号回复下载链接。
4. 对于收藏表情，后台服务异步尝试抓取，不影响主回调。

工程侧的收益也比较明显：

- Web 回调和后台任务拆成两个容器，职责清晰。
- 状态文件、待处理队列和图片目录都落在宿主机，便于备份。
- 通过 Docker Compose 管理启动顺序、重启策略和挂载目录，迁移成本较低。
- 日志集中在 `docker-compose logs` 中，排障路径简单。

## 七、可优化方向

后续可以继续补强以下能力：

- 将 `pending_unsupported.jsonl` 替换为 SQLite 或 Redis，增强并发和状态管理。
- 为 `/admin/*` 接口增加 IP 白名单或更严格的鉴权策略。
- 接入 OBS 对象存储，把表情文件从本地磁盘迁移到云上存储。
- 增加 Prometheus 或轻量健康检查，监控回调成功率、下载失败率和 scraper 登录状态。
- 在部署层增加 HTTPS 自动证书续期，降低公众号回调配置维护成本。
- 对重复图片做 hash 去重，避免长期运行后磁盘膨胀。

## 八、总结

这个案例的核心不是复杂算法，而是把一个真实的小需求做成稳定的云上服务：公网回调、消息校验、文件保存、后台补偿、容器部署和日志排障都具备了。对于个人开发者或小团队来说，这类项目很适合用来练习“从脚本到服务”的完整上云流程。

如果发布到华为云论坛，建议标题使用：

```text
〖案例共创〗基于华为云 ECS 的微信公众号表情包机器人部署实践
```

推荐发帖板块可选择“社区活动/案例共创”或与云服务器、云原生、开发实践相关的技术板块，并在正文中补充部署截图、公众号回调配置截图、容器日志截图和实际回复效果截图。

## 参考资料

- 华为云论坛：<https://bbs.huaweicloud.com/forum/topicpost>
- 华为云案例共创活动说明：<https://bbs.huaweicloud.com/blogs/443275>
- 华为云 ECS 最佳实践汇总：<https://support.huaweicloud.com/bestpractice-ecs/ecs_bp_0001.html>
- 本地项目部署说明：`/opt/wechat-bot/README-deploy.md`
- 本地项目入口：`/opt/wechat-bot/app.py`
- 本地后台抓取服务：`/opt/wechat-bot/mp_private_scraper.py`
