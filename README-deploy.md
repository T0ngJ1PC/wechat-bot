# Docker 部署说明

## 1) 准备环境

```bash
mkdir -p /opt/wechat-bot/stickers /opt/wechat-bot/verify
cp .env.example .env
```

## 2) 构建并启动

```bash
docker-compose build
docker-compose up -d
```

## 3) 查看日志

```bash
docker-compose logs -f web
docker-compose logs -f scraper
```
