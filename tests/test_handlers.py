#!/usr/bin/env python3
"""处理器级集成测试：用假的 aiohttp / web 跑通鉴权、转发、SSE、心跳、错误映射。

不依赖真实网络，也不依赖 AstrBot 运行时。
运行：python tests/test_handlers.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT

failures = 0


def check(name, condition, detail=""):
    global failures
    if condition:
        print(f"PASS  {name}")
        return
    failures += 1
    print(f"FAIL  {name}  {detail}")


# --------------------------------------------------------------------------
# 假的 aiohttp / astrbot
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload
        self.status = status
        self.headers = dict(headers or {})

    @property
    def text(self):
        return json.dumps(self.payload)


class FakeStreamResponse:
    def __init__(self, status=200, headers=None):
        self.status = status
        self.headers = dict(headers or {})
        self.written = []
        self.prepared = False
        self.eof = False

    async def prepare(self, request):
        self.prepared = True

    async def write(self, data):
        self.written.append(data)

    async def write_eof(self):
        self.eof = True


class FakeContent:
    def __init__(self, chunks, delay=0.0):
        self._chunks = list(chunks)
        self._delay = delay

    def iter_any(self):
        async def gen():
            for chunk in self._chunks:
                if self._delay:
                    await asyncio.sleep(self._delay)
                yield chunk

        return gen()


class FakeUpstream:
    def __init__(self, status=200, chunks=None, text="", delay=0.0):
        self.status = status
        self.content = FakeContent(chunks or [], delay=delay)
        self._text = text
        self.released = False

    async def text(self):
        return self._text

    def release(self):
        self.released = True


class FakeRequest:
    def __init__(self, headers=None, payload=None, origin="", query=None):
        self.headers = dict(headers or {})
        if origin:
            self.headers["Origin"] = origin
        self._payload = payload
        self.query = dict(query or {})

    async def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _AsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, post_response=None, probe_status=200, probe_statuses=None, anonymous_status=401, **kwargs):
        self._post_response = post_response
        self._probe_status = probe_status
        self._probe_statuses = dict(probe_statuses or {})
        self._anonymous_status = anonymous_status
        self.posts = []
        self.gets = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})

        async def _call():
            return self._post_response

        return _call()

    def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers})
        if not headers or "Authorization" not in headers:
            status = self._anonymous_status
        else:
            status = self._probe_status
            for fragment, value in self._probe_statuses.items():
                if fragment in url:
                    status = value
                    break
        return _AsyncCM(FakeResponse({"paths": {}}, status=status))

    async def close(self):
        pass


class FakeRouter:
    def __init__(self):
        self.routes = []

    def add_get(self, path, handler):
        self.routes.append(("GET", path, handler))

    def add_post(self, path, handler):
        self.routes.append(("POST", path, handler))

    def add_options(self, path, handler):
        self.routes.append(("OPTIONS", path, handler))


class FakeApp:
    def __init__(self, client_max_size=None):
        self.client_max_size = client_max_size
        self.router = FakeRouter()


class FakeRunner:
    def __init__(self, app, access_log=None):
        self.app = app

    async def setup(self):
        pass

    async def cleanup(self):
        pass


class FakeSite:
    def __init__(self, runner, host, port):
        self.runner = runner
        self.host = host
        self.port = port
        self.started = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.started = False


def install_stubs():
    aiohttp = types.ModuleType("aiohttp")

    class ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class TCPConnector:
        def __init__(self, *args, **kwargs):
            pass

    class ClientError(Exception):
        pass

    aiohttp.ClientTimeout = ClientTimeout
    aiohttp.TCPConnector = TCPConnector
    aiohttp.ClientSession = FakeSession
    aiohttp.ClientError = ClientError
    aiohttp.StreamReader = object

    web = types.ModuleType("aiohttp.web")
    web.Application = FakeApp
    web.AppRunner = FakeRunner
    web.TCPSite = FakeSite
    web.Request = FakeRequest
    web.Response = FakeResponse
    web.StreamResponse = FakeStreamResponse
    web.json_response = lambda payload=None, status=200, headers=None: FakeResponse(payload, status, headers)
    aiohttp.web = web

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("test")
    star = types.ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    star.Context = Context
    star.Star = Star
    api.star = star
    astrbot.api = api

    sys.modules["aiohttp"] = aiohttp
    sys.modules["aiohttp.web"] = web
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star


def load_module():
    install_stubs()
    spec = importlib.util.spec_from_file_location("bridge_main", PLUGIN / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


async def run() -> None:
    mod = load_module()

    base_config = {
        "listen_host": "127.0.0.1",
        "listen_port": 8787,
        "upstream_base": "http://127.0.0.1:6185",
        "upstream_key": "abk_test",
        "tokens": "t-ok=alice",
        "rate_per_min": 60,
        "rate_burst": 3,
        "heartbeat_sec": 5,
        "max_chars": 20,
        "feature_streaming": True,
        "feature_session": True,
        "feature_health": True,
        "feature_chat": True,
        "feature_metrics": False,
        "auto_start": True,
        "probe_fallback_path": "",
        "identity_mode": "token",
        "owner_id": "",
        "allow_unverified": False,
        "max_pending": 4,
    }

    # ---- 401：无令牌 ----
    plugin = mod.DshPetBridge(None, dict(base_config))
    await plugin.initialize()
    resp = await plugin._handle_chat(FakeRequest(payload={"message": "hi"}))
    check("chat: 无令牌 401", resp.status == 401 and resp.payload["error"] == "unauthorized", resp.text)

    # ---- 401：未知令牌 ----
    resp = await plugin._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer nope"}, payload={"message": "hi"})
    )
    check("chat: 未知令牌 401", resp.status == 401, resp.text)

    # ---- 400：空消息 ----
    resp = await plugin._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "   "})
    )
    check("chat: 空消息 400", resp.status == 400 and resp.payload["error"] == "empty_message", resp.text)

    # ---- 413：超长 ----
    resp = await plugin._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "x" * 50})
    )
    check("chat: 超长 413", resp.status == 413, resp.text)

    # ---- 503：未配置令牌 ----
    plugin_no_tokens = mod.DshPetBridge(None, {**base_config, "tokens": ""})
    await plugin_no_tokens.initialize()
    resp = await plugin_no_tokens._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check("chat: 未配置 tokens 503", resp.status == 503 and resp.payload["error"] == "tokens_not_configured")

    # ---- 503：未配置上游 Key ----
    plugin_no_key = mod.DshPetBridge(None, {**base_config, "upstream_key": ""})
    await plugin_no_key.initialize()
    resp = await plugin_no_key._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check("chat: 未配置 key 503", resp.status == 503 and resp.payload["error"] == "upstream_key_missing")

    # ---- 正常流式 ----
    chunks = [
        b'data: {"type":"session_id","data":"s1"}\n\n',
        b'data: {"type":"plain","data":"\\u4f60\\u597d"}\n\n',
        b'data: {"type":"reasoning","data":"ignored"}\n\n',
        b'data: {"type":"end"}\n\n',
    ]
    plugin._session = FakeSession(post_response=FakeUpstream(status=200, chunks=chunks))
    plugin._probe_cache = (0.0, {})
    stream = await plugin._handle_chat(
        FakeRequest(
            headers={"Authorization": "Bearer t-ok"},
            payload={"message": "在吗", "session_id": "sess-1"},
        )
    )
    posted = plugin._session.posts[-1]
    check("chat: 返回 SSE", isinstance(stream, FakeStreamResponse) and stream.prepared and stream.eof)
    check("chat: 内容原样透传", stream.written == chunks, repr(stream.written))
    check(
        "chat: 未知帧（reasoning）也原样透传，不解析不崩溃",
        b'"type":"reasoning"' in b"".join(stream.written),
    )
    check("chat: 上游 URL 正确", posted["url"] == "http://127.0.0.1:6185/api/v1/chat", posted["url"])
    check("chat: 带上游 Key", posted["headers"]["Authorization"] == "Bearer abk_test")
    check(
        "chat: 请求体字段正确",
        posted["json"]["username"] == "alice"
        and posted["json"]["message"] == "在吗"
        and posted["json"]["enable_streaming"] is True,
        json.dumps(posted["json"], ensure_ascii=False),
    )
    check("chat: session 自动加前缀", posted["json"]["session_id"] == "pet:sess-1", str(posted["json"].get("session_id")))
    check(
        "chat: 前缀幂等且过滤非法字符",
        plugin._namespaced_session("pet:x") == "pet:x" and plugin._namespaced_session("a b") == "pet:a-b",
        plugin._namespaced_session("a b"),
    )
    long_session = plugin._namespaced_session("x" * 500)
    check(
        "chat: 超长 session 截断后仍保留前缀",
        long_session.startswith("pet:") and len(long_session) == 200,
        f"len={len(long_session)} head={long_session[:12]}",
    )

    # ---- identity_mode=owner：桌宠以主人身份进入，共用记忆桶 ----
    plugin_owner = mod.DshPetBridge(
        None, {**base_config, "identity_mode": "owner", "owner_id": "10001"}
    )
    await plugin_owner.initialize()
    plugin_owner._session = FakeSession(
        post_response=FakeUpstream(status=200, chunks=[b'data: {"type":"end"}\n\n'])
    )
    await plugin_owner._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check(
        "identity: owner 模式用 owner_id 而不是令牌里的 username",
        plugin_owner._session.posts[-1]["json"]["username"] == "10001",
        plugin_owner._session.posts[-1]["json"]["username"],
    )

    # ---- 排队保护：在飞 + 排队超过阈值直接 429 ----
    plugin_busy = mod.DshPetBridge(None, {**base_config, "max_concurrent": 1, "max_pending": 0})
    await plugin_busy.initialize()
    plugin_busy._session = FakeSession(
        post_response=FakeUpstream(status=200, chunks=[b'data: {"type":"end"}\n\n'])
    )
    plugin_busy._pending = 1
    resp = await plugin_busy._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check("chat: 排队超限 429 busy", resp.status == 429 and resp.payload["error"] == "busy", resp.text)
    check("chat: busy 时不发起上游请求", plugin_busy._session.posts == [], str(plugin_busy._session.posts))

    # ---- 上游非 200 ----
    plugin_err = mod.DshPetBridge(None, dict(base_config))
    await plugin_err.initialize()
    plugin_err._session = FakeSession(post_response=FakeUpstream(status=500, text="boom"))
    resp = await plugin_err._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check("chat: 上游 500 映射 502", resp.status == 502 and resp.payload["error"] == "upstream_error", resp.text)

    # ---- 心跳：上游慢于 heartbeat ----
    plugin_slow = mod.DshPetBridge(None, {**base_config, "heartbeat_sec": 1})
    await plugin_slow.initialize()
    slow = FakeUpstream(status=200, chunks=[b"data: {\"type\":\"plain\",\"data\":\"x\"}\n\n"], delay=1.2)
    plugin_slow._session = FakeSession(post_response=slow)
    stream = await plugin_slow._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "hi"})
    )
    check("chat: 空闲时补心跳帧", b": ping\n\n" in stream.written, repr(stream.written))

    # ---- 限流 ----
    plugin2 = mod.DshPetBridge(None, {**base_config, "rate_per_min": 60, "rate_burst": 1})
    await plugin2.initialize()
    plugin2._session = FakeSession(post_response=FakeUpstream(status=200, chunks=[b"data: {\"type\":\"end\"}\n\n"]))
    first = await plugin2._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "a"})
    )
    second = await plugin2._handle_chat(
        FakeRequest(headers={"Authorization": "Bearer t-ok"}, payload={"message": "b"})
    )
    check("chat: 突发后 429", first.status == 200 and second.status == 429, f"{first.status}/{second.status}")

    # ---- 健康检查 ----
    health = await plugin2._handle_health(FakeRequest())
    check(
        "health: 结构与上游状态",
        health.status == 200
        and health.payload["ok"] is True
        and health.payload["ready"] is True
        and health.payload["upstream"]["auth_ok"] is True
        and health.payload["upstream"]["auth_state"] == "ok"
        and health.payload["upstream"]["path"] == "/api/v1/configs",
        health.text,
    )
    check(
        "health: 探针打的是需要鉴权的路径",
        plugin2._session.gets[-1]["url"].endswith("/api/v1/configs"),
        plugin2._session.gets[-1]["url"],
    )
    check(
        "health: 带 probe_at / probe_age_sec",
        health.payload["probe_at"] > 0 and health.payload["probe_age_sec"] is not None,
        health.text,
    )

    # ---- 健康检查：探针内部异常也不能 500 ----
    plugin_probe_error = mod.DshPetBridge(None, dict(base_config))
    await plugin_probe_error.initialize()

    async def _boom():
        raise RuntimeError("probe exploded")

    plugin_probe_error._probe_collect = _boom  # type: ignore[assignment]
    plugin_probe_error._probe_cache = (0.0, {})
    health = await plugin_probe_error._handle_health(FakeRequest())
    check(
        "health: 探针异常降级为 unreachable",
        health.status == 200
        and health.payload["upstream"]["auth_state"] == "unreachable"
        and health.payload["upstream"]["attempts"] == []
        and health.payload["ready"] is False,
        health.text,
    )

    # ---- 健康检查：Key 无效 ----
    plugin_401 = mod.DshPetBridge(None, dict(base_config))
    await plugin_401.initialize()
    plugin_401._session = FakeSession(probe_status=401)
    health = await plugin_401._handle_health(FakeRequest())
    check(
        "health: Key 无效时 ready=false",
        health.payload["upstream"]["auth_state"] == "invalid_key"
        and health.payload["upstream"]["auth_ok"] is False
        and health.payload["ready"] is False,
        health.text,
    )

    # ---- 健康检查：Key 有效但 scope 不足（默认不降级） ----
    plugin_403 = mod.DshPetBridge(None, dict(base_config))
    await plugin_403.initialize()
    plugin_403._session = FakeSession(
        probe_statuses={"/api/v1/configs": 403, "/chat/sessions": 403}
    )
    health = await plugin_403._handle_health(FakeRequest())
    check(
        "health: degraded 不算 ready",
        health.payload["upstream"]["auth_state"] == "scope_denied"
        and health.payload["upstream"]["auth_ok"] is None
        and health.payload["ready"] is False
        and health.payload["auth_verified"] is False
        and health.payload["degraded"] is True
        and len(health.payload["upstream"]["attempts"]) == 1,
        health.text,
    )

    # ---- allow_unverified：显式允许后才放行 ----
    plugin_unverified = mod.DshPetBridge(None, {**base_config, "allow_unverified": True})
    await plugin_unverified.initialize()
    plugin_unverified._session = FakeSession(probe_status=403)
    health = await plugin_unverified._handle_health(FakeRequest())
    check(
        "health: allow_unverified 时才 ready",
        health.payload["ready"] is True and health.payload["auth_verified"] is False,
        health.text,
    )

    # ---- 健康检查：主探针 403，启用降级后到 chat 探针 ----
    plugin_fallback = mod.DshPetBridge(
        None,
        {**base_config, "probe_fallback_path": "/api/v1/chat/sessions?username=__dsh_pet_probe__"},
    )
    await plugin_fallback.initialize()
    plugin_fallback._session = FakeSession(
        probe_statuses={"/api/v1/configs": 403, "/chat/sessions": 200}
    )
    health = await plugin_fallback._handle_health(FakeRequest())
    check(
        "health: 403 后自动降级成功",
        health.payload["upstream"]["auth_state"] == "ok"
        and health.payload["upstream"]["auth_ok"] is True
        and len(health.payload["upstream"]["attempts"]) == 2,
        health.text,
    )
    check(
        "health: 降级后 ready=true 且已验证",
        health.payload["ready"] is True and health.payload["auth_verified"] is True,
        health.text,
    )

    # ---- CORS ----
    plugin3 = mod.DshPetBridge(None, {**base_config, "feature_cors": True, "cors_origins": ""})
    await plugin3.initialize()
    r = await plugin3._handle_options(FakeRequest(origin="https://a.example"))
    check("cors: OPTIONS 返回来源", r.status == 204 and r.headers.get("Access-Control-Allow-Origin") == "https://a.example", str(r.headers))

    # ---- 关闭 chat ----
    plugin4 = mod.DshPetBridge(None, {**base_config, "feature_chat": False})
    await plugin4.initialize()
    paths = [p for _, p, _ in plugin4._runner.app.router.routes]  # type: ignore[union-attr]
    check("feature_chat=false: 不注册 /chat", "/chat" not in paths, str(paths))

    # ---- 收尾 ----
    for item in (
        plugin,
        plugin_no_tokens,
        plugin_no_key,
        plugin2,
        plugin3,
        plugin4,
        plugin_err,
        plugin_slow,
        plugin_401,
        plugin_403,
        plugin_fallback,
        plugin_unverified,
        plugin_owner,
        plugin_probe_error,
        plugin_busy,
    ):
        await item.terminate()

    print()
    print("FAILED" if failures else "ALL PASSED", f"({failures} failures)")


if __name__ == "__main__":
    asyncio.run(run())
    raise SystemExit(1 if failures else 0)
