#!/usr/bin/env python3
"""本地单元测试：不打桩 AstrBot 的运行时行为，只验证纯逻辑与配置一致性。

运行：
    python tests/test_core.py
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT


def _install_stubs() -> None:
    aiohttp = types.ModuleType("aiohttp")

    class _ClientTimeout:
        def __init__(self, *args, **kwargs):
            pass

    class _TCPConnector:
        def __init__(self, *args, **kwargs):
            pass

    class _ClientSession:
        pass

    class _ClientError(Exception):
        pass

    class _StreamReader:
        pass

    aiohttp.ClientTimeout = _ClientTimeout
    aiohttp.TCPConnector = _TCPConnector
    aiohttp.ClientSession = _ClientSession
    aiohttp.ClientError = _ClientError
    aiohttp.StreamReader = _StreamReader
    aiohttp.web = types.ModuleType("aiohttp.web")

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
    sys.modules["aiohttp.web"] = aiohttp.web
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api
    sys.modules["astrbot.api.star"] = star


def _load_module():
    _install_stubs()
    spec = importlib.util.spec_from_file_location("dsh_pet_bridge_main", PLUGIN / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def check(name, condition, detail=""):
    if condition:
        print(f"PASS  {name}")
        return 0
    print(f"FAIL  {name}  {detail}")
    return 1


def main() -> int:
    mod = _load_module()
    failures = 0

    # ---- parse_tokens ----
    failures += check(
        "tokens: 多行 + 注释 + 空行",
        mod.parse_tokens("# c\n\n a=alice \n b = bob \nbroken\n") == {"a": "alice", "b": "bob"},
        repr(mod.parse_tokens("# c\n\n a=alice \n b = bob \nbroken\n")),
    )
    failures += check("tokens: 空值返回空", mod.parse_tokens("") == {} and mod.parse_tokens(None) == {})
    failures += check(
        "tokens: JSON 对象",
        mod.parse_tokens('{"t1":"u1","t2":"u2"}') == {"t1": "u1", "t2": "u2"},
    )
    failures += check("tokens: 非法 JSON 回落为行解析", mod.parse_tokens('{"bad": ') == {})

    # ---- build_payload ----
    payload = mod.build_payload("你好", "dsh_pet", "s-1", True)
    failures += check(
        "payload: 字段齐全",
        payload["message"] == "你好"
        and payload["username"] == "dsh_pet"
        and payload["session_id"] == "s-1"
        and payload["enable_streaming"] is True
        and payload["flags"] == {"enable_streaming": True},
        json.dumps(payload, ensure_ascii=False),
    )
    failures += check(
        "payload: 不带无效的 reasoning/genui 开关",
        "enable_reasoning" not in payload["flags"] and "enable_inline_genui" not in payload["flags"],
        json.dumps(payload["flags"], ensure_ascii=False),
    )
    failures += check(
        "payload: 顶层与 flags 的 streaming 永远一致",
        payload["enable_streaming"] == payload["flags"]["enable_streaming"],
    )
    failures += check(
        "payload: 无 session 时不带字段",
        "session_id" not in mod.build_payload("hi", "u", None, False),
    )

    # ---- 指纹 ----
    fp = mod.fingerprint("secret-token")
    failures += check("fingerprint: 8 位且不含明文", len(fp) == 8 and "secret" not in fp, fp)
    failures += check(
        "fingerprint: 稳定",
        mod.fingerprint("secret-token") == fp and mod.fingerprint("other") != fp,
    )

    # ---- 限流 ----
    limiter = mod.RateLimiter(per_minute=60, burst=2)
    allowed = [limiter.allow("k") for _ in range(3)]
    failures += check("ratelimit: 突发 2 次后拒绝", allowed == [True, True, False], str(allowed))
    failures += check("ratelimit: 不同 key 独立", limiter.allow("other") is True)
    limiter0 = mod.RateLimiter(per_minute=0, burst=1)
    failures += check("ratelimit: 0 表示不限", all(limiter0.allow("k") for _ in range(10)))

    limiter_prune = mod.RateLimiter(per_minute=60, burst=5)
    for index in range(3):
        limiter_prune.allow(f"k{index}")
    for key in list(limiter_prune._stamps):
        limiter_prune._stamps[key] -= 1000
    limiter_prune._prune(time.monotonic())
    failures += check(
        "ratelimit: 过期 key 会被清理",
        limiter_prune._stamps == {} and limiter_prune._tokens == {},
        str(limiter_prune._stamps),
    )
    limiter_prune.allow("fresh")
    limiter_prune._prune(time.monotonic())
    failures += check("ratelimit: 活跃 key 不被清理", "fresh" in limiter_prune._stamps)

    # ---- 配置默认值 ----
    plugin = mod.DshPetBridge(context=None, config={})
    failures += check("config: 默认监听 127.0.0.1", plugin._str("listen_host", "127.0.0.1") == "127.0.0.1")
    failures += check("config: 默认端口 8787", plugin._int("listen_port", 8787) == 8787)
    failures += check("config: streaming 默认开", plugin._bool("feature_streaming", True) is True)
    failures += check("config: cors 默认关", plugin._bool("feature_cors", False) is False)
    failures += check("config: 空 config 不抛异常", plugin._raw("missing", "d") == "d")

    plugin2 = mod.DshPetBridge(
        context=None,
        config={"feature_streaming": "false", "listen_port": "9000", "tokens": "t=u"},
    )
    failures += check("config: 字符串 false 解析为 False", plugin2._bool("feature_streaming", True) is False)
    failures += check("config: 字符串端口解析为 int", plugin2._int("listen_port", 1) == 9000)

    # ---- 身份模式 ----
    failures += check("identity: 默认用 token 身份", plugin._effective_username("dsh_pet") == "dsh_pet")
    plugin_owner = mod.DshPetBridge(None, {"identity_mode": "owner", "owner_id": "10001"})
    failures += check(
        "identity: owner 模式覆盖为 owner_id",
        plugin_owner._effective_username("dsh_pet") == "10001",
    )
    plugin_owner_missing = mod.DshPetBridge(None, {"identity_mode": "owner", "owner_id": ""})
    failures += check(
        "identity: owner_id 为空时回退 token 身份",
        plugin_owner_missing._effective_username("dsh_pet") == "dsh_pet",
    )

    # ---- schema 与代码一致性 ----
    schema = json.loads((PLUGIN / "_conf_schema.json").read_text(encoding="utf-8"))
    source = (PLUGIN / "main.py").read_text(encoding="utf-8")
    used = set(re.findall(r'self\._(?:bool|int|str|raw|path)\("([a-z_]+)"', source))
    missing = sorted(k for k in used if k not in schema)
    failures += check("schema: 代码用到的键都存在", not missing, f"missing={missing}")
    unused = sorted(k for k in schema if k not in used and k not in {"config_version"})
    failures += check("schema: 没有未使用的键", not unused, f"unused={unused}")

    schema_problems = []
    for key, item in schema.items():
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        hint = str(item.get("hint", "")).strip()
        if not desc:
            schema_problems.append(f"{key}: 缺 description")
        elif len(desc) > 24:
            schema_problems.append(f"{key}: description 过长({len(desc)})")
        if not hint:
            schema_problems.append(f"{key}: 缺 hint")
    failures += check(
        "schema: 短描述 + hint（避免配置页被截断）",
        not schema_problems,
        "; ".join(schema_problems),
    )

    # ---- metadata ----
    metadata = (PLUGIN / "metadata.yaml").read_text(encoding="utf-8")
    failures += check(
        "metadata: 名称与目录一致",
        "name: astrbot_plugin_petlink" in metadata and "version: 0.2.0" in metadata,
    )

    print()
    print("FAILED" if failures else "ALL PASSED", f"({failures} failures)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
