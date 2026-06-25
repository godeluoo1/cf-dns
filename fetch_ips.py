import urllib.request
import json
import re
import os

# IPv4 正则校验
IPV4_PATTERN = re.compile(r'^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$')

def is_valid_ipv4(ip):
    return bool(IPV4_PATTERN.match(ip))

def http_get(url, timeout=10):
    """标准的 HTTP GET 请求，添加了 User-Agent 以防止被拦截"""
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

def fetch_cmliu():
    """1. 从 CMLiu 优选平台抓取 IP"""
    print("🛰️ 正在从 CMLiu 优选源抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    # 移动
    cm_content = http_get("https://cf.090227.xyz/cmcc?ips=30")
    if cm_content:
        for line in cm_content.splitlines():
            ip = line.split('#')[0].strip()
            if is_valid_ipv4(ip):
                results["CM"].append(ip)
                
    # 联通
    cu_content = http_get("https://cf.090227.xyz/cu?ips=30")
    if cu_content:
        for line in cu_content.splitlines():
            ip = line.split('#')[0].strip()
            if is_valid_ipv4(ip):
                results["CU"].append(ip)
                
    # 电信
    ct_content = http_get("https://cf.090227.xyz/ct?ips=30")
    if ct_content:
        for line in ct_content.splitlines():
            ip = line.split('#')[0].strip()
            if is_valid_ipv4(ip):
                results["CT"].append(ip)
                
    print(f"  ✅ CMLiu 获取结果: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def fetch_vps789():
    """2. 从 VPS789 优选 API 抓取 IP"""
    print("🛰️ 正在从 VPS789 优选源抓取...")
    results = {"CM": [], "CU": [], "CT": [], "default": []}
    
    # 分线路 IP
    line_content = http_get("https://vps789.com/openApi/cfIpApi")
    if line_content:
        try:
            data = json.loads(line_content)
            if data.get("code") == 200:
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
            
    # 混合默认优选 (CF Top 20)
    top_content = http_get("https://vps789.com/openApi/cfIpTop20")
    if top_content:
        try:
            data = json.loads(top_content)
            if data.get("code") == 200:
                for ip_obj in data.get("data", {}).get("good", []):
                    ip = ip_obj.get("ip", "").strip()
                    if is_valid_ipv4(ip):
                        results["default"].append(ip)
        except Exception as e:
            print(f"  ❌ 解析 VPS789 默认优选 JSON 失败: {e}")
            
    print(f"  ✅ VPS789 获取结果: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])} 默认={len(results['default'])}")
    return results

def fetch_cfyes():
    """3. 从 Hostmonit (CFYes) 抓取 IP"""
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

def fetch_wetest():
    """4. 从 WeTest HTML 页面抓取 IP"""
    print("🛰️ 正在从 WeTest 优选源网页抓取...")
    results = {"CM": [], "CU": [], "CT": []}
    
    html = http_get("https://www.wetest.vip/page/cloudflare/address_v4.html")
    if html:
        # 使用正则表达式匹配表格行里的 线路名称 和 优选地址
        # 匹配 <td data-label="线路名称">移动</td> ... <td data-label="优选地址">104.20.62.222</td>
        pattern = re.compile(
            r'<td\s+data-label="线路名称"\s*>\s*([^<]+?)\s*</td>\s*<td\s+data-label="优选地址"\s*>\s*([^<]+?)\s*</td>',
            re.IGNORECASE | re.DOTALL
        )
        matches = pattern.findall(html)
        for line_name, ip in matches:
            ip = ip.strip()
            line_name = line_name.strip()
            if is_valid_ipv4(ip):
                if "移动" in line_name:
                    results["CM"].append(ip)
                elif "联通" in line_name:
                    results["CU"].append(ip)
                elif "电信" in line_name:
                    results["CT"].append(ip)
                    
    print(f"  ✅ WeTest 获取结果: 移动={len(results['CM'])} 联通={len(results['CU'])} 电信={len(results['CT'])}")
    return results

def fetch_other_defaults():
    """5. 从 CFSpeedTest 和 IPDB 抓取混合默认 IP"""
    print("🛰️ 正在从 CFSpeedTest 和 IPDB 优选源抓取默认列表...")
    default_ips = []
    
    # CFSpeedTest
    speed_content = http_get("https://ip.164746.xyz/ipTop10.html")
    if speed_content:
        for ip in speed_content.split(','):
            ip = ip.strip()
            if is_valid_ipv4(ip):
                default_ips.append(ip)
                
    # IPDB bestcfv4
    ipdb_content = http_get("https://ipdb.api.030101.xyz/?type=bestcfv4")
    if ipdb_content:
        for line in ipdb_content.splitlines():
            ip = line.strip()
            if is_valid_ipv4(ip):
                default_ips.append(ip)
                
    print(f"  ✅ CFSpeedTest/IPDB 获取结果: 默认={len(default_ips)}")
    return default_ips

def main():
    print("==================================================")
    print("🚀 开始全网 Cloudflare 优选 IPv4 自动化抓取、过滤与去重")
    print("==================================================")
    
    # 汇总各线路候选 IP
    cm_all = []
    cu_all = []
    ct_all = []
    def_all = []
    
    # 1. 抓取 CMLiu
    try:
        res = fetch_cmliu()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
    except Exception as e:
        print(f"  ⚠️ 抓取 CMLiu 异常: {e}")
        
    # 2. 抓取 VPS789
    try:
        res = fetch_vps789()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
        def_all.extend(res["default"])
    except Exception as e:
        print(f"  ⚠️ 抓取 VPS789 异常: {e}")
        
    # 3. 抓取 CFYes
    try:
        res = fetch_cfyes()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
    except Exception as e:
        print(f"  ⚠️ 抓取 CFYes 异常: {e}")
        
    # 4. 抓取 WeTest
    try:
        res = fetch_wetest()
        cm_all.extend(res["CM"])
        cu_all.extend(res["CU"])
        ct_all.extend(res["CT"])
    except Exception as e:
        print(f"  ⚠️ 抓取 WeTest 异常: {e}")
        
    # 5. 抓取 CFSpeedTest & IPDB 默认列表
    try:
        res_def = fetch_other_defaults()
        def_all.extend(res_def)
    except Exception as e:
        print(f"  ⚠️ 抓取 CFSpeedTest/IPDB 异常: {e}")
        
    # 三网 IP 也应该包含进混合默认列表里作为备选
    def_all.extend(cm_all)
    def_all.extend(cu_all)
    def_all.extend(ct_all)
    
    # 过滤、去重、排序
    cm_clean = sorted(list(set([ip for ip in cm_all if is_valid_ipv4(ip)])))
    cu_clean = sorted(list(set([ip for ip in cu_all if is_valid_ipv4(ip)])))
    ct_clean = sorted(list(set([ip for ip in ct_all if is_valid_ipv4(ip)])))
    def_clean = sorted(list(set([ip for ip in def_all if is_valid_ipv4(ip)])))
    
    print("\n==================================================")
    print("📊 过滤去重统计 (去重前 -> 去重后)")
    print("==================================================")
    print(f"  中国移动 CMCC: {len(cm_all)} -> {len(cm_clean)}")
    print(f"  中国联通 CUCC: {len(cu_all)} -> {len(cu_clean)}")
    print(f"  中国电信 CTCC: {len(ct_all)} -> {len(ct_clean)}")
    print(f"  混合默认 DEFAULT: {len(def_all)} -> {len(def_clean)}")
    
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

if __name__ == '__main__':
    main()
