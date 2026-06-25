import urllib.request
import re
import socket
import time
import sys
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

# 目标域名
DOMAIN = "vip.blogluo.eu.org"
OWNER_REPO = "godeluoo1/cf-dns"
BRANCH = "bestcf"

# 1. 下载 IP 候选池
def download_ips():
    # 既然他是移动网络，我们可以优先测移动的，但为了“所有ip中选最低延迟的”，我们把移动、联通、电信、默认全部合并去重
    files = ["移动.txt", "联通.txt", "电信.txt", "默认.txt"]
    all_ips = set()
    
    print("🔄 正在从您的 GitHub 分支拉取最新的优选 IP 候选池...")
    for fn in files:
        url = f"https://cdn.jsdelivr.net/gh/{OWNER_REPO}@{BRANCH}/{fn}"
        # 加上时间戳避免 CDN 缓存
        url_with_t = f"{url}?t={int(time.time())}"
        try:
            req = urllib.request.Request(url_with_t, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    content = response.read().decode('utf-8')
                    for line in content.splitlines():
                        ip = line.strip()
                        # 校验 IPv4
                        if ip and re.match(r'^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$', ip):
                            all_ips.add(ip)
        except Exception as e:
            # 某些文件可能在分支里暂时缺失，属于正常现象，捕获异常继续下一个
            pass
            
    return list(all_ips)

# 2. TCP 握手测速
def tcp_ping(ip, port=443, timeout=1.0):
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        sock.close()
        return ip, True, (time.time() - start) * 1000
    except Exception:
        return ip, False, 9999.0

def test_ips(ips):
    print(f"⚡️ 开始对本地 {len(ips)} 个优选 IP 进行 443 端口 TCP 握手测速...")
    results = []
    # 限制 15 线程并发
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = {executor.submit(tcp_ping, ip): ip for ip in ips}
        for future in as_completed(futures):
            ip, ok, latency = future.result()
            if ok:
                results.append((ip, latency))
                
    results.sort(key=lambda x: x[1])
    return results

# 3. 修改 hosts 文件
def update_hosts(best_ip):
    hosts_path = "/etc/hosts"
    if not os.path.exists(hosts_path):
        print("❌ 错误：未找到系统 hosts 文件！")
        return False
        
    print(f"✍️ 正在尝试将域名 {DOMAIN} 绑定至最快 IP {best_ip} ...")
    
    # 读取旧 hosts
    try:
        with open(hosts_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except PermissionError:
        print("\n❌ 权限不足！修改 hosts 文件需要管理员权限。")
        print(f"👉 请在终端使用以下命令运行此脚本：\n   sudo python3 {sys.argv[0]}\n")
        return False
    except Exception as e:
        print(f"❌ 读取 hosts 失败: {e}")
        return False

    # 过滤掉原有的 vip.blogluo.eu.org 解析
    new_lines = []
    for line in lines:
        if DOMAIN in line:
            # 排除掉包含我们域名的行
            continue
        new_lines.append(line)
        
    # 在末尾追加最新的解析
    new_lines.append(f"{best_ip} {DOMAIN} # Cloudflare Best Local IP\n")
    
    # 写入新 hosts
    try:
        with open(hosts_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print("✅ hosts 文件更新成功！")
        return True
    except Exception as e:
        print(f"❌ 写入 hosts 失败: {e}")
        return False

# 4. 清理 Mac 本地 DNS 缓存
def flush_dns():
    print("🧹 正在清除 Mac 本地 DNS 缓存...")
    os.system("sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder 2>/dev/null")
    print("✅ DNS 缓存刷新成功！")

def main():
    # 检查权限
    if os.getuid() != 0:
        print("\n⚠️ 警告：修改本地 hosts 需要管理员权限！")
        print(f"👉 请使用 sudo 权限运行此一键脚本：\n   sudo python3 {sys.argv[0]}\n")
        sys.exit(1)
        
    print("==================================================")
    print("🚀 Cloudflare 优选 IP 本地一键测速与 hosts 加速绑定")
    print("==================================================")
    
    ips = download_ips()
    if not ips:
        print("❌ 错误：未获取到任何候选 IP。请检查网络或确认 bestcf 分支已成功运行发布。")
        sys.exit(1)
        
    print(f"📊 成功获取 {len(ips)} 个去重后的候选 IP。")
    results = test_ips(ips)
    
    if not results:
        print("❌ 错误：所有 IP 本地 TCP 测速均失败，请检查您的网络连接！")
        sys.exit(1)
        
    best_ip, best_latency = results[0]
    print(f"\n🏆 冠军 IP 诞生！")
    print(f"  👉 IP: {best_ip}")
    print(f"  👉 本地延迟: {best_latency:.2f} 毫秒")
    print("--------------------------------------------------")
    
    if update_hosts(best_ip):
        flush_dns()
        print("\n🎉 全部配置已大功告成！")
        print(f"✨ 现在您本地访问 {DOMAIN} 将直接秒连最快节点 {best_ip} ({best_latency:.1f}ms)！")
        print("==================================================")

if __name__ == '__main__':
    main()
