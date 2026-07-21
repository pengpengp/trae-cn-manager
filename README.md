# Trae CN Manager (TCN)

Trae CN（字节跳动 AI IDE，国内版）自动化账号管理系统：手机号注册 + 一键切换 + 额度查询。

- 通过 [text-verification.net](https://text-verification.net)（免费国际接码平台，44 个 +86 号码）获取手机号接收 SMS OTP
- Playwright 自动化浏览器完成注册流程
- JWT、Cookie 使用 AES-256-GCM 加密后存入本地 SQLite
- 一键切换账号 = 轮换设备指纹 + 恢复 profile + 重启 Trae CN

> ⚠️ **免责声明**：自动化注册可能违反 Trae CN 服务条款。本工具仅供学习与研究使用，使用本工具产生的一切后果由使用者自行承担。

---

## 系统要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（Trae CN 仅在此运行） |
| Python | 3.10+ |
| 网络 | 需要能访问 `trae.cn`、`text-verification.net` 的 HTTP(S) 代理 |
| Trae CN | 已安装（切换/捕获功能需要；纯注册可不需要） |
| Playwright | `playwright install chromium` 一次 |

---

## 安装

```bash
# 1. 克隆/下载本项目
cd trae-cn-manager

# 2. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\Activate

# 3. 安装
pip install -e "."

# 4. 安装 Playwright 浏览器
playwright install chromium
```

安装完成后会有 `tcn` 命令可用：

```bash
tcn version
# tcn 0.1.0
```

---

## 配置

### 代理（重要）

Trae CN API 可能对国内 IP 有限流，建议通过代理访问。

```bash
# 默认代理地址（config.py）
# 优先 TCN_PROXY 环境变量，其次 TAM_PROXY，默认 http://127.0.0.1:7897
set TCN_PROXY=http://127.0.0.1:7897        # Windows
export TCN_PROXY=http://127.0.0.1:7897     # Git Bash
```

关闭代理：

```bash
set TCN_PROXY=none
```

### 主加密密钥（可选）

`secrets_blob` 使用 AES-256-GCM 加密，密钥按以下顺序解析：

1. 环境变量 `TCN_MASTER_KEY`（32 字节 hex）
2. 系统钥匙串（keyring）
3. 本地文件 `master.key`（自动生成）

---

## 快速开始

```bash
# 0. 确认代理可用
curl -x "%TCN_PROXY%" -I https://www.trae.cn/

# 1. 设置 Trae CN 路径（如自动扫描失败）
tcn set-path "D:\Programs\Trae CN\Trae CN.exe"

# 2. 查看环境信息
tcn info

# 3. 注册 1 个账号（会出现浏览器窗口，需要手动过滑块验证码）
tcn register 1 --headed

# 4. 查看账号列表
tcn list

# 5. 查询当前账号额度
tcn usage

# 6. 切换账号（需要先注册/捕获至少两个账号）
tcn switch <account_id>

# 7. 捕获当前已登录的 Trae CN 会话
tcn capture --name "my-account" --phone "138xxxx"
```

---

## CLI 命令参考

### `tcn version`
显示版本号。

### `tcn list [--active]`
列出所有已注册账号。`--active` 只显示启用中的账号。

### `tcn current`
显示当前驱动 Trae CN 的账号详情。

### `tcn switch <ACCOUNT_ID> [--launch/--no-launch] [--reset-registry]`
切换到指定账号。

| 参数 | 默认 | 说明 |
|---|---|---|
| `ACCOUNT_ID` | — | 账号 ID（支持前缀匹配）或手机号 |
| `--launch/--no-launch` | `--launch` | 切换后是否启动 Trae CN |
| `--reset-registry` | False | Windows 额外重置 MachineGuid（更强隔离） |

### `tcn register [COUNT] [-c CONCURRENCY] [--headed] [--no-persist]`
注册新 Trae CN 账号。

| 参数 | 默认 | 说明 |
|---|---|---|
| `COUNT` | 1 | 注册数量（1–50） |
| `-c, --concurrency` | 1 | 最大并发 |
| `--headed` | False | 显示浏览器窗口（用于手动过滑块验证码） |
| `--no-persist` | False | 不写入数据库 |

流程：
1. 从 text-verification.net 获取可用 +86 手机号
2. 打开 trae.cn/login，填入手机号
3. 点击"获取验证码"（需手动过 Bytedance 滑块验证码）
4. 轮询 text-verification.net 收取 SMS OTP
5. 填入验证码，自动登录
6. 提取 JWT，加密后写入 SQLite

### `tcn usage [ACCOUNT_ID]`
查询用量额度。不带参数时查询当前账号。

### `tcn capture [--name] [--email] [--phone]`
把当前 Trae CN 的已登录会话捕获到本地数据库。

### `tcn clear [--launch]`
清除登录状态（登出）。`--launch` 清理后启动 Trae CN。

### `tcn delete <ACCOUNT_ID>`
从本地数据库删除账号（不影响 Trae CN 服务端）。

### `tcn set-path <PATH>`
设置 Trae CN 可执行文件路径。

### `tcn path`
显示当前 Trae CN 可执行文件路径。

### `tcn info`
显示环境信息（版本、数据目录、Trae CN 路径、代理、账号数等）。

---

## 工作原理

### 注册流程

```
text-verification.net (免费接码) → +86 手机号
        │
        ▼
Playwright 打开 trae.cn/login → 填入手机号 → 获取验证码
        │  (Bytedance 滑块验证码 → 手动完成)
        ▼
轮询 text-verification.net → OTP
        │
        ▼
填入验证码 → 登录成功 → 提取 JWT
        │
        ▼  AES-256-GCM 加密
SQLite (accounts.db)
```

### 切换流程

```
kill Trae CN → 写 machineid → 清缓存 → 写 storage.json (telemetry + iCubeAuthInfo) → 启动
```

---

## 项目结构

```
trae-cn-manager/
├── trae_cn_manager/
│   ├── __init__.py          # 版本号
│   ├── cli.py               # Typer CLI（12 个命令）
│   ├── config.py            # 路径/代理/API 端点
│   ├── models.py            # Account SQLModel
│   ├── db.py                # SQLite 持久化
│   ├── vault.py             # AES-256-GCM 加密
│   ├── machine.py           # 设备指纹 + iCube auth
│   ├── process_ctl.py       # Trae CN 进程控制
│   ├── switcher.py          # 账号切换 + capture + clear
│   ├── register.py          # SMS 注册流程（Playwright）
│   ├── trae_api.py          # Trae CN API 客户端
│   └── sms_client.py        # SMS 客户端（text-verification.net）
├── pyproject.toml            # 包定义 + tcn 入口
└── README.md
```
