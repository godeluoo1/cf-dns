# 🚀 Cloudflare 智能优选 DNS 监控与分流系统

![GitHub Workflow Status](https://img.shields.io/github/actions/workflow/status/godeluoo1/cf-dns/cf_sync.yml?label=CF%20Sync)
![Python Version](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

基于 VPS789 平台历史大数据，全自动选出丢包最低、抖动最小、三网综合最优的 Cloudflare 优选 IP/CNAME。并通过华为云 DNS API 实现电信、移动、联通三网智能分流。

配合 **GitHub Actions 全自动化** 和 **高颜值可视化监控看板**，彻底解放双手，实现 DNS 容灾的“自动驾驶”。

🔗 **[点击查看在线高颜值监控看板](https://godeluoo1.github.io/cf-dns/status.html)**

## ✨ 核心特性

- 🌌 **极致视觉体验**: 自动生成“深空玻璃拟物风 (Glassmorphism)”的监控面板，自带悬浮微动效、数据渐变高亮与红绿灯熔断警告。
- 🤖 **全自动托管 (GitHub Actions)**: 抛弃繁琐的 VPS 部署，完全免费托管在 GitHub Actions。每 6 小时自动苏醒抓取数据并部署网页，测试数据会通过 Commit 自动永久保存！
- 🌟 **融合 BestCF 全网聚合源**: 智能融合 `DustinWin/BestCF` 优选大池，直接继承并白嫖 CMLiu、WeTest、CFYes、VPS789 等全网多监测点测速的去重汇总结果，大体量提取移动、联通、电信和通用 IP。
- ⚖️ **梯度排名分级注入**: 针对 BestCF 独占优选，根据其在文件里的原始排名位置，依次注入极其精细的梯度延迟/丢包（如第 1 名 80ms、第 2 名 80.2ms），打分时 100% 保留官方测速顺序，并在主 IP 活性异常时自动平滑地顺延降级。
- 🛡️ **严格防抖与熔断**: 候选节点需“连续 3 次领先”才能上位，现任节点一旦失联，立刻从 Top5→Top20→Top100 逐级降级熔断。
- 🗃️ **三网满编备用池**: 为电信、联通、移动及综合默认线路分别构建独立的 Top100 备用节点池，冗余度极高。

## 🏗️ 核心漏斗架构

```mermaid
graph TD
    A["600+ 候选域名池"] -->|"丢包过滤与综合测速"| B("Top 100 备用大池")
    B -->|"高频探活体检"| C("Top 20 优选池")
    C -->|"信誉分评估"| D("Top 5 热点池")
    D -->|"连续三次领先加冷却"| E(("最终冠军 IP"))
    E -->|"API 自动下发"| F["华为云 DNS 智能解析"]
```

## 🚀 极简部署方案 (推荐: GitHub Actions 完全免费版)

这是目前最优雅的运行方式，**零服务器成本**，数据永久保存：

1. **Fork/Clone** 本仓库到你的私人账户（**强烈建议设置为 Private 仓库**，保护你的 API 密钥）。
2. 去你的 GitHub 仓库设置 `Settings` -> `Secrets and variables` -> `Actions`，添加以下环境变量：
   - `HUAWEICLOUD_AK` : 华为云 Access Key
   - `HUAWEICLOUD_SK` : 华为云 Secret Key
3. 赋予 GitHub Actions 读写权限：进入 `Settings` -> `Actions` -> `General` -> 勾选 `Read and write permissions`。
4. 去 `Actions` 页面手动点击 **Run workflow** 触发一次。
5. 去 `Settings` -> `Pages`，将 Source 设置为 `Deploy from a branch`，Branch 选择 `gh-pages` 目录选 `/ (root)`。
6. 等待几分钟，你的炫酷监控面板就自动上线了！

## ⚙️ 个性化配置

在 `cf.py` 顶部修改你的专属域名和监测敏感度：

```python
DOMAIN = "你的主域名.com"

# 每个子域名独立配置测速维度
SUB_DOMAINS_CONFIG = {
    "cf": 1,  # 使用 30 天长期稳定维度 (推荐给建站)
    "vip": 0, # 使用 24 小时高灵敏维度 (推荐给代理)
}
```

## 🔬 多 IP 轮询对照组实验 (`vip.py`)

如果你想进行**“多 IP 轮询 vs 单一延迟最低 IP 自动选择”**的网络对照实验，可以使用仓库中专为你编写的 `vip.py` 轻量级实验脚本：
- **子域名作用域**：专门自动同步并维护子域名 **`vip.blogluo.eu.org`**。
- **本地高并发测速**：在你的本地/VPS 自动下载 BestCF 的四大线路 IP（各选前 10 个），通过 10 线程并发发起 443 端口 TCP 握手体检，排除死 IP 并按本地延迟升序排序。
- **多 A 记录自动同步**：将各线路排名前 5 的健康 IP，以批量 A 记录（records 数组）的形式写入到对应线路上。
- **运行方式**：
  ```bash
  python3 vip.py
  ```
  运行后，可通过 `nslookup vip.blogluo.eu.org` 或多次 `ping` 观察在多 A 记录下，客户端的轮询与丢包连通性情况。

## 🔒 安全须知
- 绝对不要在代码中硬编码任何 AK/SK。
- 必须通过 GitHub Secrets 注入凭证。
- 定期在云服务商后台轮换你的 API 密钥。
