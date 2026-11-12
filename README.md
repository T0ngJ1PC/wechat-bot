# WeChat Bot

一个基于 Flask 和 `wechatpy` 的微信表情包保存机器人：用户向公众号发送图片或表情后，机器人会保存图片并返回下载链接；对暂不支持的消息类型，可配合公众号后台抓取服务继续处理。

## 功能

- 支持微信公众平台消息签名校验和 AES 加密消息。
- 接收图片、表情消息并保存到本地 `stickers` 目录。
- 返回可访问的表情包下载链接。
- 提供管理接口查看公众号登录二维码和抓取状态。
- 提供 Docker Compose 部署配置，包含 Web 服务和抓取服务。

## 快速开始

### 1. 准备配置

```bash
cp .env.example .env
```

编辑 `.env`，至少填写以下微信公众平台配置：

```dotenv
WX_TOKEN=your_wechat_token
WX_APPID=your_wechat_appid
WX_AES_KEY=your_encoding_aes_key
WX_APPSECRET=your_wechat_appsecret
BASE_URL=https://your-domain.example/stickers
ADMIN_TOKEN=change_this_admin_token
```

不要把真实密钥、AppSecret 或管理令牌提交到 Git。

### 2. 使用 Docker Compose 启动

```bash
mkdir -p stickers verify
docker compose build
docker compose up -d
```

查看日志：

```bash
docker compose logs -f web
docker compose logs -f scraper
```

健康检查：

```bash
curl http://127.0.0.1:5000/
```

预期返回：

```text
wechat-bot ok
```

## 微信公众平台配置

将服务器配置中的 URL 设置为：

```text
https://your-domain.example/wechat
```

Token、消息加解密方式和 EncodingAESKey 应与 `.env` 中的配置一致。服务器需要能够通过 HTTPS 访问，并将外部请求转发到容器的 `5000` 端口。

## 管理接口

管理接口需要通过查询参数 `token` 或请求头 `X-Admin-Token` 传入 `ADMIN_TOKEN`：

```bash
curl -H "X-Admin-Token: your_admin_token" \
  https://your-domain.example/admin/mp-status
```

登录二维码接口：

```text
GET /admin/mp-qr
```

## 目录说明

| 路径 | 用途 |
| --- | --- |
| `app.py` | Flask Web 服务和微信消息处理 |
| `mp_private_scraper.py` | 公众号后台抓取服务 |
| `docker-compose.yml` | Web 与抓取服务编排 |
| `stickers/` | 已保存的图片 |
| `verify/` | 抓取服务验证文件 |
| `README-deploy.md` | 简要部署命令 |

## 本地运行

安装依赖后可直接启动 Web 服务：

```bash
pip install -r requirements.txt
python app.py
```

抓取服务依赖额外依赖：

```bash
pip install -r requirements_scraper.txt
python mp_private_scraper.py watch
```

## 许可证

当前仓库未声明开源许可证。若要允许他人明确地复用或分发代码，请补充合适的许可证文件。
