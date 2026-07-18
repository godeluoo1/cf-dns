import os
import re
import json
import urllib.request

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

HUAWEICLOUD_AK = os.environ.get("HUAWEICLOUD_AK", "")
HUAWEICLOUD_SK = os.environ.get("HUAWEICLOUD_SK", "")

DOMAIN = "blogluo.eu.org"
SUB_DOMAIN = "vip"
REGION = "cn-east-3"

# 规避系统代理
os.environ['no_proxy'] = '*'

# IPv4 正则校验
IPV4_PATTERN = re.compile(r'^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$')

# Cloudflare 官方 IPv4 CIDR 网段，用于彻底防范任何恶意/脏 IP 混入
CLOUDFLARE_IPV4_RANGES = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22"
]

def ip_to_int(ip_str):
    parts = list(map(int, ip_str.split('.')))
    return (parts[0] << 24) + (parts[1] << 16) + (parts[2] << 8) + parts[3]

def parse_cidr(cidr_str):
    ip, mask = cidr_str.split('/')
    mask = int(mask)
    net_mask = (0xFFFFFFFF << (32 - mask)) & 0xFFFFFFFF
    return ip_to_int(ip) & net_mask, net_mask

# 预先编译子网以进行极速比对
COMPILED_RANGES = [parse_cidr(cidr) for cidr in CLOUDFLARE_IPV4_RANGES]

def is_valid_cf_ipv4(ip_str):
    if not IPV4_PATTERN.match(ip_str):
        return False
    try:
        val = ip_to_int(ip_str)
        for net, mask in COMPILED_RANGES:
            if (val & mask) == net:
                return True
    except Exception:
        pass
    return False

def http_get(url, timeout=10):
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return response.read().decode('utf-8')
    except Exception as e:
        print(f"  ❌ GET 请求失败 {url}: {e}")
    return None

def http_post_json(url, payload_dict, timeout=10):
    data = json.dumps(payload_dict).encode('utf-8')
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            'Content-Type': 'application/json',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return response.read().decode('utf-8')
    except Exception as e:
        print(f"  ❌ POST 请求失败 {url}: {e}")
    return None

def fetch_vps789():
    """1. 从 VPS789 公开优选接口抓取 IP"""
    print("🛰️ 正在从 VPS789 优选 API 抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    line_content = http_get("https://vps789.com/openApi/cfIpApi")
    if line_content:
        try:
            data = json.loads(line_content)
            if data.get("code") in (0, 200):
                inner_data = data.get("data", {})
                for ip_obj in inner_data.get("CM", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_cf_ipv4(ip):
                        results["CM"].append(ip)
                for ip_obj in inner_data.get("CU", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_cf_ipv4(ip):
                        results["CU"].append(ip)
                for ip_obj in inner_data.get("CT", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_cf_ipv4(ip):
                        results["CT"].append(ip)
        except Exception as e:
            print(f"  ❌ 解析 VPS789 分线路 JSON 失败: {e}")
            
    print(f"  ✅ VPS789 官方 IP 校验完成: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def fetch_cfyes():
    """2. 从 Hostmonit (CFYes) 优选源抓取 IP"""
    print("🛰️ 正在从 CFYes (Hostmonit) 优选源抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    response = http_post_json("https://api.hostmonit.com/get_optimization_ip", {"key": "iDetkOys"})
    if response:
        try:
            data = json.loads(response)
            for item in data.get("info", []):
                ip = item.get("ip", "").strip()
                line = item.get("line", "").upper()
                if is_valid_cf_ipv4(ip):
                    if line == "CM":
                        results["CM"].append(ip)
                    elif line == "CU":
                        results["CU"].append(ip)
                    elif line == "CT":
                        results["CT"].append(ip)
        except Exception as e:
            print(f"  ❌ 解析 CFYes JSON 失败: {e}")
            
    print(f"  ✅ CFYes 官方 IP 校验完成: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def main():
    print("==================================================")
    print("🚀 开始全网 Cloudflare 优选 IPv4 自动化抓取、过滤与去重 (ip.py)")
    print("==================================================")
    
    cm_all = []
    cu_all = []
    ct_all = []
    
    # 1. 抓取 VPS789
    try:
        res = fetch_vps789()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
    except Exception as e:
        print(f"  ⚠️ 抓取 VPS789 异常: {e}")
        
    # 2. 抓取 CFYes
    try:
        res = fetch_cfyes()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
    except Exception as e:
        print(f"  ⚠️ 抓取 CFYes 异常: {e}")
        
    # 过滤与去重：保持原有的质量先后顺序，拒绝盲目 string 排序导致降速
    def keep_order_unique(ip_list):
        seen = set()
        return [x for x in ip_list if not (x in seen or seen.add(x))]
        
    cm_clean = keep_order_unique([ip for ip in cm_all if is_valid_cf_ipv4(ip)])
    cu_clean = keep_order_unique([ip for ip in cu_all if is_valid_cf_ipv4(ip)])
    ct_clean = keep_order_unique([ip for ip in ct_all if is_valid_cf_ipv4(ip)])
    
    print("\n==================================================")
    print("📊 过滤去重统计 (去重前 -> 去重后)")
    print("==================================================")
    print(f"  中国移动 CMCC: {len(cm_all)} -> {len(cm_clean)} 个合格优选 IP")
    print(f"  中国联通 CUCC: {len(cu_all)} -> {len(cu_clean)} 个合格优选 IP")
    print(f"  中国电信 CTCC: {len(ct_all)} -> {len(ct_clean)} 个合格优选 IP")
    
    # 创建输出目录，把数据写入用于部署的分支目录
    output_dir = "bestcf_out"
    os.makedirs(output_dir, exist_ok=True)
    
    # 写入文件供 text 文件发布
    with open(os.path.join(output_dir, "移动.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(cm_clean))
        
    with open(os.path.join(output_dir, "联通.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(cu_clean))
        
    with open(os.path.join(output_dir, "电信.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(ct_clean))
        
    print(f"\n🎉 干净的 IP 列表已成功写入至 '{output_dir}/' 目录下！")
    
    # 将最新优选 IP 同步推送到华为云 DNS，每条线路同样只同步前 5 个最精粹 IP 避免 50 IP 超限崩溃
    sync_to_huaweicloud(SUB_DOMAIN, ct_clean[:5], cm_clean[:5], cu_clean[:5])

def sync_to_huaweicloud(sub_domain, ct_ips, cm_ips, cu_ips):
    if not HUAWEI_SDK_AVAILABLE:
        print(f"\n⚠️ 未检测到华为云 SDK，同步 {sub_domain}.{DOMAIN} 跳过。")
        return
    if not HUAWEICLOUD_AK or not HUAWEICLOUD_SK or not DOMAIN:
        print(f"\n⚠️ 未配置华为云 AK/SK 或域名，同步 {sub_domain}.{DOMAIN} 跳过。")
        return

    print(f"\n[同步] 正在自动同步 {sub_domain}.{DOMAIN} 到华为云 DNS (多线路并发 A 记录覆盖模式)...")
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
        
        # 查询现有的 A 记录列表
        record_request = ListRecordSetsWithLineRequest()
        record_request.zone_id = zone_id
        record_request.name = full_name
        record_request.type = "A"
        record_response = client.list_record_sets_with_line(record_request)
        
        existing_records = {r.line: r for r in record_response.recordsets}

        # 默认保底线路复用电信的 IP
        target_lines = {
            "Dianxin": ("中国电信 线路", ct_ips),
            "Yidong": ("中国移动 线路", cm_ips),
            "Liantong": ("中国联通 线路", cu_ips),
            "default_view": ("默认保底 线路", ct_ips)
        }

        for line_code, (line_name, target_ips) in target_lines.items():
            if not target_ips:
                print(f"  ⚠️ {line_name}: 没有健康的 IP 可供同步，跳过。")
                continue
            
            new_ips = [ip.strip() for ip in target_ips if ip.strip()]
            
            if line_code in existing_records:
                record_item = existing_records[line_code]
                old_ips = [ip.strip() for ip in record_item.records]
                
                # 比对已有的 IP 列表和新 IP 列表是否内容完全一致 (无视顺序)
                if set(old_ips) == set(new_ips):
                    print(f"  👉 {line_name}: 解析已是最新 {old_ips}，无需修改。")
                else:
                    print(f"  🔄 {line_name}: 变更 {old_ips} ➡️ {new_ips}，正在更新...")
                    update_req = UpdateRecordSetRequest()
                    update_req.zone_id = zone_id
                    update_req.recordset_id = record_item.id
                    update_req.body = UpdateRecordSetReq(
                        name=full_name, type="A", ttl=300, records=new_ips
                    )
                    client.update_record_set(update_req)
                    print(f"  ✅ {line_name} 批量 A 记录修改成功！")
            else:
                print(f"  ➕ {line_name}: 创建解析指向多 IP 列表 {new_ips}...")
                create_req = CreateRecordSetWithLineRequest()
                create_req.zone_id = zone_id
                create_req.body = CreateRecordSetWithLineRequestBody(
                    type="A", name=full_name, ttl=300, weight=1, records=new_ips, line=line_code
                )
                client.create_record_set_with_line(create_req)
                print(f"  ✅ {line_name} 批量 A 记录创建成功！")
            
    except Exception as e:
        print(f"❌ 华为云 API 同步出错: {e}")

if __name__ == '__main__':
    main()
