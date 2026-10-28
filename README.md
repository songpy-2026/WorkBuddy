# Buddy 加油站 · GitHub Actions 自动化方案

在 GitHub 仓库免费的 `schedule`/手动触发定时任务上，每天定时运行一次，于**同一次运行内**完成：

1. 自动签到（Buddy 加油站）；
2. 派猫猫旅行全自动闭环（先领取已到家的旅行积分，再判断能否派新一趟，当天已派过则跳过）。

运行结果**仅打印到 Actions 日志**，不对外通知（无企业微信推送）。完全运行于 GitHub 云端 runner，**无需本地常开设备**。

---

## 目录结构

```
.
├── .github/workflows/buddy-station.yml   # 定时任务定义（schedule + workflow_dispatch）
├── scripts/
│   ├── refresh_token.py                  # 【本地】读取本机登录态，导出长效 token 供注入 Secret
│   └── buddy_station_ci.py               # 【云端】刷新 token → 签到 → 派猫闭环（结果打印到日志）
└── README.md
```

---

## 凭证模型（满足「云端无本机登录态」约束）

| 项目                                  | 说明                                                                      |
| ----------------------------------- | ----------------------------------------------------------------------- |
| 本机（你已登录 WorkBuddy 的电脑）              | 运行 `scripts/refresh_token.py`，读取登录态、导出**长效 refresh token**，并调用刷新接口验证可用。 |
| 仓库 Secret `WORKBUDDY_REFRESH_TOKEN` | 填入上面的长效 token。云端每次运行先用它换新 `accessToken`，因此只配置一次即可长期有效。                  |

> 云端脚本**绝不读取任何本地登录态**，token 明文也不写入仓库或日志：
>
> - 敏感字段在日志中通过 `redact()` 递归脱敏（`accessToken`/`refreshToken` 一律显示为 `***`）；
> - CI 脚本启动时还会调用 `::add-mask::` 让 GitHub 自动遮盖该 Secret。

---

## 快速开始

### 1. 本地导出长效 token

在你**已登录 WorkBuddy 客户端**的电脑上执行：

```bash
PY="$(command -v python3 || command -v python)"
"$PY" scripts/refresh_token.py
```

脚本会打印一行：

```
WORKBUDDY_REFRESH_TOKEN=eyJhbGci....（一长串）
```

复制这一整行（不要引号、不要空格）。

### 2. 配置仓库 Secrets

GitHub 仓库 → **Settings → Secrets and variables → Actions → New repository secret**：

- `WORKBUDDY_REFRESH_TOKEN` = 上面整段字符串

### 3. 触发运行

- **定时**：默认每天 **北京时间 09:01**（cron `1 1 * * *`，UTC 01:01）自动执行；
- **手动验收**：仓库 **Actions → Buddy 加油站 · 每日签到与派猫猫 → Run workflow**。

---

## 关键实现点（对照需求）

| 需求              | 实现                                                                                                                          |
| --------------- | --------------------------------------------------------------------------------------------------------------------------- |
| 每天一次、单次内完成签到+派猫 | 一个 job 顺序执行：刷新 → 签到 → 派猫闭环。                                                                                            |
| 派猫：先领后派，当天已派则跳过 | `travel_pipeline()` 严格复刻：状态 `arrived` 先 `claim`，随后 `idle` 且 `daily_limit_reached` 为假才 `depart`；`daily_limit_reached` 为真则跳过。 |
| 猫猫失败不影响签到结论     | 旅行部分全部失败隔离；`main()` 退出码仅取决于签到：`checkin.status == "ok"` 即 `0`。                                                               |
| 结果不对外通知        | 运行结果仅打印到 Actions 日志，不推送企业微信等任何渠道。                                                                                      |
| 解释器路径不硬编码版本     | workflow 用 `command -v python3 \|\| command -v python` 动态获取。                                                              |
| 接口以实际请求为准       | 全部使用实测接口（见下），不含网上旧名。                                                                                                        |
| token 不落地/不进日志  | Secret 注入 + `redact()` 脱敏 + `::add-mask::`。                                                                                 |

---

## 实测接口清单（来自 WorkBuddy 积分助手技能，非旧接口名）

| 用途       | 方法   | 路径                                                                                   |
| -------- | ---- | ------------------------------------------------------------------------------------ |
| 刷新 token | POST | `https://copilot.tencent.com/v2/plugin/auth/token/refresh`（头 `X-Refresh-Token`）      |
| 签到状态查询   | POST | `https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status`                  |
| 自动签到（幂等） | POST | `https://www.codebuddy.cn/v2/billing/meter/daily-checkin`                            |
| 旅行状态     | GET  | `https://www.workbuddy.cn/activity/growth/buddy/travel/status`                       |
| 地点配置     | GET  | `https://www.workbuddy.cn/activity/growth/buddy/travel/config`                       |
| 领取旅行积分   | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/claim`（`{}`）                  |
| 派出旅行     | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/depart`（`{"location_id": N}`） |

> 认证统一为 `Authorization: Bearer <accessToken>`；签到走 `www.codebuddy.cn`，旅行走 `www.workbuddy.cn`（无 `/v2` 前缀）。



---

## 验收方式

1. 按上面步骤配置 `WORKBUDDY_REFRESH_TOKEN` 这一个 Secret；
2. 在 Actions 页面手动触发一次（`workflow_dispatch`）；
3. 运行日志中可见每段接口的**脱敏原始返回**（`RAW[refresh]` / `RAW[checkin-status]` / `RAW[checkin-do]` / `RAW[travel-claim]` / `RAW[travel-depart]`），token 字段均为 `***`；
4. 日志末尾「运行结果」区块显示签到结论与猫猫状态，且退出码为 0（签到成功即整体成功）即视为验收通过。

> 说明：本方案在本机已完成语法校验与（脱敏函数、失败隔离）本地冒烟测试；一次完整的联网成功运行需在配置好 Secret 的 GitHub 仓库中触发。下方「验收记录」附有一次本机真实联网运行的脱敏日志。

---

## 安全边界

- 仅调用三个已验证写接口（`daily-checkin` / `travel/claim` / `travel/depart`），绝不触碰兑换/抽奖等其他写接口；
- 旅行写操作每次运行必查 `daily_limit_reached`，达上限绝不发写请求；
- `refresh token` 等同账号钥匙，仅存于仓库 Secret 与你的本机终端输出，请勿外传；若怀疑泄露，重新登录 WorkBuddy 客户端后重跑 `refresh_token.py` 并更新 Secret 即可。
