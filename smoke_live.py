"""真机自检：直接打本机 2999 端口，看客户端在真实网络栈下的行为。

单元测试里的客户端是假的，所以"连不上时会不会抛 SSL 错、会不会卡住、
https→http 回落对不对"这些只有真跑一次才知道。

不需要 N.E.K.O，也不需要开着游戏：

    python smoke_live.py

* 没开游戏 → 应当如实报「没在对局中」，而不是卡住或抛 SSL 异常
* 开着游戏 → 应当读到实时战况，并打印出来核对

这是排查"插件装了但没反应"最快的一步：如果这里连不上，插件里也一定连不上。

关于耗时：某些安全软件 / 防火墙过滤驱动（火绒、游戏加加这类）会给
"被拒绝的本地连接"叠加固定延迟。在本机上实测**每次被拒连接约 2.05 秒**
（连原始 socket 打任意空闲端口都一样，与本插件无关）。所以没开游戏时
总耗时约 4 秒（https 试一次 + http 回落试一次）是正常的。判断依据是
"有没有干净地报出被拒绝"，而不是"快不快"。
"""

from __future__ import annotations

import asyncio
import sys
import time


def _setup_path() -> None:
    """把插件目录接入 sys.path。

    必须在 import 插件之前执行，所以调用方只能把插件 import 放在函数里 ——
    模块级"先写代码再 import"会被 lint 判 E402。
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for path in (str(here), str(here.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)

    # tests 包的初始化负责把插件注册成 neko_lol（发布仓库布局下目录名不是
    # neko_lol，直接 import 找不到）。
    import tests

    _ = tests.PLUGIN_DIR


async def main() -> int:
    _setup_path()
    from neko_lol.game_model import MatchState
    from neko_lol.live_client import LiveClient, NotInGame

    client = LiveClient(timeout=3.0)
    print(f"探测 {client.base_url}（http 回落={'开' if client.allow_http_fallback else '关'}）")

    started = time.monotonic()
    try:
        data = await client.all_game_data()
    except NotInGame as exc:
        elapsed = time.monotonic() - started
        print(f"\n[没在对局中] 耗时 {elapsed:.2f}s")
        print(f"  原因：{exc}")
        print(f"  协议：{client.scheme}")
        if "timeout" in str(exc).lower() or "超时" in str(exc):
            print("  !! 是超时而不是被拒绝 —— 有东西在静默丢包，")
            print("     检查代理 / 加速器 / 安全软件是否拦截了 127.0.0.1:2999")
        elif elapsed > 6.0:
            print(f"  !! {elapsed:.1f} 秒偏慢。本机被拒连接实测约 2 秒/次，")
            print("     https + http 两次合计约 4 秒属正常，明显超出再排查。")
        print("\n  没在打游戏时这是正常结果，不是故障。")
        return 0

    elapsed = time.monotonic() - started
    state = MatchState.from_all_game_data(data, trust_lane_order=True)
    print(f"\n[在对局中] 耗时 {elapsed:.2f}s，协议 {client.scheme}")
    print(f"  地图：{state.map_name}·{state.game_mode}")
    print(f"  时间：{state.game_time_label}")
    print(f"  比分：{state.score_line()}")
    if state.me is not None:
        print(f"  我　：{state.me.one_line(with_items=True)}")
        print(f"  经济：{int(state.me.estimated_gold)}（估算）")
    else:
        print("  !! 读不到自己的数据 —— 名字匹配失败，请把这份输出发出来")

    others = [a for a in state.allies if a is not state.me]
    if others:
        print("  队友：" + "；".join(a.one_line() for a in others))
    if state.enemies:
        print("  敌方：" + "；".join(e.one_line() for e in state.enemies))

    try:
        events = await client.event_data()
    except NotInGame as exc:
        print(f"  !! 快照读到了但事件流读不到：{exc}")
        return 1
    print(f"  事件：本局已产生 {len(events)} 条")
    for event in events[-5:]:
        print(f"    #{event.get('EventID')} {event.get('EventName')} @{event.get('EventTime')}")

    # ── 原始事件核对 ─────────────────────────────────────────────
    # 字段名是按 Riot 官方 schema 写的，但"国服到底填不填"只能在真机上验。
    # 尤其是 ChampionKill 的 Assisters（助攻数组）—— 插件用它来说
    # "主人击杀了对面的 X（助攻：某某）"，空数组就等于从没说过。
    kills = [e for e in events if e.get("EventName") == "ChampionKill"]
    if kills:
        print("\n  最近一条 ChampionKill 的原始字段（核对助攻是否可用）：")
        last = kills[-1]
        for key in ("EventID", "EventTime", "KillerName", "VictimName", "Assisters"):
            mark = "" if key in last else "   ← 字段缺失"
            print(f"    {key:<12} = {last.get(key)!r}{mark}")
        if "Assisters" not in last:
            print("    !! 接口没给 Assisters —— 助攻播报永远不会出现")
        elif not last.get("Assisters"):
            print("    !  Assisters 是空数组。也许这条击杀本来就没助攻，")
            print("      多打几条再看；若一直为空，说明国服不填这个字段。")
        else:
            print("    ✓ Assisters 有内容，助攻播报可用")
    else:
        print("  本局还没有 ChampionKill 事件，无法核对 Assisters。")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
