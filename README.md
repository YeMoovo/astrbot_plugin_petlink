# PetLink 桥接（astrbot_plugin_petlink）

给 PetLink 桌宠客户端用的 AstrBot 侧桥接插件。
协议与数据格式沿用上游 [petlink](https://github.com/PC2005-cloud/dsh-pet) 项目。

它在 AstrBot 进程内起一个小 HTTP 服务，把桌宠发来的文本转发到 AstrBot 自带的
OpenAPI（`POST /api/v1/chat`），再把 SSE 流原样回传给桌宠。

> **0.2.1 起默认监听端口改为 `18787`**，与客户端默认的 `http://127.0.0.1:18787` 一致，
> 本机使用不再需要端口映射。旧配置里的 `8787` 需要改成 `18787`，或改客户端地址。
>
> **0.2.2** 安全加固：`/balance` 纳入同一限流与并发控制，令牌使用常量时间比较，
> 纯符号/emoji 会话名不再回退成同一个 `pet:---` 桶。
>
> **0.2.3**：`/balance` 使用独立限流桶，不挤占 `/chat` 额度；新增 `session_max_chars` 配置；
> 明确 `device_token` 会覆盖多行 `tokens`，只支持单设备。
>
> **0.2.4**：移除 `feature_debug_dump` 临时目录落盘功能；logger 只从 `astrbot.api` 导入，
> 移除内置 `logging` 回退，符合插件市场上架规范。
>
> **0.2.5**：身份由客户端决定，插件移除 `identity_mode` / `owner_id` / `allow_client_identity` 配置；
> 健康检查对仅有 chat scope 的 Key 不再判死，测试连接可直接通过。
>
> **0.2.6**：`/health` 增加 `token_ok`；带设备令牌请求时会校验令牌，客户端测试连接可发现令牌不一致。

```
桌宠（Electron）
   │  HTTPS + 设备令牌
   ▼
本插件（默认 127.0.0.1:18787）
   │  HTTP + SSE → 127.0.0.1:6185/api/v1/chat
   ▼
AstrBot（大肥鱼 + 记忆/人格，全部照常生效）
```

桌宠只持有**可吊销的设备令牌**，永远拿不到 `abk_` 开头的上游 API Key。

---

## 安装

1. 把本目录打包成 zip（压缩包根目录必须是 `astrbot_plugin_petlink/`）；
2. AstrBot 面板 → 插件管理 → 安装插件 → 选择 zip；
3. 安装后**重载插件或重启 AstrBot**（插件在进程内起服务，首次需要初始化）；
4. 打开插件配置，按下节填写。

要求：AstrBot 4.29.x（OpenAPI 与插件 API 均按该版本实测）。

---

## 配置

| 字段 | 必填 | 说明 |
|---|---|---|
| `upstream_key` | 是 | AstrBot API Key，`abk_` 开头。在面板里签发，scope 只勾 **chat**（不要 provider） |
| `tokens` | 否 | 多设备令牌，每行 `token=username`；`username` 是它在 AstrBot 里的身份（例如 `dsh_pet`） |
| `device_token` | 否 | 单设备令牌；**填了会完全覆盖 `tokens` 多行**，只支持单人连接 |
| `device_username` | 否 | `device_token` 对应的身份，默认 `dsh_pet` |
| `listen_host` | 否 | 默认 `127.0.0.1`；要对外必须先配反向代理 + TLS，再改 `0.0.0.0` |
| `listen_port` | 否 | 默认 `18787` |
| `upstream_base` | 否 | 默认 `http://127.0.0.1:6185` |
| `probe_path` | 否 | 鉴权探针路径，默认 `/api/v1/configs`。必须是**需要鉴权**的接口，见下文「鉴权探针」 |
| `session_prefix` | 否 | 会话前缀，默认 `pet:`；防止与 WebChat 的 session_id 撞车 |
| `session_max_chars` | 否 | 加前缀后的会话 ID 最大长度，默认 `200` |
| `max_concurrent` / `max_pending` | 否 | 同时转发与排队的上限；在飞+排队超限直接 429 `busy` |
| 其余 | 否 | 限流、超时、心跳、开关，见插件设置页说明 |

> `device_token` 是单设备模式：只要填了它，`tokens` 里的多行会被完全忽略。需要多设备时请清空 `device_token`，只维护 `tokens`。

`tokens` 示例：

```
# 一行一个设备；换令牌就是换一行
petlink-demo-token=dsh_pet
```

没有配置 `tokens` 时，`/chat` 会返回 `503 tokens_not_configured`，**不会放行**。

---

## 验证

在 AstrBot 所在机器上执行（PowerShell）：

```powershell
# 1) 探活：ok=true 表示服务在跑；ready=true 才表示 Key 与令牌都可用
curl.exe -s "http://127.0.0.1:18787/health?refresh=1"

# 2) 对话：应看到 SSE 逐字返回
curl.exe -N -X POST http://127.0.0.1:18787/chat `
  -H "Authorization: Bearer petlink-demo-token" `
  -H "Content-Type: application/json" `
  -d '{\"message\":\"在吗\",\"session_id\":\"selftest-1\"}'
```

期望：

- `/health`：`ok=true`、`ready=true`、`upstream.auth_state="ok"`；
- `/chat`：`data: {"type":"session_id",...}`、若干 `data: {"type":"plain",...}`、最后 `data: {"type":"end"}`。

`/health?refresh=1` 会跳过 5 秒缓存，强制重新探测；换 Key 之后用它确认。

---

## 鉴权探针（重要）

`/health` 的 `auth_ok` **必须**建立在一个需要鉴权的接口上。默认用 `GET /api/v1/configs`，
并且**带 Key 与不带 Key 各请求一次**：

| 探测结果（带 Key） | 匿名对照 | `auth_state` | `auth_ok` | 含义 |
|---|---|---|---|---|
| 200 | 401/403 | `ok` | true | 接口确实要鉴权，Key 有效 |
| 200 | 200 | `public_endpoint` | null | 不鉴权也能 200，证明不了 Key，**不算通过** |
| 200 | 网络失败/其他 | `unverified` | null | 无法用匿名请求对照，不判定 |
| 401 | — | `invalid_key` | false | Key 错误、过期或被吊销 |
| 403 | — | `scope_denied` | null | Key 有效，但 scope 不够；**不判定为失效** |
| 其他 | — | `unexpected` | null | 路径不存在或返回异常，无法判定 |
| 连接失败 | — | `unreachable` | null | 上游没起 / 地址不对 |

`/health` 同时返回 `auth_hint`，把状态翻成一句人话：

- `public_endpoint` → 「探测路径不需要鉴权，证明不了 Key；请换 probe_path」
- `unverified` → 「匿名对照请求失败，无法确认路径是否需要鉴权」

两者都**不算通过**，但也不该把 `/health` 弄成红色错误；用户看到 `auth_ok=null` 时先看这句提示。

**自动降级（默认关闭）**：`probe_fallback_path` 默认为空。只有它非空、且主探针返回
403/404 时才会再试一次；命中 200 为 `ok`，返回 401 立即判定 `invalid_key`。
备用路径（例如 `/api/v1/chat/sessions?username=__dsh_pet_probe__`）**是否只读、会不会留下空会话
尚未实测**，所以默认不启用；要开请先自己确认它没有副作用。

> **不要用 `/api/v1/openapi.json` 做探针。** 它没有鉴权，谁都能拿到 215KB 的 schema，
> 拿它探测会让 `auth_ok` 永远为 true —— 这是 0.1.0 的错误，0.1.1 起已改掉。
> 0.1.14 起即使换了路径，也会用匿名对照兜住「路径其实是公开的」这种情况。

`ready` 的定义（0.1.3 起收紧）：服务已启动 **且** 配置了 tokens **且** 上游可达 **且**
`auth_ok is true` —— 必须被一次 200 真正证实。
`degraded=true` 表示「可达但证实不了」（403/unexpected），默认 `ready=false`；
只有显式打开 `allow_unverified` 才会在 degraded 时也放行，等于自己承担「Key 可能没权限」的风险。

探针结果缓存 5 秒，`probe_at` / `probe_age_sec` 会一并吐出，**不要把它当成实时值**；
换 Key 或排查时用 `?refresh=1` 强制重探。

---

## 接口

### `GET /health`

```json
{
  "ok": true,
  "ready": true,
  "auth_verified": true,
  "degraded": false,
  "identity_mode": "client",
  "version": "0.1.5",
  "probe_at": 1793386000,
  "probe_age_sec": 1,
  "upstream": {
    "reachable": true,
    "auth_ok": true,
    "auth_state": "ok",
    "status": 200,
    "path": "/api/v1/configs",
    "attempts": [
      {"path": "/api/v1/configs", "status": 200, "state": "ok", "auth_ok": true}
    ]
  },
  "configured": true,
  "started_at": 1793386000
}
```

### `POST /chat`

请求头：`Authorization: Bearer <设备令牌>`（也接受 `X-Pet-Token`）

```json
{
  "message": "今天有点累",
  "session_id": "可选；服务端会自动加 pet: 前缀"
}
```

响应：`text/event-stream`，原样透传 AstrBot 的 SSE；空闲超过 `heartbeat_sec` 会插入 `: ping` 注释帧。

错误码：

| 状态码 | 含义 |
|---|---|
| 400 | JSON 非法 / `message` 为空 |
| 401 | 缺少令牌或令牌未知 |
| 413 | 消息超长 |
| 429 | 触发限流，或排队超过 `max_concurrent + max_pending`（`busy`） |
| 502 | 上游不可达 / 上游非 200 |
| 503 | 未配置令牌、未配置 Key、服务未就绪 |
| 504 | 上游超时 |

---

## 身份与会话

- 身份由客户端决定：客户端的「QQ号模式 / 自定义模式」会把 `username` 一并发给插件；
- 插件只校验 `username` 格式（长度 1–64，仅字母数字与 `_ - . :`），合法就用它，缺失或非法才回退到令牌映射的身份；
- 令牌只决定这条连接是否可信，不再决定用哪个身份；
- 想和 QQ 共用记忆：客户端选 QQ号模式，填你的 QQ 号；
- 想用独立桌宠身份：客户端选自定义模式，填一个名字；
- `session_id` 仍会加 `pet:` 前缀，避免和 WebChat 会话撞车；它只是标签，是否共享长期记忆取决于 AstrBot/Iris 的桶键。

### 记忆桶怎么连

1. 客户端选 QQ号模式，填自己的 QQ 号；
2. 发一句话，再在 QQ 里问它记不记得；
3. 仍不记得，说明 Iris 的桶键包含 platform/session，桥这层无解，需要在 Iris 侧加身份别名或补丁。

要连续对话就保持同一个 session；要隔离记忆就换客户端身份，不要只换 session。

## 安全默认值

- 默认只监听 `127.0.0.1`，公网访问必须自己加反向代理与 TLS；
- 没有令牌就拒绝所有请求，不存在「空令牌放行」；
- 身份默认由客户端传入并校验，令牌只决定连接可信；
- 默认不记录对话正文（`log_content=false`）；
- 日志里令牌只出现 SHA-256 前 8 位指纹；
- 探针只发 GET，不写数据、不调模型。

> 不要把 `upstream_key` 写进桌宠、前端或 Git。桌宠只需要 `baseUrl` + 设备令牌。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `端口绑定失败` | 18787 被占用 | 改 `listen_port`，或关掉占用的进程 |
| `/health` 里 `upstream.reachable=false` | 6185 没起 / 地址不对 | 确认 AstrBot 面板能打开，检查 `upstream_base` |
| `auth_state=invalid_key` | Key 错、过期或被吊销 | 重新签发 `abk_` Key，改完加 `?refresh=1` |
| `auth_state=scope_denied` | 探针都 403，Key 有效但 scope 不够 | 换 `probe_path`，或启用已确认只读的 `probe_fallback_path`；此时 `ready=false`、`degraded=true` |
| `degraded=true` 但 `ready=false` | 可达但没被 200 证实 | 换一个该 Key 能读的探针路径；确认风险后可开 `allow_unverified` |
| `ready=false` 但 `ok=true` | 没配 tokens、Key 无效/未证实、或上游不可达 | 看 `configured`、`degraded` 与 `upstream.auth_state` |
| `/chat` 返回 502 | 上游拒绝（多为 Key 问题） | 看 AstrBot 日志里的 401/403 |
| 桌宠收到空流 | AstrBot 对空/非法 message 返回 `200 + 0 事件` | 检查消息是否为空、是否超长 |
| 长回复被切断 | 反代超时 | 调大反代超时，或调小 `heartbeat_sec` |

---

## 已知边界

- `flags.enable_reasoning` 由 AstrBot 配置决定，传了也无效；`enable_inline_genui` 也无法稳定
  抑制 T2I。0.1.1 起两者不再下发，桌宠只消费 `plain` 帧。
- 顶层 `enable_streaming` 与 `flags.enable_streaming` 由同一个变量写出，测试断言两者永远一致；
  AstrBot 以哪个为准都不会打架。
- **SSE 透传契约**：relay 不解析、不重写帧。桌宠客户端只累加 `type=plain`，遇到 `end` 结束；
  **未知类型（`reasoning`、`image`、`attachment_saved` 等）一律跳过，不得报错**。
  测试里已放入一条 `reasoning` 帧，验证它原样透传且不崩溃。
- `probe_fallback_path` 的只读性**未实测**（可能创建空会话），所以默认关闭；
  启用前请先用数据库快照确认。
- 身份由客户端传入，**能否连通记忆桶取决于 Iris 的键**；
  桥这层无法替 Iris 合并。
- 探针的 403 语义依赖 AstrBot 的 scope 划分；换版本后请重新看一次 `/health`。
- `session_id` 超过 200 字符时，先截断原始 id 再加 `pet:` 前缀，前缀不会被截掉。
- 限流器每 256 次调用（或 key 数超过 4096）清理一次 10 分钟无活动的记录，设备很多时不会无限增长。

---

## 许可

代码 MIT。插件内不含任何动画素材；dsh-pet 的动画素材另有授权限制，使用时请遵守原项目说明。
