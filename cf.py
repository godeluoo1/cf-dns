import json
import time
import urllib.request
import urllib.error
import socket
import os
import sys
import random
import threading
import ipaddress
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

# 🛡️ 全局并发错峰锁和请求时间戳，确保多线程调用 API 时每个请求发起之间至少间隔 250ms
_req_lock = threading.Lock()
_last_req_time = 0.0

def _rate_limit_sleep():
    global _last_req_time
    with _req_lock:
        now = time.time()
        elapsed = now - _last_req_time
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        _last_req_time = time.time()

# 尝试导入解密模块并做兼容处理
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, modes
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.backends import default_backend
    
    try:
        from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
    except ImportError:
        from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES
except ImportError:
    print("❌ 缺少必要依赖！请先在 VPS 终端执行：apt install python3-cryptography -y")
    exit(1)

# 尝试导入 dns.resolver 用于国内 DNS 健康体检
DNS_AVAILABLE = False
try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    pass

# 尝试导入华为云 SDK
HUAWEI_SDK_AVAILABLE = False
try:
    from huaweicloudsdkcore.auth.credentials import BasicCredentials
    from huaweicloudsdkdns.v2 import *
    from huaweicloudsdkdns.v2.model import * 
    from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
    HUAWEI_SDK_AVAILABLE = True
except ImportError:
    pass

# ==================== 华为云 DNS 自动化配置 ====================
# 安全加固：完全移除硬编码的 AK/SK，强制从环境变量读取
HUAWEICLOUD_AK = os.environ.get("HUAWEICLOUD_AK", "")
HUAWEICLOUD_SK = os.environ.get("HUAWEICLOUD_SK", "")

DOMAIN = "blogluo.eu.org"          # 你的主域名
REGION = "cn-east-3"               # 华为云DNS解析服务区，默认即可

# ==================== 多子域名智能分流配置 ====================
# 配置每个子域名对应的测速维度：
# 0: 近24小时数据 (按小时优选，高灵敏度，适合实时要求高的服务)
# 1: 近30天数据 (按月优选，适合注重长期稳定性的服务)
SUB_DOMAINS_CONFIG = {
    "cf": 1,      # cf.blogluo.eu.org 按月 (30天) 优选
}
# =========================================================================

# ==================== 极简核心日志与 HTML 看板生成 ====================
def log_event(state_manager, message):
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    log_line = f"[{current_time}] {message}"
    
    logs = state_manager.state.setdefault("logs", [])
    logs.append(log_line)
    
    # 日志上限调整为 100 条
    if len(logs) > 100:
        state_manager.state["logs"] = logs[-100:]
    
    print(log_line)

def generate_visual_html(state_manager, filename="status.html"):
    current_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    def get_cname_stats(cname, sub_domain, line_key):
        candidates = state_manager.state.get("candidates", [])
        monitor_type = SUB_DOMAINS_CONFIG.get(sub_domain, 0)
        key_suffix = "30day" if monitor_type == 1 else "24h"
        for c in candidates:
            if c.get("ip") == cname:
                data = c.get(f"data_{key_suffix}")
                if data:
                    if line_key == "Dianxin":
                        return data.get("dxLatencyEma", 9999.0), data.get("dxLossEma", 100.0), data.get("dxJitter", 0.0)
                    elif line_key == "Yidong":
                        return data.get("ydLatencyEma", 9999.0), data.get("ydLossEma", 100.0), data.get("ydJitter", 0.0)
                    elif line_key == "Liantong":
                        return data.get("ltLatencyEma", 9999.0), data.get("ltLossEma", 100.0), data.get("ltJitter", 0.0)
                    else:
                        return data.get("avgLatency", 9999.0), data.get("avgLoss", 100.0), data.get("avgJitter", 0.0)
                break
        return "N/A", "N/A", "N/A"

    def get_pool_health(sub_domain, pool_name):
        pool_data = state_manager.state.get(pool_name, {}).get(sub_domain, {})
        if not pool_data:
            return 0, 0, 0.0
        if isinstance(pool_data, list):
            pool = pool_data
        else:
            pool = []
            for line_list in pool_data.values():
                pool.extend(line_list)
        if not pool:
            return 0, 0, 0.0
        total = len(pool)
        healthy = sum(1 for item in pool if item.get("healthy", False))
        rate = (healthy / total * 100) if total > 0 else 0.0
        return healthy, total, round(rate, 1)

    def get_reputation(sub_domain, line_key, cname):
        return state_manager.state.get("reputation_scores", {}).get(sub_domain, {}).get(line_key, {}).get(cname, 50)

    last_update = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    last_api_time = state_manager.state.get("last_api_update_time", 0.0)
    api_time_str = time.strftime("%H:%M:%S", time.localtime(last_api_time)) if last_api_time > 0 else "从未"

    subdomains_html = ""
    for sub, monitor_type in SUB_DOMAINS_CONFIG.items():
        time_label = "30天稳定维度 (EMA加权)" if monitor_type == 1 else "24小时高频维度 (EMA加权)"
        champs = state_manager.state.get("champions", {}).get(sub, {
            "Dianxin": "N/A", "Yidong": "N/A", "Liantong": "N/A", "default_view": "N/A"
        })
        
        h5, t5, r5 = get_pool_health(sub, "top5_pool")
        h20, t20, r20 = get_pool_health(sub, "top20_pool")
        h100, t100, r100 = get_pool_health(sub, "top100_pool")

        lines_data = []
        for line_key, line_name, icon, color_cls in [
            ("Dianxin", "中国电信", "📡", "telecom"),
            ("Yidong", "中国移动", "📱", "mobile"),
            ("Liantong", "中国联通", "⚡", "unicom"),
            ("default_view", "默认保底", "🛡️", "default")
        ]:
            cname = champs.get(line_key, "N/A")
            latency, loss, jitter = get_cname_stats(cname, sub, line_key)
            reputation = get_reputation(sub, line_key, cname) if cname != "N/A" else 0
            
            latency_str = f"{latency} ms" if isinstance(latency, (int, float)) and latency < 9999 else str(latency)
            loss_str = f"{loss} %" if isinstance(loss, (int, float)) else str(loss)
            jitter_str = f"{jitter} ms" if isinstance(jitter, (int, float)) else str(jitter)
            
            loss_color = "var(--loss-ok)"
            if isinstance(loss, (int, float)):
                if loss > 5.0:
                    loss_color = "var(--color-danger)"
                elif loss > 1.0:
                    loss_color = "var(--loss-warn)"
            
            rep_color = "var(--color-success)"
            if reputation < 30:
                rep_color = "var(--color-danger)"
            elif reputation < 60:
                rep_color = "var(--color-warning)"
            
            lines_data.append(f"""
                <div class="line-row {color_cls}" data-sub="{sub}" data-line="{line_key}">
                    <div class="line-info">
                        <span class="line-icon">{icon}</span>
                        <div>
                            <div class="line-name">{line_name}</div>
                            <div class="line-cname cname-val" title="{cname}">{cname}</div>
                        </div>
                    </div>
                    <div class="line-metrics">
                        <div class="metric-item">
                            <span class="metric-label">EMA延迟</span>
                            <span class="metric-val latency-val">{latency_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">抖动</span>
                            <span class="metric-val jitter-val">{jitter_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">EMA丢包</span>
                            <span class="metric-val loss-val" style="color: {loss_color}">{loss_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">信誉分</span>
                            <span class="metric-val rep-val" style="color: {rep_color}">{reputation}</span>
                        </div>
                    </div>
                </div>
            """)

        lines_html = "\n".join(lines_data)

        subdomains_html += f"""
        <div class="card card-subdomain">
            <div class="card-header-sub">
                <div>
                    <h2 class="subdomain-title">{sub}.{DOMAIN}</h2>
                    <span class="badge badge-dim">{time_label}</span>
                </div>
                <div class="pool-status-mini">
                    <span class="indicator blink-green"></span> 正常运行中
                </div>
            </div>
            
            <div class="lines-container">
                {lines_html}
            </div>
            
            <div class="pools-grid">
                <div class="pool-box">
                    <div class="pool-box-header">
                        <span>Top 5 热池健康</span>
                        <span class="pool-ratio">{h5}/{t5}</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: {r5}%"></div>
                    </div>
                </div>
                <div class="pool-box">
                    <div class="pool-box-header">
                        <span>Top 20 热池健康</span>
                        <span class="pool-ratio">{h20}/{t20}</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: {r20}%"></div>
                    </div>
                </div>
                <div class="pool-box">
                    <div class="pool-box-header">
                        <span>Top 100 热池健康</span>
                        <span class="pool-ratio">{h100}/{t100}</span>
                    </div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: {r100}%"></div>
                    </div>
                </div>
            </div>
        </div>
        """

    logs = state_manager.state.get("logs", [])
    logs_html = ""
    if not logs:
        logs_html = '<div class="no-logs">暂无核心日志事件</div>'
    else:
        for log in reversed(logs):
            log_class = "log-item-info"
            if "🚨" in log or "❌" in log or "熔断" in log or "降级" in log:
                log_class = "log-item-danger"
            elif "🔥" in log or "切换" in log:
                log_class = "log-item-warning"
            elif "🆕" in log or "初始化" in log:
                log_class = "log-item-success"
            
            logs_html += f'<div class="log-item {log_class}">{log}</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CF 智能优选 CNAME 状态面板</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #030712;
            --card-bg: rgba(17, 24, 39, 0.4);
            --card-border: rgba(255, 255, 255, 0.08);
            --card-border-hover: rgba(255, 255, 255, 0.15);
            --text-main: #F9FAFB;
            --text-muted: #9CA3AF;
            --color-primary: #38BDF8;
            --color-primary-glow: rgba(56, 189, 248, 0.5);
            --color-success: #34D399;
            --color-warning: #FBBF24;
            --color-danger: #F87171;
            --loss-ok: #34D399;
            --loss-warn: #FBBF24;
        }}

        * {{ box-sizing: border-box; margin: 0; padding: 0; }}

        body {{
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(circle at 15% 50%, rgba(56, 189, 248, 0.04), transparent 25%),
                radial-gradient(circle at 85% 30%, rgba(167, 139, 250, 0.04), transparent 25%);
            background-attachment: fixed;
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            min-height: 100vh;
            padding: 3rem 1.5rem;
            line-height: 1.6;
            -webkit-font-smoothing: antialiased;
        }}

        .container {{ max-width: 1200px; margin: 0 auto; }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2.5rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 1.5rem;
            animation: fadeInDown 0.8s ease-out;
        }}

        .logo-section h1 {{
            font-size: 2.2rem;
            font-weight: 800;
            letter-spacing: -0.03em;
            background: linear-gradient(135deg, #38BDF8 0%, #A78BFA 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .logo-section p {{
            font-size: 0.95rem;
            color: var(--text-muted);
            margin-top: 0.35rem;
            font-weight: 400;
        }}

        .system-meta {{ text-align: right; }}

        .meta-badge {{
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(52, 211, 153, 0.1);
            border: 1px solid rgba(52, 211, 153, 0.25);
            color: var(--color-success);
            padding: 0.4rem 0.85rem;
            border-radius: 9999px;
            font-size: 0.85rem;
            font-weight: 600;
            box-shadow: 0 0 15px rgba(52, 211, 153, 0.15);
        }}

        .meta-item {{
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 0.6rem;
            font-family: 'JetBrains Mono', monospace;
            opacity: 0.8;
        }}

        .grid-layout {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 1.5rem;
            align-items: start;
        }}
        @media (min-width: 900px) {{ .grid-layout {{ grid-template-columns: 2fr 1fr; }} }}

        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 20px;
            padding: 1.75rem;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 10px 15px -3px rgba(0, 0, 0, 0.2);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            transition: transform 0.3s ease, border-color 0.3s ease;
            animation: fadeInUp 0.8s ease-out backwards;
            margin-bottom: 1.5rem;
        }}
        .card:hover {{
            border-color: var(--card-border-hover);
        }}

        .card-header-sub {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1.5rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            padding-bottom: 1.25rem;
        }}

        .subdomain-title {{ font-size: 1.4rem; font-weight: 700; color: #FFFFFF; letter-spacing: 0.5px; }}

        .badge-dim {{
            display: inline-block;
            background: rgba(255, 255, 255, 0.05);
            color: #9CA3AF;
            border: 1px solid rgba(255, 255, 255, 0.1);
            padding: 0.2rem 0.6rem;
            border-radius: 6px;
            font-size: 0.75rem;
            margin-top: 0.4rem;
            font-weight: 500;
        }}

        .pool-status-mini {{
            font-size: 0.85rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 0.5rem;
            font-weight: 500;
        }}

        .indicator {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
        .blink-green {{ background-color: var(--color-success); box-shadow: 0 0 10px var(--color-success); animation: pulse-green 2s infinite; }}

        @keyframes pulse-green {{
            0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(52, 211, 153, 0.7); }}
            70% {{ transform: scale(1); box-shadow: 0 0 0 6px rgba(52, 211, 153, 0); }}
            100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(52, 211, 153, 0); }}
        }}

        .lines-container {{ display: flex; flex-direction: column; gap: 0.85rem; margin-bottom: 1.75rem; }}

        .line-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 14px;
            padding: 1.15rem;
            transition: all 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            cursor: default;
        }}
        .line-row:hover {{
            background: rgba(255, 255, 255, 0.04);
            border-color: rgba(255, 255, 255, 0.1);
            transform: translateY(-2px);
            box-shadow: 0 10px 20px -10px rgba(0,0,0,0.5);
        }}

        .line-info {{ display: flex; align-items: center; gap: 1rem; min-width: 0; }}

        .line-icon {{
            font-size: 1.3rem;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 42px;
            height: 42px;
            background: rgba(255, 255, 255, 0.03);
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.05);
        }}

        .line-name {{ font-size: 0.95rem; font-weight: 600; color: #F1F5F9; letter-spacing: 0.5px; }}
        .line-cname {{
            font-size: 0.8rem;
            font-family: 'JetBrains Mono', monospace;
            color: var(--text-muted);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            margin-top: 0.25rem;
        }}

        .line-metrics {{ display: flex; gap: 2rem; flex-shrink: 0; }}
        .metric-item {{ text-align: right; }}

        .metric-label {{
            display: block;
            font-size: 0.65rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.08em;
            font-weight: 600;
        }}
        .metric-val {{
            display: block;
            font-size: 1rem;
            font-weight: 700;
            color: var(--color-primary);
            font-family: 'JetBrains Mono', monospace;
            margin-top: 0.2rem;
            text-shadow: 0 0 10px rgba(56, 189, 248, 0.2);
        }}

        .telecom .line-icon {{ background: rgba(56, 189, 248, 0.1); color: #38BDF8; border-color: rgba(56, 189, 248, 0.2); }}
        .mobile .line-icon {{ background: rgba(52, 211, 153, 0.1); color: #34D399; border-color: rgba(52, 211, 153, 0.2); }}
        .unicom .line-icon {{ background: rgba(251, 191, 36, 0.1); color: #FBBF24; border-color: rgba(251, 191, 36, 0.2); }}
        .default .line-icon {{ background: rgba(167, 139, 250, 0.1); color: #A78BFA; border-color: rgba(167, 139, 250, 0.2); }}

        .pools-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1rem;
            border-top: 1px solid rgba(255, 255, 255, 0.04);
            padding-top: 1.5rem;
        }}

        .pool-box {{
            background: rgba(0, 0, 0, 0.2);
            border-radius: 12px;
            padding: 1rem;
            border: 1px solid rgba(255, 255, 255, 0.02);
            transition: background 0.3s;
        }}
        .pool-box:hover {{ background: rgba(0, 0, 0, 0.3); }}

        .pool-box-header {{ display: flex; justify-content: space-between; align-items: center; font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.6rem; font-weight: 500; }}
        .pool-ratio {{ font-family: 'JetBrains Mono', monospace; font-weight: 700; color: var(--text-main); }}

        .progress-bar-bg {{ height: 5px; background: rgba(255, 255, 255, 0.05); border-radius: 3px; overflow: hidden; }}
        .progress-bar-fill {{
            height: 100%;
            background: linear-gradient(90deg, #38BDF8 0%, #A78BFA 100%);
            border-radius: 3px;
            transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
            box-shadow: 0 0 8px rgba(56, 189, 248, 0.5);
        }}

        .card-logs-title {{ font-size: 1.25rem; font-weight: 700; margin-bottom: 1.25rem; color: #FFFFFF; display: flex; align-items: center; gap: 0.6rem; letter-spacing: 0.5px; }}

        .logs-container {{
            max-height: 600px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.6rem;
            padding-right: 0.5rem;
        }}
        .logs-container::-webkit-scrollbar {{ width: 5px; }}
        .logs-container::-webkit-scrollbar-thumb {{ background: rgba(255, 255, 255, 0.15); border-radius: 3px; }}
        .logs-container::-webkit-scrollbar-thumb:hover {{ background: rgba(255, 255, 255, 0.25); }}

        .log-item {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            padding: 0.75rem 1rem;
            border-radius: 8px;
            border-left: 4px solid transparent;
            word-break: break-all;
            background: rgba(0, 0, 0, 0.2);
            transition: transform 0.2s;
        }}
        .log-item:hover {{ transform: translateX(2px); }}

        .log-item-info {{ border-left-color: var(--text-muted); color: #CBD5E1; }}
        .log-item-success {{ border-left-color: var(--color-success); color: #6EE7B7; background: rgba(52, 211, 153, 0.05); }}
        .log-item-warning {{ border-left-color: var(--color-warning); color: #FDE047; background: rgba(251, 191, 36, 0.05); }}
        .log-item-danger {{ border-left-color: var(--color-danger); color: #FCA5A5; background: rgba(248, 113, 113, 0.08); animation: pulse-red 2s infinite; }}

        @keyframes pulse-red {{
            0% {{ box-shadow: inset 0 0 0 rgba(248, 113, 113, 0); }}
            50% {{ box-shadow: inset 2px 0 10px rgba(248, 113, 113, 0.2); }}
            100% {{ box-shadow: inset 0 0 0 rgba(248, 113, 113, 0); }}
        }}

        .no-logs {{ text-align: center; color: var(--text-muted); font-size: 0.85rem; padding: 3rem 0; font-weight: 500; }}

        @keyframes fadeInDown {{ from {{ opacity: 0; transform: translateY(-15px); }} to {{ opacity: 1; transform: translateY(0); }} }}
        @keyframes fadeInUp {{ from {{ opacity: 0; transform: translateY(15px); }} to {{ opacity: 1; transform: translateY(0); }} }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-section">
                <h1>Cloudflare 智能优选监控</h1>
                <p>三网全自动漏斗体检 & 故障快速熔断系统</p>
            </div>
            <div class="system-meta">
                <span class="meta-badge">
                    <span class="indicator blink-green"></span> 正常守候中
                </span>
                <div class="meta-item">本地检测：{last_update}</div>
                <div class="meta-item">大池更新：{api_time_str}</div>
            </div>
        </header>

        <div class="grid-layout">
            <div class="main-column">
                {subdomains_html}
            </div>
            <div class="side-column">
                <div class="card" style="animation-delay: 0.2s;">
                    <h2 class="card-logs-title">📋 核心大事件日志</h2>
                    <div class="logs-container">
                        {logs_html}
                    </div>
                </div>
            </div>
        </div>
    </div>

<script>
async function pollUpdate() {{
    try {{
        const res = await fetch('./state_snapshot.json?t=' + Date.now());
        if (!res.ok) throw new Error();
        const newState = await res.json();
        reconcile(newState);
    }} catch(e) {{}}
    setTimeout(pollUpdate, 60000);
}}

function updateEl(el, val, color) {{
    if (!el || el.textContent === String(val)) return;
    el.textContent = val;
    if (color) el.style.color = color;
    el.classList.add('updated');
    setTimeout(() => el.classList.remove('updated'), 600);
}}

function reconcile(newState) {{
    if (!newState.data) return;
    for (const [sub, lines] of Object.entries(newState.data)) {{
        for (const [line_key, stats] of Object.entries(lines)) {{
            const row = document.querySelector(`.line-row[data-sub="${{sub}}"][data-line="${{line_key}}"]`);
            if (row) {{
                const cnameEl = row.querySelector('.cname-val');
                if (cnameEl && cnameEl.textContent !== stats.cname) {{
                    cnameEl.textContent = stats.cname;
                    cnameEl.title = stats.cname;
                }}
                updateEl(row.querySelector('.latency-val'), stats.latency);
                updateEl(row.querySelector('.jitter-val'), stats.jitter);
                updateEl(row.querySelector('.loss-val'), stats.loss, stats.loss_color);
                updateEl(row.querySelector('.rep-val'), stats.reputation, stats.rep_color);
            }}
        }}
    }}
}}

const style = document.createElement('style');
style.textContent = `
    .metric-val.updated {{ animation: number-tick 0.5s cubic-bezier(0.34, 1.56, 0.64, 1); }}
    @keyframes number-tick {{ 0% {{ transform: translateY(-4px); opacity: 0.3; }} 60% {{ transform: translateY(1px); }} 100% {{ transform: translateY(0); opacity: 1; }} }}
`;
document.head.appendChild(style);
setTimeout(pollUpdate, 60000);
</script>
</body>
</html>
"""
    state_dir = os.environ.get("STATE_DIR", "")
    if state_dir:
        os.makedirs(state_dir, exist_ok=True)
        filepath = os.path.join(state_dir, filename)
    else:
        filepath = filename
        
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        # 额外生成前端所需的轻量级快照
        snapshot_data = {"ts": current_time, "data": {}}
        for sub, monitor_type in SUB_DOMAINS_CONFIG.items():
            snapshot_data["data"][sub] = {}
            champs = state_manager.state.get("champions", {}).get(sub, {
                "Dianxin": "N/A", "Yidong": "N/A", "Liantong": "N/A", "default_view": "N/A"
            })
            for line_key in ["Dianxin", "Yidong", "Liantong", "default_view"]:
                cname = champs.get(line_key, "N/A")
                latency, loss, jitter = get_cname_stats(cname, sub, line_key)
                reputation = get_reputation(sub, line_key, cname) if cname != "N/A" else 0
                
                latency_str = f"{latency} ms" if isinstance(latency, (int, float)) and latency < 9999 else str(latency)
                loss_str = f"{loss} %" if isinstance(loss, (int, float)) else str(loss)
                jitter_str = f"{jitter} ms" if isinstance(jitter, (int, float)) else str(jitter)
                
                loss_color = "var(--loss-ok)"
                if isinstance(loss, (int, float)):
                    if loss > 5.0: loss_color = "var(--color-danger)"
                    elif loss > 1.0: loss_color = "var(--loss-warn)"
                
                rep_color = "var(--color-success)"
                if reputation < 30: rep_color = "var(--color-danger)"
                elif reputation < 60: rep_color = "var(--color-warning)"
                
                snapshot_data["data"][sub][line_key] = {
                    "cname": cname, "latency": latency_str, "loss": loss_str, "jitter": jitter_str,
                    "reputation": reputation, "loss_color": loss_color, "rep_color": rep_color
                }
        snapshot_path = os.path.join(state_dir, "state_snapshot.json") if state_dir else "state_snapshot.json"
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot_data, f, ensure_ascii=False)

    except Exception as e:
        print(f"❌ 生成看板页面失败: {e}")

def get_pool_list(state, pool_name, sub_domain, line_code):
    pool_data = state.get(pool_name, {}).get(sub_domain, {})
    if isinstance(pool_data, list):
        return pool_data
    return pool_data.get(line_code, [])

# ==================== 本地状态机管理器 ====================
class StateManager:
    def __init__(self, state_file="cf_state.json"):
        state_dir = os.environ.get("STATE_DIR", "")
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)
            self.state_file = os.path.join(state_dir, state_file)
        else:
            self.state_file = state_file
            
        self.state = {
            "last_api_update_time": 0.0,
            "last_top100_time": 0.0,
            "last_top20_time": 0.0,
            "last_top5_time": 0.0,
            "last_switch_time": {},
            "champions": {},
            "consecutive_lead_counts": {},
            "reputation_scores": {}, # 新增：信誉分字典 {sub: {line: {ip: score}}}
            "candidates": [],
            "top100_pool": {},
            "top20_pool": {},
            "top5_pool": {},
            "logs": []
        }
        self.load()
        self.state.setdefault("logs", [])
        self.state.setdefault("reputation_scores", {})

    def load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for k, v in data.items():
                        self.state[k] = v
            except Exception as e:
                print(f"⚠️ 读取状态文件 {self.state_file} 失败，将使用默认状态: {e}")

    def save(self):
        try:
            with open(self.state_file, 'w', encoding='utf-8') as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            # 保存状态后自动生成高级可视化 HTML 看板
            generate_visual_html(self)
        except Exception as e:
            print(f"❌ 写入状态文件 {self.state_file} 失败: {e}")

    def update_reputation(self, sub_domain, line_code, cname, delta):
        """更新节点信誉分，范围 0-100"""
        reps = self.state["reputation_scores"].setdefault(sub_domain, {}).setdefault(line_code, {})
        current = reps.get(cname, 50) # 默认 50 分
        new_score = max(0, min(100, current + delta))
        reps[cname] = new_score
        return new_score

# 使用 3DES 模拟单 DES 加密时间戳
def encrypt_token(timestamp_str):
    key_str = "".join(["385f33c", "b91484b04a177", "828829081ab7"])
    key = (key_str[:8] * 3).encode('utf-8')
    iv = b'00000000'
    padder = padding.PKCS7(64).padder()
    padded_data = padder.update(timestamp_str.encode('utf-8')) + padder.finalize()
    cipher = Cipher(TripleDES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()
    return ciphertext.hex()

# 使用 3DES 模拟单 DES 解密返回 of 密文
def decrypt_payload(ciphertext_hex):
    key_str = "".join(["125f", "33c891484b04677", "7828569081a34"])
    key = (key_str[:8] * 3).encode('utf-8')
    iv = b'00000000'
    ciphertext = bytes.fromhex(ciphertext_hex)
    
    cipher = Cipher(TripleDES(key), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    decrypted_padded = decryptor.update(ciphertext) + decryptor.finalize()
    
    unpadder = padding.PKCS7(64).unpadder()
    try:
        decrypted = unpadder.update(decrypted_padded) + unpadder.finalize()
        return decrypted.decode('utf-8')
    except Exception:
        # 🛡️ 兼容处理：在密文解密 padding 校验出错时解码，并剔除未对齐的特殊填充控制字符，确保 json.loads 不崩溃
        raw_str = decrypted_padded.decode('utf-8', errors='ignore').strip()
        return "".join(c for c in raw_str if ord(c) >= 32 or c in "\t\n\r")

# Cloudflare 官方公开的 IPv4 段列表，用于校验并过滤掉混进来的国内 CDN/普通服务器 IP
CLOUDFLARE_NETWORKS = [ipaddress.ip_network(net) for net in [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.159.0.0/16", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22"
]]

def is_ip_cloudflare(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
        for net in CLOUDFLARE_NETWORKS:
            if ip in net:
                return True
    except Exception:
        pass
    return False

# DNS 存活健康体检函数：使用国内阿里 DNS 223.5.5.5 进行验证
def is_domain_resolvable(domain, max_attempts=2):
    ips = []
    clean_domain = domain.strip().rstrip(".")
    
    for attempt in range(max_attempts):
        if DNS_AVAILABLE:
            try:
                resolver = dns.resolver.Resolver()
                resolver.nameservers = ['223.5.5.5']  # 强行指定国内阿里 DNS 检测
                resolver.timeout = 2.0
                resolver.lifetime = 2.0
                answers = resolver.resolve(clean_domain, 'A')
                ips = [str(rdata) for rdata in answers]
                if ips:
                    break
            except Exception:
                pass
                
        if not ips:
            try:
                addr_info = socket.getaddrinfo(clean_domain, None)
                ips = [info[4][0] for info in addr_info if info[0] == socket.AF_INET]
                if ips:
                    break
            except Exception:
                pass
        
        if attempt < max_attempts - 1:
            time.sleep(0.5)
            
    if not ips:
        return False
        
    for ip in ips:
        if is_ip_cloudflare(ip):
            return True
            
    return False

# ==================== 并发 DNS 存活校验 ====================
def bulk_dns_check(domains):
    unique_domains = list(set(domains))
    results = {}
    if not unique_domains:
        return results
    
    max_workers = min(len(unique_domains), 25)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(is_domain_resolvable, d): d for d in unique_domains}
        for future in as_completed(futures):
            d = futures[future]
            try:
                results[d] = future.result()
            except Exception:
                results[d] = False
    return results

# ==================== EMA 和 抖动(标准差) 计算 ====================
def ema_weighted_avg(values, span):
    """EMA 指数加权平均，最新数据点权重最大"""
    if not values:
        return 9999.0
    alpha = 2.0 / (span + 1)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema

def calculate_stddev(values):
    """计算标准差（抖动）"""
    if not values or len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    return math.sqrt(variance)

# ==================== 获取点位数据与分析 ====================
def fetch_and_calc_stats(domain_item, token, monitor_type=1, max_retries=3):
    vps_id = domain_item.get("id")
    domain_name = domain_item.get("ip")
    if not vps_id:
        return None
    
    url = f"https://vps789.com/public/getVpsPoints?vpsId={vps_id}&type={monitor_type}"
    expected_points = 72 if monitor_type == 0 else 30
    span = 7 if monitor_type == 0 else 20 # EMA span: 24h用7(alpha 0.25)，30d用20(alpha 0.095)
    
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0', 'token': token},
        method='GET'
    )
    
    for attempt in range(max_retries):
        _rate_limit_sleep()
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status != 200:
                    continue
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data.get("code") != 0:
                    continue
                points = res_data.get("data", [])
                if not points:
                    return None
                
                # 按照时间升序排序（旧数据在前，新数据在后）以正确计算 EMA
                points.sort(key=lambda x: x.get('createdTime', ''), reverse=False)
                
                def extract_series(prefix):
                    latencies = []
                    losses = []
                    disconnected = 0
                    missing = 0
                    
                    for p in points:
                        l_val = p.get(f'{prefix}l')
                        p_val = p.get(f'{prefix}p')
                        
                        if p_val is None or l_val is None:
                            missing += 1
                            disconnected += 1
                            losses.append(100.0)
                            latencies.append(5000.0) # 缺失点延迟惩罚
                        else:
                            try:
                                loss_f = float(p_val)
                                lat_f = float(l_val)
                                if loss_f >= 100.0 or lat_f <= 0:
                                    disconnected += 1
                                    losses.append(100.0)
                                    latencies.append(5000.0)
                                else:
                                    losses.append(loss_f)
                                    latencies.append(lat_f)
                            except (ValueError, TypeError):
                                missing += 1
                                disconnected += 1
                                losses.append(100.0)
                                latencies.append(5000.0)
                    
                    # 补充缺失点位直到 expected_points
                    while len(losses) < expected_points:
                        missing += 1
                        disconnected += 1
                        losses.insert(0, 100.0)
                        latencies.insert(0, 5000.0)
                        
                    losses = losses[-expected_points:]
                    latencies = latencies[-expected_points:]
                    
                    ema_loss = ema_weighted_avg(losses, span)
                    ema_lat = ema_weighted_avg(latencies, span)
                    jitter = calculate_stddev(latencies)
                    
                    return {
                        "ema_latency": round(ema_lat, 2),
                        "ema_loss": round(ema_loss, 2),
                        "jitter": round(jitter, 2),
                        "missing": missing,
                        "disconnected": disconnected
                    }

                dx_stats = extract_series('dx')
                yd_stats = extract_series('yd')
                lt_stats = extract_series('lt')
                
                avg_latency = (dx_stats['ema_latency'] + yd_stats['ema_latency'] + lt_stats['ema_latency']) / 3
                avg_loss = (dx_stats['ema_loss'] + yd_stats['ema_loss'] + lt_stats['ema_loss']) / 3
                avg_jitter = (dx_stats['jitter'] + yd_stats['jitter'] + lt_stats['jitter']) / 3

                return {
                    "ip": domain_name,
                    "dxLatencyEma": dx_stats['ema_latency'],
                    "dxLossEma": dx_stats['ema_loss'],
                    "dxJitter": dx_stats['jitter'],
                    "ydLatencyEma": yd_stats['ema_latency'],
                    "ydLossEma": yd_stats['ema_loss'],
                    "ydJitter": yd_stats['jitter'],
                    "ltLatencyEma": lt_stats['ema_latency'],
                    "ltLossEma": lt_stats['ema_loss'],
                    "ltJitter": lt_stats['jitter'],
                    "avgLatency": round(avg_latency, 2),
                    "avgLoss": round(avg_loss, 2),
                    "avgJitter": round(avg_jitter, 2)
                }
        except Exception as e:
            is_rate_limited = False
            if hasattr(e, 'code') and e.code == 429:
                is_rate_limited = True
            
            if is_rate_limited:
                time.sleep(2.0)
            else:
                time.sleep(0.5)
                
            if attempt == max_retries - 1:
                print(f"  ⚠️ 获取域名 {domain_name} 测速历史失败 (已重试 {max_retries} 次): {e}")
    return None

# ==================== 候选池获取 ====================
def get_all_cf_domains(token, max_retries=3):
    url = "https://vps789.com/public/cfMonitorList"
    post_data = json.dumps({
        "criteria": {"remarks": {"contains": "domain"}},
        "page": {"number": 1, "size": 1000, "sort": ["createdTime,desc"]}
    }).encode('utf-8')
    req = urllib.request.Request(
        url, data=post_data,
        headers={'Content-Type': 'application/json', 'User-Agent': 'Mozilla/5.0', 'token': token},
        method='POST'
    )
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status != 200:
                    continue
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data.get("code") != 0:
                    continue
                encrypted_payload = res_data.get("message", "")
                decrypted_str = decrypt_payload(encrypted_payload)
                decrypted_json = json.loads(decrypted_str)
                return decrypted_json.get("content", [])
        except Exception as e:
            if attempt == max_retries - 1:
                print(f"❌ 获取基础域名候选列表失败 (已重试 {max_retries} 次): {e}")
    return None

# ==================== 综合评分与排序 ====================
def calc_score(loss, jitter, latency):
    """新评分公式：丢包极高惩罚 + 抖动权重 + 延迟辅助"""
    return 1000 * loss + 50 * jitter + 0.7 * latency

def sort_domains(domains, mode, max_loss_threshold=10.0):
    # 过滤掉不含新版指标的旧版缓存数据
    valid_domains = [x for x in domains if "dxLossEma" in x]
    if not valid_domains:
        return domains
    
    if mode == 1: # 默认综合线路
        filtered = [x for x in valid_domains if x.get("avgLoss", 100) <= max_loss_threshold]
        target_list = filtered if filtered else valid_domains
        target_list.sort(key=lambda x: calc_score(
            x.get("avgLoss", 100),
            x.get("avgJitter", 0),
            x.get("avgLatency", 9999)
        ))
        return target_list
    elif mode == 2: # 电信
        filtered = [x for x in valid_domains if x.get("dxLossEma", 100) <= max_loss_threshold]
        target_list = filtered if filtered else valid_domains
        target_list.sort(key=lambda x: calc_score(x.get("dxLossEma", 100), x.get("dxJitter", 0), x.get("dxLatencyEma", 9999)))
        return target_list
    elif mode == 3: # 移动
        filtered = [x for x in valid_domains if x.get("ydLossEma", 100) <= max_loss_threshold]
        target_list = filtered if filtered else valid_domains
        target_list.sort(key=lambda x: calc_score(x.get("ydLossEma", 100), x.get("ydJitter", 0), x.get("ydLatencyEma", 9999)))
        return target_list
    elif mode == 4: # 联通
        filtered = [x for x in valid_domains if x.get("ltLossEma", 100) <= max_loss_threshold]
        target_list = filtered if filtered else valid_domains
        target_list.sort(key=lambda x: calc_score(x.get("ltLossEma", 100), x.get("ltJitter", 0), x.get("ltLatencyEma", 9999)))
        return target_list
    return valid_domains  # 兜底：未知 mode 返回原始列表

# ==================== 华为云 DNS 自动同步 ====================
def sync_to_huaweicloud(sub_domain, ct_cname, cm_cname, cu_cname, def_cname):
    if not HUAWEI_SDK_AVAILABLE:
        print(f"\n⚠️ 未检测到华为云 SDK，同步 {sub_domain}.{DOMAIN} 跳过。")
        return
    if not HUAWEICLOUD_AK or not HUAWEICLOUD_SK or not DOMAIN:
        print(f"\n⚠️ 未配置华为云 AK/SK 或域名，同步 {sub_domain}.{DOMAIN} 跳过。")
        return

    print(f"\n[同步] 正在自动同步 {sub_domain}.{DOMAIN} 最优解析到华为云公网 DNS...")
    try:
        credentials = BasicCredentials(HUAWEICLOUD_AK, HUAWEICLOUD_SK)
        client = DnsClient.new_builder() \
            .with_credentials(credentials) \
            .with_region(DnsRegion.value_of(REGION)) \
            .build()
        
        zone_request = ListPublicZonesRequest()
        zone_response = client.list_public_zones(zone_request)
        zone_id = None
        domain_dot = DOMAIN if DOMAIN.endswith(".") else DOMAIN + "."
        for zone in zone_response.zones:
            if zone.name == domain_dot:
                zone_id = zone.id
                break
        if not zone_id:
            print(f"❌ 同步失败：未找到主域名 {DOMAIN} 的解析 Zone。")
            return

        full_name = domain_dot if sub_domain == "@" else f"{sub_domain}.{domain_dot}"
        record_request = ListRecordSetsWithLineRequest()
        record_request.zone_id = zone_id
        record_request.name = full_name
        record_request.type = "CNAME"
        record_response = client.list_record_sets_with_line(record_request)
        
        existing_records = {r.line: r for r in record_response.recordsets}

        target_lines = {
            "Dianxin": ("中国电信 线路", ct_cname),
            "Yidong": ("中国移动 线路", cm_cname),
            "Liantong": ("中国联通 线路", cu_cname),
            "default_view": ("默认保底 线路", def_cname)
        }

        for line_code, (line_name, target_val) in target_lines.items():
            if not target_val: continue
            new_val = target_val.strip().rstrip(".")
            if line_code in existing_records:
                record_item = existing_records[line_code]
                old_val = record_item.records[0].strip().rstrip(".") if record_item.records else ""
                
                if old_val == new_val:
                    print(f"  👉 {line_name}: 解析已是最新 [{old_val}]，无需修改。")
                else:
                    print(f"  🔄 {line_name}: 变更 [{old_val}] ➡️ [{new_val}]，正在更新...")
                    update_req = UpdateRecordSetRequest()
                    update_req.zone_id = zone_id
                    update_req.recordset_id = record_item.id
                    update_req.body = UpdateRecordSetReq(
                        name=full_name, type="CNAME", ttl=300, records=[new_val]
                    )
                    client.update_record_set(update_req)
                    print(f"  ✅ {line_name} 修改成功！")
            else:
                print(f"  ➕ {line_name}: 创建解析指向 [{new_val}]...")
                create_req = CreateRecordSetWithLineRequest()
                create_req.zone_id = zone_id
                create_req.body = CreateRecordSetWithLineRequestBody(
                    type="CNAME", name=full_name, ttl=300, weight=1, records=[new_val], line=line_code
                )
                client.create_record_set_with_line(create_req)
                print(f"  ✅ {line_name} 创建成功！")
    except Exception as e:
        print(f"❌ 华为云 API 同步出错: {e}")

# ==================== 熔断机制 ====================
def run_meltdown_with_fallback(state_manager):
    champions_dict = state_manager.state.setdefault("champions", {})
    if not champions_dict: return

    champs_to_check = []
    champs_map = []
    for sub_domain, champs in champions_dict.items():
        if sub_domain not in SUB_DOMAINS_CONFIG:
            continue
        for line_code, cname in champs.items():
            if cname and cname != "N/A":
                champs_to_check.append(cname)
                champs_map.append((sub_domain, line_code, cname))

    if not champs_to_check: return

    print("\n[实时熔断体检] 正在校验当前冠军 CNAME 活性...")
    health_results = bulk_dns_check(champs_to_check)
    meltdown_any = False

    for sub_domain, line_code, cname in champs_map:
        is_healthy = health_results.get(cname, False)
        if not is_healthy:
            log_event(state_manager, f"🚨 熔断警报：{sub_domain} {line_code} 冠军 [{cname}] 失去活性！")
            state_manager.update_reputation(sub_domain, line_code, cname, -30) # 熔断惩罚
            
            backup_cname = None
            pools = [
                ("Top5", get_pool_list(state_manager.state, "top5_pool", sub_domain, line_code)),
                ("Top20", get_pool_list(state_manager.state, "top20_pool", sub_domain, line_code)),
                ("Top100", get_pool_list(state_manager.state, "top100_pool", sub_domain, line_code))
            ]
            
            for pool_name, pool_list in pools:
                cands = [item["ip"] for item in pool_list]
                if not cands: continue
                cands_health = bulk_dns_check(cands)
                for cand_cname in cands:
                    if cands_health.get(cand_cname, False):
                        backup_cname = cand_cname
                        log_event(state_manager, f"🔥 强行熔断：从 {pool_name} 检索到首个健康备选 [{cand_cname}]")
                        break
                if backup_cname: break
            
            if not backup_cname:
                t5 = get_pool_list(state_manager.state, "top5_pool", sub_domain, line_code)
                backup_cname = t5[0]["ip"] if t5 else cname

            if backup_cname != cname:
                log_event(state_manager, f"🔄 熔断降级：{sub_domain} {line_code} [{cname}] ➡️ [{backup_cname}]")
                champions_dict[sub_domain][line_code] = backup_cname
                meltdown_any = True
                
                l_counts = state_manager.state["consecutive_lead_counts"].setdefault(sub_domain, {})
                l_counts[line_code] = {"cname": backup_cname, "count": 0}

                sync_to_huaweicloud(
                    sub_domain,
                    champions_dict[sub_domain].get("Dianxin"),
                    champions_dict[sub_domain].get("Yidong"),
                    champions_dict[sub_domain].get("Liantong"),
                    champions_dict[sub_domain].get("default_view")
                )
                state_manager.state["last_switch_time"][sub_domain] = time.time()
            
    if meltdown_any:
        state_manager.save()

# ==================== Top5 决策与防抖信誉 ====================
def run_top5_and_decision(state_manager):
    print("\n[Top5 热池体检 & 防抖选拔中...]")
    state_manager.state["top5_pool"] = state_manager.state.setdefault("top5_pool", {})
    
    dns_needed_subdomains = set()
    now = time.time()

    for sub_domain, monitor_type in SUB_DOMAINS_CONFIG.items():
        top20_data = state_manager.state.setdefault("top20_pool", {}).get(sub_domain, {})
        top100_data = state_manager.state.setdefault("top100_pool", {}).get(sub_domain, {})
        
        lines_keys = ["Dianxin", "Yidong", "Liantong", "default_view"]
        
        line_t5_ips = {}
        all_unique_ips = set()
        for line_code in lines_keys:
            t20_line = top20_data if isinstance(top20_data, list) else top20_data.get(line_code, [])
            if not t20_line:
                t20_line = top100_data if isinstance(top100_data, list) else top100_data.get(line_code, [])
            
            t5_ips = [item["ip"] for item in t20_line[:5]]
            if t5_ips:
                line_t5_ips[line_code] = t5_ips
                all_unique_ips.update(t5_ips)
                
        if not all_unique_ips: continue
        
        health_res = bulk_dns_check(list(all_unique_ips))
        
        if isinstance(state_manager.state["top5_pool"].get(sub_domain), list):
            state_manager.state["top5_pool"][sub_domain] = {}
        else:
            state_manager.state["top5_pool"].setdefault(sub_domain, {})
        line_new_t5 = {}
        for line_code, t5_ips in line_t5_ips.items():
            new_t5 = [{"ip": ip, "healthy": health_res.get(ip, False)} for ip in t5_ips]
            line_new_t5[line_code] = new_t5
            
            old_pool_raw = state_manager.state["top5_pool"][sub_domain]
            old_pool = old_pool_raw.get(line_code, []) if isinstance(old_pool_raw, dict) else []
            old_t5_ips = [item["ip"] for item in old_pool]
            
            state_manager.state["top5_pool"][sub_domain][line_code] = new_t5
            
            for old_ip in old_t5_ips:
                if old_ip not in t5_ips:
                    state_manager.update_reputation(sub_domain, line_code, old_ip, -5)

        champs = state_manager.state["champions"].setdefault(sub_domain, {})
        lead_counts = state_manager.state["consecutive_lead_counts"].setdefault(sub_domain, {})
        last_switch = state_manager.state["last_switch_time"].setdefault(sub_domain, 0.0)
        candidates = state_manager.state.get("candidates", [])
        key_suffix = "30day" if monitor_type == 1 else "24h"

        lines_modes = {
            "Dianxin": (2, "电信"),
            "Yidong": (3, "移动"),
            "Liantong": (4, "联通"),
            "default_view": (1, "默认")
        }

        for line_code, (mode, line_name) in lines_modes.items():
            new_t5 = line_new_t5.get(line_code, [])
            if not new_t5: continue
            
            healthy_candidates_stats = []
            for t5_item in new_t5:
                if t5_item["healthy"]:
                    for c in candidates:
                        if c["ip"] == t5_item["ip"]:
                            if stat := c.get(f"data_{key_suffix}"):
                                healthy_candidates_stats.append(stat)
                            break
            
            if not healthy_candidates_stats:
                print(f"  ⚠️ {sub_domain} {line_name} Top5 无健康备选。")
                continue

            best_cname = sort_domains(list(healthy_candidates_stats), mode)[0]['ip']
            current_champ = champs.get(line_code, "")
            
            if current_champ in [x["ip"] for x in new_t5 if x["healthy"]]:
                state_manager.update_reputation(sub_domain, line_code, current_champ, 1)

            if not current_champ or current_champ == "N/A":
                champs[line_code] = best_cname
                lead_counts[line_code] = {"cname": best_cname, "count": 0}
                dns_needed_subdomains.add(sub_domain)
                log_event(state_manager, f"🆕 {sub_domain} {line_name}: 初始化现任冠军 [{best_cname}]")
                continue

            if current_champ == best_cname:
                lead_counts[line_code] = {"cname": best_cname, "count": 0}
                continue

            l_info = lead_counts.setdefault(line_code, {"cname": best_cname, "count": 0})
            if l_info.get("cname") == best_cname:
                l_info["count"] += 1
            else:
                l_info["cname"] = best_cname
                l_info["count"] = 1

            current_rep = state_manager.state["reputation_scores"].get(sub_domain, {}).get(line_code, {}).get(current_champ, 50)
            best_rep = state_manager.state["reputation_scores"].get(sub_domain, {}).get(line_code, {}).get(best_cname, 50)
            cooldown_elapsed = now - last_switch

            print(f"  🕒 {sub_domain} {line_name}: 挑战者 [{best_cname}](信誉{best_rep}) 领先 现任 [{current_champ}](信誉{current_rep}) 次数: {l_info['count']}/3")

            allow_switch = False
            if l_info["count"] >= 3 and cooldown_elapsed >= 1800:
                if best_rep >= 60 or current_rep < 30:
                    allow_switch = True
                else:
                    print(f"  🛡️ {sub_domain} {line_name}: 挑战者信誉不足({best_rep}<60) 且现任健康({current_rep}>=30)，拒绝替换。")

            if allow_switch:
                log_event(state_manager, f"🔥 防抖达成！{sub_domain} {line_name} 切换为 [{best_cname}]")
                champs[line_code] = best_cname
                l_info["count"] = 0
                state_manager.state["last_switch_time"][sub_domain] = now
                dns_needed_subdomains.add(sub_domain)

    for sub in dns_needed_subdomains:
        sync_to_huaweicloud(
            sub,
            state_manager.state["champions"][sub].get("Dianxin"),
            state_manager.state["champions"][sub].get("Yidong"),
            state_manager.state["champions"][sub].get("Liantong"),
            state_manager.state["champions"][sub].get("default_view")
        )
    
    state_manager.state["last_top5_time"] = time.time()
    state_manager.save()

# ==================== Top20 & Top100 漏斗 ====================
def run_top20_check(state_manager):
    print("\n[Top20 热池体检中...]")
    for sub_domain in SUB_DOMAINS_CONFIG.keys():
        top100_data = state_manager.state.setdefault("top100_pool", {}).get(sub_domain, {})
        if not top100_data: continue

        state_manager.state.setdefault("top20_pool", {})
        if isinstance(state_manager.state["top20_pool"].get(sub_domain), list):
            state_manager.state["top20_pool"][sub_domain] = {}
        else:
            state_manager.state["top20_pool"].setdefault(sub_domain, {})

        lines_keys = ["Dianxin", "Yidong", "Liantong", "default_view"]
        
        line_t20_ips = {}
        all_unique_ips = set()
        
        for line_code in lines_keys:
            t100_line = top100_data if isinstance(top100_data, list) else top100_data.get(line_code, [])
            if not t100_line: continue

            t20_ips = [item["ip"] for item in t100_line if item.get("healthy", True)][:20]
            if len(t20_ips) < 10:
                t20_ips = [item["ip"] for item in t100_line][:20]
            
            line_t20_ips[line_code] = t20_ips
            all_unique_ips.update(t20_ips)

            old_pool_data = state_manager.state["top20_pool"][sub_domain]
            old_t20_ips = [item["ip"] for item in (old_pool_data if isinstance(old_pool_data, list) else old_pool_data.get(line_code, []))]
            
            for old_ip in old_t20_ips:
                if old_ip not in t20_ips:
                    state_manager.update_reputation(sub_domain, line_code, old_ip, -15)

        if not all_unique_ips: continue
        health_res = bulk_dns_check(list(all_unique_ips))
        
        state_manager.state.setdefault("top20_pool", {}).setdefault(sub_domain, {})
        for line_code, ips in line_t20_ips.items():
            state_manager.state["top20_pool"][sub_domain][line_code] = [
                {"ip": ip, "healthy": health_res.get(ip, False)} for ip in ips
            ]
        
    state_manager.state["last_top20_time"] = time.time()
    state_manager.save()

def run_top100_check(state_manager):
    print("\n[Top100 热池体检中...]")
    candidates = state_manager.state.get("candidates", [])
    if not candidates: return

    black_list = {f"{sub}.{DOMAIN}" if sub != "@" else DOMAIN for sub in SUB_DOMAINS_CONFIG.keys()}
    candidates_clean = [item for item in candidates if item.get("ip") not in black_list]

    for sub_domain, monitor_type in SUB_DOMAINS_CONFIG.items():
        key_suffix = "30day" if monitor_type == 1 else "24h"
        
        domains_for_sort = [c.get(f"data_{key_suffix}") for c in candidates_clean if c.get(f"data_{key_suffix}")]
        if not domains_for_sort: continue

        lines_modes = {
            "Dianxin": 2,
            "Yidong": 3,
            "Liantong": 4,
            "default_view": 1
        }
        
        line_t100_ips = {}
        all_unique_ips = set()
        for line_code, mode in lines_modes.items():
            sorted_def = sort_domains(list(domains_for_sort), mode)
            ips = [item["ip"] for item in sorted_def[:100]]
            line_t100_ips[line_code] = ips
            all_unique_ips.update(ips)

        health_res = bulk_dns_check(list(all_unique_ips))
        
        state_manager.state.setdefault("top100_pool", {})
        if isinstance(state_manager.state["top100_pool"].get(sub_domain), list):
            state_manager.state["top100_pool"][sub_domain] = {}
        else:
            state_manager.state["top100_pool"].setdefault(sub_domain, {})
        for line_code, ips in line_t100_ips.items():
            state_manager.state["top100_pool"][sub_domain][line_code] = [
                {"ip": ip, "healthy": health_res.get(ip, False)} for ip in ips
            ]
        
    state_manager.state["last_top100_time"] = time.time()
    state_manager.save()

# ==================== 主流程 ====================
def main():
    print("=========================================================================")
    print(" 🛰️  VPS789 智能优选 DNS 同步 (EMA加权版)")
    print("=========================================================================")
    
    ts = str(int(time.time() * 1000))
    token = encrypt_token(ts)
    
    state_manager = StateManager()
    run_meltdown_with_fallback(state_manager)
    
    now = time.time()
    force_cascade = False
    
    # 1. API 大池数据 6h 刷新
    if not state_manager.state.get("candidates") or (now - state_manager.state.get("last_api_update_time", 0.0) >= 21600):
        log_event(state_manager, "🔄 开始刷新大池测速数据...")
        raw_domains = get_all_cf_domains(token)
        if raw_domains:
            black_list = {f"{sub}.{DOMAIN}" if sub != "@" else DOMAIN for sub in SUB_DOMAINS_CONFIG.keys()}
            raw_domains = [item for item in raw_domains if item.get("ip") not in black_list]
            
            # 保留历史缓存数据：将原有的 candidates 转换为 map 快速检索
            old_candidates_map = {c["ip"]: c for c in state_manager.state.get("candidates", []) if isinstance(c, dict) and "ip" in c}
            
            new_candidates = []
            for item in raw_domains:
                ip = item.get("ip")
                old_c = old_candidates_map.get(ip, {})
                new_candidates.append({
                    "id": item.get("id"),
                    "ip": ip,
                    "data_30day": old_c.get("data_30day"),
                    "data_24h": old_c.get("data_24h")
                })
            
            for m_type in set(SUB_DOMAINS_CONFIG.values()):
                suffix = "30day" if m_type == 1 else "24h"
                # 降低并发线程至 5，极大地减少频繁触发 WAF 并发墙拦截概率
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(fetch_and_calc_stats, item, token, m_type, 2): item for item in new_candidates}
                    for future in as_completed(futures):
                        try:
                            if res := future.result():
                                next((c for c in new_candidates if c["ip"] == res["ip"]), {})[f"data_{suffix}"] = res
                        except Exception as e:
                            print(f"  ⚠️ 处理域名测速数据异常: {e}")
                            
            state_manager.state["candidates"] = new_candidates
            state_manager.state["last_api_update_time"] = time.time()
            log_event(state_manager, "✅ candidates 测速缓存已刷新。")
            force_cascade = True

    # 2. 漏斗刷新周期调整 (Top100: 6h, Top20: 1h, Top5: 10m)
    if force_cascade or not state_manager.state.get("top100_pool") or (now - state_manager.state.get("last_top100_time", 0.0) >= 21600):
        run_top100_check(state_manager)
        
    if force_cascade or not state_manager.state.get("top20_pool") or (now - state_manager.state.get("last_top20_time", 0.0) >= 3600):
        run_top20_check(state_manager)

    if force_cascade or not state_manager.state.get("top5_pool") or (now - state_manager.state.get("last_top5_time", 0.0) >= 600):
        run_top5_and_decision(state_manager)
    
    # 强制校验并同步最新 champions 到华为云公网 DNS，确保云端状态和华为云 100% 一致
    for sub, champs in state_manager.state.get("champions", {}).items():
        if sub in SUB_DOMAINS_CONFIG:
            sync_to_huaweicloud(
                sub,
                champs.get("Dianxin"),
                champs.get("Yidong"),
                champs.get("Liantong"),
                champs.get("default_view")
            )
    
    # 强制每次运行结束都重新生成一次最新的网页和快照，以确保 Actions 每次运行时即使没有状态更新，也能生成 status.html
    generate_visual_html(state_manager)
    print("✓ 本周期级联漏斗体检与决策完成。")

if __name__ == '__main__':
    port_str = os.environ.get("PORT")
    if port_str:
        try:
            port = int(port_str)
        except ValueError:
            port = 8080
            
        def server_thread():
            import http.server
            import socketserver
            
            class RedirectHandler(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    if self.path in ('/', '/status.html'):
                        state_dir = os.environ.get("STATE_DIR", "")
                        filepath = os.path.join(state_dir, "status.html") if state_dir else "status.html"
                        if os.path.exists(filepath):
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/html; charset=utf-8')
                            self.end_headers()
                            with open(filepath, 'rb') as f:
                                self.wfile.write(f.read())
                        else:
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/plain; charset=utf-8')
                            self.end_headers()
                            self.wfile.write("Status page not found yet. Running initial sync, please wait...".encode('utf-8'))
                    else:
                        self.send_response(404)
                        self.end_headers()
            
            class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
                pass
                
            print(f"📡 启动容器 Web 网页服务，端口: {port}...")
            socketserver.TCPServer.allow_reuse_address = True
            with ThreadingTCPServer(("0.0.0.0", port), RedirectHandler) as httpd:
                print(f"✅ Web 网页服务已运行在 http://0.0.0.0:{port}/")
                httpd.serve_forever()
                
        t = threading.Thread(target=server_thread, daemon=True)
        t.start()
        
        print("🔄 开启容器 24h 持续守候模式...")
        while True:
            try:
                main()
            except Exception as e:
                print(f"❌ 运行周期异常: {e}")
            time.sleep(1200)
    else:
        main()
