"""
森空岛自动签到 AstrBot 插件
纯聊天交互，无需 WebUI 配置
"""
import asyncio
import json
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from .lib.skyland import (
    do_sign,
    get_cred_by_token,
    parse_user_token,
    verify_token,
)

# 使用 AstrBot 官方推荐的插件数据目录
# 此目录在重启/重载后不会丢失
try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    _DATA_BASE = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_skyland"
except (ImportError, Exception):
    # 降级：兼容旧版本
    _DATA_BASE = Path("data") / "plugin_data" / "astrbot_plugin_skyland"

_DATA_BASE.mkdir(parents=True, exist_ok=True)

# 用户数据文件路径（使用官方持久化目录）
DATA_FILE = str(_DATA_BASE / "users.json")
DATA_BACKUP_FILE = str(_DATA_BASE / "users.json.bak")

# 旧路径（兼容旧版本插件名迁移）
_OLD_DATA_BASE = Path(str(_DATA_BASE).replace("astrbot_plugin_skyland", "astrbot_plugin_skland"))
_OLD_DATA_FILE = str(_OLD_DATA_BASE / "users.json")


@register("astrbot_plugin_skyland", "森空岛签到", "森空岛（明日方舟/终末地）自动签到，纯聊天交互，多用户管理", "v1.4.0")
class SklandSignPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data = self._load_data()
        # 后台签到任务引用 —— 用于 terminate() 中取消，防止热重载残留
        self._sign_task: Optional[asyncio.Task] = None
        self._task_started = False

    # ==================== 官方生命周期钩子 ====================

    async def initialize(self):
        """
        AstrBot 官方生命周期钩子。
        插件实例化 + 事件绑定完成后自动调用。
        在此处启动定时签到 + 预生成 dId（避免首次用时阻塞）。
        """
        # 设置 dId 持久化缓存（避免每次重启重新计算）
        try:
            from .lib.security import set_cache_dir
            set_cache_dir(str(_DATA_BASE))
        except Exception as e:
            logger.warning(f"设置 dId 缓存目录失败: {e}")

        # 预生成设备指纹 dId，将同步阻塞移到加载阶段而非用户操作时
        try:
            from .lib.skyland import _get_login_header
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _get_login_header)
        except Exception as e:
            logger.warning(f"预生成 dId 失败（不影响签到，将在需要时重试）: {e}")

        if self.data["users"] and not self._task_started:
            self._start_auto_sign_loop()

    async def terminate(self):
        """
        AstrBot 官方生命周期钩子。
        热重载 / 卸载前自动调用。
        在此处取消后台签到任务，防止热重载后新旧两个循环同时运行。
        """
        if self._sign_task and not self._sign_task.done():
            self._sign_task.cancel()
            try:
                await self._sign_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"取消签到任务时出现异常: {e}")
            self._sign_task = None
        self._task_started = False

    # ==================== 定时签到管理 ====================

    def _start_auto_sign_loop(self):
        """启动定时签到循环（外部调用的总入口）"""
        if self._task_started:
            return
        self._task_started = True
        self._sign_task = asyncio.create_task(self._auto_sign_loop())
        logger.info("自动签到循环已启动")

    async def _auto_sign_loop(self):
        """每分钟检查是否有用户需要签到（支持每用户独立签到时间）"""
        try:
            # 首次启动等 5 秒让插件完全初始化
            await asyncio.sleep(5)
            while True:
                now = datetime.now()
                # 用整数比较，避免 "9:05" vs "09:05" 格式不匹配
                current_h, current_m = now.hour, now.minute
                today = date.today().isoformat()

                # 找出当前时间需要签到且今日未签的用户
                due_users = []
                for sid, info in self.data["users"].items():
                    user_time = info.get("sign_time", "09:05")
                    try:
                        uh, um = map(int, user_time.split(":"))
                    except (ValueError, AttributeError):
                        uh, um = 9, 5
                    if uh == current_h and um == current_m:
                        if info.get("last_sign_date") != today or not info.get("last_sign_result", "").startswith("✅"):
                            due_users.append((sid, info))

                if due_users:
                    logger.info(f"[{current_h:02d}:{current_m:02d}] 触发签到，{len(due_users)} 个用户")
                    await self._auto_sign_batch(due_users)

                # 等待到下一分钟（重新取时间，避免批量耗时导致跳分钟）
                sleep_sec = 60 - datetime.now().second + 0.5
                await asyncio.sleep(sleep_sec)
        except asyncio.CancelledError:
            logger.info("自动签到循环已被取消（热重载/卸载）")
            raise

    async def _auto_sign_batch(self, users: list):
        """为一批用户执行签到（连接复用，批量保存）"""
        today = date.today().isoformat()
        saved = False

        connector = aiohttp.TCPConnector(limit=10, limit_per_host=5)
        async with aiohttp.ClientSession(connector=connector) as session:
            for sender_id, info in users:
                try:
                    result = await self._sign_for_user_with_session(session, sender_id, info)
                    info["last_sign_date"] = today
                    info["last_sign_result"] = "✅ " + " | ".join(result) if result else "✅ 签到完成（无奖励）"
                    saved = True
                    await self._notify_user(info, f"🌠 森空岛自动签到\n📅 {today}\n" + "\n".join(result))
                    logger.info(f"用户 {sender_id} 签到成功")
                except Exception as e:
                    err_msg = f"❌ 签到失败: {e}"
                    info["last_sign_date"] = today
                    info["last_sign_result"] = err_msg
                    saved = True
                    await self._notify_user(info, f"🌠 森空岛自动签到\n📅 {today}\n{err_msg}")
                    logger.error(f"用户 {sender_id} 签到失败: {e}", exc_info=e)

                await asyncio.sleep(3 + (hash(sender_id) % 3))

        if saved:
            self.data["stats"]["last_auto_sign"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._save_data()
        logger.info(f"批量签到完成 ({len(users)} 用户)")

    async def _sign_for_user_with_session(self, session: aiohttp.ClientSession,
                                            sender_id: str, info: dict) -> list:
        """为单个用户执行签到（复用外部 session）"""
        token = info.get("token", "")
        cred_cred = info.get("cred_cred", "")
        cred_token = info.get("cred_token", "")

        if not cred_cred or not cred_token:
            cred_resp = await get_cred_by_token(session, token)
            cred_cred = cred_resp.get("cred", "")
            cred_token = cred_resp.get("token", "")
            info["cred_cred"] = cred_cred
            info["cred_token"] = cred_token

        return await do_sign(session, cred_token, cred_cred)

    async def _sign_for_user(self, sender_id: str, info: dict) -> list:
        """为单个用户执行签到"""
        token = info.get("token", "")
        cred_cred = info.get("cred_cred", "")
        cred_token = info.get("cred_token", "")

        # 复用同一个 aiohttp 会话，避免重复创建连接
        async with aiohttp.ClientSession() as session:
            # 如果 cred 或 token 不完整，重新获取凭证对
            if not cred_cred or not cred_token:
                cred_resp = await get_cred_by_token(session, token)
                cred_cred = cred_resp.get("cred", "")
                cred_token = cred_resp.get("token", "")
                info["cred_cred"] = cred_cred
                info["cred_token"] = cred_token
                self._save_data()

            return await do_sign(session, cred_token, cred_cred)

    async def _notify_user(self, info: dict, message: str):
        """向用户推送消息（需私聊绑定 + push 开关开启）

        使用 AstrBot 官方推荐的 MessageChain 构建主动消息。
        """
        if not info.get("bound_in_private"):
            logger.info(f"用户 {info.get('sender_id')} 非私聊绑定，跳过主动推送")
            return
        if not info.get("push_enabled", True):  # 旧数据默认开启
            logger.info(f"用户 {info.get('sender_id')} 已关闭推送，跳过")
            return

        target = info.get("notify_target")
        if not target:
            logger.warning(f"用户 {info.get('sender_id')} 没有通知目标，跳过推送")
            return
        try:
            chain = MessageChain().message(message + "\n\n💡 发送 /skland push off 关闭自动推送")
            await self.context.send_message(target, chain)
        except Exception as e:
            logger.error(f"推送消息失败: {e}")

    # ==================== 数据管理 ====================

    def _load_data(self) -> dict:
        """加载用户数据

        如果主文件损坏，自动尝试从备份恢复。
        如果检测到旧路径数据，自动迁移。
        """
        # 自动迁移旧路径数据
        if not os.path.exists(DATA_FILE) and os.path.exists(_OLD_DATA_FILE):
            try:
                shutil.copy2(_OLD_DATA_FILE, DATA_FILE)
                logger.info(f"已从旧路径自动迁移数据: {_OLD_DATA_FILE} → {DATA_FILE}")
            except Exception as e:
                logger.warning(f"自动迁移旧数据失败: {e}")

        if os.path.exists(DATA_FILE):
            try:
                with open(DATA_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                # 验证数据结构完整性
                if "users" not in data or "stats" not in data:
                    raise ValueError("数据结构不完整，缺少 users 或 stats 字段")
                return data
            except (json.JSONDecodeError, ValueError, Exception) as e:
                logger.error(f"加载数据文件失败 ({e})，尝试从备份恢复...")
                # 尝试从备份恢复
                if os.path.exists(DATA_BACKUP_FILE):
                    try:
                        with open(DATA_BACKUP_FILE, "r", encoding="utf-8") as f:
                            backup_data = json.load(f)
                        if "users" in backup_data and "stats" in backup_data:
                            logger.info("已从备份文件成功恢复数据")
                            # 将备份写回主文件
                            with open(DATA_FILE, "w", encoding="utf-8") as f:
                                json.dump(backup_data, f, ensure_ascii=False, indent=2)
                            return backup_data
                    except Exception as be:
                        logger.error(f"备份文件也损坏: {be}")
                logger.error(f"数据文件恢复失败，将使用空数据")
        return self._new_empty_data()

    def _new_empty_data(self) -> dict:
        """返回一个空的初始数据结构"""
        return {
            "users": {},
            "stats": {
                "total_bindings": 0,
                "last_auto_sign": None,
            }
        }

    def _save_data(self):
        """原子化保存用户数据

        先写入临时文件再 rename，防止中途崩溃导致数据损坏。
        同时保留一份备份，重载时报错时可从备份恢复。
        """
        try:
            # 写入临时文件
            fd, tmp_path = tempfile.mkstemp(dir=str(_DATA_BASE), prefix="users_", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)

            # 先备份当前文件（如果有）
            if os.path.exists(DATA_FILE):
                shutil.copy2(DATA_FILE, DATA_BACKUP_FILE)

            # 原子替换
            os.replace(tmp_path, DATA_FILE)

        except Exception as e:
            logger.error(f"保存数据失败: {e}")
            # 尝试从备份恢复
            if os.path.exists(DATA_BACKUP_FILE):
                try:
                    shutil.copy2(DATA_BACKUP_FILE, DATA_FILE)
                    logger.info("已从备份文件恢复数据")
                except Exception as restore_err:
                    logger.error(f"从备份恢复也失败: {restore_err}")

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        """获取发送者的唯一标识"""
        return event.unified_msg_origin

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断是否为管理员（AstrBot 内置管理员判断）"""
        return event.get_role() == "admin"

    def _build_user_entry(self, sid: str, event: AstrMessageEvent,
                          token: str, game_info: str, cred_resp: dict) -> dict:
        """构建用户数据条目"""
        return {
            "sender_id": sid,
            "token": token,
            "cred_cred": cred_resp.get("cred", "") if cred_resp else "",
            "cred_token": cred_resp.get("token", "") if cred_resp else "",
            "bound_at": date.today().isoformat(),
            "last_sign_date": None,
            "last_sign_result": None,
            "game_info": game_info,
            "notify_target": sid,
            "bound_in_private": not bool(event.get_group_id()),
            "push_enabled": True,  # 初始默认开启
            "sign_time": "09:05",  # 每用户独立签到时间
        }

    # ==================== 指令系统（command_group 模式） ====================

    @filter.command_group("skland")
    def skland():
        """/skland 指令组"""
        pass

    @skland.command("help")
    async def help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(
            "🌠 森空岛自动签到 v1.0\n\n"
            "📋 可用指令：\n"
            "  /skland bind <token>  绑定鹰角通行证 token（推荐，最快的方法）\n"
            "  /skland login         通过手机号+验证码登录绑定\n"
            "  /skland sign          立即手动签到\n"
            "  /skland status        查看签到状态\n"
            "  /skland unbind        解绑账号\n"
            "  /skland did           查看设备指纹状态（如遇登录问题）\n\n"
            "管理员指令：\n"
            "  /skland list          查看所有绑定用户\n"
            "  /skland remove <id>   移除指定用户的绑定\n"
            "  /skland broadcast <msg> 向所有用户群发\n\n"
            "💡 推荐使用 /skland login 手机号登录（无需浏览器）\n"
            "💡 也可用 /skland bind 绑定 token\n\n"
            "📌 绑定后每天 09:05 自动签到，结果会推送到你这里。"
        )

    @skland.command("did")
    async def did(self, event: AstrMessageEvent):
        """查看设备指纹状态"""
        from .lib.security import _load_cached_did
        from .lib.skyland import _get_login_header

        cached = _load_cached_did()
        status_lines = ["📟 设备指纹 (dId) 状态"]
        if cached:
            status_lines.append(f"✅ 已缓存: {cached[:16]}...{cached[-8:]}")
            status_lines.append("⏰ 缓存位置: plugin_data 目录")
            status_lines.append("💡 如需重新生成，可删除缓存文件后重载插件")

            # 检查是否是 fallback
            if len(cached) < 40:
                status_lines.append("⚠️ 当前使用的是 fallback dId")
                status_lines.append("   森空岛 API 可能拒绝请求")
                status_lines.append("   尝试在可访问 fp-it.portal101.cn 的网络下重载插件")
        else:
            status_lines.append("❌ 未生成 dId")
            status_lines.append("⏳ 将在首次使用 /skland login 时自动生成")

        yield event.plain_result("\n".join(status_lines))

    # ==================== 指令: 绑定 ====================

    @skland.command("bind")
    async def bind(self, event: AstrMessageEvent, token: str = None):
        """绑定鹰角通行证 token（仅限私聊）"""
        if event.get_group_id():
            yield event.plain_result("🔒 请在私聊中使用此命令（token 不应暴露在群聊中）\n发送 /skland bind <token> 到机器人私聊即可。")
            return
        if not token:
            yield event.plain_result(
                "⚠️ 请提供 token。\n\n"
                "使用方法：/skland bind <你的token>\n\n"
                "💡 如何获取 token？\n"
                "1. 打开 https://www.skland.com 并登录\n"
                "2. F12 → 控制台 → 粘贴以下代码：\n"
                "   copy(JSON.parse(localStorage.getItem('userInfo')).token)\n"
                "3. 粘贴后发送 /skland bind <token>\n\n"
                "或者发送 /skland login 通过手机号登录。"
            )
            return

        sid = self._get_sender_id(event)

        # 先验证 token
        yield event.plain_result("⏳ 正在验证 token 有效性...")
        logger.info(f"[绑定] 用户 {sid} token 长度={len(token)}，开始验证…")

        try:
            token = parse_user_token(token)
            logger.info(f"[绑定] token 解析完成，调用 verify_token …")
            success, info, cred_resp = await verify_token(token)
        except Exception as e:
            logger.error(f"[绑定] 用户 {sid} token 验证异常: {type(e).__name__}: {e}")
            yield event.plain_result(f"❌ token 验证过程出错: {e}\n请检查 token 是否正确，如持续失败请反馈后台日志。")
            return

        if not success:
            yield event.plain_result(f"❌ token 验证失败: {info}\n请检查 token 是否正确或是否已过期。")
            return

        # token 有效，保存用户数据
        self.data["users"][sid] = self._build_user_entry(sid, event, token, info, cred_resp)
        self.data["stats"]["total_bindings"] = len(self.data["users"])
        self._save_data()

        # 注册自动签到（如果尚未启动）
        self._start_auto_sign_loop()

        yield event.plain_result(
            f"✅ 绑定成功！🎉\n"
            f"检测到角色：{info}\n\n"
            f"📌 每天 09:05 将自动签到\n"
            f"💪 现在发送 /skland sign 立即签到试试吧！"
        )

    # ==================== 指令: 手机号登录 ====================

    @skland.command("login")
    async def login(self, event: AstrMessageEvent):
        """通过手机号+验证码登录绑定（仅限私聊）"""
        if event.get_group_id():
            yield event.plain_result("🔒 请在私聊中使用此命令（验证码不应暴露在群聊中）\n发送 /skland login 到机器人私聊即可。")
            return
        from .lib.skyland import api_post, LOGIN_CODE_URL, TOKEN_PHONE_CODE_URL, _get_login_header
        from astrbot.core.utils.session_waiter import session_waiter, SessionController

        yield event.plain_result('📱 请输入你的手机号（发送"取消"取消）：')

        phone = ""

        @session_waiter(timeout=120)
        async def wait_phone(controller: SessionController, event: AstrMessageEvent):
            nonlocal phone
            text = event.message_str.strip()
            if not text:
                return

            phone = text.replace(" ", "").replace("-", "").replace("+86", "")
            if not phone.isdigit() or len(phone) != 11:
                await event.send(event.plain_result("⚠️ 手机号格式不正确，请输入11位手机号，如 13800138000："))
                return

            # 发送验证码
            try:
                async with aiohttp.ClientSession() as session:
                    resp = await api_post(session, LOGIN_CODE_URL,
                                          json_data={'phone': phone, 'type': 2},
                                          headers=_get_login_header())
                    if resp.get("status") != 0:
                        await event.send(event.plain_result(
                            f"❌ {resp.get('msg', '发送验证码失败')}"))
                        controller.stop()
                        return
            except Exception as e:
                await event.send(event.plain_result(f"❌ 发送验证码出错: {e}"))
                controller.stop()
                return

            await event.send(event.plain_result('📱 验证码已发送，请输入6位验证码：'))

            @session_waiter(timeout=120)
            async def wait_code(controller2: SessionController, event2: AstrMessageEvent):
                code = event2.message_str.strip()
                if not code:
                    return

                if not code.isdigit() or len(code) != 6:
                    await event2.send(event2.plain_result("⚠️ 验证码格式不正确，请输入6位数字："))
                    return

                try:
                    async with aiohttp.ClientSession() as session:
                        r = await api_post(session, TOKEN_PHONE_CODE_URL,
                                           json_data={"phone": phone, "code": code},
                                           headers=_get_login_header())
                        if r.get("status") != 0:
                            await event2.send(event2.plain_result(
                                f"❌ 登录失败: {r.get('msg', '验证码错误')}"))
                            controller2.stop()
                            return
                        token = r['data']['token']
                except Exception as e:
                    await event2.send(event2.plain_result(f"❌ 登录出错: {e}"))
                    controller2.stop()
                    return

                success, info, cred_resp = await verify_token(token)
                if not success:
                    await event2.send(event2.plain_result(f"❌ token 验证失败: {info}"))
                    controller2.stop()
                    return

                sid = event2.unified_msg_origin
                self.data["users"][sid] = self._build_user_entry(sid, event2, token, info, cred_resp)
                self.data["stats"]["total_bindings"] = len(self.data["users"])
                self._save_data()
                self._start_auto_sign_loop()

                await event2.send(event2.plain_result(
                    f"✅ 绑定成功！🎉\n"
                    f"检测到角色：{info}\n\n"
                    f"📌 每天 09:05 将自动签到\n"
                    f"💪 现在发送 /skland sign 立即签到试试吧！"
                ))
                controller2.stop()

            try:
                await wait_code(event)
            except TimeoutError:
                await event.send(event.plain_result("⏰ 验证码输入超时。"))
            except Exception as e:
                await event.send(event.plain_result(f"❌ 出错: {e}"))

        try:
            await wait_phone(event)
        except TimeoutError:
            yield event.plain_result("⏰ 手机号输入超时。")
        except Exception as e:
            yield event.plain_result(f"❌ 出错: {e}")

    # ==================== 指令: 手动签到 ====================

    @skland.command("sign")
    async def sign(self, event: AstrMessageEvent):
        """手动立即签到"""
        sid = self._get_sender_id(event)

        if sid not in self.data["users"]:
            yield event.plain_result(
                "❌ 你还没有绑定账号！\n"
                "请使用 /skland bind <token> 或 /skland login 绑定。"
            )
            return

        info = self.data["users"][sid]
        logger.info(f"[签到] 用户 {sid} 手动签到请求，角色: {info.get('game_info', '未知')}")
        yield event.plain_result("⏳ 正在签到，请稍候...")

        try:
            result_logs = await self._sign_for_user(sid, info)
            today = date.today().isoformat()
            info["last_sign_date"] = today
            info["last_sign_result"] = "✅ " + " | ".join(result_logs) if result_logs else "✅ 签到完成（无奖励）"
            self._save_data()

            msg_lines = [
                "🌠 森空岛签到完成",
                f"📅 {today}",
            ]
            msg_lines.extend(result_logs) if result_logs else msg_lines.append("今日无可用签到项目")
            yield event.plain_result("\n".join(msg_lines))

        except Exception as e:
            err_msg = f"❌ 签到失败: {e}"
            info["last_sign_result"] = err_msg
            self._save_data()
            yield event.plain_result(err_msg)
            logger.error(f"用户 {sid} 手动签到失败: {e}", exc_info=e)

    # ==================== 指令: 推送开关 ====================

    @skland.command("push")
    async def push_toggle(self, event: AstrMessageEvent, action: str = None):
        """开关自动签到推送通知"""
        sid = self._get_sender_id(event)

        if sid not in self.data["users"]:
            yield event.plain_result("❌ 你还没有绑定账号！")
            return

        if not action or action not in ("on", "off"):
            enabled = self.data["users"][sid].get("push_enabled", True)
            yield event.plain_result(f"📢 自动推送: {'🟢 已开启' if enabled else '🔴 已关闭'}\n修改: /skland push on 或 /skland push off")
            return

        self.data["users"][sid]["push_enabled"] = (action == "on")
        self._save_data()
        yield event.plain_result(f"📢 自动推送已{'开启' if action == 'on' else '关闭'}" +
                                 ("\n签到完成后会私聊通知你。" if action == "on" else "\n不会再主动推送签到结果。"))

    # ==================== 指令: 签到时间设置 ====================

    @skland.command("time")
    async def time_config(self, event: AstrMessageEvent, action: str = None, arg: str = None):
        """查看或设置自己的自动签到时间"""
        sid = self._get_sender_id(event)

        if sid not in self.data["users"]:
            yield event.plain_result("❌ 你还没有绑定账号！")
            return

        if action == "set" and arg:
            try:
                parts = arg.split(":")
                h, m = int(parts[0]), int(parts[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, IndexError):
                yield event.plain_result("❌ 时间格式错误，请使用 HH:MM\n例如: /skland time set 06:30")
                return

            new_time = f"{h:02d}:{m:02d}"  # 归一化为零填充格式
            old_time = self.data["users"][sid].get("sign_time", "09:05")
            self.data["users"][sid]["sign_time"] = new_time
            self._save_data()
            yield event.plain_result(f"⏰ 你的签到时间已更新: {old_time} → {new_time}")
            logger.info(f"用户 {sid} 签到时间: {old_time} → {arg}")

        else:
            sign_time = self.data["users"][sid].get("sign_time", "09:05")
            yield event.plain_result(
                f"⏰ 你的自动签到时间: {sign_time}\n\n"
                f"修改: /skland time set HH:MM\n"
                f"例如: /skland time set 06:30"
            )

    # ==================== 指令: 查看状态 ====================

    @skland.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看签到状态"""
        sid = self._get_sender_id(event)

        if sid not in self.data["users"]:
            yield event.plain_result("❌ 你还没有绑定账号！")
            return

        info = self.data["users"][sid]
        today = date.today().isoformat()
        is_signed_today = info.get("last_sign_date") == today
        result = info.get("last_sign_result", "暂无记录")

        yield event.plain_result(
            f"📊 森空岛签到状态\n"
            f"🆔 绑定角色: {info.get('game_info', '未知')}\n"
            f"📅 绑定时间: {info.get('bound_at', '未知')}\n"
            f"✅ 今日已签到: {'是 🎉' if is_signed_today else '否'}\n"
            f"📋 上次结果: {result}"
        )

    # ==================== 指令: 解绑 ====================

    @skland.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """解绑账号（仅限私聊，需要确认）"""
        if event.get_group_id():
            yield event.plain_result("🔒 请在私聊中使用此命令\n发送 /skland unbind 到机器人私聊即可。")
            return
        sid = self._get_sender_id(event)

        if sid not in self.data["users"]:
            yield event.plain_result("❌ 你还没有绑定账号！")
            return

        info = self.data["users"][sid]
        yield event.plain_result(
            f"⚠️ 确定要解绑吗？\n"
            f"角色: {info.get('game_info', '未知')}\n"
            f"绑定于: {info.get('bound_at', '未知')}\n\n"
            f"解绑后将停止自动签到。\n"
            f"回复「确认」以解绑，回复其他取消。"
        )

        # 使用 session_waiter 等待确认
        from astrbot.core.utils.session_waiter import session_waiter, SessionController

        @session_waiter(timeout=30)
        async def wait_confirm(controller: SessionController, event: AstrMessageEvent):
            reply = event.message_str.strip()
            if reply == "确认":
                del self.data["users"][sid]
                self.data["stats"]["total_bindings"] = len(self.data["users"])
                self._save_data()
                await event.send(event.plain_result("✅ 已解绑！你的账号数据已清除。"))
            else:
                await event.send(event.plain_result("❌ 已取消解绑"))
            controller.stop()

        try:
            await wait_confirm(event)
        except TimeoutError:
            yield event.plain_result("⏰ 操作超时，已取消。")

    # ==================== 指令: 管理员 - 查看所有用户 ====================

    @skland.command("list")
    async def list_users(self, event: AstrMessageEvent):
        """管理员查看所有已绑定用户"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可使用此命令")
            return

        users = self.data["users"]
        if not users:
            yield event.plain_result("📋 暂无已绑定的用户")
            return

        today = date.today().isoformat()
        lines = [f"📋 已绑定用户列表 (共 {len(users)} 人)"]
        for i, (sid, info) in enumerate(users.items(), 1):
            is_signed = info.get("last_sign_date") == today
            sign_icon = "✅" if is_signed else "⏳"
            game = info.get("game_info", "未知角色")
            lines.append(f"{i}. {sign_icon} {game}")

        yield event.plain_result("\n".join(lines))

    # ==================== 指令: 管理员 - 移除用户 ====================

    @skland.command("remove")
    async def remove_user(self, event: AstrMessageEvent, user_id: str = None):
        """管理员移除指定用户的绑定"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可使用此命令")
            return

        if not user_id:
            yield event.plain_result("⚠️ 请指定要移除的用户 ID。\n使用方法：/skland remove <用户ID>\n使用 /skland list 查看用户 ID。")
            return

        if user_id not in self.data["users"]:
            yield event.plain_result(f"❌ 未找到用户: {user_id}")
            return

        info = self.data["users"][user_id]
        del self.data["users"][user_id]
        self.data["stats"]["total_bindings"] = len(self.data["users"])
        self._save_data()
        yield event.plain_result(f"✅ 已移除用户 {user_id} 的绑定（角色: {info.get('game_info', '未知')}）")

    # ==================== 指令: 管理员 - 群发 ====================

    @skland.command("broadcast")
    async def broadcast(self, event: AstrMessageEvent):
        """管理员向所有绑定用户群发消息"""
        if not self._is_admin(event):
            yield event.plain_result("❌ 仅管理员可使用此命令")
            return

        # 获取消息内容（去掉命令前缀 /skland broadcast ）
        raw = event.message_str.strip()
        for prefix in ('/skland broadcast ', '/skland broadcast', '/skland bc '):
            if raw.startswith(prefix):
                msg = raw[len(prefix):].strip()
                break
        else:
            msg = ''

        if not msg:
            yield event.plain_result("⚠️ 请提供要群发的消息内容\n使用方法：/skland broadcast <消息内容>")
            return

        success_count = 0
        fail_count = 0
        for sid, info in self.data["users"].items():
            try:
                await self._notify_user(info, f"📢 管理员消息\n{msg}")
                success_count += 1
            except Exception:
                fail_count += 1
            await asyncio.sleep(0.5)

        yield event.plain_result(f"📢 群发完成！成功: {success_count}，失败: {fail_count}")
