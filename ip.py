import urllib.request
import json
import re
import os

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

# 规避系统代理：强制绕过代理，直接请求以避免测速受代理影响或触发 API 拦截
os.environ['no_proxy'] = '*'

# IPv4 正则校验
IPV4_PATTERN = re.compile(r'^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$')

def is_valid_ipv4(ip):
    return bool(IPV4_PATTERN.match(ip))

def http_get(url, timeout=10):
    """标准的 HTTP GET 请求，添加了 User-Agent"""
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
    """发送 JSON POST 请求"""
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
    """1. 从 VPS789 优选 API 抓取 IP"""
    print("🛰️ 正在从 VPS789 优选源抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    # 分线路 IP
    line_content = http_get("https://vps789.com/openApi/cfIpApi")
    if line_content:
        try:
            data = json.loads(line_content)
            if data.get("code") in (0, 200):
                inner_data = data.get("data", {})
                for ip_obj in inner_data.get("CM", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_ipv4(ip):
                        results["CM"].append(ip)
                for ip_obj in inner_data.get("CU", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_ipv4(ip):
                        results["CU"].append(ip)
                for ip_obj in inner_data.get("CT", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_ipv4(ip):
                        results["CT"].append(ip)
        except Exception as e:
            print(f"  ❌ 解析 VPS789 分线路 JSON 失败: {e}")
            
    print(f"  ✅ VPS789 获取结果: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def fetch_cfyes():
    """2. 从 Hostmonit (CFYes) 抓取 IP"""
    print("🛰️ 正在从 CFYes (Hostmonit) 优选源抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    response = http_post_json("https://api.hostmonit.com/get_optimization_ip", {"key": "iDetkOys"})
    if response:
        try:
            data = json.loads(response)
            for item in data.get("info", []):
                ip = item.get("ip", "").strip()
                line = item.get("line", "").upper()
                if is_valid_ipv4(ip):
                    if line == "CM":
                        results["CM"].append(ip)
                    elif line == "CU":
                        results["CU"].append(ip)
                    elif line == "CT":
                        results["CT"].append(ip)
        except Exception as e:
            print(f"  ❌ 解析 CFYes JSON 失败: {e}")
            
    print(f"  ✅ CFYes 获取结果: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def main():
    print("==================================================")
    print("🚀 开始全网 Cloudflare 优选 IPv4 自动化抓取、过滤与去重")
    print("==================================================")
    
    # 汇总各线路候选 IP
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
        
    # 过滤、去重、排序
    cm_clean = sorted(list(set([ip for ip in cm_all if is_valid_ipv4(ip)])))
    cu_clean = sorted(list(set([ip for ip in cu_all if is_valid_ipv4(ip)])))
    ct_clean = sorted(list(set([ip for ip in ct_all if is_valid_ipv4(ip)])))
    
    print("\n==================================================")
    print("📊 过滤去重统计 (去重前 -> 去重后)")
    print("==================================================")
    print(f"  中国移动 CMCC: {len(cm_all)} -> {len(cm_clean)}")
    print(f"  中国联通 CUCC: {len(cu_all)} -> {len(cu_clean)}")
    print(f"  中国电信 CTCC: {len(ct_all)} -> {len(ct_clean)}")
    
    # 创建输出目录
    output_dir = "bestcf_out"
    os.makedirs(output_dir, exist_ok=True)
    
    # 写入文件
    with open(os.path.join(output_dir, "移动.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(cm_clean))
        
    with open(os.path.join(output_dir, "联通.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(cu_clean))
        
    with open(os.path.join(output_dir, "电信.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(ct_clean))
        
    print(f"\n🎉 干净的 IP 列表已成功写入至 '{output_dir}/' 目录下！")
    
    # 将最新优选 IP 同步推送到华为云 DNS，执行“先删后建”彻底替换策略 (无默认保底线)
    sync_to_huaweicloud(SUB_DOMAIN, ct_clean, cm_clean, cu_clean)

def sync_to_huaweicloud(sub_domain, ct_ips, cm_ips, cu_ips):
    if not HUAWEI_SDK_AVAILABLE:
        print(f"\n⚠️ 未检测到华为云 SDK，同步 {sub_domain}.{DOMAIN} 跳过。")
        return
    if not HUAWEICLOUD_AK or not HUAWEICLOUD_SK or not DOMAIN:
        print(f"\n⚠️ 未配置华为云 AK/SK 或域名，同步 {sub_domain}.{DOMAIN} 跳过。")
        return

    print(f"\n[同步] 正在自动同步 {sub_domain}.{DOMAIN} 到华为云 DNS (采用彻底覆盖模式)...")
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

        # 1. 彻底删除不在 ALLOWED_LINES 列表里的其他所有线路的旧 A 记录 (包括以前的 default_view 默认保底线)
        ALLOWED_LINES = {"Dianxin", "Yidong", "Liantong"}
        for line_code, record_item in existing_records.items():
            if line_code not in ALLOWED_LINES:
                print(f"  🗑️ 清理: 检测到废弃的线路记录 [{line_code}] {record_item.records}，正在彻底删除...")
                delete_req = DeleteRecordSetRequest()
                delete_req.zone_id = zone_id
                delete_req.recordset_id = record_item.id
                client.delete_record_set(delete_req)
                print(f"  ✅ 清理: 废弃的 [{line_code}] 记录删除成功。")

        target_lines = {
            "Dianxin": ("中国电信 线路", ct_ips),
            "Yidong": ("中国移动 线路", cm_ips),
            "Liantong": ("中国联通 线路", cu_ips)
        }

        for line_code, (line_name, target_ips) in target_lines.items():
            if not target_ips:
                print(f"  ⚠️ {line_name}: 没有健康的 IP 可供同步，跳过。")
                continue
            
            new_ips = [ip.strip() for ip in target_ips if ip.strip()]
            
            # 2. 彻底删除旧记录（如果存在）
            if line_code in existing_records:
                record_item = existing_records[line_code]
                print(f"  🗑️ {line_name}: 检测到旧记录 {record_item.records}，正在彻底删除...")
                delete_req = DeleteRecordSetRequest()
                delete_req.zone_id = zone_id
                delete_req.recordset_id = record_item.id
                client.delete_record_set(delete_req)
                print(f"  ✅ {line_name}: 旧记录删除成功。")
            
            # 3. 重新创建新记录
            print(f"  ➕ {line_name}: 正在创建全新解析，指向 IP 列表 {new_ips}...")
            create_req = CreateRecordSetWithLineRequest()
            create_req.zone_id = zone_id
            create_req.body = CreateRecordSetWithLineRequestBody(
                type="A", name=full_name, ttl=300, weight=1, records=new_ips, line=line_code
            )
            client.create_record_set_with_line(create_req)
            print(f"  ✅ {line_name}: 全新多值 A 记录创建成功！")
            
    except Exception as e:
        print(f"❌ 华为云 API 同步出错: {e}")

if __name__ == '__main__':
    main()
