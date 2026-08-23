import json
import os
import re
from datetime import datetime
from pathlib import Path

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star


class GroupForwarderSpecial(Star):
    """跨群消息转发插件·特殊版（NapCat / OneBot v11 / aiocqhttp 适配）。

    包含原版全部功能：向其他 QQ 群（按群号）发送文字、图片、语音、文件及
    合并转发消息，也可向指定 QQ 用户发送私聊消息。

    特殊版新增：跨对话查看消息——联动群聊日志归档插件
    astrbot_plugin_group_log_archive（https://github.com/Fangnai-byte/astrbot_plugin_group_log_archive），
    读取其按天归档的群聊日志（默认 data/workspaces/group_logs/），
    为 LLM 提供按群号查看历史消息的工具。

    底层使用 OneBot v11 原生 API（send_group_msg / send_private_msg /
    send_forward_msg 等）。

    设计说明：
    - 不维护"会话注册表"：群名 → 群号映射由 LLM 的可靠记忆持有，
      可先调用 get_group_list 获取群号与群名建立记忆，再按群号调用发送工具。
    - 定时发送需求由 AstrBot 自带的 future_task 内置工具实现。
    - 内置目标群白名单与操作审计日志，防止滥用。
    """

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 持久化数据存到 AstrBot 的 data 目录（官方规范：防止更新插件时数据被覆盖）
        self.data_dir = Path("data/plugins/group_forwarder_special")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = self.data_dir / "audit.log"

        # 以下配置项均可通过 AstrBot WebUI 插件管理页配置（见 _conf_schema.json）：
        # - admin_only: 仅管理员可用（默认 True）
        # - allowed_user_ids: 允许使用的用户 QQ 号列表（admin_only=False 时生效）
        # - allowed_groups: 目标群白名单，留空表示不限制
        self.admin_only = bool(config.get("admin_only", True))
        self.allowed_user_ids: set[str] = {
            str(x) for x in config.get("allowed_user_ids", [])
        }
        self.allowed_groups: list[str] = [
            str(x) for x in config.get("allowed_groups", [])
        ]
        # 联动：群聊日志归档目录（astrbot_plugin_group_log_archive 的输出目录）
        self.log_dir = str(config.get("log_dir", "data/workspaces/group_logs")) \
            or "data/workspaces/group_logs"
        # 联动：NapCat 历史导出目录（astrbot_plugin_napcat_history_exporter 的 export_dir）
        self.export_dir = str(config.get("export_dir", "data/workspaces/napcat_exports")) \
            or "data/workspaces/napcat_exports"
        # 联动模式：auto（默认，双源自动扫描）/ napcat（仅历史导出器）/ log_archive（仅日志归档）
        self.link_mode = str(config.get("link_mode", "auto")).strip().lower()
        if self.link_mode not in ("auto", "napcat", "log_archive"):
            self.link_mode = "auto"

    # ---------------------------------------------------------------
    # 内部工具方法
    # ---------------------------------------------------------------

    def _is_allowed(self, event: AstrMessageEvent) -> bool:
        """权限校验：管理员，或（当 admin_only=False 时）在允许名单中。"""
        if self.admin_only:
            return event.is_admin()
        return event.get_sender_id() in self.allowed_user_ids

    def _get_bot(self, event: AstrMessageEvent):
        """获取 aiocqhttp (OneBot v11/NapCat) 客户端实例。

        AiocqhttpMessageEvent 上有 bot 属性（CQHttp 实例），
        可直接调用 OneBot v11 原生 API，如 send_group_msg。
        """
        return getattr(event, "bot", None)

    def _group_allowed(self, group_id: str) -> bool:
        """目标群白名单校验：白名单为空时不限制。"""
        return not self.allowed_groups or group_id.strip() in self.allowed_groups

    def _audit(self, tool: str, target: str, content: str,
               success: bool, error: str = ""):
        """追加一条审计日志（JSON Lines）。"""
        try:
            entry = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "target": target,
                "content": content[:100],
                "success": success,
                "error": error[:200],
            }
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"审计日志写入失败: {e}")

    def _build_message(self, message: str = "", image_url: str = "",
                       caption: str = "") -> list:
        """构造 OneBot v11 消息段列表（文字 + 图片）。"""
        segments = []
        text = caption.strip() or message.strip()
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        if image_url.strip():
            segments.append({"type": "image", "data": {"file": image_url.strip()}})
        if not segments:
            segments.append({"type": "text", "data": {"text": "(空消息)"}})
        return segments

    def _build_media_message(self, media_kind: str, media_url: str,
                             caption: str = "") -> list:
        """构造媒体消息段（record/file），可带文字说明。media_kind: record|file"""
        segments = []
        text = caption.strip()
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        segments.append({"type": media_kind, "data": {"file": media_url.strip()}})
        return segments

    def _validate_media_url(self, media_url: str) -> str | None:
        """校验媒体地址（图片/语音/文件通用），返回错误信息；合法返回 None。"""
        url = media_url.strip()
        if not (url.startswith("http://") or url.startswith("https://")
                or url.startswith("file://") or url.startswith("/")):
            return ("❌ 地址格式错误：请提供 http(s):// 网络 URL、"
                    "file:// 路径或本地文件路径。")
        return None

    def _parse_archive_line(self, line: str) -> dict | None:
        """解析群聊记录归档行，支持三类格式：

        1) JSONL（astrbot_plugin_napcat_history_exporter 导出）:
           {"t":"2026-08-23 12:00:00","chat":"group","group_id":"123456789",
            "user_id":"987654321","nickname":"张三","seq":12345,"content":"..."}
        2) group_chat_context 日志行（astrbot_plugin_group_log_archive，需 DEBUG，含群号）:
           [2026-08-22 01:33:55.519] [Plug] [DBUG] [astrbot.group_chat_context:158]: \
group_chat_context | pre-config:GroupMessage:123456789 | [昵称/01:33:55]: 内容
        3) event_bus 日志行（同上插件，INFO 即可，无群号）:
           [2026-08-22 23:59:29.133] [Core] [INFO] [core.event_bus:74]: [default] [账号1(aiocqhttp)] 昵称/QQ: 内容

        返回: {"time","group_id","nickname","msg_time","content","user_id"}；
        解析失败返回 None。event_bus 行无群号，group_id 固定为 "unknown"。
        """
        line = line.strip()
        if not line:
            return None
        # 格式 1：JSONL（NapCat 历史导出器）
        if line.startswith("{"):
            try:
                d = json.loads(line)
                t = d.get("t", "")
                return {
                    "time": t,
                    "group_id": str(d.get("group_id", "")),
                    "nickname": d.get("nickname", ""),
                    "msg_time": t[11:19] if len(t) >= 19 else t,
                    "content": d.get("content", ""),
                    "user_id": str(d.get("user_id", "") or ""),
                }
            except (json.JSONDecodeError, ValueError):
                pass
        # 格式 2：group_chat_context（含群号）
        m = re.match(
            r"^\[\d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2}:\d{2})\.\d+\] "
            r"\[Plug\] \[DBUG\] \[astrbot\.group_chat_context:\d+\]: "
            r"group_chat_context \| pre-config:GroupMessage:(?P<group_id>\d+) \| "
            r"\[(?P<nickname>[^\]]+)/(?P<msg_time>[\d:]+)\]: ?(?P<content>.*)$",
            line,
        )
        if m:
            d = m.groupdict()
            return {
                "time": d["time"],
                "group_id": d["group_id"],
                "nickname": d["nickname"].strip(),
                "msg_time": d["msg_time"],
                "content": d["content"].strip(),
                "user_id": None,  # group_chat_context 行不含 QQ 号
            }
        # 格式 3：event_bus（无群号，兼容 unknown 归档）
        m2 = re.match(
            r"^\[\d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2}:\d{2})\.\d+\] "
            r"\[Core\] \[INFO\] \[core\.event_bus:\d+\]: \[default\] "
            r"\[(?P<account>[^\]]+)\] (?P<nickname>.+?)/(?P<user_id>\d+): ?"
            r"(?P<content>.*)$",
            line,
        )
        if m2:
            d = m2.groupdict()
            return {
                "time": d["time"],
                "group_id": "unknown",
                "nickname": d["nickname"].strip(),
                "msg_time": d["time"],
                "content": d["content"].strip(),
                "user_id": d["user_id"],
            }
        return None

    def _archive_dirs(self) -> list:
        """按联动模式（link_mode）返回归档数据源目录。

        - auto:        同时扫描日志归档 + NapCat 历史导出两个目录（默认）
        - napcat:      仅扫描 NapCat 历史导出目录（export_dir）
        - log_archive: 仅扫描日志归档目录（log_dir）
        """
        if self.link_mode == "napcat":
            candidates = (self.export_dir,)
        elif self.link_mode == "log_archive":
            candidates = (self.log_dir,)
        else:  # auto
            candidates = (self.log_dir, self.export_dir)
        dirs = []
        for d in candidates:
            if d and Path(d).is_dir():
                dirs.append(Path(d))
        return dirs

    def _group_archive_files(self, group_id: str) -> list:
        """返回某群所有归档文件的路径，按日期倒序（新 → 旧）。

        同时匹配两类联动文件（分布在两个数据源目录）：
        - astrbot_<群号>_YYYY-MM-DD.log（astrbot_plugin_group_log_archive）
        - napcat_<群号>_YYYY-MM-DD.jsonl（旧版导出器，按天分文件）
        - napcat_<群号>.jsonl（新版导出器 v1.1.0+，单文件合并）
        """
        gid = group_id.strip()
        prefixes = (f"astrbot_{gid}_", f"napcat_{gid}_", f"napcat_{gid}.jsonl")
        files: list = []
        for log_dir in self._archive_dirs():
            files += [p for p in log_dir.iterdir()
                      if p.is_file()
                      and p.name.startswith(prefixes)
                      and (p.name.endswith(".log") or p.name.endswith(".jsonl"))]
        files.sort(key=lambda p: p.name, reverse=True)
        return files

    def _read_group_history(self, group_id: str, count: int) -> list:
        """读取某群最近 count 条归档消息（跨天翻文件），返回格式化文本行。"""
        all_msgs: list[dict] = []
        for fpath in self._group_archive_files(group_id):
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            parsed = []
            for raw in raw_lines:
                info = self._parse_archive_line(raw)
                if info:
                    parsed.append(info)
            # 当前文件（较旧）插到已有（较新）之前，保持全局时间正序
            all_msgs = parsed + all_msgs
        return [f"[{m['msg_time']}] {m['nickname']}: {m['content']}"
                for m in all_msgs[-count:]]

    def _search_group_history(self, group_id: str, count: int = 20,
                              keyword: str | None = None,
                              date: str | None = None,
                              user_id: str | None = None,
                              nickname: str | None = None) -> list:
        """纯程序过滤搜索归档消息（不依赖 LLM），返回格式化文本行。

        过滤条件（可组合，全部满足才命中）：
          keyword : 消息内容包含该关键词（不区分大小写，子串匹配）
          date    : 仅搜索指定日期（YYYY-MM-DD）的归档文件
          user_id : QQ UID 精确匹配（仅 event_bus 格式归档含 QQ 号，其余行忽略该条件）
          nickname: 昵称包含该字符串（子串匹配）
        从新到旧扫描归档文件，收集满 count 条即返回（保证是最新命中）。
        """
        gid = group_id.strip()
        files = self._group_archive_files(gid)
        if date:
            # 旧格式按天日志文件精确匹配；jsonl 单文件（不分天）保留后行级过滤
            wanted_log = f"astrbot_{gid}_{date}.log"
            files = [p for p in files
                     if p.name == wanted_log or p.name.endswith(".jsonl")]
        kw = keyword.strip() if keyword else None
        uid = user_id.strip() if user_id else None
        nick = nickname.strip() if nickname else None
        hits: list[dict] = []
        for fpath in files:  # 已按日期倒序（新 → 旧）
            try:
                with open(fpath, encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
            except Exception as e:
                logger.error(f"读取归档文件失败 {fpath}: {e}")
                continue
            for raw in raw_lines:
                info = self._parse_archive_line(raw)
                if not info:
                    continue
                # 行级日期过滤：JSONL 行 time 含日期；日志行 time 只有时间，
                # 由文件级过滤（文件名含日期）保证
                if date:
                    _full_t = str(info.get("time", ""))
                    if len(_full_t) >= 10 and not _full_t.startswith(date):
                        continue
                if kw and kw.lower() not in info["content"].lower():
                    continue
                if uid:
                    # group_chat_context 行无 QQ 号，无法匹配则跳过
                    if info.get("user_id") is None or info["user_id"] != uid:
                        continue
                if nick and nick.lower() not in info["nickname"].lower():
                    continue
                hits.append(info)
                if len(hits) >= count:
                    break
            if len(hits) >= count:
                break
        # hits 扫描顺序为 新→旧，反转成 旧→新 时间正序输出
        hits.reverse()
        return [f"[{m['msg_time']}] {m['nickname']}: {m['content']}"
                for m in hits]

    def _list_archived_groups(self) -> list:
        """扫描全部归档数据源目录，返回已有归档的群号列表（去重）。

        含 "unknown"（event_bus 日志源产生的未分群归档）。
        同时识别 astrbot_*.log 与 napcat_*.jsonl 两类文件。
        """
        groups = set()
        for log_dir in self._archive_dirs():
            for p in log_dir.iterdir():
                if not p.is_file() or not p.name.startswith(("astrbot_", "napcat_")):
                    continue
                # 旧格式：astrbot_<群>_YYYY-MM-DD.log / napcat_<群>_YYYY-MM-DD.jsonl
                m = re.match(
                    r"(?:astrbot|napcat)_(.+?)_\d{4}-\d{2}-\d{2}\.(?:log|jsonl)$",
                    p.name)
                if m:
                    groups.add(m.group(1))
                    continue
                # 新格式：napcat_<群>.jsonl（v1.1.0 单文件）
                m2 = re.match(r"napcat_(\d+)\.jsonl$", p.name)
                if m2:
                    groups.add(m2.group(1))
        return sorted(groups)

    async def _send_to_group(self, event: AstrMessageEvent, group_id: str,
                             message_segments: list, tool: str,
                             summary: str) -> str:
        """统一的群消息发送（含权限 / 白名单 / 审计）。"""
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        if not self._group_allowed(group_id):
            return f"❌ 群 {group_id} 不在白名单中，已拒绝发送。"
        try:
            await bot.send_group_msg(
                group_id=int(group_id.strip()), message=message_segments
            )
            logger.info(f"{tool}: 已发送到群 {group_id}")
            self._audit(tool, f"群 {group_id}", summary, True)
            return f"✅ 已成功向群 {group_id} 发送"
        except Exception as e:
            logger.error(f"{tool}: 发送失败: {e}")
            self._audit(tool, f"群 {group_id}", summary, False, str(e))
            return f"❌ 发送失败: {e}"

    async def _send_to_user(self, event: AstrMessageEvent, user_id: str,
                            message_segments: list, tool: str,
                            summary: str) -> str:
        """统一的私聊发送（含权限 / 审计）。"""
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not user_id.strip().isdigit():
            return f"❌ QQ 号格式错误：{user_id}。应为纯数字。"
        try:
            await bot.send_private_msg(
                user_id=int(user_id.strip()), message=message_segments
            )
            logger.info(f"{tool}: 已私聊发送给 {user_id}")
            self._audit(tool, f"QQ {user_id}", summary, True)
            return f"✅ 已成功向 QQ {user_id} 发送私聊消息"
        except Exception as e:
            logger.error(f"{tool}: 私聊发送失败: {e}")
            self._audit(tool, f"QQ {user_id}", summary, False, str(e))
            return f"❌ 发送失败: {e}"

    # ---------------------------------------------------------------
    # LLM 工具：群消息
    # ---------------------------------------------------------------

    @filter.llm_tool("send_message_to_group")
    async def send_message_to_group(self, event: AstrMessageEvent,
                                    group_id: str, message: str):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一条文本消息。适合通知、提醒、转发信息等场景。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          message(string): 要发送的文本内容

        返回: 发送结果的描述
        '''
        if not message.strip():
            return "❌ 消息内容不能为空。"
        return await self._send_to_group(
            event, group_id, self._build_message(message=message),
            "send_message_to_group", message.strip()[:100],
        )

    @filter.llm_tool("send_image_to_group")
    async def send_image_to_group(self, event: AstrMessageEvent,
                                  group_id: str, image_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一张图片，可附带文字说明。
        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          image_url(string): 图片地址，支持 http(s):// 网络 URL、file:// 或本机可访问的图片文件路径
          caption(string): 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(image_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_message(image_url=image_url, caption=caption),
            "send_image_to_group", f"图片 {image_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_voice_to_group")
    async def send_voice_to_group(self, event: AstrMessageEvent,
                                  group_id: str, voice_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一条语音消息，可附带文字说明。
        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          voice_url(string): 语音文件地址，支持 http(s):// 网络 URL、file:// 或本机路径，通常为 .mp3/.amr/.silk 格式
          caption(string): 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(voice_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_media_message("record", voice_url, caption),
            "send_voice_to_group", f"语音 {voice_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_file_to_group")
    async def send_file_to_group(self, event: AstrMessageEvent,
                                 group_id: str, file_url: str, caption: str = ""):
        '''
        向指定的 QQ 群（按群号/群 UID）发送一个文件，可附带文字说明。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          file_url(string): 文件地址（http(s):// 网络 URL、file:// 或本地路径）
          caption(string): 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(file_url)
        if err:
            return err
        return await self._send_to_group(
            event, group_id,
            self._build_media_message("file", file_url, caption),
            "send_file_to_group", f"文件 {file_url.strip()[:60]} {caption.strip()[:40]}",
        )

    @filter.llm_tool("send_forward_to_group")
    async def send_forward_to_group(self, event: AstrMessageEvent,
                                    group_id: str, messages):
        '''
        向指定的 QQ 群发送合并转发消息（多条消息打包为一条转发卡片）。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          messages(string): 要合并转发的多条消息内容（字符串数组，按顺序排列）

        返回: 发送结果的描述
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        if not self._group_allowed(group_id):
            return f"❌ 群 {group_id} 不在白名单中，已拒绝发送。"
        # 兼容字符串输入（按换行拆分）
        if isinstance(messages, str):
            msgs = [m for m in messages.splitlines() if m.strip()]
        else:
            msgs = [str(m) for m in messages if str(m).strip()]
        if not msgs:
            return "❌ messages 不能为空。"
        try:
            self_id = "10000"
            try:
                self_id = str(event.get_self_id() or self_id)
            except Exception:
                pass
            nodes = []
            for i, m in enumerate(msgs):
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": f"消息 {i + 1}",
                        "uin": self_id,
                        "content": [{"type": "text", "data": {"text": m}}],
                    },
                })
            resp = await bot.send_forward_msg(message=nodes)
            fid = resp.get("message_id") if isinstance(resp, dict) else str(resp)
            await bot.send_group_msg(
                group_id=int(group_id.strip()),
                message=[{"type": "forward", "data": {"id": str(fid)}}],
            )
            logger.info(f"合并转发已发送到群 {group_id}（{len(msgs)} 条）")
            self._audit("send_forward_to_group", f"群 {group_id}",
                        f"合并转发 {len(msgs)} 条", True)
            return f"✅ 已成功向群 {group_id} 发送合并转发（{len(msgs)} 条消息）"
        except Exception as e:
            logger.error(f"合并转发发送失败: {e}")
            self._audit("send_forward_to_group", f"群 {group_id}",
                        f"合并转发 {len(msgs)} 条", False, str(e))
            return f"❌ 发送失败: {e}"

    # ---------------------------------------------------------------
    # LLM 工具：私聊消息
    # ---------------------------------------------------------------

    @filter.llm_tool("send_private_message")
    async def send_private_message(self, event: AstrMessageEvent,
                                   user_id: str, message: str):
        '''
        向指定的 QQ 用户（按 QQ 号）发送一条私聊文本消息。

        Args:
          user_id(string): 目标 QQ 号（纯数字，如 "123456789"）
          message(string): 要发送的文本内容

        返回: 发送结果的描述
        '''
        if not message.strip():
            return "❌ 消息内容不能为空。"
        return await self._send_to_user(
            event, user_id, self._build_message(message=message),
            "send_private_message", message.strip()[:100],
        )

    @filter.llm_tool("send_private_image")
    async def send_private_image(self, event: AstrMessageEvent,
                                 user_id: str, image_url: str, caption: str = ""):
        '''
        向指定的 QQ 用户（按 QQ 号）发送一张图片，可附带文字说明。

        Args:
          user_id(string): 目标 QQ 号（纯数字，如 "123456789"）
          image_url(string): 图片地址（http(s):// 网络 URL、file:// 或本地路径）
          caption(string): 可选，附带发送的文字说明

        返回: 发送结果的描述
        '''
        err = self._validate_media_url(image_url)
        if err:
            return err
        return await self._send_to_user(
            event, user_id,
            self._build_message(image_url=image_url, caption=caption),
            "send_private_image", f"图片 {image_url.strip()[:60]} {caption.strip()[:40]}",
        )

    # ---------------------------------------------------------------
    # LLM 工具：查询
    # ---------------------------------------------------------------

    @filter.llm_tool("get_group_list")
    async def get_group_list(self, event: AstrMessageEvent):
        '''
        获取机器人当前加入的所有 QQ 群（群号 + 群名）。
        可用于建立"群名 → 群号"的映射记忆，之后即可按群号调用发送工具。
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群发送工具。"
        bot = self._get_bot(event)
        if bot is None:
            return "❌ 当前平台不是 aiocqhttp (OneBot v11/NapCat)，无法使用此工具。"
        try:
            groups = await bot.get_group_list()
            if not groups:
                return "机器人当前未加入任何群。"
            lines = [f"{g.get('group_id')} - {g.get('group_name', '')}" for g in groups]
            return "机器人所在的群:\n" + "\n".join(lines)
        except Exception as e:
            logger.error(f"获取群列表失败: {e}")
            return f"❌ 获取群列表失败: {e}"

    # ---------------------------------------------------------------
    # LLM 工具：跨对话查看消息（联动群聊日志归档插件）
    # ---------------------------------------------------------------

    @filter.llm_tool("get_group_message_history")
    async def get_group_message_history(self, event: AstrMessageEvent,
                                        group_id: str, count: int = 20):
        '''
        查看指定 QQ 群的历史聊天记录（读取群聊日志归档插件的归档文件）。
        适合需要了解某个群最近聊了什么、查找过往消息、回顾上下文等场景。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"）
          count(string): 需要获取的消息条数（默认 20，最大 100）

        返回: 按时间顺序排列的消息列表（时间/昵称/内容）
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群查看工具。"
        if not group_id.strip().isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        msgs = self._read_group_history(group_id.strip(), count)
        if not msgs:
            # 若存在 unknown 归档（event_bus 源），给出诊断引导
            if "unknown" in self._list_archived_groups():
                return (f"📭 群 {group_id} 暂无归档消息。检测到归档插件当前使用 "
                        f"event_bus 日志源（无群号，归档为 unknown 文件）。\n"
                        f"请在联动插件 astrbot_plugin_group_log_archive 的 WebUI 配置中"
                        f"开启 auto_enable_debug（或手动设置 AstrBot log_level=DEBUG + "
                        f"文件日志），重启 AstrBot 后即可按群归档。")
            return (f"📭 群 {group_id} 暂无归档消息。请确认已启用联动插件 "
                    f"astrbot_plugin_group_log_archive 且归档目录配置正确"
                    f"（当前配置: {self.log_dir}），并且该群有消息被归档。")
        return f"群 {group_id} 最近 {len(msgs)} 条消息:\n" + "\n".join(msgs)

    @filter.llm_tool("list_archived_groups")
    async def list_archived_groups(self, event: AstrMessageEvent):
        '''
        列出已归档了聊天记录的群号列表（由群聊日志归档插件输出）。
        可用于了解哪些群有历史消息可查看，然后调用 get_group_message_history 查看具体内容。
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群查看工具。"
        groups = self._list_archived_groups()
        if not groups:
            return (f"📭 暂无归档群聊。请确认已启用联动插件 "
                    f"astrbot_plugin_group_log_archive 且归档目录配置正确"
                    f"（当前配置: {self.log_dir}）。")
        lines = [
            "unknown（未分群，event_bus 日志源，建议开启 DEBUG 以按群归档）"
            if g == "unknown" else g
            for g in groups
        ]
        return "已有归档记录的群:\n" + "\n".join(lines)

    @filter.llm_tool("search_archived_messages")
    async def search_archived_messages(self, event: AstrMessageEvent,
                                       group_id: str, keyword: str = "",
                                       date: str = "", user_id: str = "",
                                       nickname: str = "", count: int = 20):
        '''
        在指定 QQ 群的归档聊天记录中按条件搜索消息（纯程序过滤，速度快、准确）。
        适合查找特定关键词、某天/某人的聊天内容，例如"上周群里关于XX的讨论"。

        Args:
          group_id(string): 目标 QQ 群号（纯数字，如 "123456789"；必填）
          keyword(string): 消息内容关键词（子串匹配，不区分大小写；可选）
          date(string): 指定日期，格式 YYYY-MM-DD，如 "2026-08-23"（只搜该天的归档；可选）
          user_id(string): 指定 QQ UID（精确匹配；可选）
          nickname(string): 指定 QQ 名称（昵称子串匹配，如 "夏目"；可选）
          count(number): 返回条数上限（默认 20，最大 100）

        返回: 按时间顺序排列的匹配消息（时间/昵称/内容）。若没有任何条件（除 group_id 外），
        等价于查看该群最近的 count 条消息。
        '''
        if not self._is_allowed(event):
            return "❌ 无权限：仅管理员可以使用跨群查看工具。"
        gid = group_id.strip()
        if not gid.isdigit():
            return f"❌ 群号格式错误：{group_id}。群号应为纯数字。"
        try:
            count = max(1, min(int(count), 100))
        except (TypeError, ValueError):
            count = 20
        try:
            msgs = self._search_group_history(
                gid, count,
                keyword=keyword or None,
                date=date or None,
                user_id=user_id or None,
                nickname=nickname or None,
            )
        except Exception as e:
            logger.error(f"搜索归档消息失败: {e}")
            return f"❌ 搜索失败: {e}"
        if not msgs:
            conds = []
            if keyword:
                conds.append(f"关键词[{keyword}]")
            if date:
                conds.append(f"日期[{date}]")
            if user_id:
                conds.append(f"QQ[{user_id}]")
            if nickname:
                conds.append(f"昵称[{nickname}]")
            cond_str = ("、".join(conds)) if conds else "无过滤条件"
            return f"📭 群 {gid} 中未找到匹配消息（{cond_str}）。"
        conds = []
        if keyword:
            conds.append(f"关键词[{keyword}]")
        if date:
            conds.append(f"日期[{date}]")
        if user_id:
            conds.append(f"QQ[{user_id}]")
        if nickname:
            conds.append(f"昵称[{nickname}]")
        cond_str = ("，".join(conds)) if conds else "最近消息"
        return (f"群 {gid} 搜索到 {len(msgs)} 条消息（{cond_str}）:\n"
                + "\n".join(msgs))