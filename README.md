# AnyRouter + AgentRouter 多账号自动签到

推荐搭配使用[Auo](https://github.com/millylee/auo)，支持任意 Claude Code Token 切换的工具。

**维护开源不易，如果本项目帮助到了你，请帮忙点个 Star，谢谢!**

通过同一个 GitHub Actions 定时任务兼容 AnyRouter 与 AgentRouter 多账号签到。AnyRouter 使用 Cookie + `api_user`，AgentRouter 使用邮箱 + 密码登录，并通过系统日志中的实际到账记录确认签到结果。

## 功能特性

- ✅ AnyRouter / AgentRouter 可单独配置或同时运行
- ✅ 两个平台均支持单个或多个账号
- ✅ **WAF Cookies 缓存机制**（2 小时有效期，减少浏览器启动开销）
- ✅ 多种机器人通知（可选）
- ✅ 绕过 Cloudflare WAF 限制
- ✅ 智能重试机制（3 次，指数退避）
- ✅ 紫蓝玻璃态工作台风格 HTML 邮件通知，展示平台、账号与统计结果
- ✅ 单一 `NOTIFY_ONCE` 开关控制通知去重，默认每轮推送，开启后成功结果每天只推送一次
- ✅ 北京时区支持

## 技术栈

- **Python 3.11+**
- **httpx**: HTTP/2 异步客户端
- **playwright**: 浏览器自动化（WAF 绕过）
- **asyncio**: 异步并发执行
- **ruff**: 代码格式化和 linter

## 使用方法

### 1. Fork 本仓库

点击右上角的 "Fork" 按钮，将本仓库 fork 到你的账户。

### 2. 获取账号信息

#### AnyRouter

1. **Cookies**: 用于身份验证
2. **API User**: 用于请求头的 new-api-user 参数

#### 获取 Cookies：

1. 打开浏览器，访问 https://anyrouter.top/
2. 登录你的账户
3. 打开开发者工具 (F12)
4. 切换到 "Application" 或 "存储" 选项卡
5. 找到 "Cookies" 选项
6. 复制所有 cookies

#### 获取 API User：

通常在网站的用户设置或 API 设置中可以找到，每个账号都有唯一的标识。

#### AgentRouter

AgentRouter（https://agentrouter.org/）直接配置登录邮箱和密码，不需要手动提取 Cookie：

1. **Email**: AgentRouter 登录邮箱
2. **Password**: AgentRouter 登录密码

脚本使用 Playwright 操作真实登录表单，每个账号使用独立浏览器上下文，互不干扰。AgentRouter 当前存在重复登录仍提示“签到成功、额度到账”的站点问题，因此脚本不会信任页面 toast 或登录响应的 `checked_in` 字段，而是登录后查询 `type=4` 的系统日志，仅当本次登录产生“每日签到成功，增加额度”记录时才计为成功；当天已有更早记录时计为“今日已签”；当天完全没有到账记录时计为失败，避免误报。

### 3. 设置 GitHub Environment Secret

1. 在你 fork 的仓库中，点击 "Settings" 选项卡
2. 在左侧菜单中找到 "Environments" -> "New environment"
3. 新建一个名为 `production` 的环境
4. 点击新建的 `production` 环境进入环境配置页
5. 点击 "Add environment secret"，按需创建：
   - `ANYROUTER_ACCOUNTS`: AnyRouter 多账号 JSON
   - `AGENTROUTER_ACCOUNTS`: AgentRouter 多账号 JSON
6. 在 Environment variables 中添加通知开关（可选，默认 `false`）：
   - `NOTIFY_ONCE=false`：每轮完整推送，便于观察两个平台的实际状态
   - `NOTIFY_ONCE=true`：每个账号当天的签到成功只推送一次；后续运行自动隐藏已成功账号
   - 失败始终推送，不会被去重

### 4. 多账号配置格式

两个 Secret 都是可选的，但至少需要配置一个。两个同时存在时会在同一次任务中依次执行并合并通知。两者都必须是 JSON **数组**，数组里放几个对象就是几个账号。

下面每个平台先给最小必填形态，可选字段单独列在后面。

#### `ANYROUTER_ACCOUNTS`（Cookie + api_user）

| 字段 | 必填 | 类型 | 说明 |
| ---- | ---- | ---- | ---- |
| `cookies` | 是 | object 或 string | 至少包含 `session`；也可直接填 `"a=1; b=2"` 形式的 Cookie 字符串 |
| `api_user` | 是 | string | 请求头 `New-Api-User` 的值，正常是 5 位数字 |
| `name` | 否 | string | 通知中显示的账号名称，省略时依次显示 `账号 1`、`账号 2` |

```json
[
  {
    "cookies": {
      "session": "account1_session_value"
    },
    "api_user": "account1_api_user_id"
  },
  {
    "cookies": {
      "session": "account2_session_value"
    },
    "api_user": "account2_api_user_id"
  }
]
```

#### `AGENTROUTER_ACCOUNTS`（邮箱 + 密码）

| 字段 | 必填 | 类型 | 说明 |
| ---- | ---- | ---- | ---- |
| `email` | 是 | string | AgentRouter 登录邮箱 |
| `password` | 是 | string | AgentRouter 登录密码 |
| `name` | 否 | string | 通知中显示的账号名称，省略时显示脱敏邮箱，如 `us***@example.com` |

```json
[
  {
    "email": "user@example.com",
    "password": "your_password"
  },
  {
    "email": "another@example.com",
    "password": "another_password"
  }
]
```

#### 可选：自定义账号名称

两个平台都不需要 `name`，省略时会自动生成显示名。只有当你想让通知里显示自定义名称时才加上它：

```json
[
  {
    "name": "主力号",
    "email": "user@example.com",
    "password": "your_password"
  }
]
```

以上 JSON 在 GitHub Secret 中可以换行排版；本地 `.env` 文件有额外限制，见[环境变量配置](#环境变量配置)。

接下来获取 AnyRouter 的 cookies 与 api_user 的值。

通过 F12 工具，切到 Application 面板，拿到 session 的值，最好重新登录下，该值 1 个月有效期，但有可能提前失效，失效后报 401 错误，到时请再重新获取。

![获取 cookies](./assets/request-session.png)

通过 F12 工具，切到 Network 面板，可以过滤下，只要 Fetch/XHR，找到带 `New-Api-User`，这个值正常是 5 位数，如果是负数或者个位数，正常是未登录。

![获取 api_user](./assets/request-api-user.png)

### 5. 启用 GitHub Actions

1. 在你的仓库中，点击 "Actions" 选项卡
2. 如果提示启用 Actions，请点击启用
3. 找到 "Router 自动签到" workflow
4. 点击 "Enable workflow"

### 6. 测试运行

你可以手动触发一次签到来测试：

1. 在 "Actions" 选项卡中，点击 "Router 自动签到"
2. 点击 "Run workflow" 按钮
3. 确认运行

![运行结果](./assets/check-in.png)

## 执行时间

- 脚本每 6 小时执行一次（UTC 0:00, 6:00, 12:00, 18:00 → 北京时间 08:00, 14:00, 20:00, 02:00）
- 你也可以随时手动触发签到

## 注意事项

- AnyRouter 请确保 cookies 与 API User 正确；AgentRouter 请确保邮箱与密码正确
- 可以在 Actions 页面查看详细的运行日志
- 支持部分账号失败，只要有账号成功签到，整个任务就不会失败
- `WAF_CACHE_TTL` 默认为 2 小时；在 GitHub 托管 Runner 中，`.waf_cache.json` 通常不会跨定时任务保留，因此主要收益是单次任务内复用，不影响功能正确性
- 报 401 错误，请重新获取 cookies，理论 1 个月失效，但有 Bug，详见 [#6](https://github.com/millylee/anyrouter-check-in/issues/6)
- 请求 200，但出现 Error 1040（08004）：Too many connections，官方数据库问题，目前已修复，但遇到几次了，详见 [#7](https://github.com/millylee/anyrouter-check-in/issues/7)

## 开启通知

脚本支持多种通知方式，可以通过配置以下环境变量开启。如果 `webhook` 有安全设置要求，例如钉钉的自定义关键词，请填写 `Router`——推送标题固定为 `Router 自动签到结果`，填 `AnyRouter` 会被拦截。

说明：

- 失败**始终**推送，不受开关影响。
- 只保留一个 `NOTIFY_ONCE` 开关，在 GitHub Variables 里改，无需修改代码：

| `NOTIFY_ONCE` | 效果 |
| ------------- | ---- |
| `false` | 默认，每轮执行都完整推送所有账号结果，便于观察 |
| `true` | 成功结果按账号每天只推送一次；失败始终推送；已成功账号不在后续邮件重复出现 |

`NOTIFY_ONCE=true` 时，如果本轮某个平台刚签到成功，邮件会显示这个成功结果；其他尚未产生新签到奖励、且当天还没有成功推送记录的账号会标记为「未到时间」。后续另一个平台签到成功时，前面已经成功过的平台不会再次显示。GitHub Actions 会用北京时间日期缓存当天状态，第二天自动重新开始记录。

- 已移除 `PushPlus`。根据其官方文档，自 2024-08-01 起发送消息需完成实名认证，且实名认证会产生服务费，或通过付费会员完成，不再适合作为本项目默认低门槛通道。
- `息知` 仅支持文本消息。
- `Server 酱` 仍保留免费会员方案，但免费额度较低；若你推送频率不高，可以继续使用。
- `飞书 / 企业微信 / 钉钉` 走各自官方群机器人 Webhook，基础使用门槛主要是你需要有对应群和机器人配置权限。

### 邮箱通知

- `EMAIL_USER`: 发件人邮箱地址
- `EMAIL_PASS`: 发件人邮箱密码/授权码
- `EMAIL_TO`: 收件人邮箱地址

### 钉钉机器人

- `DINGDING_WEBHOOK`: 钉钉机器人的 Webhook 地址

### 飞书机器人

- `FEISHU_WEBHOOK`: 飞书机器人的 Webhook 地址

### 企业微信机器人

- `WEIXIN_WEBHOOK`: 企业微信机器人的 Webhook 地址

### 息知推送（仅文本）

- `XIZHI_KEY`: 息知的专属 Key

### Server 酱

- `SERVERPUSHKEY`: Server 酱的 SendKey

配置步骤：

1. 在仓库的 Settings -> Environments -> production -> Environment secrets 中添加上述环境变量
2. 每个通知方式都是独立的，可以只配置你需要的推送方式
3. 如果某个通知方式配置不正确或未配置，脚本会自动跳过该通知方式

## 故障排除

如果签到失败，请检查：

1. **账号配置格式是否正确** - 必须是 JSON 数组格式
2. **cookies 是否过期** - session 值通常 1 个月有效期
3. **API User 是否正确** - 正常是 5 位数字
4. **AgentRouter 邮箱或密码是否正确** - 登录失败时更新 `AGENTROUTER_ACCOUNTS`
5. **AgentRouter 系统日志是否可读** - 成功判定依赖 `/api/log/self` 的到账记录
6. **网站是否更改了签到接口** - 查看 Network 面板确认
7. **查看 Actions 运行日志** - 获取详细错误信息

### 常见错误

| 错误                 | 原因                | 解决方法               |
| -------------------- | ------------------- | ---------------------- |
| 401 Unauthorized     | cookies 过期或无效  | 重新获取 session 值    |
| WAF cookies 获取失败 | Cloudflare 验证问题 | 脚本会自动重试 3 次    |
| 今日已签到           | 24 小时内已签到过   | 无需处理，等待下次执行 |
| 未找到签到到账日志   | AgentRouter 登录成功但系统日志没有实际到账记录 | 以系统日志为准，避免误报成功 |

## 本地开发环境设置

如果你需要在本地测试或开发，请按照以下步骤设置：

```bash
# 克隆仓库
git clone https://github.com/your-username/anyrouter-check-in.git
cd anyrouter-check-in

# 使用 uv 安装依赖
uv sync --dev

# 安装 Playwright 浏览器
uv run playwright install chromium

# 按 .env.example 创建 .env 文件，配置账号信息
cp .env.example .env

# 运行签到脚本
uv run checkin.py
```

### 环境变量配置

创建 `.env` 文件（参考 `.env.example`）。

> ⚠️ **`.env` 里的 JSON 必须写成一行。** `python-dotenv` 不支持裸值跨行，多行 JSON 会被解析成错误的键值对导致启动失败。若确实需要换行，必须用单引号把整段 JSON 包起来。

```bash
# AnyRouter（可选，单行 JSON）
ANYROUTER_ACCOUNTS=[{"cookies":{"session":"your_session_value"},"api_user":"your_api_user_id"}]

# AgentRouter（可选，单行 JSON）
AGENTROUTER_ACCOUNTS=[{"email":"user@example.com","password":"your_password"}]

# 通知去重：false（默认）每轮完整推送；true 时成功结果每天只推送一次
NOTIFY_ONCE=false

# 通知配置（可选）
EMAIL_USER=your_email@example.com
EMAIL_PASS=your_password_or_app_token
EMAIL_TO=recipient@example.com
XIZHI_KEY=your_xizhi_key
SERVERPUSHKEY=your_server_pushkey
```

多账号时在同一行的数组里继续追加对象即可。如果偏好换行排版，用单引号包裹：

```bash
AGENTROUTER_ACCOUNTS='[
  {"email": "user@example.com", "password": "your_password"},
  {"email": "another@example.com", "password": "another_password"}
]'
```

## 测试

```bash
# 安装依赖
uv sync --dev

# 安装 Playwright 浏览器
uv run playwright install chromium

# 运行测试
uv run pytest tests/

# 运行测试并显示覆盖率
uv run pytest tests/ --cov=. --cov-report=term-missing
```

## 更新日志

### v2.0.0 (2026-08-21)

- ✨ 新增 AgentRouter 邮箱 + 密码多账号签到，与 AnyRouter 可单独或同时启用
- ✨ AgentRouter 使用 `type=4` 系统日志核验实际到账，规避站点重复提示导致的误报
- ✨ AnyRouter 与 AgentRouter 共用 GitHub Actions、通知统计与新版 HTML 邮件 UI
- 🔧 通知配置收敛为单一 `NOTIFY_ONCE` 开关，支持每天按账号去重并隐藏已成功平台
- 🐛 修复 AgentRouter 单账号异常会打掉整轮签到并导致完全不发通知的问题
- 🐛 修复 `name` 写成非字符串时抛 `AttributeError`，以及日志把自定义名称当邮箱打印的问题
- 🔧 依赖升级并锁定（playwright 1.62.0、httpx 0.28.1、python-dotenv 1.2.3、ruff 0.16.3 等），修复 `h2`/`idna`/`python-dotenv` 已知漏洞
- 🔧 Actions 固定到 commit SHA，生产任务改用 `uv sync --locked --no-dev`
- 📝 补全双平台配置字段说明，修正 `.env` 单行 JSON 要求与钉钉关键词

### v1.1.0 (2025-01-11)

- ✨ 新增 WAF cookies 缓存机制（2 小时有效期，减少浏览器启动开销）
- ✨ 新增 HTML 格式邮件通知，美化签到结果展示
- ✨ 添加北京时区支持，完善日志中文化
- 🐛 修复签到判断逻辑，优化执行频率和通知策略
- 🐛 修复邮件通知误报失败问题
- 🔧 更新依赖版本（playwright 1.56.0, ruff 0.14.0）
- 🔧 修复测试文件中的 mock 问题（requests → httpx）

### v1.0.0

- 初始版本，支持多账号自动签到
- 支持 Cloudflare WAF 绕过
- 支持多种通知方式

## 免责声明

本脚本仅用于学习和研究目的，使用前请确保遵守相关网站的使用条款。

## 开源协议

[MIT License](./LICENSE)
