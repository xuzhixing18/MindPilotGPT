"""登录标识符：邮箱 / 手机号的规范化、校验与类型识别。

产品形态是「一个输入框」：含 ``@`` 视为邮箱，否则按手机号处理。入库前一律规范化，
使同一号码的各种写法（``13800138000`` / ``+86 138 0013 8000`` / ``8613800138000`` /
``0086-138-0013-8000``）落到同一条唯一索引上，杜绝「同号多账号」。

手机号统一存 **E.164**（``+8613800138000``）：国际通用、可直接交给短信服务商，也避免
「本地格式 vs 带国家码」造成的重复账号。无 ``+`` 前缀时按 ``PHONE_DEFAULT_REGION``
（默认 CN）补国家码；显式带 ``+`` 的境外号码照常接受。

本模块只做纯函数式的格式判定，不触库、不发 HTTP；异常用 auth.errors 的语义化类型，
由 main.py 的统一处理器映射状态码。
"""

from __future__ import annotations

import re

from backend.auth.errors import ValidationError

# 邮箱：不做 RFC 5322 全量校验（得不偿失且误伤真实地址），只拦明显错误
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")
MAX_EMAIL_LENGTH = 254  # RFC 3696 上限

# E.164：+ 国家码（首位非 0）+ 号码，数字总长 ≤ 15
_E164_RE = re.compile(r"^\+[1-9]\d{6,14}$")
# 中国大陆手机号：1 开头、第二位 3-9、共 11 位
_CN_MOBILE_RE = re.compile(r"^1[3-9]\d{9}$")

# 输入省略国家码时补的区号（仅部分地区有此输入习惯，按需扩展）
_REGION_DIAL: dict[str, str] = {"CN": "86"}
# 补完区号后还要过的本地格式校验（境外号码不套用本地规则）
_LOCAL_RE: dict[str, re.Pattern[str]] = {"86": _CN_MOBILE_RE}

EMAIL = "email"
PHONE = "phone"


def normalize_email(raw: str | None) -> str | None:
    """邮箱规范化：去首尾空白 + 转小写（保证唯一索引命中同一账号）；空 → None。"""
    value = (raw or "").strip().lower()
    return value or None


def validate_email(raw: str | None) -> str:
    """规范化并校验邮箱，非法抛 :class:`ValidationError`。"""
    email = normalize_email(raw)
    if not email:
        raise ValidationError("请输入邮箱。")
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL_RE.match(email):
        raise ValidationError("邮箱格式不正确，请检查后重试。")
    return email


def normalize_phone(raw: str | None, *, region: str = "CN") -> str | None:
    """手机号规范化为 E.164；空或无法识别 → None（由 :func:`validate_phone` 报错）。

    容忍：空格 / 连字符 / 点 / 括号、``00`` 国际前缀、国家码写成 ``86...`` 而非 ``+86...``、
    完全省略国家码（按 region 补）。
    """
    value = re.sub(r"[\s\-.()]", "", (raw or "").strip())
    if not value:
        return None
    if value.startswith("00"):  # 0086... 这类国际拨号前缀
        value = "+" + value[2:]
    if value.startswith("+"):
        return value if _E164_RE.match(value) else None
    dial = _REGION_DIAL.get((region or "CN").upper())
    if not dial:
        return None  # 未知地区且未带 +：无法确定国家码，判为非法
    local_re = _LOCAL_RE.get(dial)
    # 「8613800138000」这类写法：仅当去掉国家码后仍符合本地格式时才视为已带国家码，
    # 避免把真实以 86 开头的本地号码误削。
    if local_re and value.startswith(dial) and local_re.match(value[len(dial):]):
        return f"+{value}"
    candidate = f"+{dial}{value}"
    return candidate if _E164_RE.match(candidate) else None


def validate_phone(raw: str | None, *, region: str = "CN") -> str:
    """规范化并校验手机号，非法抛 :class:`ValidationError`。返回 E.164 串。"""
    phone = normalize_phone(raw, region=region)
    if not phone:
        raise ValidationError("手机号格式不正确，请输入有效的手机号码。")
    dial = _REGION_DIAL.get((region or "CN").upper())
    local_re = _LOCAL_RE.get(dial or "")
    if local_re and phone.startswith(f"+{dial}") and not local_re.match(phone[1 + len(dial):]):
        raise ValidationError("手机号格式不正确，请输入有效的中国大陆手机号。")
    return phone


def classify(raw: str | None) -> str:
    """粗判输入类型：含 ``@`` 即邮箱，否则按手机号处理。"""
    return EMAIL if "@" in (raw or "") else PHONE


def parse_identifier(raw: str | None, *, region: str = "CN") -> tuple[str, str]:
    """把登录框的单个输入解析成 ``(kind, 规范化值)``；空或非法抛 :class:`ValidationError`。

    kind 为 :data:`EMAIL` 或 :data:`PHONE`，调用方据此决定按哪一列查库。
    """
    text = (raw or "").strip()
    if not text:
        raise ValidationError("请输入邮箱或手机号。")
    if classify(text) == EMAIL:
        return EMAIL, validate_email(text)
    return PHONE, validate_phone(text, region=region)


def mask_phone(phone: str | None) -> str:
    """手机号脱敏展示（``****8000``），用于日志与非本人场景。

    刻意只保留末 4 位：国家码与号段前缀看似无害，但拼上其他泄露字段就能大幅
    缩小爆破空间，故不做「部分保留」的折中。
    """
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "****"
    return f"****{digits[-4:]}"
