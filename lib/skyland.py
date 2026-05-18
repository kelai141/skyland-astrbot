"""
森空岛签到核心模块
移植自: https://gitee.com/FancyCabbage/skyland-auto-sign

提供异步的签到客户端，封装了：
- token → grant_code → cred 的凭证流程
- HMAC-SHA256 + MD5 签名算法
- 明日方舟 / 终末地 签到
- Token 刷新
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
from urllib import parse

import aiohttp

from .security import get_d_id

logger = logging.getLogger(__name__)

# 常量
APP_CODE = '4ca99fa6b56cc2ba'

# 请求头
HEADER = {
    'cred': '',
    'User-Agent': ('Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Safari/537.36; '
                   'SKLand/1.52.1'),
    'Accept-Encoding': 'gzip',
    'Connection': 'close',
    'X-Requested-With': 'com.hypergryph.skland'
}

# 登录用请求头（dId 懒加载，避免模块导入时调用同步网络请求）
_LOGIN_HEADER_CACHE = None

def _get_login_header() -> dict:
    """懒加载并缓存登录请求头（含 dId）"""
    global _LOGIN_HEADER_CACHE
    if _LOGIN_HEADER_CACHE is None:
        _LOGIN_HEADER_CACHE = {
            'User-Agent': ('Mozilla/5.0 (Linux; Android 12; SM-A5560 Build/V417IR; wv) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/101.0.4951.61 Safari/537.36; '
                           'SKLand/1.52.1'),
            'Accept-Encoding': 'gzip',
            'Connection': 'close',
            'dId': get_d_id(),
            'X-Requested-With': 'com.hypergryph.skland'
        }
    return _LOGIN_HEADER_CACHE


# 签名请求头模板（含 dId，与原始 skyland-auto-sign 完全一致）
# 原始 header_for_sign: {'platform':'3','timestamp':'','dId':header_login['dId'],'vName':'1.0.0'}
# 签名时务必包含 dId，否则森空岛 API 可能拒绝请求
_SIGN_HEADER_CACHE = None

def _get_sign_header_template() -> dict:
    """懒加载并缓存签名请求头模板（含 dId）"""
    global _SIGN_HEADER_CACHE
    if _SIGN_HEADER_CACHE is None:
        _SIGN_HEADER_CACHE = {
            'platform': '3',
            'timestamp': '',
            'dId': get_d_id(),
            'vName': '1.0.0'
        }
    return _SIGN_HEADER_CACHE

# API 地址
SIGN_URL_MAPPING = {
    'arknights': 'https://zonai.skland.com/api/v1/game/attendance',
    'endfield': 'https://zonai.skland.com/web/v1/game/endfield/attendance'
}
BINDING_URL = 'https://zonai.skland.com/api/v1/game/player/binding'
LOGIN_CODE_URL = 'https://as.hypergryph.com/general/v1/send_phone_code'
TOKEN_PHONE_CODE_URL = 'https://as.hypergryph.com/user/auth/v2/token_by_phone_code'
TOKEN_PASSWORD_URL = 'https://as.hypergryph.com/user/auth/v1/token_by_phone_password'
GRANT_CODE_URL = 'https://as.hypergryph.com/user/oauth2/v2/grant'
CRED_CODE_URL = 'https://zonai.skland.com/web/v1/user/auth/generate_cred_by_code'
REFRESH_TOKEN_URL = 'https://zonai.skland.com/web/v1/auth/refresh'


def generate_signature(path: str, body_or_query: str, token: str):
    """
    生成签名头

    算法：HMAC-SHA256(路径 + 请求体/查询 + 时间戳 + 请求头关键参数) → MD5
    """
    t = str(int(time.time()) - 2)
    header_ca = dict(_get_sign_header_template())
    header_ca['timestamp'] = t
    header_ca_str = json.dumps(header_ca, separators=(',', ':'))
    s = path + body_or_query + t + header_ca_str
    hex_s = hmac.new(
        token.encode('utf-8'), s.encode('utf-8'), hashlib.sha256
    ).hexdigest()
    md5 = hashlib.md5(hex_s.encode('utf-8')).hexdigest()
    logger.debug(f'生成签名: {md5}')
    return md5, header_ca


def get_sign_header(url: str, method: str, body: Optional[dict], h: dict, token: str):
    """为请求添加签名头"""
    p = parse.urlparse(url)
    if method.lower() == 'get':
        h['sign'], header_ca = generate_signature(p.path, p.query, token)
    else:
        body_str = json.dumps(body) if body is not None else ''
        h['sign'], header_ca = generate_signature(p.path, body_str, token)
    for key, value in header_ca.items():
        h[key] = value
    return h


async def api_post(session: aiohttp.ClientSession, url: str, json_data: dict = None,
                   headers: dict = None) -> dict:
    """异步 POST 请求（带日志）"""
    import time as _time
    start = _time.time()
    logger.info(f"🔗 POST {url}")
    try:
        async with session.post(url, json=json_data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            result = await resp.json()
            elapsed = (_time.time() - start) * 1000
            status = result.get('status') or result.get('code')
            if status != 0:
                logger.warning(f"  ⚠️ [{resp.status}] {elapsed:.0f}ms → status={status} msg={result.get('message') or result.get('msg', result)}")
            else:
                logger.info(f"  ✅ [{resp.status}] {elapsed:.0f}ms")
            return result
    except Exception as e:
        elapsed = (_time.time() - start) * 1000
        logger.error(f"  ❌ {elapsed:.0f}ms → {type(e).__name__}: {e}")
        raise


async def api_get(session: aiohttp.ClientSession, url: str,
                  headers: dict = None) -> dict:
    """异步 GET 请求（带日志）"""
    import time as _time
    start = _time.time()
    logger.info(f"🔗 GET {url}")
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            result = await resp.json()
            elapsed = (_time.time() - start) * 1000
            status = result.get('status') or result.get('code')
            if status not in (0, None):
                logger.warning(f"  ⚠️ [{resp.status}] {elapsed:.0f}ms → status={status} msg={result.get('message') or result.get('msg', result)}")
            else:
                logger.info(f"  ✅ [{resp.status}] {elapsed:.0f}ms")
            return result
    except Exception as e:
        elapsed = (_time.time() - start) * 1000
        logger.error(f"  ❌ {elapsed:.0f}ms → {type(e).__name__}: {e}")
        raise


def parse_user_token(t: str) -> str:
    """解析用户输入的 token（可能直接从网页复制的是 JSON）"""
    try:
        t = json.loads(t)
        return t['data']['content']
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return t


async def get_grant_code(session: aiohttp.ClientSession, token: str) -> str:
    """用 token 换取 grant_code"""
    resp = await api_post(session, GRANT_CODE_URL, json_data={
        'appCode': APP_CODE,
        'token': token,
        'type': 0
    }, headers=_get_login_header())
    if resp.get('status') != 0:
        raise Exception(f'获取认证代码失败: {resp.get("msg", resp)}')
    return resp['data']['code']


async def get_cred(session: aiohttp.ClientSession, grant: str) -> dict:
    """用 grant_code 换取 cred"""
    resp = await api_post(session, CRED_CODE_URL, json_data={
        'code': grant,
        'kind': 1
    }, headers=_get_login_header())
    if resp.get('code') != 0:
        raise Exception(f'获取 cred 失败: {resp.get("message", resp)}')
    return resp['data']


async def get_cred_by_token(session: aiohttp.ClientSession, token: str) -> dict:
    """完整流程：token → grant_code → cred"""
    grant_code = await get_grant_code(session, token)
    return await get_cred(session, grant_code)


async def refresh_token(session: aiohttp.ClientSession, token: str, cred: str) -> str:
    """刷新 token"""
    headers = HEADER.copy()
    headers['cred'] = cred
    headers = get_sign_header(REFRESH_TOKEN_URL, 'get', None, headers, token)
    resp = await api_get(session, REFRESH_TOKEN_URL, headers=headers)
    if resp.get('code') != 0:
        raise Exception(f'刷新 token 失败: {resp.get("message", resp)}')
    return resp['data']['token']


async def get_binding_list(session: aiohttp.ClientSession, token: str, cred: str) -> list:
    """获取已绑定的游戏角色列表"""
    headers = HEADER.copy()
    headers['cred'] = cred
    headers = get_sign_header(BINDING_URL, 'get', None, headers, token)

    resp = await api_get(session, BINDING_URL, headers=headers)
    if resp.get('code') != 0:
        raise Exception(f'获取角色列表失败: {resp.get("message", resp)}')

    characters = []
    for game in resp['data']['list']:
        if game.get('appCode') not in ('arknights', 'endfield'):
            continue
        for char in game.get('bindingList', []):
            char['appCode'] = game['appCode']
            characters.append(char)
    return characters


async def sign_for_arknights(session: aiohttp.ClientSession, token: str, cred: str,
                             char_data: dict) -> str:
    """为明日方舟角色签到"""
    body = {
        'gameId': char_data.get('gameId'),
        'uid': char_data.get('uid')
    }
    url = SIGN_URL_MAPPING['arknights']
    headers = HEADER.copy()
    headers['cred'] = cred
    headers = get_sign_header(url, 'post', body, headers, token)

    resp = await api_post(session, url, json_data=body, headers=headers)
    game_name = char_data.get('gameName', '明日方舟')
    channel = char_data.get('channelName', '')
    nickname = char_data.get('nickName', '')

    if resp.get('code') != 0:
        return f'❌ [{game_name}] {nickname}({channel}) 签到失败: {resp.get("message", "未知错误")}'

    result = ''
    awards = resp['data']['awards']
    for j in awards:
        res = j['resource']
        result += f'{res["name"]}×{j.get("count", 1)} '
    return f'✅ [{game_name}] {nickname}({channel}) 签到成功，获得: {result.strip()}'


async def sign_for_endfield(session: aiohttp.ClientSession, token: str, cred: str,
                            char_data: dict) -> list:
    """为终末地角色签到（可能有多个角色）"""
    roles: list = char_data.get('roles', [])
    game_name = char_data.get('gameName', '终末地')
    channel = char_data.get('channelName', '')
    results = []

    for role in roles:
        nickname = role.get('nickname', '')
        url = SIGN_URL_MAPPING['endfield']
        headers = HEADER.copy()
        headers['cred'] = cred
        headers['Content-Type'] = 'application/json'
        headers['dId'] = _get_login_header()['dId']
        headers['sk-game-role'] = f'3_{role["roleId"]}_{role["serverId"]}'
        headers['referer'] = 'https://game.skland.com/'
        headers['origin'] = 'https://game.skland.com/'
        headers = get_sign_header(url, 'post', None, headers, token)

        resp = await api_post(session, url, json_data=None, headers=headers)
        j = resp if isinstance(resp, dict) else await resp.json()

        if j.get('code') != 0:
            results.append(f'❌ [{game_name}] {nickname}({channel}) 签到失败: {j.get("message", "未知错误")}')
        else:
            awards_result = []
            result_data = j['data']
            result_info_map = result_data['resourceInfoMap']
            for a in result_data['awardIds']:
                award_id = a['id']
                awards = result_info_map[str(award_id)] if str(award_id) in result_info_map else result_info_map.get(award_id, {})
                award_name = awards.get('name', '未知')
                award_count = awards.get('count', 1)
                awards_result.append(f'{award_name}×{award_count}')
            results.append(f'✅ [{game_name}] {nickname}({channel}) 签到成功，获得: {", ".join(awards_result)}')

    return results


async def do_sign(session: aiohttp.ClientSession, token: str, cred: str) -> list:
    """
    执行完整签到流程

    Args:
        session: aiohttp 会话
        token: 鹰角网络通行证 token
        cred: 森空岛 cre      d

    Returns:
        签到结果消息列表
    """
    characters = await get_binding_list(session, token, cred)
    logs = []

    for char in characters:
        app_code = char['appCode']
        try:
            if app_code == 'arknights':
                msg = await sign_for_arknights(session, token, cred, char)
                logs.append(msg)
            elif app_code == 'endfield':
                msgs = await sign_for_endfield(session, token, cred, char)
                logs.extend(msgs)
            logger.info(msg if isinstance(msg, str) else str(msgs))
        except Exception as e:
            err_msg = f'❌ [{char.get("gameName", "未知")}] 签到异常: {e}'
            logs.append(err_msg)
            logger.error(err_msg, exc_info=e)

    return logs


async def verify_token(token: str) -> tuple[bool, str, Optional[dict]]:
    """
    验证 token 是否有效，并返回 cred 和角色信息

    Returns:
        (是否成功, 消息, cred_response 或 None)
    """
    try:
        # 对可能来自控制台的 JSON 格式 token 做解析
        token = parse_user_token(token)
    except Exception:
        pass

    try:
        async with aiohttp.ClientSession() as session:
            cred_resp = await get_cred_by_token(session, token)
            characters = await get_binding_list(session, token, cred_resp.get('cred', ''))

            game_info = []
            for char in characters:
                game_name = char.get('gameName', '')
                nickname = char.get('nickName', '') or char.get('nickname', '')
                channel = char.get('channelName', '')
                game_info.append(f'{game_name}({nickname}@{channel})')

            info = '、'.join(game_info) if game_info else '未检测到可签到的游戏角色'
            return True, info, cred_resp

    except Exception as e:
        return False, f'验证失败: {e}', None
