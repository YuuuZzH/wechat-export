# WeChat Export HTTP API

本地 Flask 服务默认监听 `http://127.0.0.1:<port>`（CLI / 启动参数决定端口）。  
所有接口仅本机可用，**无鉴权**——请勿把端口暴露到公网。

SSE 进度类接口（备份、导出等）返回 `text/event-stream`，事件字段一般为 `step` / `message` / `progress` / `done`。

---

## 会话与消息

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/contacts` | 聊天会话列表 |
| GET | `/api/messages?chat_id=&page=&…` | 分页消息 |
| GET | `/api/messages/<msg_id>` | 单条消息 |
| GET | `/api/chat/<chat_id>/stats` | 会话统计 |
| GET | `/api/chat/<chat_id>/dates` | 有消息的日期 |
| GET | `/api/chat/<chat_id>/group-info` | 群信息 |
| GET | `/api/media` | 媒体文件 |
| GET | `/api/voice` | 语音 |
| GET | `/api/avatar/<username>` | 头像 |

### 示例：列会话

```http
GET /api/contacts HTTP/1.1
Host: 127.0.0.1:5000
```

---

## 搜索

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/search?q=&…` | 全文搜索 |
| GET | `/api/search/status` | 索引状态 |
| POST | `/api/search/index` | 构建 / 刷新索引 |

---

## 导出

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/export/chats?kind=all\|group\|contact&q=` | 可选会话列表（含消息数） |
| POST | `/api/export/chat` | 导出会话（SSE） |
| GET | `/api/export/download/<filename>` | 下载导出文件 |
| POST | `/api/export/wordcloud` | 词云 |
| POST | `/api/export/report` | 综合 HTML 报告 |
| POST | `/api/export/search` | 搜索结果导出 |
| POST | `/api/export/voice` | 按人导出语音 |
| POST | `/api/export/employee` | 员工报表 |

### POST `/api/export/chat` 正文示例

```json
{
  "contacts": ["wxid_xxx"],
  "format": "md",
  "keyword": "",
  "date_start": "2026-01-01",
  "date_end": "2026-12-31",
  "merge": true,
  "include_asr": false
}
```

`format` 支持：`txt` · `html` · `xlsx` · `csv` · `md` · `json` · `pgsql` · `session-pack` · `chatlab-jsonl` · `chatlab-json`。

---

## 年度 / 双人 / 群聊报告

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/wrapped/annual?year=&account=&chat=` | 年度报告；`chat` 限定会话 → 双人/群聊画像 |
| GET | `/api/wrapped/annual/meta?year=` | 卡片清单（懒加载） |
| GET | `/api/wrapped/annual/cards/<id>?year=` | 单张卡片 |

页面入口：`/wrapped` · `/duo` · `/group`。

---

## 备份与密钥

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/backup/scan` | 扫描微信数据目录 |
| POST | `/api/backup/run` | 一键备份流水线（SSE） |
| POST | `/api/backup/decrypt` | 解密 |
| POST | `/api/backup/keyscan` | 密钥扫描 |
| GET/POST | `/api/keys/*` | 密钥目录 / 校验 / 保存 |

---

## ChatLab Pull

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/pull/status` | Pull 服务状态 |
| POST | `/api/pull/start` | 启动（默认另开端口） |
| POST | `/api/pull/stop` | 停止 |

---

## 通讯录

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/address-book?kind=contacts\|official\|orphan\|groups\|all` | 联系人列表（含公众号 / 可能的历史好友） |
| GET | `/api/address-book/labels` | 标签 |
| GET | `/api/address-book/<wxid>` | 详情 |
| GET | `/api/address-book/groups` | 群 |
| GET | `/api/address-book/export` | 导出 |

---

## 说明

- 本页文档随版本更新；完整行为以源码 `src/web/routes/` 为准。
- 二次开发建议：先调 `GET /api/export/chats` 拿到 `username`，再调导出或 Wrapped。
