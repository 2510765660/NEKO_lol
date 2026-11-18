"""播报时序仿真：量一量事件从"发生"到"说出口"隔了多久。

    python sim_instant.py

不连游戏、也不连 N.E.K.O —— 用测试里那套假 API + 受控时钟驱动插件状态机，
所以能精确报出"第几秒发生的人头、第几秒说出来"。

它存在的意义：播报策略改完之后，"哪些立刻说、哪些进窗口"只能靠
逐条对账来验证。跑一遍能看出有没有事件被漏掉、或者延迟超出预期。

判定口径跟着当前架构：**直接播报**（开局/一血/击杀/阵亡/结束）应当
立刻出现；其余事件进窗口，等窗口结束才说。

对账用的是插件自己记的 ``_recent``（每条播报的正文），不是猜日志措辞 ——
早先按"击杀了对面的/被对面的"这两个字符串去匹配，结果把"主人拿到了一血！"
误判成没播。
"""

from __future__ import annotations

import asyncio
import sys

TICK = 2.0
ME = "我"

# 参战名单：不在名单里的名字会以玩家名原样出现（接口就是这样的降级行为）
ROSTER = [
    ("我", "Yasuo", "ORDER"),
    ("队友A", "Lulu", "ORDER"),
    ("敌人A", "Zed", "CHAOS"),
    ("敌人B", "LeeSin", "CHAOS"),
]

# (游戏内秒数, 事件) —— 照日志第二局的节奏：人头不算太密，中段一波团灭刷屏
SCRIPT: list[tuple[float, tuple[str, ...]]] = [
    (5.0, ("GameStart",)),
    (138.0, ("FirstBlood", ME)),
    (250.0, ("ChampionKill", ME, "敌人A")),
    (300.0, ("ChampionKill", ME, "敌人B")),
    (330.0, ("Ace", "敌人A")),
    (360.0, ("ChampionKill", ME, "敌人C")),
    (390.0, ("ChampionKill", "敌人A", ME)),  # 主人阵亡
    # ── 一波五杀团战：8 秒内 5 个人头，每个人头各带一条 Multikill ──
    (600.0, ("ChampionKill", ME, "敌人A")),
    (602.0, ("Multikill",)),
    (602.0, ("ChampionKill", "队友A", "敌人B")),
    (604.0, ("Multikill",)),
    (604.0, ("ChampionKill", ME, "敌人C")),
    (606.0, ("Multikill",)),
    (606.0, ("ChampionKill", ME, "敌人D")),
    (608.0, ("Multikill",)),
    (608.0, ("ChampionKill", ME, "敌人E")),
    (609.0, ("Ace", ME)),
    # ── 团战结束 ──
    (660.0, ("BaronKill", ME)),
    (690.0, ("DragonKill", ME)),
    (750.0, ("ChampionKill", ME, "敌人F")),
    (840.0, ("TurretKilled", ME)),
    (855.0, ("InhibKilled", ME)),
    (870.0, ("ChampionKill", ME, "敌人G")),
]

# 上面那波团战的时间范围，用来单独统计它一共发了几条消息
TEAMFIGHT = (600.0, 609.0)

_CHAMPION = {name: champ for name, champ, _team in ROSTER}

# 五杀团战里第 n 条 Multikill 对应的连杀数
_STREAK_BY_TIME = {602.0: 2, 604.0: 3, 606.0: 4, 608.0: 5}


def streak_for(at: float) -> int:
    return _STREAK_BY_TIME.get(at, 2)


def _deps() -> tuple:
    """配好 sys.path 并导入依赖，返回 (fixtures, neko_lol, FakeClock, Harness)。

    插件 import 只能放在函数里：路径设置必须先于 import，而"先在模块级写
    sys.path.insert、再 import"会被 lint 判 E402。
    """
    from pathlib import Path

    here = Path(__file__).resolve().parent
    for path in (str(here / "tests"), str(here)):
        if path not in sys.path:
            sys.path.insert(0, path)

    # tests 包的初始化负责把插件目录接入 sys.path；发布仓库布局下它还会把
    # 插件目录注册成 neko_lol 包（这时目录名不是 neko_lol，普通 import 找不到）。
    import tests

    _ = tests.PLUGIN_DIR

    import fixtures
    import neko_lol
    from test_session import FakeClock, Harness

    return fixtures, neko_lol, FakeClock, Harness


def _make(event: tuple[str, ...], event_id: int, at: float) -> dict:
    fixtures = _deps()[0]
    name = event[0]
    if name == "ChampionKill":
        return fixtures.kill_event(event_id, event[1], event[2], at=at)
    if name == "FirstBlood":
        return fixtures.simple_event(event_id, name, at=at, Recipient=event[1])
    if name == "Ace":
        return fixtures.simple_event(event_id, name, at=at, Acer=event[1])
    if name == "BaronKill":
        return fixtures.simple_event(
            event_id, name, at=at, KillerName=event[1], Stolen="False"
        )
    if name == "DragonKill":
        return fixtures.simple_event(
            event_id, name, at=at, KillerName=event[1], DragonType="Fire"
        )
    if name == "TurretKilled":
        return fixtures.simple_event(
            event_id, name, at=at, KillerName=event[1], TurretKilled="Turret_T2_C_01_A"
        )
    if name == "InhibKilled":
        return fixtures.simple_event(
            event_id, name, at=at, KillerName=event[1], InhibKilled="Inhib_T2_C_01_A"
        )
    return fixtures.simple_event(event_id, name, at=at)


def _expectation(event: tuple[str, ...]) -> tuple[float, str, str] | None:
    """(游戏内秒数, 用来对账的关键词, 说明)。只跟人头/阵亡有关。"""
    name = event[0]
    if name == "FirstBlood":
        return (event[0], "一血", "一血")[0], "一血", "一血"
    if name == "ChampionKill":
        killer, victim = event[1], event[2]
        if killer == ME:
            label = _CHAMPION.get(victim, victim)
            return 0.0, f"击杀了对面的 {label}", f"击杀 {label}"
        if victim == ME:
            label = _CHAMPION.get(killer, killer)
            return 0.0, "主人被对面的", f"被 {label} 击杀"
    return None


def _make_multikill(event_id: int, streak: int, at: float) -> dict:
    fixtures = _deps()[0]
    return fixtures.simple_event(
        event_id, "Multikill", at=at, KillerName=ME, KillStreak=streak
    )


def _players() -> list:
    fixtures = _deps()[0]
    return [
        fixtures.make_player(
            "我", "Yasuo", "ORDER", kills=8, deaths=1, assists=2, cs=90, level=13
        ),
        fixtures.make_player(
            "队友A", "Lulu", "ORDER", kills=1, deaths=2, assists=9, cs=20, level=10
        ),
        fixtures.make_player(
            "敌人A", "Zed", "CHAOS", kills=1, deaths=5, assists=1, cs=70, level=11
        ),
        fixtures.make_player(
            "敌人B", "LeeSin", "CHAOS", kills=0, deaths=4, assists=2, cs=60, level=10
        ),
    ]


async def main() -> int:
    fixtures, neko_lol, FakeClock, Harness = _deps()
    clock = FakeClock()
    neko_lol.time = clock  # type: ignore[assignment]

    h = Harness(clock)
    await h.start()
    h.client.in_game = True
    h.client.events = []
    h.client.data = fixtures.make_all_game_data(game_time=1.0, players=_players())

    fired: list[tuple[float, str, str]] = []   # (秒, 关键词, 说明)
    seen_pushed = 0
    announced: list[tuple[float, str]] = []    # (秒, 播报正文)
    cursor = 0
    event_id = 0

    duration = max(at for at, _ in SCRIPT) + 120
    while True:
        elapsed = clock.now - 1000.0
        if elapsed > duration:
            break

        while cursor < len(SCRIPT) and SCRIPT[cursor][0] <= elapsed:
            at, event = SCRIPT[cursor]
            if event[0] == "Multikill":
                h.client.events.append(_make_multikill(event_id, streak_for(at), at))
            else:
                h.client.events.append(_make(event, event_id, at))
                expected = _expectation(event)
                if expected is not None:
                    fired.append((at, expected[1], expected[2]))
            event_id += 1
            cursor += 1

        h.client.data = fixtures.make_all_game_data(
            game_time=max(1.0, elapsed), players=_players()
        )
        await h.plugin.poll_match()
        clock.advance(TICK)

        # 用真正发出去的播报对账。不要读 _recent —— 它是容量 20 的环形缓冲，
        # 满了之后长度不再增长，按长度做增量切片会漏掉后面所有条目。
        for payload in h.pushed[seen_pushed:]:
            announced.append((elapsed, payload["parts"][0]["text"]))
        seen_pushed = len(h.pushed)

    print(f"喂入 {len(fired)} 个人头/阵亡事件；共发出 {len(h.pushed)} 条播报\n")
    print("人头发生 → 说出口")
    print("-" * 58)
    late = missing = batched = 0
    for at, needle, label in fired:
        hit = next((t for t, body in announced if t >= at - 0.5 and needle in body), None)
        if hit is None:
            missing += 1
            verdict, delta = "缺失", "  ——"
        elif hit - at <= TICK + 0.1:
            verdict, delta = "直接播报", "%+.0fs" % (hit - at)
        else:
            # 进窗口合并是**设计如此**，不是延迟故障。
            batched += 1
            verdict, delta = "窗口合并", "%+.0fs" % (hit - at)
        print(f"  {at:6.0f}s  {label:<12} → {verdict}  ({delta})")

    print()
    print(f"直接播报 {len(fired) - late - missing} / 窗口合并 {batched} / 缺失 {missing}")

    lo, hi = TEAMFIGHT
    during = [
        (t, txt) for t, txt in announced if lo - 1 <= t <= hi + 12
    ]
    print()
    print(f"其中 {lo:.0f}s–{hi:.0f}s 那波五杀团战（8 秒内 5 个人头 + 各带一条 Multikill）")
    print(f"一共发出 {len(during)} 条消息：")
    for t, txt in during:
        print(f"  [{t:6.0f}s] {txt[:76]}")

    print()
    print("全部播报：")
    for i, payload in enumerate(h.pushed, 1):
        body = payload["parts"][0]["text"].replace("\n", " ⏎ ")
        print(f"  {i:2d}. {body[:72]}")
    return 0 if not late and not missing else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
