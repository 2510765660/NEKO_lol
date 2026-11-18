"""英雄联盟 Live Client Data API 客户端（只读）。

对接 Riot 官方的本地对局数据接口：``https://127.0.0.1:2999/liveclientdata``。

关于这个接口要知道的三件事：

* **只在进入对局后存在。** 大厅、选人、结算阶段端口 2999 不监听，
  连不上是正常状态，不是故障。调用方应该把 :class:`NotInGame`
  当成"当前没在打"的信号，而不是错误。
* **证书是自签的。** 必须关掉校验，否则每次都 SSL 报错。
* **不同区服可能只开 http 或 https。** 所以先试 https（官方形态），
  连接层失败再回落 http。注意回落只针对"连不上"，HTTP 4xx/5xx
  说明服务在、只是这次请求有问题，不该再试另一个协议。

这个模块只做 GET，不发送任何会改变游戏状态的东西。
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiohttp

DEFAULT_BASE_URL = "https://127.0.0.1:2999/liveclientdata"
HTTP_BASE_URL = "http://127.0.0.1:2999/liveclientdata"


class NotInGame(Exception):
    """拿不到对局数据。通常等于"当前不在对局中"，不是错误。"""


class LiveClient:
    """Live Client Data API 的极简只读客户端。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 3.0,
        allow_http_fallback: bool = True,
    ) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = max(0.5, float(timeout))
        self.allow_http_fallback = bool(allow_http_fallback)
        # 自签证书：不校验主机名、不校验链。
        self._ssl = ssl.create_default_context()
        self._ssl.check_hostname = False
        self._ssl.verify_mode = ssl.CERT_NONE
        # 记录上次成功的协议，后续优先用它，省掉一次失败的往返。
        self._prefer_http = self.base_url.startswith("http://")

    # ── 内部 ────────────────────────────────────────────────────

    def _urls(self) -> list[str]:
        primary = self.base_url
        if not self.allow_http_fallback:
            return [primary]
        if primary.startswith("https://"):
            return [primary, primary.replace("https://", "http://", 1)]
        return [primary]

    async def _get_once(self, url: str, session: aiohttp.ClientSession) -> Any:
        async with session.get(url, ssl=self._ssl) as resp:
            if resp.status != 200:
                # 服务在，但这次请求被拒 —— 不换协议重试，直接当"没数据"。
                raise NotInGame(f"HTTP {resp.status}")
            # content_type=None：国服/不同版本返回的 Content-Type
            # 未必是 application/json，不要因此解析失败。
            return await resp.json(content_type=None)

    async def _get(self, path: str, session: aiohttp.ClientSession) -> Any:
        urls = self._urls()
        if self._prefer_http and len(urls) > 1:
            urls = [urls[1], urls[0]]
        last_error: Exception | None = None
        for index, base in enumerate(urls):
            try:
                data = await self._get_once(f"{base}{path}", session)
            except NotInGame as exc:
                # 服务确实在响应（HTTP 非 200），换协议没有意义。
                if str(exc).startswith("HTTP "):
                    raise
                last_error = exc
                continue
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                continue
            # 成功：记住这个协议。
            self._prefer_http = base.startswith("http://")
            return data
        raise NotInGame(str(last_error) if last_error else "unreachable")

    # ── 对外 ────────────────────────────────────────────────────

    @property
    def scheme(self) -> str:
        """最近一次成功使用的协议（``https`` / ``http``）。诊断入口用。"""
        return "http" if self._prefer_http else "https"

    def _session(self) -> aiohttp.ClientSession:
        """一次轮询共用一个会话。

        早年每取一个端点就新建一个 ClientSession —— 2 秒一次轮询、
        每次两个请求，等于每分钟建 60 个连接池。共用一个省掉这些开销，
        同时保留"每次轮询新建"的简单性（不跨事件循环复用）。
        """
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))

    async def snapshot(self) -> tuple[list[dict], dict]:
        """一次取回事件流 + 全量快照（共用一个会话）。

        ``LiveClient`` 的主要入口。两个端点必须来自同一次轮询，
        否则事件和战绩会错开一拍。
        """
        async with self._session() as session:
            raw_events = await self._get("/eventdata", session)
            data = await self._get("/allgamedata", session)
        events = raw_events.get("Events") if isinstance(raw_events, dict) else None
        return (
            [e for e in events if isinstance(e, dict)] if isinstance(events, list) else [],
            data if isinstance(data, dict) else {},
        )

    async def event_data(self) -> list[dict]:
        """只取事件流。

        轮询主路径用 :meth:`snapshot`（两个端点同一会话、同一拍）。
        这个方法留给诊断和 ``smoke_live.py``：那里只需要一个端点，
        不该为了取事件而多打一次 allgamedata。
        """
        async with self._session() as session:
            data = await self._get("/eventdata", session)
        events = data.get("Events") if isinstance(data, dict) else None
        return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []

    async def all_game_data(self) -> dict:
        """只取全量快照：十人名单、KDA、装备、对局时间。

        「连接自检」入口和 ``smoke_live.py`` 用这个 —— 它们要判断的是
        "端口通不通"，取事件流没有意义。
        """
        async with self._session() as session:
            data = await self._get("/allgamedata", session)
        return data if isinstance(data, dict) else {}
