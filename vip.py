import urllib.request
import json
import socket
import time
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

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

def fetch_bestcf_ips(max_retries=3):
    # 优先使用 GitHub Raw 链接，如果失败会自动回退到 jsDelivr CDN
    files = {
        "Dianxin": "ctcc.txt",
        "Yidong": "cmcc.txt",
        "Liantong": "cucc.txt",
        "default_view": "default.txt"
    }
    
    owner_repo = "godeluoo1/cf-dns"
    branch = "ips"
    
    bestcf_data = {}
    for line_code, filename in files.items():
        primary_url = f"https://raw.githubusercontent.com/{owner_repo}/{branch}/{filename}"
        fallback_url = f"https://cdn.jsdelivr.net/gh/{owner_repo}@{branch}/{filename}"
        
        success = False
        content = None
        
        for attempt in range(max_retries):
            # 尝试一：Primary URL
            try:
                req = urllib.request.Request(primary_url, headers={'User-Agent': 'Mozilla/5.0'}, method='GET')
                with urllib.request.urlopen(req, timeout=8) as response:
                    if response.status == 200:
                        content = response.read().decode('utf-8')
                        success = True
                        break
            except Exception as e_primary:
                print(f"  ⚠️ 尝试从 GitHub Raw 下载 {filename} 失败: {e_primary}，尝试 CDN...")
                # 尝试二：Fallback URL
                try:
                    req = urllib.request.Request(fallback_url, headers={'User-Agent': 'Mozilla/5.0'}, method='GET')
                    with urllib.request.urlopen(req, timeout=8) as response:
                        if response.status == 200:
                            content = response.read().decode('utf-8')
                            success = True
                            break
                except Exception as e_fallback:
                    print(f"  ❌ 从 CDN 下载 {filename} 也失败: {e_fallback}")
            
            time.sleep(1)
            
        if success and content:
            ips = []
            for line in content.splitlines():
                ip = line.strip()
                # 我们的 txt 里面本来就是干净的纯 IP，但为了健壮性我们还是加一下基本格式校验
                if ip and not ip.startswith("#") and ":" not in ip:
                    ips.append(ip)
            bestcf_data[line_code] = ips
            print(f"  📥 成功获取 {line_code} 候选 IP 数: {len(ips)}")
        else:
            print(f"  ⚠️ 无法获取 {line_code} 的候选 IP 列表。")
            bestcf_data[line_code] = []
            
    return bestcf_data

def tcp_ping(ip, port=443, timeout=1.5):
    """在本地对 IP 的 443 端口进行真实的 TCP 握手测速"""
    start_time = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.close()
        return ip, True, (time.time() - start_time) * 1000
    except Exception:
        return ip, False, 9999.0

def evaluate_line_ips(ips, line_name):
    """并发测试列表里的 IP 活性并按本地 TCP 延迟排序，同步所有健康 IP"""
    if not ips:
        return []
    
    print(f"  ⚡️ 正在测试 {line_name} 的 {len(ips)} 个候选 IP 的本地 443 端口连通性...")
    checked_ips = []
    
    # 限制 10 线程并发测速，测试全部候选 IP
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(tcp_ping, ip): ip for ip in ips}
        for future in as_completed(futures):
            try:
                ip, healthy, latency = future.result()
                if healthy:
                    checked_ips.append({"ip": ip, "latency": latency})
            except Exception:
                pass
                
    # 按照延迟从小到大排序
    checked_ips.sort(key=lambda x: x["latency"])
    
    # 提取所有健康的 IP
    final_ips = [x["ip"] for x in checked_ips]
    print(f"  ✅ {line_name} 本地健康 IP 排序与过滤结果（全部共 {len(final_ips)} 个）: {final_ips}")
    return final_ips

def sync_to_huaweicloud(sub_domain, ct_ips, cm_ips, cu_ips, def_ips):
    if not HUAWEI_SDK_AVAILABLE:
        print(f"\n⚠️ 未检测到华为云 SDK，同步 {sub_domain}.{DOMAIN} 跳过。")
        return
    if not HUAWEICLOUD_AK or not HUAWEICLOUD_SK or not DOMAIN:
        print(f"\n⚠️ 未配置华为云 AK/SK 或域名，同步 {sub_domain}.{DOMAIN} 跳过。")
        return

    print(f"\n[同步] 正在自动同步 {sub_domain}.{DOMAIN} 多 A 记录列表到华为云公网 DNS...")
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

        target_lines = {
            "Dianxin": ("中国电信 线路", ct_ips),
            "Yidong": ("中国移动 线路", cm_ips),
            "Liantong": ("中国联通 线路", cu_ips),
            "default_view": ("默认保底 线路", def_ips)
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

def main():
    print("=========================================================================")
    print(" 🛰️  Cloudflare 多 IP A 记录智能解析测试同步 (vip.py)")
    print("=========================================================================")
    
    # 1. 拉取 BestCF 数据源
    print("🔄 开始从 BestCF 下载最新全网聚合 IP 列表...")
    bestcf_data = fetch_bestcf_ips()
    
    # 2. 本地评估和过滤
    ct_ips = evaluate_line_ips(bestcf_data.get("Dianxin", []), "中国电信")
    cm_ips = evaluate_line_ips(bestcf_data.get("Yidong", []), "中国移动")
    cu_ips = evaluate_line_ips(bestcf_data.get("Liantong", []), "中国联通")
    def_ips = evaluate_line_ips(bestcf_data.get("default_view", []), "默认保底")
    
    # 3. 同步到华为云 A 记录
    sync_to_huaweicloud(SUB_DOMAIN, ct_ips, cm_ips, cu_ips, def_ips)
    print("\n✓ 多 IP A 记录同步流程完成。")

if __name__ == '__main__':
    main()
