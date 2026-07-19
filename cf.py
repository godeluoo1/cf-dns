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
import warnings

# 过滤并忽略过时的 TripleDES 警告，保持日志干净
try:
    from cryptography.utils import CryptographyDeprecationWarning
    warnings.filterwarnings("ignore", category=CryptographyDeprecationWarning)
except ImportError:
    pass

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
    "cfs": 1,     # cfs.blogluo.eu.org 针对美国 (只写入默认保底线路)
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

def refresh_cloudflare_ips():
    global CLOUDFLARE_NETWORKS
    url = "https://www.cloudflare.com/ips-v4"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}, method='GET')
    try:
        # 使用 3 秒超时快速尝试获取，防止阻塞主进程
        with urllib.request.urlopen(req, timeout=3) as response:
            if response.status == 200:
                body = response.read().decode('utf-8').strip()
                lines = [line.strip() for line in body.split('\n') if line.strip()]
                if lines:
                    CLOUDFLARE_NETWORKS = [ipaddress.ip_network(net) for net in lines]
                    print(f"📡 成功实时同步 Cloudflare 官方最新 IPv4 段（共 {len(lines)} 个）")
    except Exception as e:
        print(f"⚠️ 实时同步 Cloudflare IP 段失败（将使用内置硬编码段）: {e}")

_cf_ip_cache = {}

def is_ip_cloudflare(ip_str):
    if ip_str in _cf_ip_cache:
        return _cf_ip_cache[ip_str]
    try:
        ip = ipaddress.ip_address(ip_str)
        for net in CLOUDFLARE_NETWORKS:
            if ip in net:
                _cf_ip_cache[ip_str] = True
                return True
    except Exception:
        pass
    _cf_ip_cache[ip_str] = False
    return False

# DNS 存活健康体检函数：使用国内阿里 DNS 223.5.5.5 进行验证
def is_domain_resolvable(domain, max_attempts=3):
    ips = []
    clean_domain = domain.strip().rstrip(".")
    # 覆盖阿里(默认)、移动、联通、电信的公共 DNS 节点，进行多网络联合探活比对，消除跨网盲区
    dns_servers = ['223.5.5.5', '120.196.165.24', '116.116.116.116', '101.226.4.6']
    
    for attempt in range(max_attempts):
        if DNS_AVAILABLE:
            try:
                resolver = dns.resolver.Resolver()
                # 每次尝试使用不同的运营商公共 DNS 探测
                server = dns_servers[attempt % len(dns_servers)]
                resolver.nameservers = [server]
                resolver.timeout = 2.0
                resolver.lifetime = 2.0
                answers = resolver.resolve(clean_domain, 'A')
                ips = [str(rdata) for rdata in answers]
                if ips:
                    break
            except (dns.resolver.NoNameservers, dns.exception.Timeout, dns.resolver.NXDOMAIN):
                # 针对超时或暂无DNS服务的抖动进行温和退避，避免误熔断
                time.sleep(0.5)
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
                    time.sleep(1.0)  # 加上冷却，避免重试闪击
                    continue
                res_data = json.loads(response.read().decode('utf-8'))
                if res_data.get("code") != 0:
                    time.sleep(1.0)  # 加上冷却，避免重试闪击
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
                # 兜底保底逻辑：如果 API 被限流或超时拿不到历史点，直接使用 domain_item 原生包含的周期平均统计数据拼装返回，防止优质域名丢失！
                try:
                    return {
                        "ip": domain_name,
                        "dxLatencyEma": float(domain_item.get("dxLatencyAvg", 9999.0) if domain_item.get("dxLatencyAvg") is not None else 9999.0),
                        "dxLossEma": float(domain_item.get("dxPkgLostRateAvg", 100.0) if domain_item.get("dxPkgLostRateAvg") is not None else 100.0),
                        "dxJitter": 0.0,
                        "ydLatencyEma": float(domain_item.get("ydLatencyAvg", 9999.0) if domain_item.get("ydLatencyAvg") is not None else 9999.0),
                        "ydLossEma": float(domain_item.get("ydPkgLostRateAvg", 100.0) if domain_item.get("ydPkgLostRateAvg") is not None else 100.0),
                        "ydJitter": 0.0,
                        "ltLatencyEma": float(domain_item.get("ltLatencyAvg", 9999.0) if domain_item.get("ltLatencyAvg") is not None else 9999.0),
                        "ltLossEma": float(domain_item.get("ltPkgLostRateAvg", 100.0) if domain_item.get("ltPkgLostRateAvg") is not None else 100.0),
                        "ltJitter": 0.0,
                        "avgLatency": float(domain_item.get("avgLatency", 9999.0) if domain_item.get("avgLatency") is not None else 9999.0),
                        "avgLoss": float(domain_item.get("avgPkgLostRate", 100.0) if domain_item.get("avgPkgLostRate") is not None else 100.0),
                        "avgJitter": 0.0
                    }
                except Exception:
                    pass
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
    """
    智能优选评分公式：
    由于 max_loss_threshold=10.0 已强制截断了高丢包黑洞，
    在此安全前提下，以“极致低延迟体验”为最高导向，降低抖动和微小丢包的过度惩罚，
    确保 60ms 级别的低延迟节点能以压倒性优势击败 180ms 级别的平庸节点。
    """
    return 80.0 * loss + 3.0 * jitter + 1.5 * latency

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

def get_score_for_mode(x, mode):
    if mode == 1:
        return calc_score(x.get("avgLoss", 100), x.get("avgJitter", 0), x.get("avgLatency", 9999))
    elif mode == 2:
        return calc_score(x.get("dxLossEma", 100), x.get("dxJitter", 0), x.get("dxLatencyEma", 9999))
    elif mode == 3:
        return calc_score(x.get("ydLossEma", 100), x.get("ydJitter", 0), x.get("ydLatencyEma", 9999))
    elif mode == 4:
        return calc_score(x.get("ltLossEma", 100), x.get("ltJitter", 0), x.get("ltLatencyEma", 9999))
    return 99999.0

# ==================== 华为云 DNS 自动同步 ====================
def resolve_domain_to_ips(domain, line_type="default", max_attempts=3):
    """
    将 CNAME 域名解析为 CF 优选 IP 列表。
    优先调用国内免费的 HTTPDNS (腾讯 DNS HTTP 接口) 进行高可靠查询，
    通过传入对应国内运营商段的 client_ip 解决海外 Actions 容器解析偏差，
    确保 100% 解析出真正属于中国国内电信/联通/移动视角的极速 Anycast 路由 IP。
    """
    if not domain or domain == "N/A":
        return []
    
    clean_domain = domain.strip().rstrip(".")
    # 如果本身就是 IP，直接校验并返回
    if is_ip_cloudflare(clean_domain):
        return [clean_domain]
    
    ips = []
    
    # 如果是针对美国的解析，跳过国内 HTTPDNS 注入，直接使用本地 DNS 解析
    if line_type != "usa":
        # 针对不同运营商，传入其国内骨干网的典型客户端 IP 视角进行 HTTPDNS 精准查询
        client_ip = "121.33.0.1"  # 默认广东电信
        if line_type == "Dianxin":
            client_ip = "121.33.0.1"   # 广东电信
        elif line_type == "Yidong":
            client_ip = "120.196.0.1"  # 广东移动
        elif line_type == "Liantong":
            client_ip = "120.80.0.1"   # 广东联通
            
        # 1. 优先使用国内 HTTPDNS 接口进行精准分流查询
        url = f"http://119.29.29.29/d?dn={clean_domain}&ip={client_ip}"
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'}, method='GET')
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(req, timeout=3.0) as response:
                    if response.status == 200:
                        body = response.read().decode('utf-8').strip()
                        # 腾讯 HTTPDNS 返回以分号隔开的 IP 列表
                        if body and not body.startswith("err"):
                            parsed_ips = [ip.strip() for ip in body.split(';') if ip.strip()]
                            for ip_str in parsed_ips:
                                if is_ip_cloudflare(ip_str):
                                    ips.append(ip_str)
                            if ips:
                                break
            except Exception:
                time.sleep(0.2)
            
    # 2. 兜底使用 dns.resolver 配合国内公共 DNS
    if not ips and DNS_AVAILABLE:
        dns_servers = ['223.5.5.5', '114.114.114.114']
        for attempt in range(max_attempts):
            try:
                resolver = dns.resolver.Resolver()
                server = dns_servers[attempt % len(dns_servers)]
                resolver.nameservers = [server]
                resolver.timeout = 2.0
                resolver.lifetime = 2.0
                answers = resolver.resolve(clean_domain, 'A')
                for rdata in answers:
                    ip_str = str(rdata).strip()
                    if is_ip_cloudflare(ip_str):
                        ips.append(ip_str)
                if ips:
                    break
            except Exception:
                time.sleep(0.2)
                
    # 3. 兜底使用 socket.getaddrinfo
    if not ips:
        for attempt in range(max_attempts):
            try:
                addr_info = socket.getaddrinfo(clean_domain, None)
                for info in addr_info:
                    if info[0] == socket.AF_INET:
                        ip_str = info[4][0].strip()
                        if is_ip_cloudflare(ip_str):
                            ips.append(ip_str)
                if ips:
                    break
            except Exception:
                time.sleep(0.2)
                
    return sorted(list(set(ips)))

def sync_to_huaweicloud(sub_domain, ct_cname, cm_cname, cu_cname, def_cname):
    if not HUAWEI_SDK_AVAILABLE:
        print(f"\n⚠️ 未检测到华为云 SDK，同步 {sub_domain}.{DOMAIN} 跳过。")
        return
    if not HUAWEICLOUD_AK or not HUAWEICLOUD_SK or not DOMAIN:
        print(f"\n⚠️ 未配置华为云 AK/SK 或域名，同步 {sub_domain}.{DOMAIN} 跳过。")
        return

    print(f"\n[同步] 正在自动同步 {sub_domain}.{DOMAIN} 智能分流 CNAME 到华为云公网 DNS...")
    
    # 针对 cfs (美国特化)，由于是用于美区优化，若将其电信/移动/联通接入亚太优选会因跨太平洋回源导致延迟飙升。
    # 故强制将其三网线路的目标值均重置为美西默认冠军（def_cname），保证全网连 cfs 均直达美西，不绕道亚洲机房。
    if sub_domain == "cfs":
        ct_cname = def_cname
        cm_cname = def_cname
        cu_cname = def_cname

    target_lines = {
        "Dianxin": ("中国电信 线路", ct_cname),
        "Yidong": ("中国移动 线路", cm_cname),
        "Liantong": ("中国联通 线路", cu_cname),
        "default_view": ("默认保底 线路", def_cname)
    }

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
         
        # 1. 查询现有的 A 记录列表 (仅作列表分析，不提前全局删除)
        record_request_a = ListRecordSetsWithLineRequest()
        record_request_a.zone_id = zone_id
        record_request_a.name = full_name
        record_request_a.type = "A"
        record_response_a = client.list_record_sets_with_line(record_request_a)
        existing_a_records = {r.line.lower(): r for r in record_response_a.recordsets if r.line}

        # 2. 查询现有的 CNAME 记录列表
        record_request_cname = ListRecordSetsWithLineRequest()
        record_request_cname.zone_id = zone_id
        record_request_cname.name = full_name
        record_request_cname.type = "CNAME"
        record_response_cname = client.list_record_sets_with_line(record_request_cname)
        existing_cname_records = {r.line.lower(): r for r in record_response_cname.recordsets if r.line}
        
        for line_code, (line_name, target_cname) in target_lines.items():
            try:
                if not target_cname or target_cname == "N/A":
                    print(f"  ⚠️ {line_name}: 冠军 CNAME 为空，跳过更新。")
                    continue
                
                clean_cname = target_cname.strip()
                new_value = [clean_cname]
                line_lower = line_code.lower()
                
                # 2.1 如果存在同线路的 CNAME 记录，直接对比更新
                if line_lower in existing_cname_records:
                    cname_item = existing_cname_records[line_lower]
                    old_value = [v.strip() for v in cname_item.records]
                    if old_value == new_value:
                        print(f"  👉 {line_name}: 解析已是最新 CNAME {old_value}，无需修改。")
                    else:
                        update_req = UpdateRecordSetRequest()
                        update_req.zone_id = zone_id
                        update_req.recordset_id = cname_item.id
                        update_req.body = UpdateRecordSetReq(
                            name=full_name, type="CNAME", ttl=300, records=new_value
                        )
                        client.update_record_set(update_req)
                        print(f"  ✅ {line_name} CNAME 记录更新成功！➔ {clean_cname}")
                        time.sleep(0.35)
                
                # 2.2 如果不存在 CNAME 记录，但存在同线路的 A 记录，尝试原地升级或删除重建
                elif line_lower in existing_a_records:
                    a_item = existing_a_records[line_lower]
                    print(f"  🔄 {line_name}: 发现存量 A 记录 [{a_item.records}]，尝试原地升级为 CNAME...")
                    try:
                        update_req = UpdateRecordSetRequest()
                        update_req.zone_id = zone_id
                        update_req.recordset_id = a_item.id
                        update_req.body = UpdateRecordSetReq(
                            name=full_name, type="CNAME", ttl=300, records=new_value
                        )
                        client.update_record_set(update_req)
                        print(f"  ✅ {line_name} A ➔ CNAME 原地类型升级成功！")
                        time.sleep(0.35)
                    except Exception as up_err:
                        print(f"  ⚠️ 原地升级类型失败 ({up_err})，启用删除重建兜底方案...")
                        # 兜底第一步：删除 A 记录
                        try:
                            del_req = DeleteRecordSetRequest()
                            del_req.zone_id = zone_id
                            del_req.recordset_id = a_item.id
                            client.delete_record_set(del_req)
                            print(f"  ⏳ 已发送 A 记录删除指令，等待 3.0 秒...")
                            time.sleep(3.0)
                        except Exception as del_err:
                            if "DNS.0313" not in str(del_err):
                                print(f"  ⚠️ 兜底删除 A 记录 [{a_item.id}] 失败: {del_err}")
                        
                        # 兜底第二步：创建 CNAME
                        create_req = CreateRecordSetWithLineRequest()
                        create_req.zone_id = zone_id
                        create_req.body = CreateRecordSetWithLineRequestBody(
                            type="CNAME", name=full_name, ttl=300, weight=1, records=new_value, line=line_code
                        )
                        client.create_record_set_with_line(create_req)
                        print(f"  ✅ {line_name} CNAME 记录重建成功！")
                        time.sleep(0.35)
                
                # 2.3 既没有 CNAME 也没有 A 记录，直接全新创建
                else:
                    print(f"  ➕ {line_name}: 创建全新智能 CNAME 解析指向 {clean_cname}...")
                    create_req = CreateRecordSetWithLineRequest()
                    create_req.zone_id = zone_id
                    create_req.body = CreateRecordSetWithLineRequestBody(
                        type="CNAME", name=full_name, ttl=300, weight=1, records=new_value, line=line_code
                    )
                    client.create_record_set_with_line(create_req)
                    print(f"  ✅ {line_name} 智能 CNAME 记录创建成功！")
                    time.sleep(0.35)
            except Exception as line_err:
                print(f"  ❌ 同步 {line_name} 时出错: {line_err}，跳过该线路继续处理其它线路。")
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
        key_suffix = "30day"

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
            print(f"  ⚔️ {sub_domain} {line_name} 决选: 当前 Top 1 挑战者 [{best_cname}]，现任冠军 [{current_champ or '无'}]")
            
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
            
            # 引入现任真实网络质量体检：如果测速显示延迟/丢包极差，即使 DNS 可达，也不再享受防抖保护
            is_current_poor = False
            current_stat = None
            for c in candidates:
                if c["ip"] == current_champ:
                    current_stat = c.get(f"data_{key_suffix}")
                    break
            
            if current_stat:
                curr_lat, curr_loss = 0.0, 0.0
                if mode == 1:
                    curr_lat = current_stat.get("avgLatency", 0.0)
                    curr_loss = current_stat.get("avgLoss", 0.0)
                elif mode == 2:
                    curr_lat = current_stat.get("dxLatencyEma", 0.0)
                    curr_loss = current_stat.get("dxLossEma", 0.0)
                elif mode == 3:
                    curr_lat = current_stat.get("ydLatencyEma", 0.0)
                    curr_loss = current_stat.get("ydLossEma", 0.0)
                elif mode == 4:
                    curr_lat = current_stat.get("ltLatencyEma", 0.0)
                    curr_loss = current_stat.get("ltLossEma", 0.0)
                
                # 若延迟 > 250ms 或 丢包率 > 8.0%，判定为差节点
                if curr_lat > 250.0 or curr_loss > 8.0:
                    is_current_poor = True
                    print(f"  ⚠️ {sub_domain} {line_name}: 现任 [{current_champ}] 测速表现差(延迟 {curr_lat}ms, 丢包 {curr_loss}%)，临时降低其信誉防抖保护门槛。")
            
            if is_current_poor:
                current_rep = min(current_rep, 20)
            best_rep = state_manager.state["reputation_scores"].get(sub_domain, {}).get(line_code, {}).get(best_cname, 50)
            cooldown_elapsed = now - last_switch

            print(f"  🕒 {sub_domain} {line_name}: 挑战者 [{best_cname}](信誉{best_rep}) 领先 现任 [{current_champ}](信誉{current_rep}) 次数: {l_info['count']}/3")

            allow_switch = False
            if l_info["count"] >= 3 and cooldown_elapsed >= 1800:
                # 优化防抖逻辑：只要挑战者比现任信誉度高，或者现任信誉度偏低，连续领先3次均允许直接替换
                if best_rep > current_rep or current_rep < 40 or best_rep >= 60:
                    allow_switch = True
                else:
                    print(f"  🛡️ {sub_domain} {line_name}: 挑战者信誉不足({best_rep}<={current_rep}) 且现任尚可({current_rep}>=40)，拒绝替换。")

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
            healthy_top3 = [item["ip"] for item in state_manager.state["top20_pool"][sub_domain][line_code] if item["healthy"]][:3]
            print(f"  🛡️ {sub_domain} {line_code} 活性健康前三: {', '.join(healthy_top3)}")
        
    state_manager.state["last_top20_time"] = time.time()
    state_manager.save()

def run_top100_check(state_manager):
    print("\n[Top100 热池体检中...]")
    candidates = state_manager.state.get("candidates", [])
    if not candidates: return

    black_list = {f"{sub}.{DOMAIN}" if sub != "@" else DOMAIN for sub in SUB_DOMAINS_CONFIG.keys()}
    candidates_clean = [item for item in candidates if item.get("ip") not in black_list]

    for sub_domain, monitor_type in SUB_DOMAINS_CONFIG.items():
        key_suffix = "30day"
        
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
            
            top3_msgs = []
            for item in sorted_def[:3]:
                score = get_score_for_mode(item, mode)
                if mode == 1:
                    lat, loss = item.get("avgLatency", 9999), item.get("avgLoss", 100)
                elif mode == 2:
                    lat, loss = item.get("dxLatencyEma", 9999), item.get("dxLossEma", 100)
                elif mode == 3:
                    lat, loss = item.get("ydLatencyEma", 9999), item.get("ydLossEma", 100)
                elif mode == 4:
                    lat, loss = item.get("ltLatencyEma", 9999), item.get("ltLossEma", 100)
                top3_msgs.append(f"[{item['ip']}](延迟{lat:.1f}ms,丢包{loss:.1f}%,得分{score:.1f})")
            print(f"  📊 {sub_domain} {line_code} 大池排名前三: {', '.join(top3_msgs)}")

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
    
    refresh_cloudflare_ips()
    
    ts = str(int(time.time() * 1000))
    token = encrypt_token(ts)
    
    state_manager = StateManager()
    run_meltdown_with_fallback(state_manager)
    
    now = time.time()
    force_cascade = not os.environ.get("PORT")
    
    # 1. API 大池数据 6h 刷新 (只拉取 30天 优选指标，彻底移除 24h 抓取)
    if force_cascade or not state_manager.state.get("candidates") or (now - state_manager.state.get("last_api_update_time", 0.0) >= 21600):
        log_event(state_manager, "🔄 开始刷新大池测速数据 (30天历史模式)...")
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
                    "data_30day": old_c.get("data_30day")
                })
            
            m_type = 1 # 强行锁定 30 天优选维度类型
            # 降低并发线程至 5，极大地减少频繁触发 WAF 并发墙拦截概率
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(fetch_and_calc_stats, item, token, m_type, 2): item for item in new_candidates}
                for future in as_completed(futures):
                    try:
                        if res := future.result():
                            next((c for c in new_candidates if c["ip"] == res["ip"]), {})["data_30day"] = res
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
                    if self.path == '/':
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/plain; charset=utf-8')
                        self.end_headers()
                        self.wfile.write("Cloudflare DNS Sync Service is running OK.".encode('utf-8'))
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
