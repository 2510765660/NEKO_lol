"""测试夹具：伪造 Live Client Data API 的返回结构。

真实的 ``/allgamedata`` 结构见 Riot 官方文档。这里只保留插件实际读的字段，
另外特意**故意**包含国服常见的形态差异（``summonerName`` 为空、只有
``riotId``），因为那正是最容易出 bug 的地方。
"""

from __future__ import annotations


def make_player(
    name: str,
    champion: str,
    team: str,
    *,
    kills: int = 0,
    deaths: int = 0,
    assists: int = 0,
    cs: int = 0,
    level: int = 1,
    is_dead: bool = False,
    respawn: float = 0.0,
    items: list | None = None,
    riot_id_only: bool = True,
) -> dict:
    """造一条 allPlayers 记录。

    ``riot_id_only=True`` 模拟国服：``summonerName`` 是空串，名字只在
    ``riotId`` 的 ``名字#TAG`` 里。
    """
    return {
        "riotId": f"{name}#CN1",
        "summonerName": "" if riot_id_only else name,
        "championName": champion,
        "team": team,
        "level": level,
        "isDead": is_dead,
        "respawnTimer": respawn,
        "scores": {
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "creepScore": cs,
            "wardScore": 0,
        },
        "items": items or [],
    }


def make_all_game_data(
    *,
    game_time: float = 600.0,
    players: list | None = None,
    active: dict | None = None,
    map_number: int = 11,
) -> dict:
    return {
        "activePlayer": active or {"riotId": "我#CN1", "summonerName": "", "level": 11},
        "allPlayers": players
        if players is not None
        else [
            make_player("我", "Yasuo", "ORDER", kills=3, deaths=1, assists=2, cs=120, level=11),
            make_player("敌人A", "Zed", "CHAOS", kills=1, deaths=3, assists=1, cs=90, level=9),
            make_player("敌人B", "LeeSin", "CHAOS", kills=0, deaths=2, assists=4, cs=80, level=8),
            make_player("队友A", "Lulu", "ORDER", kills=0, deaths=1, assists=6, cs=20, level=8),
        ],
        "gameData": {
            "gameMode": "CLASSIC",
            "gameTime": game_time,
            "mapName": f"Map{map_number}",
            "mapNumber": map_number,
        },
        "events": {"Events": []},
    }


def kill_event(
    event_id: int,
    killer: str,
    victim: str,
    *,
    at: float = 0.0,
    assisters: list | None = None,
) -> dict:
    event = {
        "EventID": event_id,
        "EventName": "ChampionKill",
        "EventTime": at,
        "KillerName": killer,
        "VictimName": victim,
    }
    if assisters:
        event["Assisters"] = assisters
    return event


def simple_event(event_id: int, name: str, at: float = 0.0, **extra) -> dict:
    return {"EventID": event_id, "EventName": name, "EventTime": at, **extra}
