"""
森空岛自动签到 AstrBot 插件
纯聊天交互，无需 WebUI 配置
"""
import asyncio
import json
import logging
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import aiohttp

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

from .lib.skyland import (
    do_sign,
    get_cred_by_token,
    parse_user_token,
    verify_token,
)

logger = logging.getLogger(__name__)

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


@register("astrbot_plugin_skyland", "森空岛签到", "森空岛（明日方舟/终末地）自动签到，纯聊天交互，多用户管理", "v1.0.0")
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
        """每日定时签到循环"""
        try:
            while True:
                now = datetime.now()
                # 计算下一次签到时间：每天 09:05（给一点余量）
                target = now.replace(hour=9, minute=5, second=0, microsecond=0)
                if target <= now:
                    target += timedelta(days=1)
                wait_seconds = (target - now).total_seconds()
                logger.info(f"下次自动签到时间: {target.strftime('%Y-%m-%d %H:%M:%S')} (等待 {wait_seconds:.0f} 秒)")
                await asyncio.sleep(wait_seconds)
                await self._auto_sign_all()
        except asyncio.CancelledError:
            logger.info("自动签到循环已被取消（热重载/卸载）")
            raise  # 必须重新抛出，让 asyncio 知道任务已被取消

    async def _auto_sign_all(self):
        """为所有已绑定用户自动签到"""
        today = date.today().isoformat()
        users = list(self.data["users"].items())
        if not users:
            logger.info("没有已绑定的用户，跳过自动签到")
            return

        logger.info(f"开始自动签到，共 {len(users)} 个用户")
        for sender_id, info in users:
            if info.get("last_sign_date") == today and info.get("last_sign_result", "").startswith("✅"):
                logger.info(f"用户 {sender_id} 今日已签到，跳过")
                continue

            try:
                result = await self._sign_for_user(sender_id, info)
                info["last_sign_date"] = today
                info["last_sign_result"] = "✅ " + " | ".join(result) if result else "✅ 签到完成（无奖励）"

                # 推送结果给用户
                await self._notify_user(info, f"🌠 森空岛自动签到\n📅 {today}\n" + "\n".join(result))
                logger.info(f"用户 {sender_id} 签到成功")
            except Exception as e:
                err_msg = f"❌ 签到失败: {e}"
                info["last_sign_date"] = today
                info["last_sign_result"] = err_msg
                await self._notify_user(info, f"🌠 森空岛自动签到\n📅 {today}\n{err_msg}")
                logger.error(f"用户 {sender_id} 签到失败: {e}", exc_info=e)

            self._save_data()
            await asyncio.sleep(2)  # 每个用户间隔 2s，防限流

        self.data["stats"]["last_auto_sign"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._save_data()
        logger.info("自动签到完成")

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

            return await do_sign(session, token, cred_cred)

    async def _notify_user(self, info: dict, message: str):
        """向用户推送消息

        使用 AstrBot 官方推荐的 MessageChain 构建主动消息。
        """
        target = info.get("notify_target")
        if not target:
            logger.warning(f"用户 {info.get('sender_id')} 没有通知目标，跳过推送")
            return
        try:
            chain = MessageChain().message(message)
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
            "  /skland bind <token>  绑定鹰角通行证 token\n"
            "  /skland login         通过手机号+验证码登录绑定\n"
            "  /skland sign          立即手动签到\n"
            "  /skland status        查看签到状态\n"
            "  /skland unbind        解绑账号\n\n"
            "管理员指令：\n"
            "  /skland list          查看所有绑定用户\n"
            "  /skland remove <id>   移除指定用户的绑定\n"
            "  /skland broadcast <msg> 向所有用户群发\n\n"
            "💡 如何获取 token？\n"
            "  1. 打开 https://www.skland.com 并登录\n"
            "  2. 按 F12 → 控制台，粘贴：\n"
            "     copy(JSON.parse(localStorage.getItem('userInfo')).token)\n"
            "  3. 发送 /skland bind 加上你复制的内容\n\n"
            "📌 绑定后每天 09:05 自动签到，结果会推送到你这里。"
        )

    # ==================== 指令: 绑定 ====================

    @skland.command("bind")
    async def bind(self, event: AstrMessageEvent, token: str = None):
        """绑定鹰角通行证 token"""
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

        try:
            token = parse_user_token(token)
            success, info, cred_resp = await verify_token(token)
        except Exception as e:
            yield event.plain_result(f"❌ token 验证过程出错: {e}")
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
        """通过手机号+验证码登录绑定

        使用单层 session_waiter + 状态变量实现多步流程，
        避免嵌套 session_waiter 导致的状态混乱。
        """
        from .lib.skyland import api_post, LOGIN_CODE_URL, TOKEN_PHONE_CODE_URL, _get_login_header
        from astrbot.core.utils.session_waiter import session_waiter, SessionController

        state = {"step": "phone", "phone": ""}

        yield event.plain_result('📱 请输入你的手机号（发送"取消"可取消操作）：')

        @session_waiter(timeout=180)
        async def login_handler(controller: SessionController, event: AstrMessageEvent):
            text = event.message_str.strip()

            # 取消操作（任何步骤都支持）
            if text == "取消":
                await event.send(event.plain_result("❌ 已取消绑定"))
                controller.stop()
                return

            if state["step"] == "phone":
                # —— 步骤1：接收手机号 ——
                phone = text.replace(" ", "").replace("-", "").replace("+86", "")
                if not phone.isdigit() or len(phone) != 11:
                    await event.send(event.plain_result("⚠️ 手机号格式不正确，请输入11位手机号，如 13800138000："))
                    return  # 同一 session，继续等待

                state["phone"] = phone

                # 发送验证码
                await event.send(event.plain_result("⏳ 正在发送验证码..."))
                try:
                    async with aiohttp.ClientSession() as session:
                        resp = await api_post(session, LOGIN_CODE_URL,
                                              json_data={'phone': phone, 'type': 2},
                                              headers=_get_login_header())
                        if resp.get("status") != 0:
                            await event.send(event.plain_result(
                                f"❌ 发送验证码失败: {resp.get('msg', '未知错误')}\n"
                                "请稍后重试，或改用 /skland bind <token> 方式绑定。"))
                            controller.stop()
                            return
                except Exception as e:
                    err_text = str(e)
                    if "dId" in err_text or "数美" in err_text:
                        await event.send(event.plain_result(
                            "⚠️ 设备指纹生成失败，请稍后重试。\n"
                            "或者改用 /skland bind <token> 方式绑定。"))
                    else:
                        await event.send(event.plain_result(f"❌ 发送验证码出错: {err_text}"))
                    controller.stop()
                    return

                # 成功进入下一步
                state["step"] = "code"
                await event.send(event.plain_result('📱 验证码已发送，请输入6位验证码（发送"取消"可取消）：'))

            elif state["step"] == "code":
                # —— 步骤2：接收验证码 ——
                code = text
                if not code.isdigit() or len(code) != 6:
                    await event.send(event.plain_result("⚠️ 验证码格式不正确，请输入6位数字验证码："))
                    return

                phone = state["phone"]
                await event.send(event.plain_result("⏳ 正在验证..."))

                try:
                    async with aiohttp.ClientSession() as session:
                        r = await api_post(session, TOKEN_PHONE_CODE_URL,
                                           json_data={"phone": phone, "code": code},
                                           headers=_get_login_header())
                        if r.get("status") != 0:
                            await event.send(event.plain_result(
                                f"❌ 登录失败: {r.get('msg', '验证码错误，请重新输入')}"))
                            # 重新回到验证码输入状态
                            state["step"] = "code"
                            await event.send(event.plain_result('请重新输入6位验证码（发送"取消"取消）：'))
                            return

                        token = r['data']['token']
                except Exception as e:
                    await event.send(event.plain_result(f"❌ 登录出错: {e}"))
                    controller.stop()
                    return

                # 验证 token 并绑定
                success, info, cred_resp = await verify_token(token)
                if not success:
                    await event.send(event.plain_result(f"❌ token 验证失败: {info}"))
                    controller.stop()
                    return

                sid = event.unified_msg_origin
                self.data["users"][sid] = self._build_user_entry(sid, event, token, info, cred_resp)
                self.data["stats"]["total_bindings"] = len(self.data["users"])
                self._save_data()
                self._start_auto_sign_loop()

                await event.send(event.plain_result(
                    f"✅ 绑定成功！🎉\n"
                    f"检测到角色：{info}\n\n"
                    f"📌 每天 09:05 将自动签到\n"
                    f"💪 现在发送 /skland sign 立即签到试试吧！"
                ))
                controller.stop()

        try:
            await login_handler(event)
        except TimeoutError:
            yield event.plain_result("⏰ 操作超时，已取消。请重新发送 /skland login 重试。")
        except Exception as e:
            yield event.plain_result(f"❌ 出错: {e}")
            logger.error(f"登录流程出错: {e}", exc_info=e)

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
                "─" * 20,
            ]
            msg_lines.extend(result_logs) if result_logs else msg_lines.append("今日无可用签到项目")
            yield event.plain_result("\n".join(msg_lines))

        except Exception as e:
            err_msg = f"❌ 签到失败: {e}"
            info["last_sign_result"] = err_msg
            self._save_data()
            yield event.plain_result(err_msg)
            logger.error(f"用户 {sid} 手动签到失败: {e}", exc_info=e)

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
            f"─" * 20 + "\n"
            f"🆔 绑定角色: {info.get('game_info', '未知')}\n"
            f"📅 绑定时间: {info.get('bound_at', '未知')}\n"
            f"✅ 今日已签到: {'是 🎉' if is_signed_today else '否'}\n"
            f"📋 上次结果: {result}\n"
            f"─" * 20 + "\n"
            f"/skland sign - 手动签到\n"
            f"/skland unbind - 解绑账号"
        )

    # ==================== 指令: 解绑 ====================

    @skland.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """解绑账号（需要确认）"""
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

        # 获取消息内容（去掉 /skland broadcast 前缀后的纯文本）
        msg = event.message_str.strip()
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
