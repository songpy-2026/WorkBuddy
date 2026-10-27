#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buddy 加油站 · GitHub Actions 云端执行脚本（纯标准库，无第三方依赖）

职责（单次运行内顺序完成）：
  1. 用仓库 Secret 中的长效 refresh token，向官方刷新接口换取短效 access token；
  2. 查询今日签到状态并执行自动签到（幂等，已签则跳过）；
  3. 派猫猫旅行全自动闭环：先领取已到家的旅行积分，再判断是否可派新一趟
     （idle 且未达每日上限才派，当天已派过则跳过）；
  4. 将「签到结果」与「猫猫状态」分开展示，推送至企业微信群机器人 webhook。

安全约定：
  - 云端不读取任何本地登录态，凭证仅来自环境变量（GitHub Secret）；
  - token 明文绝不写入日志 / 仓库；必要时通过 ::add-mask:: 脱敏；
  - 仅调用三个已验证写接口：daily-checkin、travel/claim、travel/depart。
"""

import base64
import hashlib
import hmac
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# 实测接口常量（来自 WorkBuddy 积分助手技能 api-spec.md，非网上流传旧名）
# ---------------------------------------------------------------------------
PLUGIN_API = "https://copilot.tencent.com"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

BILLING_BASE = "https://www.codebuddy.cn/v2"          # 签到基址（含 /v2）
CHECKIN_STATUS_PATH = "/billing/meter/checkin-activity-status"   # POST 状态查询
CHECKIN_PATH = "/billing/meter/daily-checkin"                   # POST 自动签到（幂等）

TRAVEL_BASE = "https://www.workbuddy.cn"              # 旅行基址（无 /v2 前缀）
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"     # GET 旅行状态
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"     # GET 地点配置
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"       # POST 领取（body {}）
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"     # POST 派出（body {location_id}）

HTTP_TIMEOUT = 20
CHECKIN_MAX_RETRY = 2

TRAVEL_STATE_TEXT = {
    "idle": "空闲",
    "traveling": "旅行中",
    "arrived": "已到达，待领取",
}

SENSITIVE_KEYS = {"accesstoken", "refreshtoken", "token", "authorization",
                  "x-refresh-token", "secret", "key"}


# ---------------------------------------------------------------------------
# 脱敏：递归抹掉 token 类敏感字段，供日志 / 验收展示
# ---------------------------------------------------------------------------
def _tokenish(s):
    return (
        len(s) > 36
        and all(c.isalnum() or c in "-_.~+/=" for c in s)
        and s.count(".") >= 0
    )


def redact(value):
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            kl = str(k).lower()
            if kl in SENSITIVE_KEYS or kl.endswith("token"):
                out[k] = "***" if v not in (None, "", False) else v
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, list):
        return [redact(x) for x in value]
    if isinstance(value, str) and _tokenish(value):
        return value[:6] + "***" + value[-4:]
    return value


def log_raw(label, code, parsed):
    """打印一份脱敏后的原始接口返回内容（验收用）。"""
    safe = redact(parsed)
    print("---- RAW[%s] http=%s ----" % (label, code))
    print(json.dumps(safe, ensure_ascii=False))
    print("--------------------------")


# ---------------------------------------------------------------------------
# HTTP 封装
# ---------------------------------------------------------------------------
def api_call(base, path, token, method="POST", payload=None):
    """统一请求；返回 (http_code, parsed_or_None, raw_text)。"""
    url = base + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WorkBuddy-BuddyStation-CI/1.0")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            code = resp.getcode()
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
    except Exception as e:
        return None, None, "NETWORK_ERROR: %s" % str(e)[:160]
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    return code, parsed, text


def refresh_access_token(rt):
    """用长效 refresh token 换取短效 access token。返回原始应答 dict。"""
    url = PLUGIN_API + REFRESH_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        return {"code": -1, "msg": str(e)[:200]}


# ---------------------------------------------------------------------------
# 签到
# ---------------------------------------------------------------------------
def extract_balance(body):
    if not isinstance(body, dict):
        return None
    d = body.get("data") if isinstance(body.get("data"), dict) else body
    for k in ("total_credits", "balance", "credit_balance", "points"):
        if k in d and isinstance(d[k], (int, float)):
            return d[k]
    return None


def do_checkin(token):
    """查询 → 必要时领取。返回 (result_dict)。"""
    result = {"status": "unknown", "action": None, "points": None,
              "balance": None, "msg": "", "raw": {}}

    attempt = 0
    while attempt <= CHECKIN_MAX_RETRY:
        attempt += 1
        try:
            # 1) 状态查询
            st_code, st_body, _ = api_call(BILLING_BASE, CHECKIN_STATUS_PATH, token)
            result["raw"]["status"] = {"http": st_code, "body": st_body}
            log_raw("checkin-status", st_code, st_body)
            result["balance"] = extract_balance(st_body)

            today_signed = False
            if isinstance(st_body, dict):
                if st_body.get("today_checked_in") is True:
                    today_signed = True
                elif isinstance(st_body.get("data"), dict) and \
                        st_body["data"].get("today_checked_in") is True:
                    today_signed = True
                elif str(st_body.get("code")) == "10001":
                    today_signed = True

            if today_signed:
                result.update(status="ok", action="skip_already_signed",
                              msg="今日已签到，无需重复领取")
                return result

            # 2) 领取签到
            ck_code, ck_body, _ = api_call(BILLING_BASE, CHECKIN_PATH, token)
            result["raw"]["checkin"] = {"http": ck_code, "body": ck_body}
            log_raw("checkin-do", ck_code, ck_body)

            if isinstance(ck_body, dict):
                code = str(ck_body.get("code", ""))
                msg = ck_body.get("msg") or ck_body.get("message") or ""
                data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
                # 幂等保护：已签 / 重复提示
                if code == "10001" or "已签到" in msg or "今天已签到" in msg:
                    result.update(status="ok", action="skip_already_signed",
                                  msg="今日已签到（接口返回 code=10001）")
                    return result
                success_code = code in ("", "0", "200")
                if 200 <= ck_code < 300 and success_code:
                    credit = (ck_body.get("credit") or data.get("credit")
                              or data.get("daily_credit") or data.get("today_credit"))
                    streak = ck_body.get("streak_days") or data.get("streak_days")
                    bal = extract_balance(ck_body)
                    if bal is not None:
                        result["balance"] = bal
                    result.update(status="ok", action="clicked",
                                  points=credit,
                                  msg="领取成功"
                                       + (("，+%s 积分" % credit) if credit else "")
                                       + (("，连续第 %s 天" % streak) if streak else ""))
                    return result
                result.update(status="error", action="failed",
                              msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
                return result
            result.update(status="error", action="failed",
                          msg="领取接口返回非 JSON")
            return result
        except Exception as e:
            if attempt <= CHECKIN_MAX_RETRY:
                time.sleep(2)
                continue
            result.update(status="error", action="failed", msg="异常: %s" % str(e)[:160])
            return result

    result.update(status="error", action="failed", msg="重试后仍失败")
    return result


# ---------------------------------------------------------------------------
# 派猫猫旅行（先领后派；失败不影响签到结论）
# ---------------------------------------------------------------------------
def travel_get(token, path):
    st, body, _ = api_call(TRAVEL_BASE, path, token, method="GET")
    if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
        return None
    return body.get("data")


def travel_claim(token):
    st, body, _ = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
    log_raw("travel-claim", st, body)
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        credit = data.get("reward_credit")
        return {"action": "claimed", "success": True, "reward_credit": credit,
                "message": "已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?")}
    return {"action": "failed", "success": False,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)}


def travel_depart(token, location_id, locations):
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (locations or []) if loc.get("id") is not None]
        if not ids:
            return {"action": "failed", "success": False,
                    "message": "未能获取可选地点列表，已跳过派遣"}
        chosen = random.choice(ids)
    st, body, _ = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token,
                           payload={"location_id": chosen})
    log_raw("travel-depart", st, body)
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        data = body.get("data") or {}
        loc = data.get("location") or {}
        return {"action": "departed", "success": True, "location_id": chosen,
                "location_name": loc.get("name"),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    return {"action": "failed", "success": False,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)}


def travel_pipeline(token, location_id=None):
    """全自动闭环：先领后派。返回结构化结果（供 WeCom 分开展示）。"""
    lines = []
    out = {"ok": True, "skipped": False, "claimed": None,
           "departed": None, "final_state": None, "lines": lines}

    status = travel_get(token, TRAVEL_STATUS_PATH)
    if status is None:
        out["ok"] = False
        out["skipped"] = True
        lines.append("旅行状态查询失败，已跳过（不影响签到结论）")
        return out

    state = status.get("state")
    if state == "arrived":
        c = travel_claim(token)
        lines.append(c.get("message") or c.get("action"))
        if c.get("action") == "claimed":
            out["claimed"] = c.get("reward_credit")
        fresh = travel_get(token, TRAVEL_STATUS_PATH)
        if fresh is not None:
            status = fresh
            state = status.get("state")

    if status.get("daily_limit_reached"):
        lines.append("今日派遣次数已用完，跳过派遣（当天已派过）")
    elif state in (None, "", "idle"):
        cfg = travel_get(token, TRAVEL_CONFIG_PATH) or {}
        d = travel_depart(token, location_id, cfg.get("locations") or [])
        lines.append(d.get("message") or d.get("action"))
        if d.get("action") == "departed":
            out["departed"] = d
    else:
        lines.append("Buddy 正在旅行中，无需派遣")

    final = travel_get(token, TRAVEL_STATUS_PATH) or status
    out["final_state"] = final.get("state") if isinstance(final, dict) else None
    return out


def fmt_duration(seconds):
    if seconds is None:
        return None
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours > 0:
        return "%d 小时 %d 分" % (hours, minutes)
    if minutes > 0:
        return "%d 分钟" % minutes
    return "%d 秒" % seconds


def build_travel_section(travel):
    """组装猫猫部分展示文案（含最终状态）。"""
    if travel.get("skipped"):
        return ("**🐱 猫猫旅行**\n- 状态：<font color=\"comment\">跳过</font>\n- %s"
                % travel["lines"][0])
    lines = list(travel.get("lines", []))
    final_state = travel.get("final_state")
    if final_state:
        lines.append("当前猫猫状态：%s" % TRAVEL_STATE_TEXT.get(final_state, final_state))
    body = "\n".join("- %s" % x for x in lines) if lines else "- 无操作"
    return "**🐱 猫猫旅行**\n%s" % body


# ---------------------------------------------------------------------------
# 企业微信推送
# ---------------------------------------------------------------------------
def sign_wecom(webhook, secret):
    if not secret:
        return webhook
    timestamp = str(int(time.time()))
    string_to_sign = "%s\n%s" % (timestamp, secret)
    hmac_code = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"),
                         digestmod=hashlib.sha256).digest()
    sign = base64.b64encode(hmac_code).decode("utf-8")
    sep = "&" if "?" in webhook else "?"
    return "%s%ssign=%s&timestamp=%s" % (webhook, sep, sign, timestamp)


def push_wecom(webhook, secret, title, content):
    if not webhook:
        print("[wecom] 未配置 WECOM_WEBHOOK，跳过推送（不影响整体结论）")
        return False
    url = sign_wecom(webhook, secret)
    payload = {"msgtype": "markdown", "content": {"content": content}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8", "replace")
        log_raw("wecom-push", code, safe_parse(body))
        return 200 <= code < 300
    except Exception as e:
        print("[wecom] 推送失败: %s" % str(e)[:160])
        return False


def safe_parse(text):
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text[:200]}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def build_message(checkin, travel):
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    if checkin["status"] == "ok":
        if checkin["action"] == "skip_already_signed":
            ck_state = "已签到（跳过）"
        else:
            ck_state = "成功"
        ck_line = "- 状态：<font color=\"info\">%s</font>\n- 说明：%s\n- 当前积分余额：%s" % (
            ck_state, checkin["msg"], checkin["balance"])
    else:
        ck_line = "- 状态：<font color=\"warning\">失败</font>\n- 说明：%s" % checkin["msg"]

    travel_section = build_travel_section(travel)

    content = (
        "# 🐾 Buddy 加油站 · 每日播报\n"
        "> 执行时间：%s\n\n"
        "**✅ 签到结果**\n%s\n\n"
        "%s\n\n"
        "---\n"
        "由 GitHub Actions 自动执行"
    ) % (now, ck_line, travel_section)
    return content


def main():
    rt = os.environ.get("WORKBUDDY_REFRESH_TOKEN", "")
    webhook = os.environ.get("WECOM_WEBHOOK", "")
    secret = os.environ.get("WECOM_SECRET", "")

    # 凭证脱敏：即使后续意外打印也不会泄露
    if rt:
        print("::add-mask::%s" % rt)

    if not rt:
        sys.stderr.write("缺少环境变量 WORKBUDDY_REFRESH_TOKEN（请在仓库 Secrets 配置）\n")
        sys.exit(1)

    # 1) 刷新 token
    r = refresh_access_token(rt)
    log_raw("refresh", r.get("code"), r)
    if r.get("code") != 0 or not isinstance(r.get("data"), dict):
        print("[refresh] 刷新失败 code=%s %s" % (r.get("code"), r.get("msg") or ""))
        push_wecom(webhook, secret, "Buddy 加油站执行失败",
                   "# ❌ Buddy 加油站执行失败\n> 长效令牌刷新失败，请重新导出并更新 Secret。")
        sys.exit(1)
    at = r["data"].get("accessToken") or ""
    if not at:
        print("[refresh] 刷新应答缺少 accessToken")
        sys.exit(1)

    # 2) 签到
    checkin = do_checkin(at)

    # 3) 派猫猫旅行（失败不影响签到结论）
    travel = travel_pipeline(at)

    # 4) 推送企业微信（签到结果与猫猫状态分开展示）
    content = build_message(checkin, travel)
    pushed = push_wecom(webhook, secret, "Buddy 加油站每日播报", content)
    if not pushed:
        print("[wecom] 推送未成功（见上方日志）；本地仍保留结果。")

    # 控制台汇总
    print("\n==== 运行结果 ====")
    print("签到: %s / %s" % (checkin["status"], checkin["msg"]))
    print("猫猫: %s" % ("；".join(travel.get("lines", [])) or "跳过"))

    # 退出码：只要签到成功即视为整体成功（猫猫失败不影响）
    sys.exit(0 if checkin["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
