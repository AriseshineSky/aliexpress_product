# AliExpress Product Crawler (alixq3)

从 Elasticsearch 读取 AliExpress 商品链接，用 Playwright 抓取详情，校验 `StandardProduct` 格式后写入 ES 索引 `user1_aliexpress_us_products`。

`.com` 与 `.us` 站点分开保存：`source` 为 `aliexpress.com` / `aliexpress.us`，文档 ID 为 `{source}_{product_id}`。

## 一键启动（直接运行）

| 平台 | 首次安装 | 启动抓取 |
|------|----------|----------|
| **Windows** | 双击或运行 `install.bat` | 双击或运行 `start.bat` |
| **Linux / macOS** | `./install.sh` | `./start.sh` |

等价命令也在 `scripts/` 目录：`scripts\start.bat`（Windows）、`scripts/start.sh`（Linux）。

## Windows 部署（Git + .env）

### 1. 准备环境

- Windows 10/11
- [Git for Windows](https://git-scm.com/download/win)
- Python 3.10+（安装时勾选 **Add Python to PATH**）
- 可访问 ES 与 Webshare 代理的网络

### 2. 克隆仓库

```powershell
git clone <你的仓库地址> aliexpress_product
cd aliexpress_product
```

### 3. 配置敏感信息（不入库）

```powershell
copy .env.example .env
notepad .env
```

必填项示例：

```env
ES_HOST=34.16.105.219
ES_PORT=9200
ES_USER=your_es_username
ES_PASSWORD=your_es_password

WEBSHARE_USER=your_webshare_username
WEBSHARE_PASSWORD=your_webshare_password
WEBSHARE_COUNTRY=US
```

**`.env` 已在 `.gitignore` 中，切勿提交到 Git。**

### 4. 安装依赖

双击或在 PowerShell 中运行：

```powershell
scripts\install.bat
```

脚本会：

1. 创建 `.venv` 虚拟环境
2. 安装 `requirements.txt`
3. 安装 Playwright Chromium
4. 若不存在 `.env`，从 `.env.example` 复制一份

### 5. 运行抓取

双击项目根目录的 `start.bat`，或在 PowerShell 中：

```powershell
start.bat
```

输出目录：`产品详情/`（本地 jsonl + 进度文件）

浏览器使用 persistent profile（`browser_playwright/`），同一 Worker 会复用会话连续抓多个商品；**只有确认无法获取商品信息**（验证码/网络错误/字段不完整等硬失败）时才会清空 profile 并硬重启。本地试跑可在 `.env` 设 `MAX_PRODUCTS=1`、`WORKER_COUNT=1`、`HEADLESS=0`。

### 代理模式

| `PROXY_MODE` | 说明 |
|--------------|------|
| `rotate`（默认） | 现有 Webshare 网关 + rotate，硬失败可换 IP |
| `static` | 使用 `data/*.txt` 固定代理（`host:port:user:pass`），同一会话保持 IP/cookies |

`static` 模式会先预热首页→分类→商品页，验证码 LLM 失败后**保留 session**并换下一 URL；连续失败达到 `PROXY_MAX_CONSECUTIVE_CAPTCHA` 后停止该 Worker。

反检测（默认开启）：`playwright-stealth` + 与代理绑定的 Canvas/WebGL 指纹池（`data/fingerprints.json`）+ 贝塞尔鼠标轨迹。可用 `STEALTH_ENABLED` / `FINGERPRINT_ENABLED` / `HUMAN_MOUSE_ENABLED` 开关。

单代理容量测试：

```bash
.venv/bin/python scripts/test_one_proxy.py --proxy-index 0
```

### 6. 更新代码

```powershell
git pull
scripts\install.bat   # 依赖有变化时再跑
scripts\start.bat
```

## 仓库里有什么 / 没有什么

| 提交到 Git | 不提交（本地/机密） |
|-----------|---------------------|
| `alixq3.py`、`html_utils.py`、`em_product/` | `.env` |
| `scripts/`、`requirements.txt` | `.venv/` |
| `.env.example`（模板，无真实密码） | `产品详情/`、`browser_playwright/`、`img/` |

## Linux 运行

```bash
./install.sh    # 首次
./start.sh      # 启动抓取
```

## 测试

```bash
.venv\Scripts\python.exe -m unittest discover -s . -p test_product_parse.py -v
```
