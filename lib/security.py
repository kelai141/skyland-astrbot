"""
数美设备指纹 (dId) 生成模块
移植自: https://gitee.com/FancyCabbage/skyland-auto-sign

支持 dId 持久化缓存到磁盘，避免每次重启都重新计算。
如果数美 API 不可用，自动使用缓存或生成 fallback dId。
"""
import base64
import gzip
import hashlib
import json
import logging
import os
import time
import uuid

import requests

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers.base import Cipher
from cryptography.hazmat.primitives.ciphers.modes import CBC, ECB

logger = logging.getLogger(__name__)

# 查询dId请求头
devices_info_url = "https://fp-it.portal101.cn/deviceprofile/v4"

# 数美配置
SM_CONFIG = {
    "organization": "UWXspnCCJN4sfYlNfqps",
    "appId": "default",
    "publicKey": "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCmxMNr7n8ZeT0tE1R9j/mPixoinPkeM+k4VGIn/s0k7N5rJAfnZ0eMER+QhwFvshzo0LNmeUkpR8uIlU/GEVr8mN28sKmwd2gpygqj0ePnBmOW4v0ZVwbSYK+izkhVFk2V/doLoMbWy6b+UnA8mkjvg0iYWRByfRsK2gdl7llqCwIDAQAB",
    "protocol": "https",
    "apiHost": "fp-it.portal101.cn"
}

PK = serialization.load_der_public_key(base64.b64decode(SM_CONFIG['publicKey']))

DES_RULE = {
    "appId": {"cipher": "DES", "is_encrypt": 1, "key": "uy7mzc4h", "obfuscated_name": "xx"},
    "box": {"is_encrypt": 0, "obfuscated_name": "jf"},
    "canvas": {"cipher": "DES", "is_encrypt": 1, "key": "snrn887t", "obfuscated_name": "yk"},
    "clientSize": {"cipher": "DES", "is_encrypt": 1, "key": "cpmjjgsu", "obfuscated_name": "zx"},
    "organization": {"cipher": "DES", "is_encrypt": 1, "key": "78moqjfc", "obfuscated_name": "dp"},
    "os": {"cipher": "DES", "is_encrypt": 1, "key": "je6vk6t4", "obfuscated_name": "pj"},
    "platform": {"cipher": "DES", "is_encrypt": 1, "key": "pakxhcd2", "obfuscated_name": "gm"},
    "plugins": {"cipher": "DES", "is_encrypt": 1, "key": "ioy1geet", "obfuscated_name": "ul"},
    "pmf": {"cipher": "DES", "is_encrypt": 1, "key": "chz98fi1", "obfuscated_name": "mx"},
    "protocol": {"cipher": "DES", "is_encrypt": 1, "key": "qzlh3rfw", "obfuscated_name": "dv"},
    "referer": {"cipher": "DES", "is_encrypt": 1, "key": "n6vuedk4", "obfuscated_name": "jd"},
    "res": {"cipher": "DES", "is_encrypt": 1, "key": "tn5yqsru", "obfuscated_name": "pf"},
    "rtype": {"cipher": "DES", "is_encrypt": 1, "key": "ehyrblnu", "obfuscated_name": "if"},
    "sdkver": {"cipher": "DES", "is_encrypt": 1, "key": "vox0mspz", "obfuscated_name": "cf"},
    "smid": {"cipher": "DES", "is_encrypt": 1, "key": "j0q0kxyf", "obfuscated_name": "as"},
    "status": {"cipher": "DES", "is_encrypt": 1, "key": "j5x7m9vq", "obfuscated_name": "mf"},
    "subVersion": {"cipher": "DES", "is_encrypt": 1, "key": "eo3i2puh", "obfuscated_name": "ns"},
    "svm": {"cipher": "DES", "is_encrypt": 1, "key": "fzj3kaeh", "obfuscated_name": "qr"},
    "time": {"cipher": "DES", "is_encrypt": 1, "key": "q2t3odsk", "obfuscated_name": "nb"},
    "timezone": {"cipher": "DES", "is_encrypt": 1, "key": "1uv05lj5", "obfuscated_name": "as"},
    "tn": {"cipher": "DES", "is_encrypt": 1, "key": "x9nzj1bp", "obfuscated_name": "py"},
    "trees": {"cipher": "DES", "is_encrypt": 1, "key": "acfs0xo4", "obfuscated_name": "pi"},
    "ua": {"cipher": "DES", "is_encrypt": 1, "key": "k92crp1t", "obfuscated_name": "bj"},
    "url": {"cipher": "DES", "is_encrypt": 1, "key": "y95hjkoo", "obfuscated_name": "cf"},
    "version": {"is_encrypt": 0, "obfuscated_name": "version"},
    "vpw": {"cipher": "DES", "is_encrypt": 1, "key": "r9924ab5", "obfuscated_name": "ca"}
}

BROWSER_ENV = {
    'plugins': 'MicrosoftEdgePDFPluginPortableDocumentFormatinternal-pdf-viewer1,MicrosoftEdgePDFViewermhjfbmdgcfjbbpaeojofohoefgiehjai1',
    'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0',
    'canvas': '259ffe69',
    'timezone': -480,
    'platform': 'Win32',
    'url': 'https://www.skland.com/',
    'referer': '',
    'res': '1920_1080_24_1.25',
    'clientSize': '0_0_1080_1920_1920_1080_1920_1080',
    'status': '0011',
}

# 持久化缓存文件路径（由 set_cache_dir 设置）
_DID_CACHE_DIR = None
_DID_CACHE_FILE = None


def set_cache_dir(cache_dir: str):
    """设置 dId 持久化缓存目录

    应在插件初始化时调用，传入 plugin_data 路径。
    缓存文件路径为 {cache_dir}/did.cache
    """
    global _DID_CACHE_DIR, _DID_CACHE_FILE
    _DID_CACHE_DIR = cache_dir
    try:
        os.makedirs(cache_dir, exist_ok=True)
        _DID_CACHE_FILE = os.path.join(cache_dir, "did.cache")
    except Exception as e:
        logger.warning(f"创建 dId 缓存目录失败: {e}")


def _load_cached_did() -> str:
    """从磁盘加载缓存的 dId"""
    if _DID_CACHE_FILE and os.path.exists(_DID_CACHE_FILE):
        try:
            with open(_DID_CACHE_FILE, "r") as f:
                did = f.read().strip()
                if did and did.startswith("B"):
                    return did
        except Exception as e:
            logger.warning(f"读取 dId 缓存失败: {e}")
    return ""


def _save_did_cache(did: str):
    """将 dId 保存到磁盘缓存"""
    if _DID_CACHE_FILE and did:
        try:
            with open(_DID_CACHE_FILE, "w") as f:
                f.write(did)
            logger.info(f"dId 已缓存到磁盘: {did[:20]}...")
        except Exception as e:
            logger.warning(f"保存 dId 缓存失败: {e}")


def _generate_fallback_did() -> str:
    """生成 fallback dId

    当数美 API 不可用时使用。
    格式: B + 32位十六进制字符（与数美返回的格式一致）
    """
    fallback = 'B' + hashlib.md5(str(uuid.uuid4()).encode()).hexdigest()
    logger.warning(f"数美API不可用，使用 fallback dId")
    return fallback


def _try_call_shumei_api() -> str:
    """尝试调用数美 API 获取 dId"""
    uid = str(uuid.uuid4()).encode('utf-8')
    priId = hashlib.md5(uid).hexdigest()[0:16]
    ep = PK.encrypt(uid, padding.PKCS1v15())
    ep = base64.b64encode(ep).decode('utf-8')

    browser = BROWSER_ENV.copy()
    current_time = int(time.time() * 1000)
    browser.update({
        'vpw': str(uuid.uuid4()),
        'svm': current_time,
        'trees': str(uuid.uuid4()),
        'pmf': current_time
    })

    des_target = {
        **browser,
        'protocol': 102,
        'organization': SM_CONFIG['organization'],
        'appId': SM_CONFIG['appId'],
        'os': 'web',
        'version': '3.0.0',
        'sdkver': '3.0.0',
        'box': '',
        'rtype': 'all',
        'smid': _get_smid(),
        'subVersion': '1.0.0',
        'time': 0
    }
    des_target['tn'] = hashlib.md5(get_tn(des_target).encode()).hexdigest()

    des_result = _AES(GZIP(_DES(des_target)), priId.encode('utf-8'))

    response = requests.post(devices_info_url, json={
        'appId': 'default',
        'compress': 2,
        'data': des_result,
        'encode': 5,
        'ep': ep,
        'organization': SM_CONFIG['organization'],
        'os': 'web'
    }, timeout=10)

    resp = response.json()
    if resp['code'] != 1100:
        raise Exception(f"数美API返回异常: code={resp.get('code', 'unknown')}")

    return 'B' + resp['detail']['deviceId']


def get_d_id() -> str:
    """获取设备指纹 dId

    优先级策略:
      1. 内存缓存（由 skyland.py 管理）
      2. 磁盘缓存（持久化，重启不丢）
      3. 数美 API（实时生成，3次重试）
      4. fallback dId（UUID-based）

    调用方负责缓存结果到内存。
    """
    # 1. 尝试从磁盘加载缓存的 dId
    cached = _load_cached_did()
    if cached:
        logger.info("使用磁盘缓存的 dId")
        return cached

    # 2. 尝试调用数美 API（3次重试）
    last_error = None
    for attempt in range(3):
        try:
            did = _try_call_shumei_api()
            _save_did_cache(did)
            return did
        except Exception as e:
            last_error = e
            logger.warning(f"dId 生成第 {attempt + 1} 次尝试失败: {e}")
            if attempt < 2:
                time.sleep(1)

    # 3. 所有方式失败，生成 fallback
    logger.warning(f"数美API均不可用: {last_error}")
    fallback = _generate_fallback_did()
    _save_did_cache(fallback)
    logger.warning("使用 fallback dId，森空岛API可能拒绝请求")
    return fallback


def _get_smid():
    """生成 SMID"""
    t = time.localtime()
    _time = '{}{:0>2d}{:0>2d}{:0>2d}{:0>2d}{:0>2d}'.format(
        t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec
    )
    uid = str(uuid.uuid4())
    v = _time + hashlib.md5(uid.encode('utf-8')).hexdigest() + '00'
    smsk_web = hashlib.md5(('smsk_web_' + v).encode('utf-8')).hexdigest()[0:14]
    return v + smsk_web + '0'


def _DES(o: dict):
    """DES 加密规则"""
    result = {}
    for i in o.keys():
        if i in DES_RULE.keys():
            rule = DES_RULE[i]
            res = o[i]
            if rule['is_encrypt'] == 1:
                c = Cipher(TripleDES(rule['key'].encode('utf-8')), ECB())
                data = str(res).encode('utf-8')
                data += b'\x00' * 8
                res = base64.b64encode(c.encryptor().update(data)).decode('utf-8')
            result[rule['obfuscated_name']] = res
        else:
            result[i] = o[i]
    return result


def _AES(v: bytes, k: bytes):
    """AES 加密"""
    iv = '0102030405060708'
    key = AES(k)
    c = Cipher(key, CBC(iv.encode('utf-8')))
    v += b'\x00'
    while len(v) % 16 != 0:
        v += b'\x00'
    return c.encryptor().update(v).hex()


def GZIP(o: dict):
    """GZIP 压缩"""
    json_str = json.dumps(o, ensure_ascii=False)
    stream = gzip.compress(json_str.encode('utf-8'), 2, mtime=0)
    return base64.b64encode(stream)


def get_tn(o: dict):
    """计算 tn 值"""
    sorted_keys = sorted(o.keys())
    result_list = []
    for i in sorted_keys:
        v = o[i]
        if isinstance(v, (int, float)):
            v = str(v * 10000)
        elif isinstance(v, dict):
            v = get_tn(v)
        result_list.append(v)
    return ''.join(result_list)
