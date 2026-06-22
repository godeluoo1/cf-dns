# 🛰️ Cloudflare 智能优选 CNAME 系统

基于 VPS789.com 平台三网历史数据，全自动选出丢包最低、抖动最小、三网综合最优的 Cloudflare 优选域名，并通过华为云 DNS API 实现电信/移动/联通三网智能分流。

## 🏗️ 核心架构

```
600 个候选域名 (VPS789 API)
        │
        ▼ 黑名单 + DNS + CF IP 过滤
   ┌────────────┐
   │  Top 100   │ ← 6 小时刷新
   └─────┬──────┘
         ▼ DNS 健康体检
   ┌────────────┐
   │  Top 20    │ ← 1 小时刷新
   └─────┬──────┘
         ▼ 综合评分筛选
   ┌────────────┐
   │  Top 5     │ ← 每次执行检查
   └─────┬──────┘
         ▼ 连续领先3次 + 冷却30min + 信誉分联合判定
   ┌────────────┐
   │ Champion   │ → 华为云 DNS 同步
   └────────────┘
```

## ⚡ 核心特性

| 特性 | 说明 |
|------|------|
| **EMA 加权评分** | 近 7 天数据权重显著高于历史数据 |
| **评分公式** | `1000×丢包 + 50×抖动 + 0.7×延迟`，稳定性远优先于速度 |
| **三网独立统计** | 电信/移动/联通各自计算延迟、丢包、抖动、失联次数 |
| **信誉分机制** | 历史冠军信用评价 + 降级审查体系 |
| **防抖切换** | 连续 3 次领先 + 30 分钟冷却期 |
| **DNS 熔断** | 实时检测冠军存活，逐级 Top5→Top20→Top100 降级 |
| **状态持久化** | 跨运行保持所有状态，支持重启恢复 |

## 🚀 部署方式

### 方案 A：在 Northflank / Render 等云平台运行（推荐）

1. 新建一个 **Web Service**（不要选 Job），连接你的 GitHub 私有仓库。
2. 平台会自动根据根目录下的 `Dockerfile` 自动构建和启动服务。
3. 在环境变量中添加：
   * `HUAWEICLOUD_AK` - 华为云 Access Key
   * `HUAWEICLOUD_SK` - 华为云 Secret Key
   * `PORT` - 服务运行端口（如 `8080`，容器会自动在此端口启动 Web 看板服务）
   * `STATE_DIR` -（可选）设置为 `/data`。并在平台挂载一个 `1GB` 的持久化卷 (Volume) 到 `/data`，这可以确保容器重启后测速数据状态不丢失。

### 方案 B：在 VPS 上持续运行（Docker/Python 守护进程模式）

你可以使用 Docker 容器或直接通过 Python 守护进程持续在 VPS 后台运行：

#### 方法 1：使用 Docker 运行
```bash
# 1. 构建 Docker 镜像
docker build -t cf-dns-sync .

# 2. 启动容器（挂载状态卷并启动 8080 看板端口）
docker run -d \
  --name cf-sync \
  -p 8080:8080 \
  -e HUAWEICLOUD_AK="你的AK" \
  -e HUAWEICLOUD_SK="你的SK" \
  -e PORT=8080 \
  -e STATE_DIR="/data" \
  -v /root/cf-data:/data \
  --restart always \
  cf-dns-sync
```

#### 方法 2：直接使用 Python 守护运行
```bash
# 安装依赖
pip3 install -r requirements.txt

# 设置环境变量并使用 nohup 在后台死循环运行
export HUAWEICLOUD_AK="你的AK"
export HUAWEICLOUD_SK="你的SK"
export PORT=8080
export STATE_DIR="/root/cf-data"

nohup python3 cf.py > cf_run.log 2>&1 &
```

## ⚙️ 配置

在 `cf.py` 顶部配置：

```python
DOMAIN = "blogluo.eu.org"  # 你的主域名

# 每个子域名独立配置测速维度
SUB_DOMAINS_CONFIG = {
    "vip": 1,  # 30 天长期稳定维度
    "l": 0,    # 24 小时高灵敏维度
}
```

## 🔒 安全须知

- **AK/SK 必须通过环境变量或 GitHub Secrets 注入**，代码中不包含任何硬编码凭证
- 仓库**必须设置为 Private**
- 定期轮换华为云 AK/SK
- 1
