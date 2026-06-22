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

# 🛡️ 全局并发错峰锁和请求时间戳，确保多线程调用 API 时每个请求发起之间至少间隔 80ms
_req_lock = threading.Lock()
_last_req_time = 0.0

def _rate_limit_sleep():
    global _last_req_time
    with _req_lock:
        now = time.time()
        elapsed = now - _last_req_time
        if elapsed < 0.08:
            time.sleep(0.08 - elapsed)
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
                <div class="line-row {color_cls}">
                    <div class="line-info">
                        <span class="line-icon">{icon}</span>
                        <div>
                            <div class="line-name">{line_name}</div>
                            <div class="line-cname" title="{cname}">{cname}</div>
                        </div>
                    </div>
                    <div class="line-metrics">
                        <div class="metric-item">
                            <span class="metric-label">EMA延迟</span>
                            <span class="metric-val">{latency_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">抖动</span>
                            <span class="metric-val">{jitter_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">EMA丢包</span>
                            <span class="metric-val" style="color: {loss_color}">{loss_str}</span>
                        </div>
                        <div class="metric-item">
                            <span class="metric-label">信誉分</span>
                            <span class="metric-val" style="color: {rep_color}">{reputation}</span>
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
    <meta http-equiv="refresh" content="60">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CF 智能优选 CNAME 状态面板</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {{
            --bg-color: #080C14;
            --card-bg: rgba(18, 26, 44, 0.7);
            --card-border: rgba(255, 255, 255, 0.06);
            --text-main: #E2E8F0;
            --text-muted: #64748B;
            --color-primary: #00F2FE;
            --color-success: #10B981;
            --color-warning: #F59E0B;
            --color-danger: #EF4444;
            --loss-ok: #10B981;
            --loss-warn: #F59E0B;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-color);
            background-image: 
                radial-gradient(at 0% 0%, rgba(0, 242, 254, 0.05) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(79, 172, 254, 0.05) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            font-family: 'Outfit', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            min-height: 100vh;
            padding: 2rem 1.5rem;
            line-height: 1.5;
        }}

        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            border-bottom: 1px solid var(--card-border);
            padding-bottom: 1.5rem;
        }}

        .logo-section h1 {{
            font-size: 1.75rem;
            font-weight: 700;
            letter-spacing: -0.025em;
            background: linear-gradient(135deg, #00F2FE 0%, #4FACFE 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }}

        .logo-section p {{
            font-size: 0.875rem;
            color: var(--text-muted);
            margin-top: 0.25rem;
        }}

        .system-meta {{
            text-align: right;
        }}

        .meta-badge {{
            display: inline-flex;
            align-items: center;
            gap: 0.5rem;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
            color: var(--color-success);
            padding: 0.35rem 0.75rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            font-weight: 600;
        }}

        .meta-item {{
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-top: 0.5rem;
            font-family: 'JetBrains Mono', monospace;
        }}

        .grid-layout {{
            display: grid;
            grid-template-columns: 1fr;
            gap: 1.5rem;
            align-items: start;
        }}

        @media (min-width: 900px) {{
            .grid-layout {{
                grid-template-columns: 2fr 1fr;
            }}
        }}

        .card {{
            background: var(--card-bg);
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 1.5rem;
            box-shadow: 0 10px 30px -10px rgba(0, 0, 0, 0.3);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
        }}

        .card-subdomain {{
            margin-bottom: 1.5rem;
        }}

        .card-header-sub {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1.25rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            padding-bottom: 1rem;
        }}

        .subdomain-title {{
            font-size: 1.35rem;
            font-weight: 600;
            color: #FFFFFF;
        }}

        .badge {{
            display: inline-block;
            font-size: 0.75rem;
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            margin-top: 0.25rem;
            font-weight: 500;
        }}

        .badge-dim {{
            background: rgba(255, 255, 255, 0.06);
            color: #94A3B8;
            border: 1px solid rgba(255, 255, 255, 0.08);
        }}

        .pool-status-mini {{
            font-size: 0.8125rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 0.4rem;
        }}

        .indicator {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
        }}

        .blink-green {{
            background-color: var(--color-success);
            box-shadow: 0 0 8px var(--color-success);
            animation: pulse-green 2s infinite;
        }}

        @keyframes pulse-green {{
            0% {{ transform: scale(0.95); opacity: 0.5; }}
            50% {{ transform: scale(1.1); opacity: 1; }}
            100% {{ transform: scale(0.95); opacity: 0.5; }}
        }}

        .lines-container {{
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            margin-bottom: 1.5rem;
        }}

        .line-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.02);
            border-radius: 12px;
            padding: 1rem;
            transition: all 0.2s ease;
        }}

        .line-row:hover {{
            background: rgba(255, 255, 255, 0.04);
            border-color: rgba(255, 255, 255, 0.06);
        }}

        .line-info {{
            display: flex;
            align-items: center;
            gap: 0.85rem;
            min-width: 0;
        }}

        .line-icon {{
            font-size: 1.25rem;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 36px;
            height: 36px;
            background: rgba(255, 255, 255, 0.04);
            border-radius: 10px;
        }}

        .line-name {{
            font-size: 0.875rem;
            font-weight: 600;
            color: #F1F5F9;
        }}

        .line-cname {{
            font-size: 0.8125rem;
            font-family: 'JetBrains Mono', monospace;
            color: var(--text-muted);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            margin-top: 0.15rem;
        }}

        .line-metrics {{
            display: flex;
            gap: 1.5rem;
            flex-shrink: 0;
        }}

        .metric-item {{
            text-align: right;
        }}

        .metric-label {{
            display: block;
            font-size: 0.6875rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }}

        .metric-val {{
            display: block;
            font-size: 0.9375rem;
            font-weight: 700;
            color: var(--color-primary);
            font-family: 'JetBrains Mono', monospace;
            margin-top: 0.15rem;
        }}

        .telecom .line-icon {{ background: rgba(59, 130, 246, 0.1); color: #3B82F6; }}
        .mobile .line-icon {{ background: rgba(16, 185, 129, 0.1); color: #10B981; }}
        .unicom .line-icon {{ background: rgba(245, 158, 11, 0.1); color: #F59E0B; }}
        .default .line-icon {{ background: rgba(139, 92, 246, 0.1); color: #8B5CF6; }}

        .pools-grid {{
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 0.75rem;
            border-top: 1px solid rgba(255, 255, 255, 0.04);
            padding-top: 1.25rem;
        }}

        .pool-box {{
            background: rgba(0, 0, 0, 0.15);
            border-radius: 8px;
            padding: 0.75rem;
        }}

        .pool-box-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.75rem;
            color: var(--text-muted);
            margin-bottom: 0.5rem;
        }}

        .pool-ratio {{
            font-family: 'JetBrains Mono', monospace;
            font-weight: 600;
            color: var(--text-main);
        }}

        .progress-bar-bg {{
            height: 4px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 2px;
            overflow: hidden;
        }}

        .progress-bar-fill {{
            height: 100%;
            background: linear-gradient(90deg, #00F2FE 0%, #4FACFE 100%);
            border-radius: 2px;
            transition: width 0.5s ease;
        }}

        .card-logs-title {{
            font-size: 1.125rem;
            font-weight: 600;
            margin-bottom: 1rem;
            color: #FFFFFF;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .logs-container {{
            max-height: 520px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            padding-right: 0.25rem;
        }}

        .logs-container::-webkit-scrollbar {{
            width: 4px;
        }}

        .logs-container::-webkit-scrollbar-thumb {{
            background: rgba(255, 255, 255, 0.1);
            border-radius: 2px;
        }}

        .log-item {{
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.75rem;
            padding: 0.6rem 0.8rem;
            border-radius: 6px;
            border-left: 3px solid transparent;
            word-break: break-all;
            background: rgba(0, 0, 0, 0.12);
        }}

        .log-item-info {{
            border-left-color: var(--text-muted);
            color: #94A3B8;
        }}

        .log-item-success {{
            border-left-color: var(--color-success);
            color: #A7F3D0;
            background: rgba(16, 185, 129, 0.05);
        }}

        .log-item-warning {{
            border-left-color: var(--color-warning);
            color: #FDE68A;
            background: rgba(245, 158, 11, 0.05);
        }}

        .log-item-danger {{
            border-left-color: var(--color-danger);
            color: #FCA5A5;
            background: rgba(239, 68, 68, 0.05);
            animation: flash-red 2s infinite;
        }}

        @keyframes flash-red {{
            0% {{ background-color: rgba(239, 68, 68, 0.05); }}
            50% {{ background-color: rgba(239, 68, 68, 0.12); }}
            100% {{ background-color: rgba(239, 68, 68, 0.05); }}
        }}

        .no-logs {{
            text-align: center;
            color: var(--text-muted);
            font-size: 0.8125rem;
            padding: 2rem 0;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="logo-section">
                <h1>Cloudflare 智能优选监控</h1>
                <p>三网全自动漏斗体检 & 故障快速熔断系统 (EMA加权版)</p>
            </div>
            <div class="system-meta">
                <span class="meta-badge">
                    <span class="indicator blink-green"></span> 正常守候中
                </span>
                <div class="meta-item">最后检测：{last_update}</div>
                <div class="meta-item">大池API更新：{api_time_str}</div>
            </div>
        </header>

        <div class="grid-layout">
            <div class="main-column">
                {subdomains_html}
            </div>
            <div class="side-column">
                <div class="card">
                    <h2 class="card-logs-title">📋 核心大事件日志</h2>
                    <div class="logs-container">
                        {logs_html}
                    </div>
                </div>
            </div>
        </div>
    </div>
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
    span = 7 if monitor_type == 0 else 10 # EMA span: 24h用7(alpha 0.25)，30d用10(alpha 0.18)
    
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

def sort_domains(domains, mode, max_loss_threshold=3.0):
    # 过滤掉不含新版指标的旧版缓存数据
    valid_domains = [x for x in domains if "dxLossEma" in x]
    if not valid_domains:
        return domains
    
    if mode == 1: # 默认综合线路
        filtered = [x for x in valid_domains if max(x.get("dxLossEma", 100), x.get("ydLossEma", 100), x.get("ltLossEma", 100)) <= max_loss_threshold]
        target_list = filtered if filtered else valid_domains
        target_list.sort(key=lambda x: calc_score(
            max(x.get("dxLossEma", 100), x.get("ydLossEma", 100), x.get("ltLossEma", 100)),
            max(x.get("dxJitter", 0), x.get("ydJitter", 0), x.get("ltJitter", 0)),
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
        
        state_manager.state["top5_pool"].setdefault(sub_domain, {})
        line_new_t5 = {}
        for line_code, t5_ips in line_t5_ips.items():
            new_t5 = [{"ip": ip, "healthy": health_res.get(ip, False)} for ip in t5_ips]
            line_new_t5[line_code] = new_t5
            
            old_pool = state_manager.state["top5_pool"][sub_domain].get(line_code, [])
            old_t5_ips = [item["ip"] for item in (old_pool if isinstance(old_pool, list) else old_pool)]
            
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

            old_pool_data = state_manager.state.setdefault("top20_pool", {}).setdefault(sub_domain, {})
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
        
        state_manager.state.setdefault("top100_pool", {}).setdefault(sub_domain, {})
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
            
            new_candidates = [{"id": item.get("id"), "ip": item.get("ip"), "data_30day": None, "data_24h": None} for item in raw_domains]
            
            for m_type in set(SUB_DOMAINS_CONFIG.values()):
                suffix = "30day" if m_type == 1 else "24h"
                with ThreadPoolExecutor(max_workers=15) as executor:
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
