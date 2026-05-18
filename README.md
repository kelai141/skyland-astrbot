# 🌠 森空岛自动签到 AstrBot 插件

[![AstrBot](https://img.shields.io/badge/AstrBot-v4.23+-blue)](https://github.com/AstrBotDevs/AstrBot)

**纯聊天交互，无需 WebUI 配置** — 在聊天软件中完成绑定、签到、查看状态等所有操作。

## ✨ 功能

- ✅ **纯聊天交互** — 所有操作通过聊天指令完成，无需打开 WebUI
- ✅ **多用户管理** — 每个用户独立绑定自己的鹰角通行证
- ✅ **每日自动签到** — 每天 09:05 自动签到，结果推送到用户聊天
- ✅ **手动签到** — 随时发送 `/skland sign` 立即签到
- ✅ **手机号登录** — 支持通过手机号+验证码直接绑定（无需浏览器）
- ✅ **Token 绑定** — 也支持从森空岛网页获取 token 绑定
- ✅ **多游戏支持** — 支持明日方舟（Arknights）和终末地（Endfield）签到
- ✅ **管理员管理** — 查看/移除用户、群发消息
- ✅ **状态查询** — 随时查看签到状态和记录

## 📋 指令列表

| 指令 | 说明 | 权限 |
|------|------|:----:|
| `/skland help` | 显示帮助信息 | 所有人 |
| `/skland bind <token>` | 绑定鹰角通行证 token | 所有人 |
| `/skland login` | 通过手机号+验证码登录绑定 | 所有人 |
| `/skland sign` | 立即手动签到 | 所有人 |
| `/skland status` | 查看我的签到状态 | 所有人 |
| `/skland unbind` | 解绑账号 | 所有人 |
| `/skland did` | 查看设备指纹状态 | 所有人 |
| `/skland list` | 查看所有已绑定用户 | 管理员 |
| `/skland remove <id>` | 移除指定用户的绑定 | 管理员 |
| `/skland broadcast <msg>` | 向所有用户群发消息 | 管理员 |

## 🔧 安装

在 AstrBot 中使用以下命令安装：

```
plugin i https://github.com/kelai141/skyland-astrbot
```

或手动将插件目录放入 `data/plugins/` 后重载插件。

## 🚀 使用指南

### 方式一：Token 绑定（推荐）

1. 打开 [森空岛官网](https://www.skland.com) 并登录
2. 按 F12 打开开发者工具 → 控制台（Console）
3. 粘贴以下代码获取 token：
   ```javascript
   copy(JSON.parse(localStorage.getItem('userInfo')).token)
   ```
4. 在聊天中发送：
   ```
   /skland bind 你复制的token内容
   ```

### 方式二：手机号登录

发送 `/skland login`，然后按提示输入手机号和验证码即可。

### 签到

- **自动签到**：绑定后每天 09:05 自动签到，结果推送到你的聊天
- **手动签到**：发送 `/skland sign` 立即签到

## 📦 项目结构

```
astrbot_plugin_skyland/
├── metadata.yaml         # 插件元数据
├── main.py               # 插件主入口（指令 + 定时任务）
├── requirements.txt      # 依赖声明
├── lib/
│   ├── __init__.py
│   ├── skyland.py        # 签到核心逻辑（移植）
│   └── security.py       # 设备指纹 dId 生成（移植）
└── README.md
```

## 🔄 数据存储

用户数据保存在 `data/plugin_data/astrbot_plugin_skyland/users.json`，无需手动编辑。

## 📝 注意事项

- token 的有效期较长，但若遇到签到失败提示"用户未登录"，请重新绑定
- 各用户之间签到间隔 2 秒，防止 API 限流
- 首次使用会自动获取 dId（设备指纹），该值会缓存

## 🙏 致谢

- 签到核心逻辑移植自 [FancyCabbage/skyland-auto-sign](https://gitee.com/FancyCabbage/skyland-auto-sign)
- [AstrBot](https://github.com/AstrBotDevs/AstrBot) 插件框架

## 📝 变更日志

### v1.2.0 (2026-05-18)
- 📋 **日志规范化**：全部切换为 `astrbot.api.logger`，日志直接输出到 AstrBot 后台
- 🔍 **全链路详细日志**：`verify_token` → `get_cred_by_token` → `get_binding_list` 每步均有 INFO 日志
- 🐛 **改进异常消息**：失败时附带 `code`、异常类型、原始响应摘要，方便后台排查
- 🛡️ **JSON 解析保护**：`api_get`/`api_post` 对非 JSON 响应做保护性处理

### v1.1.0 (2026-05-18)
- 🔧 **修复**：签名请求头补全 `dId` 字段，与原始 skyland-auto-sign 完全一致
- 🔧 **修复**：`/skland broadcast` 命令正确提取消息内容
- 🔧 **修复**：终末地签到显式添加 `dId` 头

### v1.0.0
- 初始版本: 纯聊天交互的森空岛自动签到插件
