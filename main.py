#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-pet 桥接插件（AstrBot 侧）。

把桌宠的文本请求转发到 AstrBot 自带的 OpenAPI（默认 127.0.0.1:6185），
并把上游的 SSE 原样回传给桌宠。桌宠侧只需要知道 baseUrl + 设备令牌，
永远拿不到 ``abk_`` 开头的上游 API Key。

设计约束：
- 默认只监听 127.0.0.1，对外必须自己配反向代理/TLS；
- 没有配置令牌时拒绝所有 /chat 请求，不提供「空令牌放行」；
- 身份（username）由客户端传入并在令牌鉴权后校验，令牌只决定连接可信；
- 所有环境相关的值都走配置，代码里不写死地址、端口、QQ 号；
- 运行在 AstrBot 进程内，所有 handler 全包异常，绝不向上冒泡。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional, Tuple

import aiohttp
from aiohttp import web

from astrbot.api import logger

try:  # AstrBot 4.x
    from astrbot.api.star import Context, Star
except Exception:  # pragma: no cover - 兼容旧导入路径
    try:
        from astrbot.core.star import Context, Star  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "无法导入 AstrBot 的 Star 基类（已尝试 astrbot.api.star 与 astrbot.core.star）。"
            "请确认插件运行在 AstrBot 4.x 上。"
        ) from exc


PLUGIN_NAME = "astrbot_plugin_petlink"
PLUGIN_VERSION = "0.2.6"
UPSTREAM_CHAT_PATH = "/api/v1/chat"


# --------------------------------------------------------------------------
# 纯函数（不依赖网络，方便单测）
# --------------------------------------------------------------------------


def parse_tokens(raw: Any) -> Dict[str, str]:
    """解析令牌映射。

    支持两种写法：
    - 多行文本：每行 ``token=username``，``#`` 开头为注释；
    - JSON 对象：``{"token": "username"}``。

    空值或格式错误一律返回空字典 —— 调用方据此拒绝所有请求，而不是放行。
    """
    if not raw:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip() and str(v).strip()}
    text = str(raw).strip()
    if not text:
        return {}
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in obj.items()
                if str(k).strip() and str(v).strip()
            }
    result: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        token, username = line.split("=", 1)
        token, username = token.strip(), username.strip()
        if token and username:
            result[token] = username
    return result


_AUTH_HINTS: Dict[str, str] = {
    "ok": "Key 已验证",
    "invalid_key": "Key 无效、过期或被吊销",
    "scope_denied": "Key 有效但 scope 不足，至少需要 chat",
    "public_endpoint": "探测路径不需要鉴权（匿名也返回 200），证明不了 Key；请把 probe_path 换成需要鉴权的接口",
    "unverified": "带 Key 返回 200，但匿名对照请求失败，无法确认路径是否需要鉴权",
    "unexpected": "探测路径返回异常，无法判定",
    "unreachable": "上游不可达，检查 upstream_base 与 AstrBot 是否在运行",
}


def _auth_hint(probe: Dict[str, Any]) -> str:
    """把 auth_state 翻成一句人话，避免用户只看到 auth_ok=null。"""
    state = str(probe.get("auth_state", ""))
    return _AUTH_HINTS.get(state, "未知状态，查看 upstream.attempts")


def build_payload(message: str, username: str, session_id: Optional[str], streaming: bool) -> Dict[str, Any]:
    """构造 AstrBot OpenAPI 的聊天请求体（字段依据 2026-10-31 实测）。

    只保留实际生效的开关：``enable_reasoning`` 由服务端配置决定，
    ``enable_inline_genui`` 无法稳定抑制 T2I，二者都不传。
    """
    payload: Dict[str, Any] = {
        "message": message,
        "username": username,
        "enable_streaming": bool(streaming),
        "flags": {"enable_streaming": bool(streaming)},
    }
    if session_id:
        payload["session_id"] = session_id
    return payload


def fingerprint(token: str) -> str:
    """令牌指纹：日志里只出现这个，不出现明文。"""
    return hashlib.sha256(token.encode("utf-8", "ignore")).hexdigest()[:8]


class RateLimiter:
    """按 key 的令牌桶限流。线程/协程安全由单事件循环保证。"""

    def __init__(self, per_minute: int, burst: int) -> None:
        self.per_minute = max(int(per_minute), 0)
        self.capacity = max(int(burst), 1)
        self._tokens: Dict[str, float] = {}
        self._stamps: Dict[str, float] = {}
        self._calls = 0

    def _prune(self, now: float) -> None:
        """清掉 10 分钟没有活动的 key，避免换令牌多了以后缓慢泄漏。"""
        cutoff = now - 600.0
        stale = [key for key, stamp in self._stamps.items() if stamp < cutoff]
        for key in stale:
            self._tokens.pop(key, None)
            self._stamps.pop(key, None)

    def allow(self, key: str) -> bool:
        if self.per_minute <= 0:
            return True
        now = time.monotonic()
        self._calls += 1
        if self._calls % 256 == 0 or len(self._stamps) > 4096:
            self._prune(now)
        tokens = self._tokens.get(key, float(self.capacity))
        stamp = self._stamps.get(key, now)
        tokens = min(float(self.capacity), tokens + (now - stamp) * self.per_minute / 60.0)
        if tokens >= 1.0:
            self._tokens[key] = tokens - 1.0
            self._stamps[key] = now
            return True
        self._tokens[key] = tokens
        self._stamps[key] = now
        return False


# --------------------------------------------------------------------------
# 插件
# --------------------------------------------------------------------------


class DshPetBridge(Star):
    """桌宠 ↔ AstrBot 的桥。"""

    def __init__(self, context: Context = None, config: Any = None) -> None:
        super().__init__(context)
        self.config = config if config is not None else {}
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._sem: Optional[asyncio.Semaphore] = None
        self._limiter: Optional[RateLimiter] = None
        self._tokens: Dict[str, str] = {}
        self._probe_cache: Tuple[float, Dict[str, Any]] = (0.0, {})
        self._started_at = 0.0
        self._disabled_reason = ""
        self._pending = 0
        self._counters = {"requests": 0, "chat_ok": 0, "errors": 0, "rejected": 0}

    # ---------------- 配置读取 ----------------

    def _raw(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)  # type: ignore[union-attr]
        except Exception:
            value = default
        return default if value is None else value

    def _bool(self, key: str, default: bool) -> bool:
        value = self._raw(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    def _int(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(self._raw(key, default))
        except Exception:
            value = default
        return max(value, minimum)

    def _str(self, key: str, default: str) -> str:
        value = self._raw(key, default)
        return str(value).strip() if value is not None else default

    def _path(self, key: str, default: str) -> str:
        path = self._str(key, default) or default
        return path if path.startswith("/") else "/" + path

    @property
    def _upstream_base(self) -> str:
        base = self._str("upstream_base", "http://127.0.0.1:6185")
        return base.rstrip("/")

    @property
    def _cors_enabled(self) -> bool:
        return self._bool("feature_cors", False)

    def _cors_headers(self, request: web.Request) -> Dict[str, str]:
        if not self._cors_enabled:
            return {}
        origin = request.headers.get("Origin", "")
        allowed = self._str("cors_origins", "")
        if not allowed or allowed == "*":
            allow_origin = origin or "*"
        else:
            allow_origin = origin if origin in [x.strip() for x in allowed.split(",") if x.strip()] else ""
        if not allow_origin:
            return {}
        return {
            "Access-Control-Allow-Origin": allow_origin,
            "Access-Control-Allow-Headers": "Authorization, Content-Type, X-Pet-Token",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Vary": "Origin",
        }

    # ---------------- 生命周期 ----------------

    async def initialize(self) -> None:
        if self._runner is not None:  # 已启动（重复初始化时直接返回）
            return
        if not self._bool("auto_start", True):
            logger.info("[petlink] auto_start=false，桥接服务未启动")
            return

        self._disabled_reason = ""
        device_token = str(self._raw("device_token", "") or "").strip()
        if device_token:
            device_username = str(self._raw("device_username", "dsh_pet") or "dsh_pet").strip() or "dsh_pet"
            self._tokens = {device_token: device_username}
        else:
            self._tokens = parse_tokens(self._raw("tokens", ""))
        if self._bool("allow_unverified", False):
            # 这是一条「我知道 Key 可能没 scope，仍然放行」的调试开关，启动时必须可见。
            logger.warning(
                "[petlink] allow_unverified=true：Key 未被 200 证实也会放行，仅用于调试，生产请关闭"
            )
        self._limiter = RateLimiter(self._int("rate_per_min", 30), self._int("rate_burst", 5, 1))
        # /balance 单独一个桶：刷余额不挤占 /chat 的额度。
        self._balance_limiter = RateLimiter(self._int("rate_per_min", 30), self._int("rate_burst", 5, 1))
        self._sem = asyncio.Semaphore(self._int("max_concurrent", 4, 1))

        try:
            timeout = aiohttp.ClientTimeout(
                total=self._int("timeout_sec", 120, 5),
                connect=self._int("connect_timeout_sec", 10, 1),
            )
            connector = aiohttp.TCPConnector(limit=self._int("max_concurrent", 4, 1) + 4)
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector)

            app = web.Application(client_max_size=self._int("max_body_kb", 64, 1) * 1024)
            if self._bool("feature_health", True):
                app.router.add_get("/", self._handle_index)
                app.router.add_get("/health", self._handle_health)
            if self._bool("feature_chat", True):
                app.router.add_post("/chat", self._handle_chat)
                app.router.add_get("/balance", self._handle_balance)
                if self._cors_enabled:
                    app.router.add_options("/chat", self._handle_options)
            if self._bool("feature_metrics", False):
                app.router.add_get("/metrics", self._handle_metrics)

            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            host = self._str("listen_host", "127.0.0.1")
            port = self._int("listen_port", 18787, 1)
            self._site = web.TCPSite(self._runner, host, port)

            for attempt in range(1, 4):
                try:
                    await self._site.start()
                    self._started_at = time.time()
                    logger.info(
                        "[petlink] 桥接服务已启动 http://%s:%s（upstream=%s，tokens=%d）",
                        host,
                        port,
                        self._upstream_base,
                        len(self._tokens),
                    )
                    if not self._tokens:
                        logger.warning("[petlink] 尚未配置 tokens，/chat 将返回 503")
                    if host == "0.0.0.0":
                        logger.warning("[petlink] 正在监听 0.0.0.0，请确认已配置反向代理与 TLS")
                    return
                except OSError as exc:
                    logger.error("[petlink] 端口 %s:%s 绑定失败（第 %d 次）：%s", host, port, attempt, exc)
                    if attempt < 3:
                        await asyncio.sleep(2)

            self._disabled_reason = "port_bind_failed"
            logger.error("[petlink] 桥接服务启动失败：端口被占用或权限不足")
            # 关键：半启动状态必须彻底清掉，否则重载插件时 initialize 会直接返回，
            # 表现为「插件在、服务永远起不来」。
            await self._stop_server()
            await self._close_session()
        except Exception as exc:  # pragma: no cover - 启动异常
            self._disabled_reason = "start_failed"
            logger.exception("[petlink] 桥接服务启动异常：%s", exc)
            await self._stop_server()
            await self._close_session()

    async def _stop_server(self) -> None:
        if self._site is not None:
            try:
                await self._site.stop()
            except Exception as exc:  # pragma: no cover
                logger.debug("[petlink] 停止 site 失败：%s", exc)
            finally:
                self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception as exc:  # pragma: no cover
                logger.debug("[petlink] 清理 runner 失败：%s", exc)
            finally:
                self._runner = None

    async def _close_session(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:  # pragma: no cover
                pass
            finally:
                self._session = None

    async def terminate(self) -> None:
        await self._stop_server()
        await self._close_session()
        logger.info("[petlink] 桥接服务已停止")

    # ---------------- 上游探测 ----------------

    async def _probe_status(self, path: str, key: str) -> Optional[int]:
        """发一次 GET，只回 status；key 为空时不带 Authorization。"""
        url = self._upstream_base + path
        headers: Dict[str, str] = {"Authorization": f"Bearer {key}"} if key else {}
        async with self._session.get(  # type: ignore[union-attr]
            url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)
        ) as response:
            return response.status

    async def _probe_one(self, path: str) -> Dict[str, Any]:
        """带 Key 与不带 Key 各探一次：只有「带 Key 200、匿名 401/403」才算真验过。"""
        key = self._str("upstream_key", "")
        try:
            status = await self._probe_status(path, key)
        except Exception as exc:
            return {
                "path": path,
                "reachable": False,
                "state": "unreachable",
                "auth_ok": None,
                "error": type(exc).__name__,
            }
        try:
            anonymous_status = await self._probe_status(path, "")
        except Exception:
            anonymous_status = None

        if status == 200:
            if anonymous_status == 200:
                # 不鉴权也能 200：这个路径证明不了 Key 有效，不能算 auth_ok。
                state, auth_ok = "public_endpoint", None
            elif anonymous_status in (401, 403):
                state, auth_ok = "ok", True
            else:
                state, auth_ok = "unverified", None
        elif status == 401:
            state, auth_ok = "invalid_key", False
        elif status == 403:
            # 403 = Key 有效但 scope 不够，不能据此判定 Key 失效
            state, auth_ok = "scope_denied", None
        else:
            state, auth_ok = "unexpected", None
        return {
            "path": path,
            "reachable": True,
            "status": status,
            "anonymous_status": anonymous_status,
            "state": state,
            "auth_ok": auth_ok,
        }

    async def _probe_upstream(self, force: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        cached_at, cached = self._probe_cache
        if not force and cached and now - cached_at < 5.0:
            return cached
        try:
            result = await self._probe_collect()
        except Exception as exc:
            # 任何未预料的异常都不能让 /health 变成 500
            logger.warning("[petlink] 探针异常：%s", exc)
            result = {
                "reachable": False,
                "auth_ok": None,
                "auth_state": "unreachable",
                "path": self._path("probe_path", "/api/v1/configs"),
                "attempts": [],
                "error": type(exc).__name__,
            }
        result["probe_at"] = time.time()
        self._probe_cache = (now, result)
        return result

    async def _probe_collect(self) -> Dict[str, Any]:
        paths = [self._path("probe_path", "/api/v1/configs")]
        fallback = self._str("probe_fallback_path", "")
        if fallback:
            fallback = fallback if fallback.startswith("/") else "/" + fallback
            if fallback not in paths:
                paths.append(fallback)

        attempts: list = []
        if self._session is not None:
            for path in paths:
                try:
                    attempt = await self._probe_one(path)
                except Exception as exc:  # pragma: no cover - 兜底，正常不会走到
                    attempt = {
                        "path": path,
                        "reachable": False,
                        "state": "unreachable",
                        "auth_ok": None,
                        "error": type(exc).__name__,
                    }
                attempts.append(attempt)
                # 200 / 401 已是确定结论，不必再试兜底
                if attempt["state"] in ("ok", "invalid_key"):
                    break

        primary: Dict[str, Any] = attempts[0] if attempts else {"path": paths[0], "status": None}
        states = [item["state"] for item in attempts]
        if "ok" in states:
            primary = next(item for item in attempts if item["state"] == "ok")
            auth_state, auth_ok = "ok", True
        elif "invalid_key" in states:
            primary = next(item for item in attempts if item["state"] == "invalid_key")
            auth_state, auth_ok = "invalid_key", False
        elif "scope_denied" in states:
            primary = next(item for item in attempts if item["state"] == "scope_denied")
            auth_state, auth_ok = "scope_denied", None
        elif "public_endpoint" in states:
            primary = next(item for item in attempts if item["state"] == "public_endpoint")
            auth_state, auth_ok = "public_endpoint", None
        elif "unverified" in states:
            primary = next(item for item in attempts if item["state"] == "unverified")
            auth_state, auth_ok = "unverified", None
        elif any(item["reachable"] for item in attempts):
            primary = next(item for item in attempts if item["reachable"])
            auth_state, auth_ok = "unexpected", None
        else:
            auth_state, auth_ok = "unreachable", None

        return {
            "reachable": any(item["reachable"] for item in attempts),
            "auth_ok": auth_ok,
            "auth_state": auth_state,
            "path": primary.get("path"),
            "status": primary.get("status"),
            "attempts": attempts,
        }

    # ---------------- 路由 ----------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "name": PLUGIN_NAME,
                "version": PLUGIN_VERSION,
                "routes": ["/health", "/chat"] if self._bool("feature_chat", True) else ["/health"],
            },
            headers=self._cors_headers(request),
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        force = False
        try:
            force = str(request.query.get("refresh", "")).lower() in ("1", "true", "yes")
        except Exception:
            force = False
        probe = await self._probe_upstream(force=force)
        started = self._started_at != 0 and self._disabled_reason == ""
        configured = bool(self._tokens)
        supplied = self._extract_token(request)
        token_ok = None if not supplied or not configured else self._match_token(supplied) is not None
        # ready 必须是「被 200 证实过」。403/未知只能算 degraded：
        # 默认不让桌宠放行，除非显式打开 allow_unverified。
        verified = probe.get("auth_ok") is True
        degraded = bool(probe.get("reachable") and probe.get("auth_ok") is None)
        # 公开路径连 allow_unverified 都不该放行：它连「Key 有没有权限」都证明不了。
        public_endpoint = probe.get("auth_state") == "public_endpoint"
        # 403 scope_denied 说明 Key 本身有效，只是探针接口（configs）权限不够；
        # 对只申请 chat 的正常部署，这不应把整个桥接判死，聊天时会再验证一次。
        scope_denied = probe.get("auth_state") == "scope_denied"
        allow_unverified = self._bool("allow_unverified", False)
        ready = bool(
            started
            and configured
            and probe.get("reachable")
            and token_ok is not False
            and (verified or (degraded and (allow_unverified or scope_denied) and not public_endpoint))
        )
        payload: Dict[str, Any] = {
            "ok": started,
            "ready": ready,
            "auth_verified": verified,
            "degraded": degraded,
            "allow_unverified": allow_unverified,
            "token_ok": token_ok,
            "auth_hint": _auth_hint(probe),
            "identity_mode": "client",
            "version": PLUGIN_VERSION,
            "upstream": probe,
            "probe_at": int(probe.get("probe_at") or 0),
            "probe_age_sec": (
                int(max(0.0, time.time() - float(probe.get("probe_at"))))
                if probe.get("probe_at")
                else None
            ),
            "configured": configured,
            "started_at": int(self._started_at) if self._started_at else 0,
        }
        if self._disabled_reason:
            payload["error"] = self._disabled_reason
        return web.json_response(payload, headers=self._cors_headers(request))

    async def _handle_metrics(self, request: web.Request) -> web.Response:
        payload = dict(self._counters)
        payload["uptime_sec"] = int(time.time() - self._started_at) if self._started_at else 0
        return web.json_response(payload, headers=self._cors_headers(request))

    async def _handle_options(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=self._cors_headers(request))

    # ---------------- 聊天转发 ----------------

    def _extract_token(self, request: web.Request) -> str:
        header = request.headers.get("Authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.headers.get("X-Pet-Token", "").strip()

    def _match_token(self, token: str) -> Optional[str]:
        """常量时间比较所有已知令牌，避免字典查找的短路时序。"""
        if not token:
            return None
        probe = token.encode("utf-8")
        for known, username in self._tokens.items():
            if hmac.compare_digest(known.encode("utf-8"), probe):
                return username
        return None

    def _effective_username(self, token_username: str, requested: Any) -> str:
        """客户端决定身份；校验后覆盖令牌映射，非法或空值回退令牌身份。"""
        if isinstance(requested, str):
            cleaned = requested.strip()
            if 0 < len(cleaned) <= 64 and all(ch.isalnum() or ch in "_-.:" for ch in cleaned):
                return cleaned
        return token_username

    def _namespaced_session(self, session_id: str) -> str:
        """给桌宠会话加前缀，避免与 WebChat 的会话 id 撞车，也方便按前缀检索。

        注意顺序：先按「总长 - 前缀长」截断原始 id，再加前缀，
        否则超长 id 会把 ``pet:`` 自己截掉。
        """
        prefix = self._str("session_prefix", "pet:")
        limit = self._int("session_max_chars", 200, 16)
        cleaned = "".join(ch if (ch.isalnum() or ch in "_:.-") else "-" for ch in session_id)
        body = cleaned[len(prefix):] if prefix and cleaned.startswith(prefix) else cleaned
        # 纯符号/emoji 清理后只剩连字符：不要造出所有会话共用一个 pet:--- 桶。
        if not any(ch.isalnum() for ch in body):
            return ""
        if prefix and not cleaned.startswith(prefix):
            cleaned = prefix + cleaned[: max(0, limit - len(prefix))]
        return cleaned[:limit]

    async def _handle_balance(self, request: web.Request) -> web.StreamResponse:
        """代查余额：客户端拿不到上游 Key，只拿到归一化后的钱包列表。"""
        cors = self._cors_headers(request)
        if self._disabled_reason or self._session is None or self._sem is None or self._balance_limiter is None:
            return self._json({"error": "bridge_not_ready"}, 503, cors)
        if not self._tokens:
            return self._json({"error": "tokens_not_configured"}, 503, cors)
        token = self._extract_token(request)
        if self._match_token(token) is None:
            return self._json({"error": "unauthorized"}, 401, cors)
        if not self._balance_limiter.allow(token):
            rate = self._int("rate_per_min", 30, 1)
            retry_after = str(max(1, (60 + rate - 1) // rate))
            return self._json(
                {"error": "rate_limited"},
                429,
                {**cors, "Retry-After": retry_after},
            )
        url = str(self._raw("balance_url", "") or "").strip()
        key = str(self._raw("balance_key", "") or "").strip()
        if not url or not key:
            return self._json({"error": "balance_not_configured"}, 503, cors)
        try:
            async with self._sem:
                async with self._session.get(
                    url,
                    headers={"authorization": f"Bearer {key}"},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        return self._json({"error": "balance_http", "status": response.status}, 502, cors)
                    data = await response.json(content_type=None)
        except Exception as error:  # 网络/JSON 都归成一次可展示的失败
            logger.warning("[petlink] 余额查询失败：%s", error)
            return self._json({"error": "balance_fetch"}, 502, cors)
        infos = data.get("balance_infos") if isinstance(data, dict) else None
        if not isinstance(infos, list):
            return self._json({"error": "balance_shape"}, 502, cors)
        wallets = []
        for info in infos:
            if not isinstance(info, dict):
                continue
            wallets.append(
                {
                    "currency": str(info.get("currency", "CNY")),
                    "total": str(info.get("total_balance", "")),
                    "granted": str(info.get("granted_balance", "")),
                    "toppedUp": str(info.get("topped_up_balance", "")),
                }
            )
        return self._json({"ok": True, "shape": "money", "wallets": wallets}, 200, cors)

    async def _handle_chat(self, request: web.Request) -> web.StreamResponse:
        self._counters["requests"] += 1
        cors = self._cors_headers(request)

        if self._disabled_reason or self._session is None or self._sem is None or self._limiter is None:
            self._counters["errors"] += 1
            return self._json({"error": "bridge_not_ready"}, 503, cors)

        if not self._tokens:
            self._counters["rejected"] += 1
            return self._json({"error": "tokens_not_configured"}, 503, cors)

        token = self._extract_token(request)
        if not token:
            self._counters["rejected"] += 1
            return self._json({"error": "unauthorized"}, 401, cors)

        # 顺序要求：先验令牌，再解析请求体，最后才做 owner 覆盖。
        # 身份覆盖只发生在鉴权之后，未通过令牌的请求根本走不到 body。
        username = self._match_token(token)
        if not username:
            self._counters["rejected"] += 1
            logger.warning("[petlink] 未知令牌 fingerprint=%s", fingerprint(token))
            return self._json({"error": "unauthorized"}, 401, cors)

        if not self._limiter.allow(token):
            self._counters["rejected"] += 1
            rate = self._int("rate_per_min", 30, 1)
            retry_after = str(max(1, (60 + rate - 1) // rate))
            return self._json(
                {"error": "rate_limited"},
                429,
                {**cors, "Retry-After": retry_after},
            )

        try:
            body = await request.json()
        except Exception:
            return self._json({"error": "invalid_json"}, 400, cors)
        if not isinstance(body, dict):
            return self._json({"error": "invalid_body"}, 400, cors)

        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return self._json({"error": "empty_message"}, 400, cors)
        message = message.strip()
        max_chars = self._int("max_chars", 2000, 1)
        if len(message) > max_chars:
            return self._json(
                {"error": "message_too_long", "max_chars": max_chars},
                413,
                cors,
            )

        if self._bool("log_content", False):
            logger.info("[petlink] chat msg fingerprint=%s text=%r", fingerprint(token), message[:80])

        # 身份由客户端决定；令牌只决定这条连接是否可信。
        username = self._effective_username(username, body.get("username"))

        session_id: Optional[str] = None
        if self._bool("feature_session", True):
            raw_session = body.get("session_id")
            if isinstance(raw_session, (str, int)) and str(raw_session).strip():
                session_id = self._namespaced_session(str(raw_session).strip())

        payload = build_payload(
            message=message,
            username=username,
            session_id=session_id,
            streaming=self._bool("feature_streaming", True),
        )

        max_concurrent = self._int("max_concurrent", 4, 1)
        max_pending = self._int("max_pending", 4, 0)
        self._pending += 1
        try:
            # 排队保护：在飞 + 排队超过阈值就直接 429，避免慢上游把请求堆成雪崩
            if self._pending > max_concurrent + max_pending:
                self._counters["rejected"] += 1
                return self._json({"error": "busy"}, 429, {**cors, "Retry-After": "2"})
            async with self._sem:
                return await self._proxy_chat(request, payload, token, cors)
        finally:
            self._pending -= 1

    async def _proxy_chat(
        self,
        request: web.Request,
        payload: Dict[str, Any],
        token: str,
        cors: Dict[str, str],
    ) -> web.StreamResponse:
        assert self._session is not None
        key = self._str("upstream_key", "")
        if not key:
            self._counters["errors"] += 1
            return self._json({"error": "upstream_key_missing"}, 503, cors)

        url = self._upstream_base + UPSTREAM_CHAT_PATH
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        started = time.monotonic()
        try:
            upstream = await self._session.post(url, json=payload, headers=headers)
        except asyncio.TimeoutError:
            self._counters["errors"] += 1
            logger.warning("[petlink] 上游超时 fingerprint=%s", fingerprint(token))
            return self._json({"error": "upstream_timeout"}, 504, cors)
        except aiohttp.ClientError as exc:
            self._counters["errors"] += 1
            logger.warning("[petlink] 上游连接失败 fingerprint=%s err=%s", fingerprint(token), exc)
            return self._json({"error": "upstream_unreachable"}, 502, cors)

        if upstream.status != 200:
            text = ""
            try:
                text = (await upstream.text())[:300]
            except Exception:
                pass
            upstream.release()
            self._counters["errors"] += 1
            logger.warning(
                "[petlink] 上游非 200 fingerprint=%s status=%s body=%s",
                fingerprint(token),
                upstream.status,
                text,
            )
            return self._json({"error": "upstream_error", "status": upstream.status}, 502, cors)

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream; charset=utf-8",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **cors,
            },
        )
        try:
            await response.prepare(request)
        except Exception:
            upstream.release()
            return response

        completed = False
        try:
            await self._pipe_stream(upstream.content, response)
            completed = True
            self._counters["chat_ok"] += 1
        except ConnectionResetError:
            # 客户端断开：不再 write_eof，否则 Windows 上会持续刷 ConnectionResetError
            logger.debug("[petlink] 客户端提前断开 fingerprint=%s", fingerprint(token))
        except asyncio.CancelledError:
            logger.debug("[petlink] 转发被取消 fingerprint=%s", fingerprint(token))
            raise
        except Exception as exc:  # pragma: no cover - 网络异常
            self._counters["errors"] += 1
            logger.warning("[petlink] 转发中断 fingerprint=%s err=%s", fingerprint(token), exc)
        finally:
            upstream.release()

        if completed:
            try:
                await response.write_eof()
            except (ConnectionResetError, RuntimeError) as exc:
                logger.debug("[petlink] write_eof 忽略：%s", exc)
            except Exception as exc:  # pragma: no cover
                logger.debug("[petlink] write_eof 失败：%s", exc)

        cost = int((time.monotonic() - started) * 1000)
        logger.info("[petlink] chat 完成 fingerprint=%s user=%s %dms", fingerprint(token), payload["username"], cost)
        return response

    async def _pipe_stream(self, content: aiohttp.StreamReader, response: web.StreamResponse) -> None:
        """把上游 SSE 原样写下，空闲时补心跳注释帧。"""
        heartbeat = self._int("heartbeat_sec", 15, 1)
        iterator = content.iter_any()
        pending: Optional[asyncio.Future] = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=heartbeat)
                if pending not in done:
                    if not await self._write_stream_piece(response, b": ping\n\n"):
                        return
                    continue
                future, pending = pending, None
                try:
                    chunk = future.result()
                except StopAsyncIteration:
                    break
                if not chunk:
                    break
                if not await self._write_stream_piece(response, chunk):
                    return
        finally:
            if pending is not None and not pending.done():
                pending.cancel()

    @staticmethod
    async def _write_stream_piece(response: web.StreamResponse, data: bytes) -> bool:
        """写一段 SSE；客户端断开时返回 False，让上层安静收尾，不刷 warning。"""
        try:
            await response.write(data)
            return True
        except (ConnectionResetError, BrokenPipeError, RuntimeError) as exc:
            logger.debug("[petlink] 客户端已断开，停止转发：%s", exc)
            return False

    # ---------------- 工具 ----------------

    def _json(self, payload: Dict[str, Any], status: int, headers: Dict[str, str]) -> web.Response:
        return web.json_response(payload, status=status, headers=headers)


__all__ = ["DshPetBridge", "parse_tokens", "build_payload", "RateLimiter"]
