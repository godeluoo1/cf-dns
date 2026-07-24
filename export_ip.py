import json
import time
import urllib.request
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.algorithms import TripleDES

# 3DES 密文解密
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
        return unpadder.update(decrypted_padded) + unpadder.finalize().decode('utf-8')
    except Exception:
        raw_str = decrypted_padded.decode('utf-8', errors='ignore').strip()
        return "".join(c for c in raw_str if ord(c) >= 32 or c in "\t\n\r")

def encrypt_token(timestamp_str):
    key_str = "".join(["385f33c", "b91484b04a177", "828829081ab7"])
    key = (key_str[:8] * 3).encode('utf-8')
    iv = b'00000000'
    padder = padding.PKCS7(64).padder()
    padded_data = padder.update(timestamp_str.encode('utf-8')) + padder.finalize()
    cipher = Cipher(TripleDES(key), modes.CBC(iv), backend=default_backend())
    return cipher.encryptor().update(padded_data) + cipher.encryptor().finalize().hex()

def fetch_600_ips():
    ts = str(int(time.time() * 1000))
    token = encrypt_token(ts)
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
    
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 200:
                res_data = json.loads(response.read().decode('utf-8'))
                decrypted_str = decrypt_payload(res_data.get("message", ""))
                content = json.loads(decrypted_str).get("content", [])
                ips = [item.get("ip") for item in content if item.get("ip")]
                
                with open("ip.txt", "w", encoding="utf-8") as f:
                    for ip in ips:
                        f.write(f"{ip}\n")
                print(f"✅ 成功抓取并导出 {len(ips)} 个优选 IP 到 ip.txt！")
    except Exception as e:
        print(f"❌ 抓取导出失败: {e}")

if __name__ == "__main__":
    fetch_600_ips()
