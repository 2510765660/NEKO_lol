"""分路里程碑：按主人所在分路，在对局里挑几个"值得说一声"的时刻。

## 为什么按分路

同一条规则套所有位置会很荒谬：辅助的 KPI 是助攻数，ADC 的是补刀领先，
上单的是对位压制。所以这里每个分路一套判定，各自独立触发。

## 只信可信的分路数据

所有判定都要求 ``lanes_trusted``（召唤师峡谷 + 征召模式 + 用户开了
``infer_lane_positions``）。普通匹配没有位置选择流程，数组顺序不保证，
拿它推分路会一本正经地胡说。不可信时 :meth:`check` 直接返回 None。

## 状态从哪来

接口只给**当前快照**（KDA、补刀、等级），不给历史。所以"对位单杀三次"
这种累积指标只能由插件自己数 —— 就是本模块的 ``_state`` 字典。
它跟着一局走，换局时由插件调用 :meth:`reset`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .game_model import LANE_NAMES, MatchState


@dataclass
class MilestoneCue:
    """一条里程碑播报的内容。"""

    key: str
    text: str
    # 同一个 key 一局只播一次（除非规则自己用 ``times`` 允许重复）。
    times: int = 1


@dataclass
class LaneMilestones:
    """按分路累计状态并判定里程碑。"""

    # 队伍总人头达到这个数时，打野播一次。用户 2026-11-16 指定 10。
    jungle_team_kills: int = 10
    # 打野还要求"主人参与度"达到这个比例，否则等于把队友的功劳算给主人。
    jungle_participation: float = 0.6
    # 上单：多少分钟内完成多少次对位单杀（且全程没被对面击杀/助攻击杀）。
    top_deadline_seconds: float = 15 * 60
    top_kills_needed: int = 3
    # 中路：多少分钟内完成转移击杀（击杀者不是主人，主人在助攻名单里）。
    mid_deadline_seconds: float = 12 * 60
    mid_assists_needed: int = 5
    # ADC：多少分钟内补刀领先对面 ADC 多少。
    adc_deadline_seconds: float = 20 * 60
    adc_cs_lead: int = 50
    # 辅助：第几个助攻时报一次。
    support_assist_steps: tuple[int, ...] = (10, 20)

    _state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        """本局状态归零。换局时必须调用，否则上一局的计数会延续。"""
        self._state = {
            "said": set(),           # 已播过的里程碑 key
            # 上单：对位单杀次数 / 是否被对面（同分路的那个）击杀或助攻过
            "top_solo_kills": 0,
            "top_opponent": "",      # 对位选手的玩家名（只用于匹配，不播）
            "top_opponent_champion": "",  # 对位选手的英雄名（播报用）
            "top_disqualified": False,
            # 中路：12 分钟内的转移击杀次数
            "mid_rotations": 0,
            # ADC：上一次观察到的补刀差（用于日志）
            "adc_last_lead": 0,
            # 辅助：上一次播报的阶梯
            "support_last_step": 0,
            # 打野：队伍人头是否已经播过
            "jungle_said": False,   # 保留字段：旧快照可能还带着它
        }

    # ── 主入口 ──────────────────────────────────────────────────

    def check(self, state: MatchState) -> list[MilestoneCue]:
        """检查"此刻"是否该播里程碑。返回 0~1 条。

        只在轮询里调用 —— 它读的是当前快照，不依赖具体事件。
        """
        if not state.lanes_trusted or state.me is None:
            return []
        lane = state.my_lane
        handler = {
            "上单": self._check_top,
            "打野": self._check_jungle,
            "中单": self._check_mid,
            "ADC": self._check_adc,
            "辅助": self._check_support,
        }.get(lane)
        if handler is None:
            return []
        cue = handler(state)
        return [cue] if cue is not None else []

    # ── 事件侧：需要逐条看击杀的地方 ────────────────────────────

    def note_kill(self, event: dict, state: MatchState) -> None:
        """喂一条 ``ChampionKill``，维护需要"逐次计数"的状态。

        上单的对位单杀、中路的转移击杀、以及"被对面上单击杀过"的取消资格，
        都只能这样累计 —— 接口不给历史。
        """
        if not state.lanes_trusted or state.me is None:
            return
        lane = state.my_lane
        if lane not in ("上单", "中单"):
            return

        killer = str(event.get("KillerName") or "").strip()
        victim = str(event.get("VictimName") or "").strip()
        assisters = [str(a or "").strip() for a in (event.get("Assisters") or [])
                     if isinstance(a, str)]

        if lane == "上单":
            opponent = state.lane_opponent()
            if opponent is None:
                return
            self._state["top_opponent"] = opponent.name
            self._state["top_opponent_champion"] = opponent.champion or opponent.name
            # 我被对位选手（或他对我的击杀里有助攻）击杀 → 取消资格
            if state.is_me(victim) and self._is_opponent(self._state["top_opponent"], killer, state):
                self._state["top_disqualified"] = True
                return
            if state.is_me(victim) and any(
                self._is_opponent(self._state["top_opponent"], a, state) for a in assisters
            ):
                self._state["top_disqualified"] = True
                return
            # 对位单杀：我击杀的正是对位那个人，且没有助攻
            if state.is_me(killer) and not assisters and self._is_opponent(
                self._state["top_opponent"], victim, state
            ):
                self._state["top_solo_kills"] += 1
            return

        # 中单：转移击杀 = 不是我杀、我助攻，且被杀的不是我的对位
        if lane == "中单":
            if state.is_me(killer) or state.is_me(victim):
                return
            if not any(state.is_me(a) for a in assisters):
                return
            opponent = state.lane_opponent()
            if opponent is not None and self._is_opponent(opponent.name, victim, state):
                return    # 这是对位击杀，不算"转移"
            self._state["mid_rotations"] += 1

    def _is_opponent(self, opponent_name: str, candidate: str, state: MatchState) -> bool:
        """``candidate`` 是不是我方对位的那位。

        只比较玩家名/归一化名：对位关系本身已经由数组下标确定，
        这里不该再按英雄名兜底（否则同英雄会误判）。
        """
        if not opponent_name or not candidate:
            return False
        if candidate == opponent_name:
            return True
        from .game_model import name_key

        return bool(name_key(candidate)) and name_key(candidate) == name_key(opponent_name)

    # ── 各分路判定 ──────────────────────────────────────────────

    def _once(self, key: str) -> bool:
        if key in self._state["said"]:
            return False
        self._state["said"].add(key)
        return True

    def _check_top(self, state: MatchState) -> MilestoneCue | None:
        """15 分钟内对位单杀 3 次，且全程没被对面单杀/参与击杀。"""
        if self._state["top_disqualified"]:
            return None
        if state.game_time > self.top_deadline_seconds:
            return None
        if self._state["top_solo_kills"] < self.top_kills_needed:
            return None
        if not self._once("top_solo_triple"):
            return None
        # 报的是英雄名，不是玩家名 —— 早先存了 opponent.name，播出去变成
        # "单杀对位 欲将爱意隐于风中 3 次"这种玩家昵称，实测很难读。
        opponent = (
            self._state["top_opponent_champion"]
            or self._state["top_opponent"]
            or "对位"
        )
        # 措辞只讲成绩，不复述触发条件（"15 分钟前""3 次""没被抓掉"
        # 那套是代码里的判定，不该念给用户听）。
        return MilestoneCue(
            key="milestone_top",
            text=f"主人上路打穿了，{opponent} 已经被单杀三次。",
        )

    def _check_jungle(self, state: MatchState) -> MilestoneCue | None:
        """全队人头超过阈值，且**主人参与其中 ≥ 60%** 时才播。

        只卡全队人头是不够的：主人一次 gank 都没去、队友自己在打，也会
        触发"打野这局带得不错"，等于把功劳硬塞给主人。
        参与度 = (主人击杀 + 主人助攻) / 全队人头。
        """
        if state.me is None:
            return None
        team_kills = sum(a.kills for a in state.allies)
        if team_kills <= self.jungle_team_kills:
            return None

        involved = state.me.kills + state.me.assists
        rate = involved / team_kills if team_kills else 0.0
        if rate < self.jungle_participation:
            return None

        if not self._once("jungle_team_kills"):
            return None
        return MilestoneCue(
            key="milestone_jungle",
            text=(
                f"主人这局节奏带得好：全队 {team_kills} 个人头里，"
                f"主人参与了 {involved} 个。"
            ),
        )

    def _check_mid(self, state: MatchState) -> MilestoneCue | None:
        """12 分钟内完成 5 次转移击杀。"""
        if state.game_time > self.mid_deadline_seconds:
            return None
        if self._state["mid_rotations"] < self.mid_assists_needed:
            return None
        if not self._once("mid_rotation_five"):
            return None
        return MilestoneCue(
            key="milestone_mid",
            text=f"主人中路游走很凶，队友的人头里有 {self._state['mid_rotations']} 个是主人帮出来的。",
        )

    def _check_adc(self, state: MatchState) -> MilestoneCue | None:
        """20 分钟前补刀领先对面 ADC 50 个。"""
        if state.game_time > self.adc_deadline_seconds:
            return None
        opponent = state.lane_opponent()
        if opponent is None or state.me is None:
            return None
        lead = state.me.cs - opponent.cs
        self._state["adc_last_lead"] = lead
        if lead < self.adc_cs_lead:
            return None
        if not self._once("adc_cs_lead"):
            return None
        return MilestoneCue(
            key="milestone_adc",
            text=f"主人补刀压得很死，对面 ADC 已经被甩开 {lead} 刀了。",
        )

    def _check_support(self, state: MatchState) -> MilestoneCue | None:
        """助攻数跨过 10 / 20 时各播一次。"""
        if state.me is None:
            return None
        assists = state.me.assists
        hit = 0
        for step in self.support_assist_steps:
            if assists >= step:
                hit = step
        if hit == 0 or hit <= self._state["support_last_step"]:
            return None
        self._state["support_last_step"] = hit
        return MilestoneCue(
            key="milestone_support",
            text=f"主人已经 {hit} 个助攻了，这局跟团和保人做得很好。",
        )

    # ── 诊断 ────────────────────────────────────────────────────

    def snapshot(self, state: MatchState) -> dict[str, Any]:
        """给面板显示的当前进度（不含任何文案）。"""
        lane = state.my_lane if state.lanes_trusted else ""
        data: dict[str, Any] = {"lane": lane, "enabled": bool(lane)}
        if lane == "上单":
            data.update(
                solo_kills=self._state["top_solo_kills"],
                need=self.top_kills_needed,
                disqualified=bool(self._state["top_disqualified"]),
            )
        elif lane == "打野":
            team_kills = sum(a.kills for a in state.allies)
            involved = (state.me.kills + state.me.assists) if state.me else 0
            data.update(
                team_kills=team_kills,
                involved=involved,
                participation=(
                    round(involved / team_kills, 2) if team_kills else 0.0
                ),
                need=self.jungle_team_kills,
                need_rate=self.jungle_participation,
            )
        elif lane == "中单":
            data.update(
                rotations=self._state["mid_rotations"], need=self.mid_assists_needed
            )
        elif lane == "ADC":
            opponent = state.lane_opponent()
            data.update(
                cs=state.me.cs if state.me else 0,
                opp_cs=opponent.cs if opponent else 0,
                need=self.adc_cs_lead,
            )
        elif lane == "辅助":
            assists = state.me.assists if state.me else 0
            data.update(
                assists=assists,
                next_step=self._next_support_step(assists),
                # 面板统一读 need 显示"目标值"，辅助这里的目标是"下一个阶梯"。
                need=self._next_support_step(assists) or self.support_assist_steps[-1],
            )
        return data

    def _next_support_step(self, assists: int) -> int:
        for step in self.support_assist_steps:
            if assists < step:
                return step
        return 0


# 分路中文名，供面板/日志用（与 LANE_NAMES 同源）。
LANE_LABELS: tuple[str, ...] = LANE_NAMES
