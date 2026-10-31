# PetLink 桥接（astrbot_plugin_petlink）

给 PetLink 桌宠客户端用的 AstrBot 侧桥接插件。
协议与数据格式沿用上游 [petlink](https://github.com/PC2005-cloud/dsh-pet) 项目。

它在 AstrBot 进程内起一个小 HTTP 服务，把桌宠发来的文本转发到 AstrBot 自带的
OpenAPI（`POST /api/v1/chat`），再把 SSE 流原样回传给桌宠。

```
桌宠（Electron）
   │  HTTPS + 设备令牌
   ▼
本插件（默认 127.0.0.1:8787）
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
| `tokens` | 是 | 桌宠的设备令牌，每行 `token=username`。`username` 是它在 AstrBot 里的身份（例如 `dsh_pet`） |
| `listen_host` | 否 | 默认 `127.0.0.1`；要对外必须先配反向代理 + TLS，再改 `0.0.0.0` |
| `listen_port` | 否 | 默认 `8787` |
| `upstream_base` | 否 | 默认 `http://127.0.0.1:6185` |
| `probe_path` | 否 | 鉴权探针路径，默认 `/api/v1/configs`。必须是**需要鉴权**的接口，见下文「鉴权探针」 |
| `session_prefix` | 否 | 会话前缀，默认 `pet:`；防止与 WebChat 的 session_id 撞车 |
| `max_concurrent` / `max_pending` | 否 | 同时转发与排队的上限；在飞+排队超限直接 429 `busy` |
| 其余 | 否 | 限流、超时、心跳、开关，见插件设置页说明 |

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
curl.exe -s "http://127.0.0.1:8787/health?refresh=1"

# 2) 对话：应看到 SSE 逐字返回
curl.exe -N -X POST http://127.0.0.1:8787/chat `
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
  "identity_mode": "token",
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

- **身份优先级**：`identity_mode=owner` 时永远用 `owner_id`，客户端传的 `username` 无效；
  `identity_mode=token` 且 `allow_client_identity=true` 时，客户端才能选身份（默认关闭）。

> ⚠️ **身份冒充风险（不是普通隐私开关）**：`allow_client_identity=true` 时，任何拿到设备令牌的人
> 都能把 `username` 改成任意用户，从而让 AstrBot 以那个人的身份读写历史。只有在你完全信任
> 令牌持有人、且接受这个风险时才开启。
> owner 的桶额外有保护：token 模式下即使开启客户端身份，`username == owner_id` 也会被拒绝。
> 比较前会做 NFKC + 去空白/零宽 + `casefold`；**代价是只差大小写的两个名字会被当成同一身份**，
> 这是为了防止 `MQ-xxx`、全角数字等变形绕过 owner 保护。

- WebChat/OpenAPI 不校验账号是否存在，两种身份都能用，区别只在记忆落点。
- **owner 模式不会改 `session_prefix`**：同一个你在 Iris 里会同时看到 `umo=你的QQ` 和
  `pet:xxx` 两个会话。桶是否合并取决于 Iris 用 `user_id` 还是 `platform/session` 做键。
- **桥只能决定「以谁的身份进去」，不能替 Iris 合并两个桶。**
- **session_id 一律加 `pet:` 前缀**（可配置），避免和 WebChat 会话撞车；它只是标签，不会让 Iris 认亲。

### 记忆桶怎么连

1. 先试 `identity_mode=owner` + `owner_id=你的QQ号`（纯配置，零代码）；
2. 发一句话，再在 QQ 里问它记不记得 —— 记得，说明桶按 user_id 走，问题解决；
3. 仍不记得，说明 Iris 的桶键包含 platform/session，桥这层无解，得在 Iris 侧加身份别名或补丁。

**实测（2026-10-31，identity=dsh_pet）**：

- 同一 `session=pet:selftest-memory`：第一轮「我叫阿测」，第二轮问「我叫什么」答出「阿测」→ 短上下文按 session 保留；
- 换 `session=pet:selftest-memory-2` 仍答出「阿测」→ Iris 长期记忆按身份/平台桶走，不随 session 隔离。

所以：要连续对话就保持同一个 session；要隔离记忆就换 username/身份，别只换 session。

---

## 升级行为变更（0.1.11 起）

- `allow_client_identity` 的默认值从 `true` 改为 `false`；
- 旧配置如果没有显式保存过这一项，升级后客户端「QQ号模式/自定义模式」会静默失效，
  回退到 `tokens` 右边那个身份；
- 启动日志会提示：

  ```
  客户端身份覆盖已关闭（默认 false）；旧版依赖它的请显式开启 allow_client_identity=true
  ```

- 继续用客户端选身份：显式打开 `allow_client_identity`，并接受上面的冒充风险；
- 更安全的做法：`identity_mode=owner` + 非空 `owner_id`，客户端无法覆盖；
- **`owner_id` 为空时**：启动告警并回退 `tokens` 身份，**记忆会落到令牌对应的人身上**，
  不再是 owner。

---

## 安全默认值

- 默认只监听 `127.0.0.1`，公网访问必须自己加反向代理与 TLS；
- 没有令牌就拒绝所有请求，不存在「空令牌放行」；
- 身份默认不允许客户端自报（`allow_client_identity=false`）；`identity_mode=owner` 时客户端永远改不了身份；
- 默认不记录对话正文（`log_content=false`）；
- 日志里令牌只出现 SHA-256 前 8 位指纹；
- 探针只发 GET，不写数据、不调模型。

> 不要把 `upstream_key` 写进桌宠、前端或 Git。桌宠只需要 `baseUrl` + 设备令牌。

---

## 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动日志 `端口绑定失败` | 8787 被占用 | 改 `listen_port`，或关掉占用的进程 |
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
- `identity_mode=owner` 只是把身份切成你，**能否连通记忆桶取决于 Iris 的键**；
  桥这层无法替 Iris 合并。
- 探针的 403 语义依赖 AstrBot 的 scope 划分；换版本后请重新看一次 `/health`。
- `session_id` 超过 200 字符时，先截断原始 id 再加 `pet:` 前缀，前缀不会被截掉。
- 限流器每 256 次调用（或 key 数超过 4096）清理一次 10 分钟无活动的记录，设备很多时不会无限增长。

---

## 许可

代码 MIT。插件内不含任何动画素材；dsh-pet 的动画素材另有授权限制，使用时请遵守原项目说明。
