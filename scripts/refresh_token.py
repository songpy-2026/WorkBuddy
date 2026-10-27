#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Buddy 加油站 · 本地刷新脚本（仅在本机、WorkBuddy 已登录时运行）

作用：读取本机 WorkBuddy 登录态，导出「长效 refresh token」，
      并调用官方刷新接口验证其可用，随后输出一段可直接粘贴到
      GitHub 仓库 Secrets（变量名 WORKBUDDY_REFRESH_TOKEN）的字符串。

为什么是 refresh token 而不是 access token：
  - access token 短效、过期后云端无法再使用；
  - refresh token 长效，云端 Actions 每次运行先用它换新 access token，
    因此仓库 Secret 只需配置一次，长期有效（与「无本地常开设备」的要求一致）。

安全约定：
  - 仅在本地读取登录态；绝不修改 / 删除登录态文件；
  - 导出的 token 只输出到你本机终端，不会写入任何文件或仓库；
  - 脚本不在云端运行，云端只消费 Secret 中的 token。

用法：
  python3 scripts/refresh_token.py            # 导出并验证，打印 token
  python3 scripts/refresh_token.py --verify   # 仅验证（不强制重读，仍会读取本机态）
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

PLUGIN_API = "https://copilot.tencent.com"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"
HTTP_TIMEOUT = 15

# ---- 候选登录态路径（与技能 api-spec.md 一致） ----
def _auth_candidates():
    home = os.path.expanduser("~")
    paths = [
        os.path.join(home, "AppData", "Roaming", "CodeBuddyExtension",
                     "Data", "Public", "auth", "workbuddy-desktop.info"),
        os.path.join(home, "AppData", "Local", "CodeBuddyExtension",
                     "Data", "Public", "auth", "workbuddy-desktop.info"),
        os.path.join(home, "Library", "Application Support",
                     "CodeBuddyExtension", "Data", "Public", "auth",
                     "workbuddy-desktop.info"),
        os.path.join(home, ".config", "CodeBuddyExtension", "Data",
                     "Public", "auth", "workbuddy-desktop.info"),
        os.path.join(home, ".workbuddy", "auth", "workbuddy-desktop.info"),
    ]
    return paths


def _plain_refresh_token():
    """兜底：直接从登录态读 refreshToken（适用于明文客户端，或 5.6.2+ 解密依赖不可用时给提示）。"""
    for p in _auth_candidates():
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                auth = json.load(f).get("auth", {})
            raw = auth.get("refreshToken")
            if isinstance(raw, str) and raw:
                # 明文 JWT 三段式（旧客户端）
                if raw.count(".") == 2 and all(
                        c.isalnum() or c in "-_.~" for c in raw):
                    return raw, p
                # 信封形式需要解密，交给技能模块处理
                return None, p
        except Exception:
            continue
    return None, None


def _load_via_skill():
    """优先复用已安装技能中已验证的解密能力（支持 5.6.2+ 信封）。"""
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".workbuddy", "skills", "totorosir-workbuddy-score", "scripts"),
        os.path.join(home, ".workbuddy", "skills", "totorosir-workbuddy-score__skillhub", "scripts"),
    ]
    # 宽匹配：任意 totorosir-workbuddy-score* 目录
    base = os.path.join(home, ".workbuddy", "skills")
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.startswith("totorosir-workbuddy-score"):
                candidates.append(os.path.join(base, name, "scripts"))
    for d in candidates:
        if not os.path.isdir(d):
            continue
        if d not in sys.path:
            sys.path.insert(0, d)
        try:
            import rt_auth  # noqa: F401
            return rt_auth._load_local_rt()
        except Exception as e:
            sys.stderr.write("[info] 候选技能目录 %s 不可用：%s\n" % (d, str(e)[:120]))
    raise RuntimeError("未找到可用的 WorkBuddy 积分助手技能模块")


def load_refresh_token():
    try:
        token, path = _load_via_skill()
        return token, path, "skill"
    except Exception:
        token, path = _plain_refresh_token()
        if token:
            return token, path, "plaintext"
    return None, None, None


def mask_token(s):
    if not s or len(s) < 12:
        return "***"
    return s[:8] + "..." + s[-4:]


def verify_refresh_token(rt):
    url = PLUGIN_API + REFRESH_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return False, "HTTP %s" % e.code
    except Exception as e:
        return False, str(e)[:160]
    ok = data.get("code") == 0 and isinstance(data.get("data"), dict) \
        and bool(data["data"].get("accessToken"))
    return ok, ("code=%s %s" % (data.get("code"), data.get("msg") or ""))


def main():
    if "--verify" not in sys.argv:
        print("正在读取本机 WorkBuddy 登录态……")

    token, path, kind = load_refresh_token()
    if not token:
        sys.stderr.write(
            "未能从本机登录态读取长效令牌。请确认：\n"
            "  1) 已在本机登录 WorkBuddy 客户端；\n"
            "  2) 已安装「WorkBuddy积分助手」技能（含 rt_auth 解密能力）；\n"
            "  3) 若为旧客户端，登录态中 refreshToken 应为明文 JWT。\n"
            "登录态候选路径：\n  %s\n" % "\n  ".join(_auth_candidates()))
        sys.exit(1)

    print("登录态路径：%s（方式：%s）" % (path, kind))
    print("长效令牌预览：%s" % mask_token(token))

    ok, detail = verify_refresh_token(token)
    if not ok:
        sys.stderr.write(
            "验证失败：%s\n"
            "令牌可能已失效，请在客户端重新登录后重试导出。\n" % detail)
        sys.exit(1)

    print("验证通过：长效令牌有效，可用于 GitHub Actions 刷新。\n")
    print("↓ ↓ ↓ 请将下面这一整行字符串，填入 GitHub 仓库 Secrets ↓ ↓ ↓")
    print("WORKBUDDY_REFRESH_TOKEN=%s" % token)
    print("↑ ↑ ↑ 仅此一行，切勿包含引号或多余空格 ↑ ↑ ↑\n")
    print("后续步骤：")
    print("  1. 仓库 Settings → Secrets and variables → Actions → New repository secret")
    print("  2. Name = WORKBUDDY_REFRESH_TOKEN，Secret = 上面的整段字符串")
    print("  3. 再添加一个 WECOM_WEBHOOK（企业微信群机器人 webhook 地址）")
    print("  4. 可选：WECOM_SECRET（群机器人加签密钥，用于签名）")
    print("  5. 在 Actions 页面手动触发一次（workflow_dispatch）完成验收。")


if __name__ == "__main__":
    main()
