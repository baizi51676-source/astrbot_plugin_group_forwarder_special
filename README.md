# 跨群消息转发插件·特殊版 (astrbot_plugin_group_forwarder_special)

> ## ⚠️ 维护模式公告（重要）
>
> **本插件已停止功能开发，进入维护模式**（仅修复问题，不再新增功能）。
>
> **跨对话查看消息与归档搜索功能已整合到 [`astrbot_plugin_napcat_history_exporter`](https://github.com/baizi51676-source/astrbot_plugin_napcat_history_exporter)（v1.4.0+）**，新用户请直接使用导出器插件的 `get_group_message_history` / `search_archived_messages` / `list_archived_groups` 工具。
>
> **本特殊版插件仅支持 napcat 历史消息导出器 v1.3.3**（联动兼容性以 v1.3.3 为限；v1.4.0 起导出器不再提供联动机制，两者不再互通）。

> ⭐ **特殊版 = 原版全部功能 + 跨对话查看消息（联动日志归档插件）**

给 LLM 提供工具，使其可以在一个聊天中，向其他 QQ 群（**按群号/群 UID 指定**）发送文字/图片/语音/文件/合并转发消息、向指定 QQ 用户发送私聊消息，并**跨对话查看**各群的聊天记录。
专为 **NapCat + AstrBot**（aiocqhttp / OneBot v11 适配器）设计。

## 📦 与原版的关系

本插件是 [astrbot_plugin_cross_group_forwarder](https://github.com/baizi51676-source/astrbot_plugin_cross_group_forwarder) 的**特殊增强版**：

| 能力 | 原版 | 特殊版 |
|---|---|---|
| 跨群发送（文字/图片/语音/文件/合并转发） | ✅ | ✅ |
| 私聊发送 | ✅ | ✅ |
| 获取群列表 `get_group_list` | ✅ | ✅ |
| 白名单 / 审计 / WebUI 配置 | ✅ | ✅ |
| **跨对话查看消息** | ❌ | ✅ **新增** |
| **联动日志归档插件** | ❌ | ✅ **新增** |

## 🔗 插件联动说明

**跨对话查看消息**依赖联动插件 **astrbot_plugin_group_log_archive**（群聊日志归档）：

- 联动插件仓库：https://github.com/Fangnai-byte/astrbot_plugin_group_log_archive
- 它负责：读取 AstrBot 文件日志（需 DEBUG），按天/按群导出为纯文本归档（默认 `data/workspaces/group_logs/astrbot_<群号>_YYYY-MM-DD.log`）
- 本插件负责：读取归档文件，解析出时间/昵称/内容，为 LLM 提供查询工具

**使用前提**（两个插件需同时启用）：
1. 安装并启用 `astrbot_plugin_group_log_archive`，确认其能正常产出归档文件
2. 本插件 WebUI 配置项 `log_dir` 与归档插件的 `output_dir` 保持一致（默认均为 `data/workspaces/group_logs`）
3. 归档插件需持续运行积累消息后，本插件才能查到历史记录

> **💡 日志源模式说明**（对应归档插件的 `log_source` 配置）：
> - **推荐：`group_chat_context` 模式**（含群号，按群归档）——在归档插件配置中开启 `auto_enable_debug`（或手动设置 AstrBot `log_level=DEBUG` + 文件日志），重启 AstrBot 生效。归档文件名为 `astrbot_<群号>_YYYY-MM-DD.log`，可按群精确查询。
> - **兜底：`event_bus` 模式**（INFO 即可，无群号）——未开 DEBUG 时自动使用，归档为 `astrbot_unknown_YYYY-MM-DD.log`。本插件仍可读取（按时间倒序），但无法按群过滤；`list_archived_groups` 会显示 `unknown` 并提示开启 DEBUG。

## 功能

### LLM 工具（11 个）

| 工具 | 功能 |
|---|---|
| `send_message_to_group` | 按**群号**向指定 QQ 群发送文本消息 |
| `send_image_to_group` | 按**群号**向指定群发送**图片**（可带文字） |
| `send_voice_to_group` | 按**群号**向指定群发送**语音**（可带文字） |
| `send_file_to_group` | 按**群号**向指定群发送**文件**（可带文字） |
| `send_forward_to_group` | 按**群号**发送**合并转发**（多条消息打包成转发卡片） |
| `send_private_message` | 向指定 **QQ 用户**发送私聊文本消息 |
| `send_private_image` | 向指定 **QQ 用户**发送私聊图片（可带文字） |
| `get_group_list` | 获取机器人加入的所有群（群号 + 群名） |
| `get_group_message_history` | 🆕 **跨对话查看**：读取某群最近 N 条历史消息（联动日志归档/历史导出插件） |
| `search_archived_messages` | 🆕 **归档搜索**：按日期 / QQ UID / QQ 名称 / 关键词**纯程序过滤**搜索消息（联动日志归档/历史导出插件） |
| `list_archived_groups` | 🆕 列出已归档聊天记录的群号（联动日志归档/历史导出插件） |

### 权限控制

- 默认仅管理员可用（`admin_only = True`）
- 设置为 `False` 时，`allowed_user_ids` 名单中的用户也可使用

## 跨对话查看消息

```
用户A（群1）: 看看群 987654321 最近都在聊什么
LLM: 群 987654321 最近 20 条消息:
     [14:02:11] 张三: 今晚不回家吃饭了
     [14:03:30] 李四: 那明天中午聚餐？
     ...
```

LLM 可先调用 `list_archived_groups` 知道哪些群有归档，再调用 `get_group_message_history(group_id, count)` 查看内容，并结合"可靠记忆"长期掌握各群动态。

### 归档搜索（纯程序过滤）

`search_archived_messages(group_id, keyword, date, user_id, nickname, count)` 在插件内直接对归档文件做**程序化过滤**（不经过 LLM 推理搜索），支持条件**组合**使用：

| 参数 | 说明 |
|---|---|
| `group_id` | 目标群号（必填） |
| `keyword` | 消息内容关键词（子串匹配，不区分大小写） |
| `date` | 指定日期 `YYYY-MM-DD`（只搜该天归档） |
| `user_id` | QQ UID 精确匹配（仅当日志归档含 QQ 号时有效） |
| `nickname` | QQ 名称子串匹配 |
| `count` | 返回条数上限（默认 20，最大 100） |

```
用户: 上周群里关于"周末聚餐"都聊了什么？
LLM: 调用 search_archived_messages(group_id="987654321", keyword="聚餐", count=20)
     → 群 987654321 搜索到 3 条消息（关键词[聚餐]）:
        [08-23 12:01] 张三: 这周聚餐还是老地方吗
        ...
```

> 💡 搜索基于归档文件内容（昵称 + 消息文本）。QQ UID 过滤需要归档包含 QQ 号（`event_bus` 源格式或 **NapCat 导出器的 JSONL**）；若归档为 `group_chat_context` 源（无 QQ 号行内），该条件将不命中任何行，请改用昵称/关键词/日期过滤。

## 联动数据源（三类联动模式，自动识别）

本插件通过配置项 `link_mode` 选择数据源，默认 **auto**（同时扫描两个目录，自动混合解析）：

| 模式 | 值 | 扫描目录 | 适用场景 |
|---|---|---|---|
| 🏷️ Auto（默认）| `auto` | `log_dir` + `export_dir` | 两个插件都用，自动合并 |
| 📦 NapCat 导出器 | `napcat` | 仅 `export_dir` | 只用 `astrbot_plugin_napcat_history_exporter` |
| 📜 日志归档 | `log_archive` | 仅 `log_dir` | 只用 `astrbot_plugin_group_log_archive` |

默认目录结构（均位于 AstrBot 工作目录的 `data/workspaces/` 下）：

```
data/workspaces/
├── group_logs/      ← 日志归档插件（astrbot_plugin_group_log_archive）：astrbot_<群号>_*.log
└── napcat_exports/  ← 历史导出插件（astrbot_plugin_napcat_history_exporter）：napcat_<群号>_*.jsonl
```

| 数据源 | 插件 | 文件 | 说明 |
|---|---|---|---|
| 日志归档 | `astrbot_plugin_group_log_archive` | `astrbot_<群号>_YYYY-MM-DD.log` | 依赖 AstrBot DEBUG 日志，含群号；`event_bus` 源时为 `unknown` |
| 历史导出 | `astrbot_plugin_napcat_history_exporter` | `napcat_<群号>_YYYY-MM-DD.jsonl` | NapCat 扩展 API 直接导出，含 **QQ UID**，支持增量（推荐搭配） |

> 💡 推荐方案：用 `astrbot_plugin_napcat_history_exporter`（auto 模式每 120s 增量导出，输出到 `data/workspaces/napcat_exports`）作为主要数据源，日志归档插件作为补充/兜底；两边配置保持默认即可互通。

> ⚠️ 查看范围受联动插件归档内容限制（仅群聊、需先积累归档、脱敏配置会影响内容）。

## 安全配置（WebUI 可视化）

所有配置项均可直接在 **AstrBot WebUI → 插件管理 → 跨群消息转发（特殊版）→ 配置** 中修改，无需编辑代码：

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `log_dir` | string | `data/workspaces/group_logs` | 🆕 日志归档目录（与联动插件 `output_dir` 一致） |
| `export_dir` | string | `data/workspaces/napcat_exports` | 🆕 NapCat 历史导出目录（与导出插件 `export_dir` 一致） |
| `link_mode` | string | `auto` | 🆕 联动模式：`auto` 双源 / `napcat` 仅导出器 / `log_archive` 仅日志归档 |
| `admin_only` | bool | `true` | 仅 AstrBot 管理员可用 |
| `allowed_user_ids` | list | `[]` | 允许使用的用户 QQ 号（`admin_only=false` 时生效） |
| `allowed_groups` | list | `[]` | 目标群白名单：仅允许向这些群发送消息，留空不限制 |

- **目标群白名单**：防止 LLM 被 prompt 注入诱导向任意群发送垃圾消息
- **审计日志**：所有发送操作（时间/工具/目标/内容摘要/成败）记录在
  `AstrBot/data/plugins/group_forwarder_special/audit.log`（JSON Lines），便于事后追查

## 实现原理

- 底层使用 OneBot v11 原生 API（`send_group_msg` / `send_private_msg` / `send_forward_msg`）
- 通过 `event.bot`（aiocqhttp 的 CQHttp 客户端实例）直接调用，NapCat 原生支持
- 因此**不依赖** unified_msg_origin，可以直接指定任意群 UID / 用户 QQ 发送
- 历史消息：读取联动插件 `astrbot_plugin_group_log_archive` 的按天/按群纯文本归档，正则解析群号/昵称/时间/内容

## 安装

1. 克隆本仓库到 `AstrBot/data/plugins/` 目录
2. 同时安装联动插件：https://github.com/Fangnai-byte/astrbot_plugin_group_log_archive
3. 在 AstrBot WebUI 的插件管理页中启用两个插件（或重启 AstrBot）
4. 完成！现在可以跨群发送 + 跨对话查看消息

## 使用示例

```
用户A（群1）: 帮我给群 987654321 发条消息：今晚不回家吃饭了
LLM: ✅ 已成功向群 987654321 发送消息

用户A: 看看工作群（123456789）昨天聊了什么
LLM: 群 123456789 最近 20 条消息:
     [2026-08-22 17:20:05] 王五: 明早 10 点开会
     ...
```

## 注意事项

- 本插件依赖 `event.bot`（aiocqhttp 适配器），仅适用于 NapCat / OneBot v11 接入方式
- 机器人需要先加入目标群才能向其发送消息；私聊需对方未拒绝接收机器人消息
- 合并转发依赖 NapCat 的 `send_forward_msg` 扩展 API
- 跨对话查看依赖联动插件 `astrbot_plugin_group_log_archive` 的归档产物，请保证其正常运行
- 请谨慎授权使用，防止 LLM 被诱导向任意群发送垃圾消息