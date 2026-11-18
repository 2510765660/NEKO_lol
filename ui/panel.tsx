// N.E.K.O Hosted TSX 面板：播报开关 + 实时状态 + 分路里程碑进度。
//
// 运行时不是 React，是 N.E.K.O 自研的 VDOM。约定（照 lifekit / minecraft
// 的现役面板抄的，别自己发明）：
//
// * 组件、hooks、类型一律从 "@neko/plugin-ui" 导入，**不要** import npm 包。
// * 必须 default export 一个函数组件，收 props: PluginSurfaceProps<State>。
// * props.state 是 Python 侧 @ui.context(id="lol_settings") 的返回值。
// * props.api.call(actionId, payload) 返回信封，真正的结果在 .result 里。
// * surface 跑在 sandbox="allow-scripts"（无 allow-same-origin）的 iframe 里，
//   裸 fetch 会因不透明源被拒 —— 一切后端交互都必须走 props.api.call。
//
// 用的组件都已在真机上跑过（Page / Card / Stack / Grid / StatCard /
// KeyValue / Divider / StatusBadge / Switch / Text / Alert）。
// 不引入没验证过的组件 —— 这个运行时一旦 import 到不存在的导出，
// 面板整块白屏，且报错只出现在宿主控制台里，很难查。
//
// 文案策略：**全部内联中文，不走 props.t()**。
// 早期版本用 t("events.<key>") 取文案，结果要依赖宿主 locale 能否解析到
// zh-CN 语言包；一旦解析落到别的语言（或语言包没加载），面板就会显示英文
// 甚至原始 key。选项文本不该由 locale 决定，所以这里直接写死中文。
// 事件的中文名由 Python 侧 _EVENT_LABELS 通过 context 的 label 字段下发，
// 分组顺序也由 Python 侧 event_groups 下发，TSX 不维护第二份分组表。
//
// 动作 set_announce / set_dry_run 都声明了 refresh_context=True，所以每次
// 调用成功后宿主会自动重拉 props.state —— 这就是本地状态最终对齐服务端的
// 那条路，不必手动刷新。

import {
  Alert,
  Card,
  Columns,
  Divider,
  Grid,
  KeyValue,
  Page,
  Progress,
  Stack,
  StatCard,
  StatusBadge,
  Switch,
  Tabs,
  Text,
  Tip,
  useEffect,
  useState,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

type EventToggle = {
  key: string
  label: string
  enabled: boolean
}

type EventGroup = {
  id: string
  title: string
  keys: string[]
}

type LaneInfo = {
  lane?: string
  trusted?: boolean
  opponent?: string
}

type MilestoneProgress = {
  lane?: string
  enabled?: boolean
  // 各分路自己的进度字段（由 Python 侧 snapshot 决定，缺省即不显示）。
  solo_kills?: number
  need?: number
  disqualified?: boolean
  team_kills?: number
  involved?: number
  participation?: number
  need_rate?: number
  rotations?: number
  cs?: number
  opp_cs?: number
  assists?: number
  next_step?: number
}

type SettingsState = {
  events?: EventToggle[]
  event_groups?: EventGroup[]
  dry_run?: boolean
  show_prompt_in_chat?: boolean
  enabled?: boolean
  in_game?: boolean
  game_time?: string
  queued?: number
  backend?: string
  lane?: LaneInfo
  milestone?: MilestoneProgress
  milestone_enabled?: boolean
  score?: string
  window_seconds?: number
  normal_gap?: number
}

// props.api.call 解析成 { plugin_id, action_id, result }；这里把结果剥出来。
// 同时兼容将来宿主直接返回 entry 值的情况。
function unwrapActionResult(envelope: any): Record<string, any> {
  if (envelope && typeof envelope === "object") {
    if (envelope.result && typeof envelope.result === "object") return envelope.result
    return envelope
  }
  return {}
}

// 里程碑进度的可读文案。数值由 Python 给，措辞在面板侧定。
function milestoneLine(progress: MilestoneProgress): string {
  const p = progress || {}
  if (p.disqualified) return "上单里程碑已失效（被对位击杀过）"
  switch (p.lane) {
    case "上单":
      return "对位单杀 " + (p.solo_kills ?? 0) + " / " + (p.need ?? 3)
    case "打野": {
      const rate =
        typeof p.participation === "number"
          ? Math.round(p.participation * 100) + "%"
          : "—"
      return (
        "全队人头 " + (p.team_kills ?? 0) + "（超过 " + (p.need ?? 10) + " 触发）" +
        " · 主人参与 " + (p.involved ?? 0) + " 个（" + rate + "）"
      )
    }
    case "中单":
      return "转移击杀 " + (p.rotations ?? 0) + " / " + (p.need ?? 5)
    case "ADC": {
      const lead = (p.cs ?? 0) - (p.opp_cs ?? 0)
      return (
        "补刀 " + (p.cs ?? 0) + " 对 " + (p.opp_cs ?? 0) +
        "（领先 " + lead + " / " + (p.need ?? 50) + "）"
      )
    }
    case "辅助":
      return p.next_step
        ? "助攻 " + (p.assists ?? 0) + "，下个里程碑 " + p.next_step
        : "助攻 " + (p.assists ?? 0) + "（里程碑已全部达成）"
    default:
      return "分路未识别"
  }
}

// 里程碑的完成度百分比（给 Progress 用）。没在推进的返回 undefined，
// Progress 会显示成不定态而不是"0%"。
function milestonePercent(p: MilestoneProgress): number | undefined {
  const v = p || {}
  if (v.disqualified) return 0
  switch (v.lane) {
    case "上单":
      return Math.min(100, Math.round(((v.solo_kills ?? 0) / (v.need ?? 3)) * 100))
    case "打野":
      return Math.min(100, Math.round(((v.team_kills ?? 0) / (v.need ?? 10)) * 100))
    case "中单":
      return Math.min(100, Math.round(((v.rotations ?? 0) / (v.need ?? 5)) * 100))
    case "ADC": {
      const lead = (v.cs ?? 0) - (v.opp_cs ?? 0)
      return Math.max(0, Math.min(100, Math.round((lead / (v.need ?? 50)) * 100)))
    }
    case "辅助": {
      const step = v.next_step || 0
      if (!step) return 100
      return Math.min(100, Math.round(((v.assists ?? 0) / step) * 100))
    }
    default:
      return undefined
  }
}

// 某一组开关里开着几个 / 共几个。给卡片标题做摘要用。
function groupSummary(
  keys: string[],
  local: Record<string, boolean>,
  events: EventToggle[],
): string {
  const valid = keys.filter((k) => events.some((e) => e.key === k))
  const on = valid.filter((k) => local[k] !== false).length
  return on + " / " + valid.length + " 开"
}

export default function NekoLolSettingsPanel(props: PluginSurfaceProps<SettingsState>) {
  const state = props.state || {}
  const events = Array.isArray(state.events) ? state.events : []
  const groups = Array.isArray(state.event_groups) ? state.event_groups : []
  const lane = state.lane || {}
  const milestone = state.milestone || {}

  // 乐观本地状态：点一下立刻翻，不等网络往返。服务端刷新回来后以服务端为准。
  const [local, setLocal] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState<Record<string, boolean>>({})
  const [dryRun, setDryRun] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const [promptBusy, setPromptBusy] = useState(false)
  const [dryBusy, setDryBusy] = useState(false)
  const [error, setError] = useState("")

  // 用序列化签名做依赖，避免依赖对象引用 —— state 每次刷新都是新对象，
  // 直接依赖它容易变成"刷新→setState→再刷新"的循环。
  const signature = JSON.stringify({
    events: events.map((item) => [item.key, item.enabled !== false]),
    dry: !!state.dry_run,
    prompt: !!state.show_prompt_in_chat,
  })

  useEffect(() => {
    const next: Record<string, boolean> = {}
    for (const item of events) next[item.key] = item.enabled !== false
    setLocal(next)
    setDryRun(!!state.dry_run)
    setShowPrompt(!!state.show_prompt_in_chat)
  }, [signature])

  const toggleEvent = async (key: string, value: boolean) => {
    setLocal((prev) => ({ ...prev, [key]: value }))
    setBusy((prev) => ({ ...prev, [key]: true }))
    setError("")
    try {
      await props.api.call("set_announce", { event: key, enabled: value })
    } catch (exc: any) {
      // 失败就把开关翻回去，别让界面骗人。
      setLocal((prev) => ({ ...prev, [key]: !value }))
      setError(String((exc && exc.message) || exc))
    } finally {
      setBusy((prev) => ({ ...prev, [key]: false }))
    }
  }

  const toggleDryRun = async (value: boolean) => {
    setDryRun(value)
    setDryBusy(true)
    setError("")
    try {
      await props.api.call("set_dry_run", { enabled: value })
    } catch (exc: any) {
      setDryRun(!value)
      setError(String((exc && exc.message) || exc))
    } finally {
      setDryBusy(false)
    }
  }

  const toggleShowPrompt = async (value: boolean) => {
    setShowPrompt(value)
    setPromptBusy(true)
    setError("")
    try {
      await props.api.call("set_show_prompt_in_chat", { enabled: value })
    } catch (exc: any) {
      setShowPrompt(!value)
      setError(String((exc && exc.message) || exc))
    } finally {
      setPromptBusy(false)
    }
  }

  const renderSwitch = (key: string) => {
    const item = events.find((e) => e.key === key)
    if (!item) return null
    return (
      <Switch
        key={item.key}
        checked={local[item.key] !== false}
        label={item.label || item.key}
        disabled={!!busy[item.key]}
        onChange={(value) => toggleEvent(item.key, value)}
      />
    )
  }

  // 有分组就按分组渲染；拿不到分组时退回平铺，别让面板空白。
  const sections = groups.length
    ? groups
    : [{ id: "all", title: "播报开关", keys: events.map((e) => e.key) }]

  const totalOn = events.filter((e) => local[e.key] !== false).length
  const laneText = lane.trusted ? lane.lane || "未识别" : "不可信"
  const percent = milestonePercent(milestone)

  // Tabs 是受控组件：不传 activeId 它会自己用 useLocalState 记，
  // 但那样拿不到"当前是哪个 tab"，没法按 tab 渲染不同内容。
  const [tab, setTab] = useState("broadcast")
  const tabs = [
    { id: "broadcast", label: "播报开关" },
    { id: "milestone", label: "分路里程碑" },
    { id: "runtime", label: "运行参数" },
  ]

  return (
    <Page
      title={props.plugin.name}
      subtitle="选择猫娘要对哪些对局事件开口。改动立即生效，并会写回配置。"
    >
      <Card title="当前状态">
        <Stack>
          <Grid cols={3}>
            <StatCard
              label="对局"
              value={state.in_game ? "进行中" : "未开始"}
            />
            <StatCard label="对局时间" value={state.game_time || "—"} />
            <StatCard label="已开启播报" value={totalOn + " / " + events.length} />
          </Grid>
          <Grid cols={3}>
            <StatCard label="我的分路" value={laneText} />
            <StatCard label="对位英雄" value={lane.opponent || "—"} />
            <StatCard label="排队中" value={String(state.queued ?? 0)} />
          </Grid>
          <StatusBadge tone={state.in_game ? "success" : "default"}>
            {state.in_game
              ? "对局中" + (state.game_time ? " · " + state.game_time : "")
              : "不在对局中"}
          </StatusBadge>
          {state.score ? <Text>{state.score}</Text> : null}
        </Stack>
      </Card>

      <Tabs id="neko-lol-settings" items={tabs} activeId={tab} onChange={setTab}>
        {tab === "broadcast" ? (
          <Stack>
            <Tip>
              关掉的事件完全不会说话，连排队都不会。改动立即生效并写回配置。
            </Tip>
            {state.milestone_enabled !== false ? null : (
              <Alert tone="warning">
                分路里程碑当前是关闭的，可以在「分路里程碑」页重新打开。
              </Alert>
            )}
            <Columns fluid minWidth={280} gap={12}>
              {sections.map((group) => (
                <Card
                  key={group.id}
                  title={
                    group.title + "　·　" + groupSummary(group.keys, local, events)
                  }
                >
                  <Stack>{group.keys.map((key) => renderSwitch(key))}</Stack>
                </Card>
              ))}
            </Columns>

            <Card title="测试">
              <Stack>
                <Switch
                  checked={dryRun}
                  label="干跑（只写日志，不让猫娘开口）"
                  disabled={dryBusy}
                  onChange={toggleDryRun}
                />
                <Text>
                  调试事件识别时打开：完整走一遍识别和仲裁，但只在日志里留下痕迹。确认识别无误后关掉。
                </Text>
                <Divider />
                <Switch
                  checked={showPrompt}
                  label="在聊天里显示提示文本"
                  disabled={promptBusy}
                  onChange={toggleShowPrompt}
                />
                <Text>
                  关掉（默认）时猫娘照样读到并带语音回应，只是聊天窗口里看不到
                  那段括号提示。想确认模型收到了什么，就打开这个。
                </Text>
              </Stack>
            </Card>
          </Stack>
        ) : null}

        {tab === "milestone" ? (
          <Stack>
            <Card title="开关">
              <Stack>
                {renderSwitch("milestone")}
                <Text>
                  按主人所在分路给阶段性播报，只在召唤师峡谷征召局、且分路可信时生效。
                </Text>
              </Stack>
            </Card>

            {state.in_game ? (
              <Card title={"当前进度 · " + (milestone.lane || "未识别")}>
                <Stack>
                  <Text>{milestoneLine(milestone)}</Text>
                  {percent === undefined ? null : (
                    <Progress value={percent} label="完成度" />
                  )}
                  {milestone.disqualified ? (
                    <Alert tone="warning">
                      上单里程碑已失效：本局被对位击杀过。
                    </Alert>
                  ) : null}
                </Stack>
              </Card>
            ) : (
              <Card title="当前进度">
                <Text>不在对局中。开始一局后这里会显示实时进度。</Text>
              </Card>
            )}

            <Card title="判定条件">
              <Stack>
                <KeyValue
                  data={{
                    "上单": "15 分钟内对位单杀 3 次，且全程没被对位击杀或参与击杀",
                    "打野": "全队人头超过 10，且主人参与度 ≥ 60%",
                    "中单": "12 分钟内 5 次转移击杀（我助攻、非对位）",
                    "ADC": "20 分钟前补刀领先对面 ADC 50",
                    "辅助": "第 10 / 20 个助攻各播一次",
                  }}
                />
                <Divider />
                <Text>
                  每条里程碑一局只播一次（辅助是 10 和 20 两次）。播报只讲成绩，
                  不复述上面这些判定条件。
                </Text>
              </Stack>
            </Card>
          </Stack>
        ) : null}

        {tab === "runtime" ? (
          <Stack>
            <Card title="节流">
              <Stack>
                <KeyValue
                  data={{
                    "常规档间隔": (state.normal_gap ?? 3) + " 秒",
                    "合并窗口": (state.window_seconds ?? 10) + " 秒",
                  }}
                />
                <Text>
                  常规档（击杀、阵亡、龙）隔够间隔就立刻说；环境档（塔、兵营、
                  先锋、小结）进窗口合并。关键档（团灭、大龙、超神）永远立刻说。
                </Text>
              </Stack>
            </Card>

            <Card title="链路">
              <KeyValue
                data={{
                  "接口": state.backend || "—",
                  "分路推断": lane.trusted ? "当前局可用" : "不可用（非峡谷征召）",
                  "插件启用": state.enabled === false ? "否" : "是",
                  "干跑": dryRun ? "开" : "关",
                }}
              />
            </Card>
          </Stack>
        ) : null}
      </Tabs>

      {error ? <Alert tone="warning">{"设置失败：" + error}</Alert> : null}
    </Page>
  )
}
