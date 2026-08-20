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
- ✅ 紫蓝工作台风格 HTML 邮件通知，展示平台、账号与统计结果
- ✅ 失败始终通知，成功通知可通过 GitHub Variable 控制
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

AgentRouter 直接配置登录邮箱和密码，不需要手动提取 Cookie：

1. **Email**: AgentRouter 登录邮箱
2. **Password**: AgentRouter 登录密码

脚本使用 Playwright 操作真实登录表单。AgentRouter 当前存在重复登录仍提示“签到成功、额度到账”的站点问题，因此脚本不会信任页面 toast 或登录响应的 `checked_in` 字段，而是登录后查询 `type=4` 的系统日志，仅当本次登录产生“每日签到成功，增加额度”记录时才计为成功；当天已有更早记录时计为“今日已签”。

### 3. 设置 GitHub Environment Secret

1. 在你 fork 的仓库中，点击 "Settings" 选项卡
2. 在左侧菜单中找到 "Environments" -> "New environment"
3. 新建一个名为 `production` 的环境
4. 点击新建的 `production` 环境进入环境配置页
5. 点击 "Add environment secret"，按需创建：
   - `ANYROUTER_ACCOUNTS`: AnyRouter 多账号 JSON
   - `AGENTROUTER_ACCOUNTS`: AgentRouter 多账号 JSON
6. 在 Environment variables 中添加 `NOTIFY_ON_SUCCESS`：
   - `false`（默认）：仅有失败账号时通知
   - `true`：成功、今日已签或失败都通知

### 4. 多账号配置格式

两个 Secret 都是可选的，但至少需要配置一个。两个同时存在时会在同一次任务中依次执行并合并通知。

#### `ANYROUTER_ACCOUNTS`

```json
[
  {
    "name": "AnyRouter 主账号",
    "cookies": {
      "session": "account1_session_value"
    },
    "api_user": "account1_api_user_id"
  }
]
```

#### `AGENTROUTER_ACCOUNTS`

```json
[
  {
    "name": "AgentRouter 主账号",
    "email": "user@example.com",
    "password": "your_password"
  },
  {
    "name": "AgentRouter 备用账号",
    "email": "another@example.com",
    "password": "another_password"
  }
]
```

接下来获取 cookies 与 api_user 的值。

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

## 配置示例

下面是两个平台同时启用时的 Secret 结构；它们需要分别保存，不能合并成同一个 JSON。

`ANYROUTER_ACCOUNTS`：

```json
[
  {
    "name": "AnyRouter 主账号",
    "cookies": {
      "session": "abc123session"
    },
    "api_user": "user123"
  }
]
```

`AGENTROUTER_ACCOUNTS`：

```json
[
  {
    "name": "AgentRouter 主账号",
    "email": "user@example.com",
    "password": "your_password"
  }
]
```

## 开启通知

脚本支持多种通知方式，可以通过配置以下环境变量开启，如果 `webhook` 有要求安全设置，例如钉钉，可以在新建机器人时选择自定义关键词，填写 `AnyRouter`。

说明：

- `NOTIFY_ON_SUCCESS=false` 时，成功和“今日已签”不会推送；任何失败仍会强制推送。
- `NOTIFY_ON_SUCCESS=true` 时，每次定时任务都会按实际结果推送。
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

创建 `.env` 文件（参考 `.env.example`）：

```bash
# AnyRouter（可选）
ANYROUTER_ACCOUNTS=[
  {
	"name": "AnyRouter 主账号",
    "cookies": {"session": "your_session_value"},
    "api_user": "your_api_user_id"
  }
]

# AgentRouter（可选）
AGENTROUTER_ACCOUNTS=[
  {
	"name": "AgentRouter 主账号",
	"email": "user@example.com",
	"password": "your_password"
  }
]

# false：仅失败通知；true：所有结果通知
NOTIFY_ON_SUCCESS=false

# 通知配置（可选）
EMAIL_USER=your_email@example.com
EMAIL_PASS=your_password_or_app_token
EMAIL_TO=recipient@example.com
XIZHI_KEY=your_xizhi_key
SERVERPUSHKEY=your_server_pushkey
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

### 双平台版本

- 新增 AgentRouter 邮箱 + 密码多账号签到
- AgentRouter 使用 `type=4` 系统日志核验实际签到到账，规避重复登录误报
- AnyRouter 与 AgentRouter 共用 GitHub Actions、通知统计与新版 HTML UI
- 新增 `NOTIFY_ON_SUCCESS` GitHub Variable，无需修改代码即可切换成功通知

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
