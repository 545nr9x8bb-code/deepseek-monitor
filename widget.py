#!/usr/bin/env python3
"""DeepSeek Monitor — usage & balance desktop panel (platform.deepseek.com layout)."""

import base64
import calendar
import ctypes
import json
import os
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import webview

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ── Config ─────────────────────────────────────────────────────────────
CFG = Path.home() / ".deepseek_widget_config.json"
BAL_URL = "https://api.deepseek.com/user/balance"
PLAT = "https://platform.deepseek.com"
W, HH = 360, 820

# Approximate DeepSeek V4 pricing (CNY per 1M tokens)
RATE = {
    "PROMPT_TOKEN": 2.0,
    "PROMPT_CACHE_HIT_TOKEN": 0.5,
    "PROMPT_CACHE_MISS_TOKEN": 2.0,
    "RESPONSE_TOKEN": 8.0,
    "REQUEST": 0,
}
PALETTE = ["#4b9fff", "#4caf93", "#9b7ed8", "#f0a060", "#4db8b8", "#e07070"]


def _rc():
    try:
        if CFG.exists():
            return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _wc(c):
    CFG.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def load_api_key():
    k = os.environ.get("DEEPSEEK_API_KEY", "")
    if k and k.startswith("sk-"):
        return k
    c = _rc()
    # Backward compat: old single key
    if c.get("api_key") and c["api_key"].startswith("sk-"):
        return c["api_key"]
    # New format: key list
    keys = c.get("api_keys", [])
    idx = c.get("active_key", 0)
    if keys and 0 <= idx < len(keys):
        return keys[idx].get("key", "")
    return ""


def load_keys():
    c = _rc()
    keys = c.get("api_keys", [])
    # Migrate old single key
    if not keys and c.get("api_key","").startswith("sk-"):
        keys = [{"name": "默认", "key": c["api_key"]}]
        c.pop("api_key", None); c["api_keys"] = keys; c["active_key"] = 0
        _wc(c)
    return [{"name": k.get("name","?"), "key": k.get("key","")[:8]+"****"+k.get("key","")[-4:]} for k in keys]


def save_key(name, key):
    k = (key or "").strip()
    if not k.startswith("sk-"):
        return False
    c = _rc()
    keys = c.get("api_keys", [])
    keys.append({"name": (name or "").strip() or "未命名", "key": k})
    c["api_keys"] = keys
    c["active_key"] = len(keys) - 1
    _wc(c)
    return True


def select_key(idx):
    c = _rc(); c["active_key"] = idx; _wc(c)


def delete_key(idx):
    c = _rc(); keys = c.get("api_keys", [])
    if 0 <= idx < len(keys):
        keys.pop(idx)
        if c.get("active_key", 0) >= len(keys):
            c["active_key"] = max(0, len(keys) - 1)
        _wc(c)


def load_pt():
    return _rc().get("platform_token", "")


def save_pt(t):
    c = _rc(); c["platform_token"] = t; _wc(c)


# ── API ────────────────────────────────────────────────────────────────
def _bearer():
    pt = load_pt()
    if not pt:
        return None
    t = pt
    try:
        p = json.loads(pt)
        if isinstance(p, dict) and "value" in p:
            t = p["value"]
    except (json.JSONDecodeError, TypeError):
        pass
    return t


def fetch_balance():
    k = load_api_key()
    if not k:
        return None
    try:
        req = urllib.request.Request(BAL_URL, headers={
            "Accept": "application/json", "Authorization": f"Bearer {k}",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def fetch_usage(year=None, month=None):
    t = _bearer()
    if not t:
        return None
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    try:
        req = urllib.request.Request(
            f"{PLAT}/api/v0/usage/cost?month={m:02d}&year={y}",
            headers={
                "Authorization": f"Bearer {t}",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://platform.deepseek.com/usage",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return _parse(json.loads(r.read().decode()), y, m)
    except Exception:
        return None


def _cost_to_tokens(type_, amount_cny):
    """Estimate token count from cost using approximate pricing."""
    rate = RATE.get(type_, 0)
    if rate <= 0 or amount_cny <= 0:
        return 0
    return int(amount_cny / rate * 1_000_000)


def _parse(raw, year=None, month=None):
    """Convert platform API response to widget data model."""
    now = datetime.now()
    y = year or now.year
    m = month or now.month
    result = {"models": [], "total_cost": 0, "total_tokens": 0, "active_model_days": 0,
              "currency": "CNY", "today_cost": 0, "today_models": [], "daily": [],
              "year": y, "month": m}
    try:
        biz = raw.get("data", {}).get("biz_data", [{}])[0]
        currency = biz.get("currency", "CNY")
        result["currency"] = currency

        model_data = {}  # name → {cost, tokens, usage_types}
        for item in biz.get("total", []):
            name = item.get("model", "unknown")
            cost = 0
            tokens = 0
            types = {}
            for u in item.get("usage", []):
                amt = float(u.get("amount", 0))
                tp = u.get("type", "")
                cost += amt
                types[tp] = amt
                tokens += _cost_to_tokens(tp, amt)
            model_data[name] = {"cost": cost, "tokens": tokens, "types": types}

        total_cost = sum(m["cost"] for m in model_data.values())
        total_tokens = sum(m["tokens"] for m in model_data.values())
        # Count non-zero daily model entries as approximate request count
        active_model_days = sum(
            1 for d in biz.get("days", [])
            for mi in d.get("data", [])
            if sum(float(u.get("amount", 0)) for u in mi.get("usage", [])) > 0
        )
        result["total_cost"] = round(total_cost, 4)
        result["total_tokens"] = total_tokens
        result["active_model_days"] = active_model_days

        # Sort by cost desc, only include models with usage
        sorted_m = sorted(model_data.items(), key=lambda x: x[1]["cost"], reverse=True)
        i = 0
        for name, md in sorted_m:
            if md["cost"] > 0:
                result["models"].append({
                    "name": name,
                    "color": PALETTE[i % len(PALETTE)],
                    "cost": round(md["cost"], 4),
                    "tokens": md["tokens"],
                    "pct": round(md["cost"] / total_cost * 100, 1) if total_cost > 0 else 0,
                    "active": True,
                    "usage_types": md["types"],
                })
                i += 1

        # Today per-model — always build from full model list, override with today data
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_total = 0
        today_models = []
        today_map = {}  # name -> {cost, types, tokens}
        for d in biz.get("days", []):
            if d.get("date") == today_str:
                for mi in d.get("data", []):
                    name = mi.get("model", "unknown")
                    cost = sum(float(u.get("amount", 0)) for u in mi.get("usage", []))
                    types = {}
                    tokens = 0
                    for u in mi.get("usage", []):
                        tp = u.get("type", "")
                        amt = float(u.get("amount", 0))
                        types[tp] = amt
                        tokens += _cost_to_tokens(tp, amt)
                    today_map[name] = {"cost": cost, "types": types, "tokens": tokens}
                    today_total += cost
                break
        # Build today_models — only include models with today usage
        j = 0
        for name, md in sorted_m:
            td = today_map.get(name, {})
            cost = td.get("cost", 0)
            if cost > 0:
                today_models.append({
                    "name": name,
                    "color": PALETTE[j % len(PALETTE)],
                    "cost": round(cost, 4),
                    "tokens": td.get("tokens", 0),
                    "pct": round(cost / today_total * 100, 1) if today_total > 0 else 0,
                    "active": True,
                    "usage_types": td.get("types", {}),
                })
                j += 1
        result["today_cost"] = round(today_total, 4)
        result["today_models"] = today_models

        # All days of the month for chart
        daily_map = {}
        for d in biz.get("days", []):
            daily_map[d.get("date", "")] = sum(
                float(u.get("amount", 0))
                for mi in d.get("data", [])
                for u in mi.get("usage", [])
            )
        days_in_month = calendar.monthrange(y, m)[1]
        for day in range(1, days_in_month + 1):
            dt = f"{y}-{m:02d}-{day:02d}"
            result["daily"].append({
                "date": f"{m:02d}-{day:02d}",
                "cost": round(daily_map.get(dt, 0), 4),
            })
    except Exception as e:
        import sys
        print(f"[DeepSeek Monitor] Parse error: {e}", file=sys.stderr)
    return result


# ── Logo ───────────────────────────────────────────────────────────────
LOGO_B64 = ""
# Try multiple locations: same dir as script, then Desktop
_LOGO_PATHS = [
    Path(__file__).parent / "logo.png",
    Path.home() / "Desktop" / "logo.png",
]
for _lp in _LOGO_PATHS:
    if _lp.exists():
        LOGO_B64 = "data:image/png;base64," + base64.b64encode(_lp.read_bytes()).decode()
        break
# Fallback: inline SVG whale icon
if not LOGO_B64:
    LOGO_B64 = "data:image/svg+xml," + urllib.parse.quote(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<circle cx="16" cy="16" r="15" fill="#3b82f6"/>'
        '<ellipse cx="13" cy="16" rx="7" ry="8" fill="#fff"/>'
        '</svg>'
    )

# ── HTML ───────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<style>
  :root {
    --bg: #0b1019; --card: #131a27; --border: #1c2537;
    --text: #c3cddb; --dim: #7b8597; --white: #edf3fb; --r: 10px;
  }
  body.light {
    --bg: #f5f6f8; --card: #ffffff; --border: #e0e3e8;
    --text: #2c3e50; --dim: #8e99a5; --white: #1a1a2e;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{
    background:var(--bg);color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
    font-size:13px;user-select:none;overflow-y:auto;overflow-x:hidden;scrollbar-gutter:stable;
  }
  body::-webkit-scrollbar{width:3px}
  body::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
  .w{padding:56px 16px 20px 16px;display:flex;flex-direction:column;gap:14px}

  /* header */
  .hdr{display:flex;align-items:center;position:fixed;top:0;left:0;right:0;
    z-index:50;background:var(--bg);padding:14px 16px 10px 16px;flex-wrap:nowrap}
  .hdr .t{font-size:16px;font-weight:700;color:var(--white);flex:1}
  .hdr select{background:var(--card);color:var(--text);border:1px solid var(--border);
    border-radius:6px;padding:4px 8px;font-size:12px;cursor:pointer;outline:none}
  .hdr .bi{background:none;border:1px solid var(--border);color:var(--dim);
    width:26px;height:26px;border-radius:6px;cursor:pointer;font-size:14px;
    display:flex;align-items:center;justify-content:center}
  .hdr .bi:hover{color:var(--text);border-color:var(--dim)}

  /* cards */
  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:14px}
  .sec{font-size:12px;font-weight:600;color:var(--dim);letter-spacing:.4px}
  .big{font-size:26px;font-weight:700;color:var(--white)}
  .bar{height:6px;background:var(--border);border-radius:3px;overflow:hidden;margin:8px 0 6px}
  .bar-fill{height:100%;border-radius:3px;transition:width .4s;background:linear-gradient(90deg,#4b9fff,#9b7ed8)}
  .row2{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}
  /* model row */
  .mr{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:11px 12px;margin-bottom:8px}
  .mr:last-child{margin-bottom:0}
  .mr-h{display:flex;align-items:center;gap:8px;margin-bottom:6px}
  .mr-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
  .mr-name{flex:1;font-size:12px;font-weight:500;color:var(--white)}
  .mr-badge{font-size:10px;padding:2px 8px;border-radius:10px;
    background:rgba(63,185,80,.12);color:#3fb950}
  .mr-bar{height:5px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:5px}
  .mr-fill{height:100%;border-radius:3px;transition:width .4s}
  .mr-info{display:flex;justify-content:space-between;font-size:11px;color:var(--dim)}

  /* usage breakdown */
  .ub{font-size:11px;color:var(--dim);display:flex;flex-wrap:wrap;gap:4px 14px;margin-top:6px;padding-top:6px;border-top:1px solid var(--border)}
  .ub span{white-space:nowrap}
  .ub .val{color:var(--text)}

  /* daily chart */
  .chart{display:flex;align-items:flex-end;gap:2px;height:70px;padding:0 2px;overflow-x:auto;scrollbar-width:none}
  .chart::-webkit-scrollbar{display:none}
  .ch-bar{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}
  .ch-fill{width:100%;border-radius:3px 3px 0 0;transition:height .4s;min-height:2px;cursor:pointer}
  .ch-fill:hover{opacity:0.75}
  .ch-label{font-size:9px;color:var(--dim);text-align:center}

  /* balance */
  .bl-v{font-size:26px;font-weight:700;color:var(--white);margin:3px 0 10px}
  /* footer */
  .ft{display:flex;align-items:center;justify-content:space-between}
  .ft .ts{font-size:11px;color:var(--dim)}
  .ft button{background:var(--card);border:1px solid var(--border);color:var(--text);
    padding:5px 14px;border-radius:6px;font-size:12px;cursor:pointer}
  .ft button:hover{background:var(--border)}

  /* settings */
  #stg{display:none;flex-direction:column;gap:6px;background:var(--card);
    border:1px solid var(--border);border-radius:var(--r);padding:10px 12px}
  #stg .h{font-size:11px;color:var(--dim);line-height:1.6}
  #stg .h code{color:#58a6ff;background:rgba(88,166,255,.1);padding:1px 4px;border-radius:3px;font-size:10px}
  #stg textarea{background:var(--bg);border:1px solid var(--border);color:var(--text);
    padding:6px 8px;border-radius:6px;font-size:11px;outline:none;resize:none;
    height:48px;font-family:monospace;width:100%}
  #stg textarea:focus{border-color:#58a6ff}
  #stg button{align-self:flex-end;background:var(--border);color:var(--text);
    border:none;padding:5px 12px;border-radius:5px;font-size:11px;cursor:pointer}

  /* prompt */
  #prompt{position:fixed;inset:0;background:var(--bg);z-index:100;
    display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px}
  #prompt.hidden{display:none}
  #prompt h3{font-size:15px;color:var(--white)}
  #prompt input{background:var(--card);border:1px solid var(--border);color:var(--text);
    padding:8px 12px;border-radius:6px;width:270px;font-size:12px;outline:none;font-family:monospace}
  #prompt input:focus{border-color:#58a6ff}
  #prompt .btn{background:#238636;color:#fff;border:none;padding:8px 28px;
    border-radius:6px;font-size:13px;font-weight:600;cursor:pointer}
  .hint{font-size:11px;color:var(--dim);text-align:center;line-height:1.5}
  .empty{color:var(--dim);font-size:12px;padding:4px 0}

  /* peek tab */
  #peek-tab{position:fixed;left:0;top:0;bottom:0;width:3px;z-index:999;
    background:linear-gradient(180deg,#4b9fff,#9b7ed8,#f0a060,#4caf93,#4db8b8);
    display:none;cursor:pointer;transition:width .15s,filter .15s}
  #peek-tab:hover{width:10px;filter:brightness(1.3)}
  body.peek #peek-tab{display:block}
  body.peek #grip{display:none}
  body.revealing #peek-tab{display:none}
</style>
</head>
<body>
<div class="w">
  <div class="hdr">
    <span style="display:flex;align-items:center;gap:6px;flex-shrink:0">
      <img src="LOGO_PLACEHOLDER" height="24" style="flex-shrink:0;object-fit:contain">
      <span class="t">DeepSeek Monitor</span>
    </span>
    <span style="display:flex;align-items:center;gap:4px;flex-shrink:0;margin-left:auto">
      <button class="bi" id="btn-refresh" title="更新于 --" style="width:auto;padding:4px 12px;font-size:11px">刷新</button>
      <button class="bi" id="btn-stg" title="设置">&#9881;</button>
      <button class="bi" id="btn-min" title="最小化">&#x2013;</button>
      <button class="bi" id="btn-close" title="关闭">&times;</button>
    </span>
  </div>

  <div id="stg">
    <div class="h">
      用量数据需要 <code>platformToken</code><br>
      <code>platform.deepseek.com</code> → F12 → Application → Local Storage → <code>userToken</code>
    </div>
    <textarea id="pt-inp" placeholder="粘贴 platformToken..."></textarea>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
      <span style="font-size:11px;color:var(--dim)">设置</span>
      <button id="stg-close" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;line-height:1">&times;</button>
    </div>
    <button id="pt-save">保存</button>
    <div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
        <span style="font-size:11px;color:var(--dim)">API Keys</span>
        <button id="stg-add-key" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:16px;line-height:1" title="添加 Key">+</button>
      </div>
      <div id="stg-key-list" style="display:flex;flex-direction:column;gap:3px;margin-bottom:6px"></div>
      <div id="stg-key-form" style="display:none;flex-direction:column;gap:4px">
        <input id="stg-key-name" placeholder="名称" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:4px;font-size:10px;outline:none">
        <input id="stg-key-inp" type="password" placeholder="sk-..." style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:3px 6px;border-radius:4px;font-size:10px;outline:none;font-family:monospace">
        <div style="display:flex;gap:4px">
          <button id="stg-key-save" style="background:#238636;color:#fff;border:none;padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer">保存</button>
          <button id="stg-key-cancel" style="background:var(--border);color:var(--text);border:none;padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer">取消</button>
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:11px;color:var(--dim);white-space:nowrap">主题</span>
      <button id="th-toggle" onclick="toggleTheme()" style="background:var(--card);border:1px solid var(--border);color:var(--dim);padding:3px 10px;border-radius:5px;font-size:13px;cursor:pointer;line-height:1" title="切换深色/浅色">&#9790;</button>
    </div>
  </div>

  <div id="prompt">
    <div class="hdr" id="prompt-hdr" style="justify-content:space-between">
      <span class="t" style="font-size:14px">&#128273; DeepSeek API Key</span>
      <span style="display:flex;gap:4px">
        <button class="bi" id="prompt-stg" title="设置">&#9881;</button>
        <button class="bi" id="prompt-min" title="最小化">&#x2013;</button>
        <button class="bi" id="prompt-close" title="关闭">&times;</button>
      </span>
    </div>
    <div style="margin-top:28px"></div>
    <input id="key-inp" type="password" placeholder="sk-..." style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:8px 12px;border-radius:6px;width:270px;font-size:12px;outline:none;font-family:monospace">
    <button class="btn" id="key-save">保存并连接</button>
    <div class="hint" style="margin-top:8px">也支持环境变量 <code>DEEPSEEK_API_KEY</code></div>
  </div>

  <!-- Close confirm dialog -->
  <div id="close-dlg" style="position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:300;
    display:none;flex-direction:column;align-items:center;justify-content:center;gap:14px">
    <div style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:20px 28px;text-align:center">
      <div style="font-size:14px;color:var(--white);margin-bottom:16px">关闭 DeepSeek Monitor？</div>
      <div style="display:flex;gap:8px;justify-content:center">
        <button id="dlg-min" style="background:var(--border);color:var(--text);border:none;padding:6px 18px;border-radius:6px;font-size:12px;cursor:pointer">最小化到托盘</button>
        <button id="dlg-quit" style="background:#da3633;color:#fff;border:none;padding:6px 18px;border-radius:6px;font-size:12px;cursor:pointer">退出</button>
        <button id="dlg-cancel" style="background:var(--card);border:1px solid var(--border);color:var(--text);padding:6px 18px;border-radius:6px;font-size:12px;cursor:pointer">取消</button>
      </div>
    </div>
  </div>

  <div id="content" style="display:flex;flex-direction:column;gap:14px">
    <!-- row 1: today usage (full width) -->
    <div class="card">
      <div class="sec" id="ov-t">今日用量</div>
      <div class="big" id="ov-v">--</div>
      <div class="bar" style="margin:6px 0 4px"><div class="bar-fill" id="ov-fill" style="width:0%"></div></div>
      <div class="row2">
        <span id="ov-l">--</span><span id="ov-r">--</span>
      </div>
    </div>

    <!-- row 2: balance (left) + monthly cost (right) -->
    <div style="display:flex;gap:14px">
      <div class="card" style="flex:1">
        <div class="sec" style="margin-bottom:0">总余额</div>
        <div class="big" id="bl-v">--</div>
        <div style="font-size:11px;color:var(--dim);margin-top:2px" id="bl-st">--</div>
      </div>
      <div class="card" style="flex:1">
        <div class="sec">本月消费</div>
        <div class="big" id="mc-v">--</div>
        <div style="font-size:11px;color:var(--dim);margin-top:2px" id="mc-sub">--</div>
      </div>
    </div>

    <!-- monthly chart -->
    <div class="sec" style="display:flex;align-items:center;justify-content:space-between">
      <span>每月用量</span>
      <span style="display:flex;align-items:center;gap:6px">
        <button id="ch-prev" onclick="changeMonth(-1)" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:12px">&#9664;</button>
        <span id="ch-month" style="font-size:11px;color:var(--text);min-width:70px;text-align:center"></span>
        <button id="ch-next" onclick="changeMonth(1)" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:12px">&#9654;</button>
      </span>
    </div>
    <div class="chart" id="chart" style="height:70px"></div>

    <!-- model list -->
    <div class="sec" style="display:flex;align-items:center;justify-content:space-between">
      <span>模型用量</span>
      <span style="display:flex;align-items:center;gap:2px;background:var(--border);border-radius:6px;padding:2px">
        <button id="mdl-today" onclick="switchModels('today')" style="background:var(--card);color:var(--text);border:none;padding:2px 10px;border-radius:5px;font-size:10px;cursor:pointer">今日</button>
        <button id="mdl-month" onclick="switchModels('month')" style="background:none;color:var(--dim);border:none;padding:2px 10px;border-radius:5px;font-size:10px;cursor:pointer">本月</button>
      </span>
    </div>
    <div id="models"></div>

    <!-- Open platform page -->
    <div id="open-platform" style="background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:12px;
      display:flex;align-items:center;justify-content:center;gap:8px;cursor:pointer;
      transition:border-color .2s" onclick="if(typeof pywebview!=='undefined')pywebview.api.open_platform();"
      onmouseover="this.style.borderColor='#58a6ff'" onmouseout="this.style.borderColor='#1c2537'">
      <span style="font-size:12px;color:var(--dim)">查看完整用量数据</span>
      <span style="font-size:12px;color:#58a6ff">platform.deepseek.com &#8599;</span>
    </div>


  </div>
</div>

<div id="peek-tab"></div>
<div id="grip" style="position:fixed;bottom:0;right:0;width:20px;height:20px;
  cursor:nwse-resize;z-index:200;"></div>

<script>
var _last = null, _mdlPeriod = 'today', _chYear=0, _chMonth=0;

async function changeMonth(delta){
  if(typeof pywebview==='undefined')return;
  var m=_chMonth+delta, y=_chYear;
  if(m<1){m=12;y--;}else if(m>12){m=1;y++;}
  _chYear=y;_chMonth=m;
  document.getElementById('ch-month').textContent=y+'年'+m+'月';
  var d=await pywebview.api.get_month_data(y,m);
  _updateChart(d);
}
function _updateChart(d){
  var daily=d.daily||[], cur=d.currency||'CNY', ch='';
  if(daily.length){
    var maxD=Math.max.apply(null,daily.map(function(x){return x.cost||0}));
    if(maxD<=0) maxD=1;
    daily.forEach(function(day){
      var h=Math.max(2,Math.round((day.cost||0)/maxD*56));
      ch+='<div class="ch-bar">'+
        '<div class="ch-fill" style="height:'+h+'px;background:#4b9fff" title="'+day.date+': ¥'+day.cost.toFixed(2)+'"></div>'+
        '<div class="ch-label">'+day.date.slice(3)+'</div>'+
      '</div>';
    });
  }
  document.getElementById('chart').innerHTML=ch||'<div class="empty">--</div>';
}
function switchModels(p){
  _mdlPeriod = p;
  document.getElementById('mdl-today').style.background = p==='today'?'var(--card)':'none';
  document.getElementById('mdl-today').style.color = p==='today'?'var(--text)':'var(--dim)';
  document.getElementById('mdl-month').style.background = p==='month'?'var(--card)':'none';
  document.getElementById('mdl-month').style.color = p==='month'?'var(--text)':'var(--dim)';
  if(_last)render(_last);
}
function fm(n){if(n==null)return'--';return Number(n).toFixed(2)}
function fcur(n,c){if(n==null)return'--';return '¥'+Number(n).toFixed(2)+' <span style="color:var(--dim);font-size:11px">'+ (c||'CNY')+'</span>'}
function fmt(n){if(n==null)return'--';if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return String(Math.round(n))}
function pc(a,b){if(!b||b<=0)return 0;return Math.min(100,Math.round(a*100/b))}
function esc(s){return String(s||'').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function ts(){var d=new Date();return String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')+':'+String(d.getSeconds()).padStart(2,'0')}

function render(d){
  d=d||{};
  _last=d;
  var bal=d.balance, models=d.models||[], mc=d.total_cost||0, cur=d.currency||'CNY';

  // overview (today)
  var cost=d.today_cost||0;
  document.getElementById('ov-t').textContent='今日用量';
  document.getElementById('ov-v').innerHTML=fcur(cost,cur);
  var todayModels=d.today_models||[];
  var topPct=todayModels.length?todayModels[0].pct||0:0;
  document.getElementById('ov-fill').style.width=topPct+'%';
  document.getElementById('ov-l').textContent='费用最高占比 '+topPct+'%';
  document.getElementById('ov-r').textContent='';

  // monthly cost card
  document.getElementById('mc-v').innerHTML=fcur(mc,cur);
  document.getElementById('mc-sub').textContent=models.length+' 个活跃模型';

  // model rows (respect period toggle)
  var displayModels = _mdlPeriod==='today' ? (d.today_models||[]) : models;
  var mh='';
  if(displayModels.length){
    displayModels.forEach(function(m){
      var p=m.pct||0, c=m.color||'#58a6ff';
      var ut=m.usage_types||{};
      mh+='<div class="mr">'+
        '<div class="mr-h">'+
          '<span class="mr-dot" style="background:'+c+'"></span>'+
          '<span class="mr-name">'+esc(m.name)+'</span>'+
          '<span class="mr-badge">'+(m.active?'活跃':'闲置')+'</span>'+
        '</div>'+
        '<div class="mr-bar"><div class="mr-fill" style="width:'+p+'%;background:'+c+'"></div></div>'+
        '<div class="mr-info"><span>占比 '+p+'%</span><span>'+fcur(m.cost||0,cur)+'</span></div>'+
        (m.tokens>0?'<div class="mr-info" style="margin-top:2px"><span>约 '+fmt(m.tokens)+' tokens</span></div>':'')+
        '<div class="ub">'+
          (ut.PROMPT_CACHE_HIT_TOKEN>0?'<span>缓存命中 <span class="val">'+fcur(ut.PROMPT_CACHE_HIT_TOKEN,cur)+'</span></span>':'')+
          (ut.PROMPT_CACHE_MISS_TOKEN>0?'<span>缓存未命中 <span class="val">'+fcur(ut.PROMPT_CACHE_MISS_TOKEN,cur)+'</span></span>':'')+
          (ut.RESPONSE_TOKEN>0?'<span>输出 <span class="val">'+fcur(ut.RESPONSE_TOKEN,cur)+'</span></span>':'')+
        '</div>'+
      '</div>';
    });
  }else{
    mh='<div class="empty">暂无用量数据。点击右上角齿轮 &#9881; 配置 platformToken 获取实时数据。</div>';
  }
  document.getElementById('models').innerHTML=mh;


  // monthly chart
  if(!_chYear){ _chYear=d.year||new Date().getFullYear(); _chMonth=d.month||(new Date().getMonth()+1); }
  document.getElementById('ch-month').textContent=_chYear+'年'+_chMonth+'月';
  _updateChart(d);

  // balance
  if(bal&&!bal.error){
    try{
      var inf=bal.balance_infos[0];
      var bc=inf.currency||cur;
      document.getElementById('bl-v').innerHTML=fcur(parseFloat(inf.total_balance),bc);
      var tp = parseFloat(inf.topped_up_balance)||0;
      var gr = parseFloat(inf.granted_balance)||0;
      var parts = [];
      if(gr > 0 && gr !== tp) parts.push('赠送 '+inf.granted_balance);
      if(tp > 0) parts.push('充值 '+inf.topped_up_balance);
      document.getElementById('bl-st').textContent = parts.join(' · ');
    }catch(e){}
  }
  document.getElementById('btn-refresh').title='更新于 '+ts();

}

async function load(){
  if(typeof pywebview==='undefined'){
    render({
      currency:'CNY',total_cost:3.54,total_tokens:1560000,today_cost:0.12,
      today_models:[
        {name:'deepseek-v4-pro',color:'#4b9fff',cost:0.08,pct:66.7,active:true},
        {name:'deepseek-v4-flash',color:'#4caf93',cost:0.04,pct:33.3,active:true},
      ],
      models:[
        {name:'deepseek-v4-pro',color:'#4b9fff',cost:2.29,tokens:950000,pct:64.6,active:true,
         usage_types:{PROMPT_CACHE_HIT_TOKEN:0.30,PROMPT_CACHE_MISS_TOKEN:1.16,RESPONSE_TOKEN:0.96}},
        {name:'deepseek-v4-flash',color:'#4caf93',cost:1.25,tokens:610000,pct:35.4,active:true,
         usage_types:{PROMPT_CACHE_HIT_TOKEN:0.46,PROMPT_CACHE_MISS_TOKEN:0.44,RESPONSE_TOKEN:0.36}},
      ],
      year:2026,month:5,
      daily:[
        {date:'05-01',cost:0},{date:'05-02',cost:0},{date:'05-03',cost:0.12},{date:'05-04',cost:0.45},{date:'05-05',cost:0.89},
        {date:'05-06',cost:0.33},{date:'05-07',cost:0.56},{date:'05-08',cost:1.02},{date:'05-09',cost:0.17},
      ],
      balance:{is_available:true,balance_infos:[{currency:'CNY',total_balance:'110.00',granted_balance:'10.00',topped_up_balance:'100.00'}]}
    });
    return;
  }
  try{
    var d=await pywebview.api.get_data();
    render(d);
  }catch(e){document.getElementById('btn-refresh').title='Error: '+e}
}

async function init(){
  if(typeof pywebview==='undefined'){load();return}
  var hk=await pywebview.api.has_key();
  if(hk){
    document.getElementById('prompt').style.display='none';
    load();
    if(typeof pywebview!=='undefined')pywebview.api.init_auto_hide();
  }else{
    document.getElementById('prompt').style.display='flex';
    refreshKeyList();
  }
}

document.getElementById('key-save').onclick=async function(){
  var k=document.getElementById('key-inp').value.trim();
  if(!k)return;
  if(!k.startsWith('sk-')){
    document.getElementById('key-save').textContent='Key 须以 sk- 开头';
    setTimeout(function(){document.getElementById('key-save').textContent='保存并连接';},1500);
    return;
  }
  if(typeof pywebview!=='undefined')await pywebview.api.save_key('默认',k);
  document.getElementById('prompt').style.display='none';
  load();
};
document.getElementById('key-inp').onkeypress=function(e){if(e.key==='Enter')document.getElementById('key-save').click()};

document.getElementById('prompt-min').onclick=async function(){
  if(typeof pywebview!=='undefined')await pywebview.api.minimize_window();
};
document.getElementById('prompt-close').onclick=function(){
  document.getElementById('close-dlg').style.display='flex';
};
var _stgFromPrompt=false;
document.getElementById('prompt-stg').onclick=function(){
  _stgFromPrompt=true;
  document.getElementById('prompt').style.display='none';
  document.getElementById('stg').style.display='flex';
  refreshStgKeys();
};
document.getElementById('stg-close').onclick=function(){
  document.getElementById('stg').style.display='none';
  if(_stgFromPrompt){_stgFromPrompt=false;document.getElementById('prompt').style.display='flex';}
};
document.getElementById('btn-stg').onclick=function(){
  _stgFromPrompt=false;
  var s=document.getElementById('stg');
  s.style.display=s.style.display==='none'?'flex':'none';
  if(s.style.display==='flex')refreshStgKeys();
};
async function refreshStgKeys(){
  if(typeof pywebview==='undefined')return;
  var keys=await pywebview.api.get_keys()||[];
  var h='';
  keys.forEach(function(k,i){
    h+='<div style="display:flex;align-items:center;gap:4px;font-size:10px">'+
      '<span style="flex:1;color:var(--dim)">'+esc(k.name)+'</span>'+
      '<span style="color:var(--dim);font-family:monospace;font-size:9px">'+esc(k.key)+'</span>'+
      '<button onclick="stgUseKey('+i+')" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:10px">&#10003;</button>'+
      '<button onclick="stgDelKey('+i+')" style="background:none;border:none;color:var(--dim);cursor:pointer;font-size:12px">&times;</button>'+
    '</div>';
  });
  document.getElementById('stg-key-list').innerHTML=h||'<div style="font-size:10px;color:var(--dim)">暂无</div>';
}
async function stgUseKey(i){await pywebview.api.select_key(i);refreshStgKeys();load();}
async function stgDelKey(i){await pywebview.api.delete_key(i);refreshStgKeys();}
document.getElementById('stg-add-key').onclick=function(){
  document.getElementById('stg-key-form').style.display='flex';
  this.style.display='none';
};
document.getElementById('stg-key-cancel').onclick=function(){
  document.getElementById('stg-key-form').style.display='none';
  document.getElementById('stg-add-key').style.display='block';
};
document.getElementById('stg-key-save').onclick=async function(){
  var n=document.getElementById('stg-key-name').value.trim();
  var k=document.getElementById('stg-key-inp').value.trim();
  if(!k||!k.startsWith('sk-'))return;
  await pywebview.api.save_key(n,k);
  document.getElementById('stg-key-form').style.display='none';
  document.getElementById('stg-add-key').style.display='block';
  document.getElementById('stg-key-name').value='';document.getElementById('stg-key-inp').value='';
  refreshStgKeys();load();
};
document.getElementById('btn-close').onclick=function(){
  document.getElementById('close-dlg').style.display='flex';
};
document.getElementById('dlg-cancel').onclick=function(){
  document.getElementById('close-dlg').style.display='none';
};
document.getElementById('dlg-min').onclick=async function(){
  document.getElementById('close-dlg').style.display='none';
  if(typeof pywebview!=='undefined')await pywebview.api.minimize_window();
};
document.getElementById('dlg-quit').onclick=async function(){
  if(typeof pywebview!=='undefined')await pywebview.api.quit_app();
};


document.getElementById('btn-min').onclick=async function(){
  if(typeof pywebview!=='undefined')await pywebview.api.minimize_window();
};
var _theme='dark';
function toggleTheme(){
  _theme=_theme==='dark'?'light':'dark';
  document.body.classList.toggle('light',_theme==='light');
  document.getElementById('th-toggle').innerHTML=_theme==='dark'?'&#9790;':'&#9728;';
  if(typeof pywebview!=='undefined')pywebview.api.set_theme(_theme);
}
// Load saved theme
(function(){
  if(typeof pywebview!=='undefined'){
    pywebview.api.get_theme().then(function(t){
      if(t==='light'){_theme='light';document.body.classList.add('light');document.getElementById('th-toggle').innerHTML='&#9728;';}
    });
  }
})();

document.getElementById('pt-save').onclick=async function(){
  var t=document.getElementById('pt-inp').value.trim();
  if(!t)return;
  await pywebview.api.save_platform_token(t);
  document.getElementById('stg').style.display='none';
  load();
};
document.getElementById('btn-refresh').onclick=load;

// Window drag via header + double-click maximize
(function(){
  var dg=null, clicks=0, clickTimer=null;
  document.addEventListener('mousedown',async function(e){
    var hdr=e.target.closest('.hdr');
    if(!hdr)return;
    var isPrompt=hdr.closest('#prompt');
    if(e.target.closest('select,button,input,textarea'))return;
    if(!isPrompt){
      clicks++;
      if(clicks===2){
        clearTimeout(clickTimer);clicks=0;
        if(typeof pywebview!=='undefined')pywebview.api.toggle_maximize();
        return;
      }
      clickTimer=setTimeout(function(){clicks=0;},350);
    }
    // Get window position for absolute tracking
    var pos={x:0,y:0};
    if(typeof pywebview!=='undefined'){
      try{pos=await pywebview.api.get_pos();}catch(ex){}
    }
    dg={startX:e.screenX,startY:e.screenY,winX:pos.x,winY:pos.y};
    document.body.setPointerCapture(e.pointerId);
  });
  document.addEventListener('pointermove',function(e){
    if(!dg)return;
    var newX=dg.winX+(e.screenX-dg.startX);
    var newY=dg.winY+(e.screenY-dg.startY);
    if(typeof pywebview!=='undefined')pywebview.api.move_to(newX,newY);
  });
  document.addEventListener('pointerup',function(){
    if(dg){dg=null;
      if(typeof pywebview!=='undefined')pywebview.api.snap_check();
    }
  });
})();

// Peek mode + auto-hide behavior
(function(){
  var isPeeking=false, hideTimer=null;
  setInterval(function(){
    if(typeof pywebview==='undefined')return;
    pywebview.api.is_peeking().then(function(v){
      isPeeking=v; document.body.classList.toggle('peek',v);
    });
  },500);
  // Reveal on click at right edge (hover handled by Python polling)
  document.addEventListener('click',function(e){
    if(e.clientX >= window.innerWidth - 10){
      document.body.classList.add('revealing');
      if(typeof pywebview!=='undefined')pywebview.api.reveal_window();
      setTimeout(function(){document.body.classList.remove('revealing');},400);
    }
  });
  // Auto-hide: use pointerleave on body (fires when pointer exits the window)
  document.body.addEventListener('pointerleave',function(){
    if(isPeeking)return;
    hideTimer=setTimeout(function(){
      if(typeof pywebview!=='undefined')pywebview.api.auto_hide();
    },600);
  });
  document.body.addEventListener('pointerenter',function(){
    if(hideTimer){clearTimeout(hideTimer);hideTimer=null;}
  });
})();

// Resize grip (bottom-right corner)
(function(){
  var rs=null, rw=MIN_W_PLACEHOLDER, rh=820, rlast=0;
  document.getElementById('grip').addEventListener('mousedown',function(e){
    e.preventDefault();e.stopPropagation();
    rs={mx:e.screenX,my:e.screenY};
    rw=window.innerWidth;rh=window.innerHeight;
  });
  window.addEventListener('mousemove',function(e){
    if(!rs)return;
    var now=Date.now();if(now-rlast<15)return;rlast=now;
    rw=Math.max(MIN_W_PLACEHOLDER,Math.round(rw+(e.screenX-rs.mx)));
    rh=Math.max(500,Math.round(rh+(e.screenY-rs.my)));
    rs.mx=e.screenX;rs.my=e.screenY;
    if(typeof pywebview!=='undefined')pywebview.api.resize_window(rw,rh);
  });
  window.addEventListener('mouseup',function(){rs=null;});
})();

setInterval(load,180000);
init();
</script>
</body>
</html>"""

# Inject logo
HTML = HTML.replace("LOGO_PLACEHOLDER", LOGO_B64)
HTML = HTML.replace("MIN_W_PLACEHOLDER", str(W))


# ── JS API ─────────────────────────────────────────────────────────────
class Api:
    def has_key(self):
        return bool(load_api_key())

    def get_keys(self):
        return load_keys()

    def save_key(self, name, key):
        return save_key(name, key)

    def select_key(self, idx):
        select_key(idx)

    def delete_key(self, idx):
        delete_key(idx)

    def get_active_idx(self):
        return _rc().get("active_key", 0)

    def init_auto_hide(self):
        self.start_auto_hide_polling()

    def save_platform_token(self, tok):
        save_pt(tok)
        return True

    def get_data(self):
        balance = fetch_balance()
        usage = fetch_usage()
        if usage:
            usage["balance"] = balance
            return usage
        return {"balance": balance, "models": [], "total_cost": 0,
                "total_tokens": 0, "active_model_days": 0, "currency": "CNY", "today_cost": 0, "today_models": [], "daily": [],
                "year": datetime.now().year, "month": datetime.now().month}

    def get_month_data(self, year, month):
        usage = fetch_usage(int(year), int(month))
        if usage:
            return usage
        return {"models": [], "total_cost": 0, "total_tokens": 0, "active_model_days": 0,
                "currency": "CNY", "today_cost": 0, "today_models": [], "daily": [],
                "year": int(year), "month": int(month)}

    def move_to(self, x, y):
        w = webview.windows[0] if webview.windows else None
        if w:
            w.move(int(x), int(y))

    def get_pos(self):
        w = webview.windows[0] if webview.windows else None
        if w:
            return {"x": w.x, "y": w.y}
        return {"x": 0, "y": 0}

    def resize_window(self, wd, ht):
        win = webview.windows[0] if webview.windows else None
        if win:
            win.resize(int(wd), int(ht))

    def toggle_maximize(self):
        w = webview.windows[0] if webview.windows else None
        if w:
            sw = ctypes.windll.user32.GetSystemMetrics(0)
            sh = ctypes.windll.user32.GetSystemMetrics(1)
            if w.width >= sw and w.height >= sh:
                w.resize(W, HH)
                w.move((sw - W) // 2, (sh - HH) // 2)
            else:
                w.move(0, 0)
                w.resize(sw, sh)
            # Prevent snap_check from hiding after maximize/restore
            self._skip_snap = True

    def minimize_window(self):
        w = webview.windows[0] if webview.windows else None
        if w:
            w.hide()

    def quit_app(self):
        self._peek_polling = False
        self._hide_polling = False
        w = webview.windows[0] if webview.windows else None
        if w:
            w.destroy()

    def open_platform(self):
        webbrowser.open("https://platform.deepseek.com/usage")

    def snap_check(self):
        """Check if window should snap to right edge (auto-hide)."""
        if getattr(self, '_skip_snap', False):
            self._skip_snap = False
            return
        w = webview.windows[0] if webview.windows else None
        if not w:
            return
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        sh = ctypes.windll.user32.GetSystemMetrics(1)
        # Don't snap if maximized
        if w.width >= sw and w.height >= sh:
            return
        if w.x + w.width >= sw - 40:
            w.move(sw - 3, w.y)
            self.start_peek_polling()

    def reveal_window(self):
        """Restore window from peek mode."""
        w = webview.windows[0] if webview.windows else None
        if not w:
            return
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        # Move to target first (hidden), then show
        new_x = sw - w.width
        if new_x < 0:
            new_x = (sw - w.width) // 2
        w.move(new_x, w.y)
        self._peek_polling = False
        self._just_revealed = True
        time.sleep(0.12)
        w.evaluate_js("document.body.classList.remove('peek')")
        self.start_auto_hide_polling()

    def start_peek_polling(self):
        """Start polling mouse position to detect hover on peek edge."""
        if getattr(self, '_peek_polling', False):
            return
        self._peek_polling = True
        def poll():
            hover_count = 0
            while self._peek_polling:
                time.sleep(0.2)
                w = webview.windows[0] if webview.windows else None
                if not w: break
                sw = ctypes.windll.user32.GetSystemMetrics(0)
                sh = ctypes.windll.user32.GetSystemMetrics(1)
                # Only poll when peeking
                if w.x < sw - 20:
                    self._peek_polling = False
                    break
                # Check mouse position
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                if pt.x >= sw - 8:
                    hover_count += 1
                    if hover_count >= 3:  # 0.6s continuous hover
                        self.reveal_window()
                        break
                else:
                    hover_count = 0
        threading.Thread(target=poll, daemon=True).start()

    def is_peeking(self):
        """Check if window is in peek (hidden) mode."""
        w = webview.windows[0] if webview.windows else None
        if not w:
            return False
        sw = ctypes.windll.user32.GetSystemMetrics(0)
        return w.x >= sw - 20

    def auto_hide(self):
        """Snap window to right edge (peek mode)."""
        if getattr(self, '_just_revealed', False):
            self._just_revealed = False
            return
        self._peek_polling = False
        self.snap_check()

    def start_auto_hide_polling(self):
        """Background thread: auto-hide when mouse leaves window."""
        if getattr(self, '_hide_polling', False):
            return
        self._hide_polling = True
        def poll():
            away_count = 0
            while self._hide_polling:
                time.sleep(0.3)
                w = webview.windows[0] if webview.windows else None
                if not w: break
                # Skip if already peeking
                sw = ctypes.windll.user32.GetSystemMetrics(0)
                if w.x >= sw - 20: continue
                # Check if mouse is within window bounds
                pt = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                in_window = (w.x <= pt.x <= w.x + w.width and
                            w.y <= pt.y <= w.y + w.height)
                if not in_window:
                    away_count += 1
                    if away_count >= 2:  # ~0.6s outside
                        self.auto_hide()
                        break
                else:
                    away_count = 0
            self._hide_polling = False
        threading.Thread(target=poll, daemon=True).start()

    def set_theme(self, t):
        c = _rc(); c["theme"] = t; _wc(c)

    def get_theme(self):
        return _rc().get("theme", "dark")


# ── Tray ───────────────────────────────────────────────────────────────
def _tray_img():
    # Use logo.png for tray icon (same as app icon), keep aspect ratio
    for lp in _LOGO_PATHS:
        if lp.exists():
            img = Image.open(lp).convert("RGBA")
            # Scale to fit 64x64, preserve aspect ratio
            scale = 62 / max(img.size)
            nw, nh = int(img.width * scale), int(img.height * scale)
            img = img.resize((nw, nh), Image.LANCZOS)
            canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            canvas.paste(img, ((64 - nw) // 2, (64 - nh) // 2))
            return canvas
    # Fallback
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse([8, 8, 56, 56], fill="#0b1019", outline="#58a6ff", width=3)
    d.polygon([(26, 20), (32, 16), (38, 20), (38, 36), (26, 36)], fill="#58a6ff")
    d.polygon([(20, 28), (26, 24), (26, 40), (20, 40)], fill="#58a6ff")
    d.polygon([(38, 28), (44, 24), (44, 40), (38, 40)], fill="#58a6ff")
    return img


def _tray():
    def show(icon, item):
        w = webview.windows[0] if webview.windows else None
        if w:
            w.show()
            w.restore()

    def quit_(icon, item):
        w = webview.windows[0] if webview.windows else None
        if w:
            w.destroy()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("显示", show, default=True),
        pystray.MenuItem("刷新", lambda i, it: (
            webview.windows[0].evaluate_js("load()") if webview.windows else None
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", quit_),
    )
    icon = pystray.Icon("dsm", _tray_img(), "DeepSeek Monitor", menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# ── Main ───────────────────────────────────────────────────────────────
def main():
    # Center window on screen
    sw = ctypes.windll.user32.GetSystemMetrics(0)
    sh = ctypes.windll.user32.GetSystemMetrics(1)
    cx, cy = (sw - W) // 2, (sh - HH) // 2
    webview.create_window(
        title="DeepSeek Monitor", html=HTML, js_api=Api(),
        width=W, height=HH, x=cx, y=cy,
        frameless=True, on_top=True, easy_drag=False,
        background_color="#0b1019", resizable=True,
    )
    if HAS_TRAY:
        _tray_icon = _tray()  # Keep reference to prevent GC
    # Ensure correct size after WebView2 initializes
    def _fix_size():
        time.sleep(0.3)
        wins = webview.windows
        if wins: wins[0].resize(W, HH)
    threading.Thread(target=_fix_size, daemon=True).start()
    webview.start()


if __name__ == "__main__":
    main()
