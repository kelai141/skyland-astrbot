"""
签到与状态相关命令处理器

处理: /skland sign, /skland status, /skland push, /skland time
"""
from datetime import date, datetime

from astrbot.api.event import AstrMessageEvent

from ..lib.notification import NotificationTemplates, PushPolicy
from ..lib.skyland_engine import SignResult


async def handle_sign(plugin, event: AstrMessageEvent):
    """处理 /skland sign（手动签到）"""
    sid = plugin._get_sender_id(event)
    state = plugin._load_user_state(sid)

    if state is None:
        yield event.plain_result(
            "❌ 你还没有绑定账号！\n"
            "请使用 /skland bind <token> 或 /skland login 绑定。"
        )
        return

    yield event.plain_result("⏳ 正在签到，请稍候…")

    try:
        result = await plugin.engine.sign(state)
    except Exception as e:
        yield event.plain_result(f"❌ 签到失败: {e}")
        return

    # 保存状态
    plugin._save_user_state(sid, state)

    # 格式化结果
    decision = PushPolicy.decide(state, result, is_manual=True)
    yield event.plain_result(decision.message)


async def handle_push_toggle(plugin, event: AstrMessageEvent, action: str = None):
    """处理 /skland push [on|off]"""
    sid = plugin._get_sender_id(event)
    state = plugin._load_user_state(sid)

    if state is None:
        yield event.plain_result("❌ 你还没有绑定账号！")
        return

    if not action or action not in ("on", "off"):
        enabled = state.push_enabled
        yield event.plain_result(
            f"📢 自动推送: {'🟢 已开启' if enabled else '🔴 已关闭'}\n"
            f"修改: /skland push on 或 /skland push off"
        )
        return

    state.push_enabled = (action == "on")
    plugin._save_user_state(sid, state)

    msg = (
        f"📢 自动推送已{'开启' if action == 'on' else '关闭'}"
        + ("\n签到完成后会私聊通知你。" if action == "on" else "\n不会再主动推送签到结果。")
    )
    yield event.plain_result(msg)


async def handle_time_config(plugin, event: AstrMessageEvent, action: str = None, arg: str = None):
    """处理 /skland time [set HH:MM]"""
    sid = plugin._get_sender_id(event)
    state = plugin._load_user_state(sid)

    if state is None:
        yield event.plain_result("❌ 你还没有绑定账号！")
        return

    if action == "set" and arg:
        try:
            parts = arg.split(":")
            h, m = int(parts[0]), int(parts[1])
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except (ValueError, IndexError):
            yield event.plain_result(
                "❌ 时间格式错误，请使用 HH:MM\n例如: /skland time set 08:30"
            )
            return

        state.sign_time = f"{h:02d}:{m:02d}"
        plugin._save_user_state(sid, state)

        # 如果当前时间 ≥ 设置的时间 且 今天还没签过 → 立即触发签到
        today = date.today().isoformat()
        now = datetime.now()
        if (h < now.hour or (h == now.hour and m <= now.minute)) \
                and state.last_sign_date != today:
            yield event.plain_result(
                f"⏰ 签到时间已设置为 每天 {state.sign_time}\n"
                f"⏳ 今日 {state.sign_time} 已过，现在为你执行签到…"
            )
            async for msg in handle_sign(plugin, event):
                yield msg
        else:
            yield event.plain_result(f"⏰ 签到时间已设置为 每天 {state.sign_time}")
    else:
        yield event.plain_result(
            f"⏰ 当前签到时间: 每天 {state.sign_time}\n"
            f"修改: /skland time set HH:MM\n"
            f"例如: /skland time set 08:30"
        )


async def handle_status(plugin, event: AstrMessageEvent):
    """处理 /skland status"""
    sid = plugin._get_sender_id(event)
    state = plugin._load_user_state(sid)

    if state is None:
        yield event.plain_result("❌ 你还没有绑定账号！")
        return

    report = NotificationTemplates.status_report(state)
    yield event.plain_result(report)


async def handle_did(plugin, event: AstrMessageEvent):
    """处理 /skland did"""
    from ..lib.security import _load_cached_did, _DID_CACHE_FILE
    import os

    cached = _load_cached_did()
    lines = ["📟 设备指纹 (dId) 状态"]

    if cached:
        lines.append(f"✅ 已缓存: {cached[:16]}...{cached[-8:]}")
        lines.append(f"📁 缓存文件: {_DID_CACHE_FILE or '未设置'}")

        if len(cached) < 40:
            lines.append("⚠️ 当前使用的是 fallback dId")
            lines.append("   森空岛 API 可能拒绝请求")
            lines.append("   尝试在可访问 fp-it.portal101.cn 的网络下重载插件")
        else:
            lines.append("✅ dId 来自数美 API")

        if _DID_CACHE_FILE and os.path.exists(_DID_CACHE_FILE):
            mtime = datetime.fromtimestamp(os.path.getmtime(_DID_CACHE_FILE))
            lines.append(f"🕐 缓存时间: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    else:
        lines.append("❌ 未生成 dId")
        lines.append("   请重载插件以重新生成")

    lines.append("")
    lines.append("💡 dId 是设备指纹，用于森空岛 API 鉴权")
    lines.append("   如果遇到登录问题，可删除缓存文件后重载插件")

    yield event.plain_result("\n".join(lines))
