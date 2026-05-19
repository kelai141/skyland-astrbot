"""
森空岛自动签到 AstrBot 插件 — v2.0.0
纯聊天交互，无需 WebUI 配置

重构要点:
- 模块化架构：引擎 / API / 存储 / 通知 / 处理器 完全解耦
- 统一连接池管理，签名算法对齐原始 skyland-auto-sign
- 支持手机验证码登录、Token 绑定、多游戏（明日方舟/终末地）
- 每用户独立签到时间、推送开关
- 管理员批量管理
"""
import asyncio
import json
import random
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

from .lib.storage import FileStore, migrate_from_old
from .lib.skyland_api import SkylandApiClient
from .lib.skyland_engine import (
    SkylandSignEngine,
    EngineConfig,
    UserSignState,
    UserCredential,
    SignResult,
)
from .lib.notification import PushPolicy
from .lib.security import set_cache_dir, fetch_did

# ==================== 路径配置 ====================

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
    _DATA_BASE = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_skyland"
except (ImportError, Exception):
    _DATA_BASE = Path("data") / "plugin_data" / "astrbot_plugin_skyland"

_DATA_BASE.mkdir(parents=True, exist_ok=True)


# ==================== 插件入口 ====================

@register(
    "astrbot_plugin_skyland",
    "森空岛签到",
    "森空岛（明日方舟/终末地）自动签到，支持多用户管理、手机号登录、定时推送，纯聊天交互",
    "v2.0.0",
)
class SklandSignPlugin(Star):
    """森空岛自动签到插件

    架构：
    - self.engine: 签到引擎（纯业务逻辑）
    - self.store: 数据存储（文件持久化）
    - 命令处理器: handlers/ 模块
    """

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context, config)
        self.config = config or {}

        # 数据存储
        self.store = FileStore(str(_DATA_BASE))

        # 签到引擎
        engine_cfg = EngineConfig(
            default_sign_time=self.config.get("sign_time", "09:05"),
            sign_interval_seconds=self.config.get("sign_interval_seconds", 2),
            sign_retry_count=self.config.get("sign_retry_count", 2),
            cred_refresh_window_hours=self.config.get("cred_refresh_window_hours", 24),
            push_enabled_default=self.config.get("push_enabled_default", True),
        )
        self.engine = SkylandSignEngine(engine_cfg)

        # 后台任务
        self._sign_task: Optional[asyncio.Task] = None
        self._task_started = False

    # ==================== 生命周期 ====================

    async def initialize(self):
        """插件初始化：加载数据 → 迁移 → 启动引擎 → 预加载 dId → 启动定时签到"""
        # 数据迁移
        migrate_from_old(self.store)

        # 加载数据
        self.store.load()

        # 初始化引擎
        await self.engine.initialize()

        # 设置 dId 缓存
        set_cache_dir(str(_DATA_BASE))

        # 预加载 dId（避免首次调用阻塞）
        try:
            await fetch_did()
        except Exception as e:
            logger.warning(f"预加载 dId 失败（不影响签到）: {e}")

        # 启动定时签到
        if self.store.get_users():
            self._start_auto_sign_loop()

        logger.info(f"森空岛签到插件 v2.0.0 已初始化，{len(self.store.get_users())} 个用户")

    async def terminate(self):
        """插件卸载：取消后台任务 → 关闭引擎"""
        if self._sign_task and not self._sign_task.done():
            self._sign_task.cancel()
            try:
                await self._sign_task
            except asyncio.CancelledError:
                pass
            logger.info("自动签到循环已取消")
        await self.engine.shutdown()
        logger.info("森空岛签到插件已关闭")

    # ==================== 内部方法 ====================

    def _get_sender_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识（私聊用 sender_id，群聊用 unified_msg_origin）"""
        gid = event.get_group_id()
        if gid:
            return f"{gid}:{event.get_sender_id()}"
        return event.get_sender_id()

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断用户是否为管理员"""
        sender_id = event.get_sender_id()
        admin_ids = self.config.get("admin_users", [])
        if sender_id in admin_ids:
            return True
        try:
            return self.context.is_admin(sender_id)
        except Exception:
            return False

    def _load_user_state(self, sender_id: str) -> Optional[UserSignState]:
        """从存储加载用户签到状态"""
        data = self.store.get_user(sender_id)
        if data is None:
            return None
        return UserSignState(
            sender_id=sender_id,
            credential=UserCredential(
                token=data.get("token", ""),
                cred=data.get("cred", ""),
                sign_token=data.get("sign_token", data.get("token", "")),
                refreshed_at=data.get("refreshed_at", ""),
            ),
            game_info=data.get("game_info", ""),
            last_sign_date=data.get("last_sign_date", ""),
            last_sign_result=data.get("last_sign_result", ""),
            push_enabled=data.get("push_enabled", True),
            sign_time=data.get("sign_time", "09:05"),
            bound_at=data.get("bound_at", ""),
            notify_target=data.get("notify_target", sender_id),
        )

    def _save_user_state(self, sender_id: str, state: UserSignState):
        """将用户签到状态保存到存储"""
        self.store.set_user(sender_id, {
            "token": state.credential.token,
            "cred": state.credential.cred,
            "sign_token": state.credential.sign_token,
            "refreshed_at": state.credential.refreshed_at,
            "game_info": state.game_info,
            "last_sign_date": state.last_sign_date,
            "last_sign_result": state.last_sign_result,
            "push_enabled": state.push_enabled,
            "sign_time": state.sign_time,
            "bound_at": state.bound_at,
            "notify_target": state.notify_target,
        })
        logger.info(f"[数据] 已保存用户 {sender_id[:16]} | sign_time={state.sign_time} | push={state.push_enabled}")

    async def _notify_user(self, user_info: dict, message: str):
        """向用户发送通知消息"""
        target = user_info.get("notify_target", "")
        if not target:
            return
        chain = MessageChain().message(message)
        await self.context.send_message(target, chain)

    # ==================== 定时签到 ====================

    def _start_auto_sign_loop(self):
        """启动定时签到循环"""
        if self._task_started:
            return
        self._task_started = True
        self._sign_task = asyncio.create_task(self._auto_sign_loop())
        logger.info("自动签到循环已启动")

    async def _auto_sign_loop(self):
        """每分钟检查并执行签到，带防漏分钟机制

        如果批量签到耗时 > 60s 导致跳过若干分钟，会回溯处理被跳过的分钟，
        确保不会因为网络延迟或大批量处理而漏掉用户的签到时间。
        """
        try:
            await asyncio.sleep(5)  # 初始化缓冲
            last_checked_slot: Optional[int] = None  # 上次检查的"一天中的分钟索引"

            while True:
                try:
                    now = datetime.now()
                    current_minute_slot = now.hour * 60 + now.minute  # 0-1439
                    today = date.today().isoformat()

                    # 每 60 分钟打印一次心跳日志（方便排查循环是否存活）
                    if current_minute_slot % 60 == 0:
                        user_count = len(self.store.get_users())
                        logger.info(
                            f"[心跳] {now.hour:02d}:{now.minute:02d} "
                            f"循环正常 | 已绑定用户: {user_count}"
                        )

                    # 确定需要检查的分钟范围（含当前分钟，防漏）
                    if last_checked_slot is None:
                        slots_to_check = [current_minute_slot]
                    else:
                        # 从上次检查的下一分钟到当前分钟（包含）
                        start = last_checked_slot + 1
                        if start > current_minute_slot:
                            # 跨越了 0 点（极少情况），重置
                            slots_to_check = [current_minute_slot]
                        else:
                            slots_to_check = list(range(start, current_minute_slot + 1))

                    for slot in slots_to_check:
                        ch, cm = divmod(slot, 60)
                        due_users = []
                        for sid, info in self.store.get_users().items():
                            user_time = info.get("sign_time", "09:05")
                            try:
                                uh, um = map(int, user_time.split(":"))
                            except (ValueError, AttributeError):
                                uh, um = 9, 5
                            if uh == ch and um == cm:
                                state = self._load_user_state(sid)
                                if state:
                                    due_users.append((sid, state))

                        if due_users:
                            logger.info(
                                f"[{ch:02d}:{cm:02d}] 触发签到，{len(due_users)} 个用户: "
                                + ", ".join(sid[:16] for sid, _ in due_users)
                            )
                            await self._auto_sign_batch(due_users)

                    last_checked_slot = current_minute_slot

                    # 等待到下一分钟
                    sleep_sec = 60 - datetime.now().second + 0.5
                    await asyncio.sleep(sleep_sec)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # 循环内部异常不得导致整个后台任务退出
                    logger.error(f"自动签到循环内部异常（3s 后恢复）: {e}", exc_info=True)
                    await asyncio.sleep(3)

        except asyncio.CancelledError:
            logger.info("自动签到循环被取消")
            raise
        except Exception as e:
            # 极端情况：最后一次保底
            logger.critical(f"自动签到循环致命异常，已退出: {e}", exc_info=True)

    async def _auto_sign_batch(self, users: list[tuple[str, UserSignState]]):
        """批量自动签到"""
        for sender_id, state in users:
            try:
                result = await self.engine.sign(state)
                self._save_user_state(sender_id, state)

                # 推送决策
                decision = PushPolicy.decide(state, result, is_manual=False)
                if decision.should_push and decision.message:
                    info = self.store.get_user(sender_id) or {}
                    await self._notify_user(info, decision.message)
                    logger.info(f"[推送] {sender_id}: {decision.reason}")

            except Exception as e:
                logger.error(f"[自动签到] {sender_id} 失败: {e}", exc_info=True)

            # 随机间隔防风控
            interval = self.config.get("sign_interval_seconds", 2) * random.uniform(0.5, 1.5)
            await asyncio.sleep(interval)

    # ==================== 指令系统 ====================

    @filter.command_group("skland")
    def skland():
        """/skland 指令组"""
        pass

    # ---- 帮助 ----

    @skland.command("help")
    async def help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        yield event.plain_result(
            "🌠 森空岛自动签到 v2.0\n\n"
            "📋 可用指令：\n"
            "  /skland bind <token>    绑定鹰角通行证 token（最快）\n"
            "  /skland login           通过手机号+验证码登录绑定\n"
            "  /skland sign            立即手动签到\n"
            "  /skland status          查看签到状态\n"
            "  /skland push on|off     开关自动推送通知\n"
            "  /skland time [set HH:MM] 查看/设置签到时间\n"
            "  /skland did             查看设备指纹状态\n"
            "  /skland unbind          解绑账号\n\n"
            "🔧 管理员指令：\n"
            "  /skland list            查看所有绑定用户\n"
            "  /skland remove <id>     移除指定用户的绑定\n"
            "  /skland broadcast <msg> 向所有用户群发\n\n"
            "💡 推荐使用 /skland login 手机号登录（无需浏览器）\n"
            "💡 也可用 /skland bind 绑定 token\n\n"
            "📌 绑定后自动签到，结果私聊推送。"
        )

    # ---- Token 绑定 ----

    @skland.command("bind")
    async def bind(self, event: AstrMessageEvent, token: str = None):
        """绑定鹰角通行证 token"""
        from .handlers.bind import handle_bind
        async for msg in handle_bind(self, event, token):
            yield msg

    # ---- 手机验证码登录 ----

    @skland.command("login")
    async def login(self, event: AstrMessageEvent):
        """通过手机号+验证码登录绑定"""
        from .handlers.bind import handle_login
        async for msg in handle_login(self, event):
            yield msg

    # ---- 手动签到 ----

    @skland.command("sign")
    async def sign(self, event: AstrMessageEvent):
        """立即手动签到"""
        from .handlers.sign import handle_sign
        async for msg in handle_sign(self, event):
            yield msg

    # ---- 推送开关 ----

    @skland.command("push")
    async def push_toggle(self, event: AstrMessageEvent, action: str = None):
        """开关自动推送通知"""
        from .handlers.sign import handle_push_toggle
        async for msg in handle_push_toggle(self, event, action):
            yield msg

    # ---- 签到时间 ----

    @skland.command("time")
    async def time_config(self, event: AstrMessageEvent, action: str = None, arg: str = None):
        """查看或设置签到时间"""
        from .handlers.sign import handle_time_config
        async for msg in handle_time_config(self, event, action, arg):
            yield msg

    # ---- 签到状态 ----

    @skland.command("status")
    async def status(self, event: AstrMessageEvent):
        """查看签到状态"""
        from .handlers.sign import handle_status
        async for msg in handle_status(self, event):
            yield msg

    # ---- 设备指纹 ----

    @skland.command("did")
    async def did(self, event: AstrMessageEvent):
        """查看设备指纹状态"""
        from .handlers.sign import handle_did
        async for msg in handle_did(self, event):
            yield msg

    # ---- 解绑 ----

    @skland.command("unbind")
    async def unbind(self, event: AstrMessageEvent):
        """解绑账号"""
        from .handlers.bind import handle_unbind
        async for msg in handle_unbind(self, event):
            yield msg

    # ---- 管理员: 查看用户 ----

    @skland.command("list")
    async def list_users(self, event: AstrMessageEvent):
        """管理员查看所有已绑定用户"""
        from .handlers.admin import handle_list_users
        async for msg in handle_list_users(self, event):
            yield msg

    # ---- 管理员: 移除用户 ----

    @skland.command("remove")
    async def remove_user(self, event: AstrMessageEvent, user_id: str = None):
        """管理员移除指定用户的绑定"""
        from .handlers.admin import handle_remove_user
        async for msg in handle_remove_user(self, event, user_id):
            yield msg

    # ---- 管理员: 群发 ----

    @skland.command("broadcast")
    async def broadcast(self, event: AstrMessageEvent):
        """管理员向所有用户群发消息"""
        from .handlers.admin import handle_broadcast
        async for msg in handle_broadcast(self, event):
            yield msg
