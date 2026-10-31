"""Facts shared by every province on the State Grid PMOS trading platform.

All three sites (pmos.ah / pmos.ln / pmos.sx .sgcc.com.cn) run the same Vue
front end and gateway. Province profiles may extend these defaults but should
not need to redefine them. Evidence for each fact is in docs/ARCHITECTURE.md.
"""
from __future__ import annotations

import re

# Request headers that the front end computes and the gateway checks. They are
# captured from the page's own requests and replayed in the same frame; cookies
# (X-Ticket, Admin-Token<port>) travel automatically with credentials:'include'.
REPLAY_HEADERS = frozenset({"accept", "content-type", "clienttag", "currentroute", "x-token",
                            "secure-sign-reponse"})

# Business status codes in JSON envelopes {"status": .., "message": .., "data": ..}.
SUCCESS_STATUS = frozenset({"0", "200"})
AUTH_STATUS = frozenset({"401", "10003", "300051"})
PERMISSION_STATUS = frozenset({"403", "40302"})
RATE_LIMIT_STATUS = frozenset({"429"})

RATE_LIMIT_TEXT = re.compile(r"访问过于频繁|请求过于频繁|请求太频繁|限流|too many requests", re.I)
AUTH_TEXT = re.compile(r"会话已注销|会话已过期|登[录陆]已?(?:过期|失效|超时)|请重新登[录陆]|未授权|unauthorized", re.I)
NETWORK_CHANGED_TEXT = re.compile(r"网络环境发生变化")

# Visible notices (Element UI message boxes/toasts) that mean the login is gone.
NOTICE_SELECTORS = ".el-message-box__message,.el-message__content,.el-notification__content"

# Native background calls that are not business activity and whose failures
# must never stop collection: the site's own renewal timer posts an unresolved
# Promise as its token (always 4000/40302), and token/clock polling.
# Requests a page operation may make besides its one business request, as the
# Shanxi coordinator's owned-frame gate allows them (SSO / gateway exchanges).
GATE_AUTH_PATHS = frozenset({
    "/px-common-authcenter/sso/token", "/px-gateway/GatewayConfig/security",
    "/px-common-authcenter/auth/v2/information", "/px-gateway/GatewayConfig/getGatewayConfig",
})
GATE_STATIC_TYPES = frozenset({"document", "script", "image", "font", "stylesheet", "media"})

NATIVE_BACKGROUND_PATHS = frozenset({
    "/px-common-authority/framework/login/refreshExpireTime",
    "/px-common-authority/framework/login/forAWhile",
    "/px-common-authcenter/sso/token",
    "/px-gateway-Token/getPxGatewayToken",
    "/px-gateway-Token/batchGetPxGatewayToken",
    "/px-gateway/systemTime/getSystemTime",
})

# The server ends a session ~30 (Shanxi) to ~40 (Liaoning) minutes after the
# last *business* request; clock polling does not count as activity.
OBSERVED_IDLE_LOGOUT_SECONDS = 30 * 60


def business_status(body: object) -> str | None:
    if isinstance(body, dict):
        value = body.get("status", body.get("code"))
        if type(value) is int or (isinstance(value, str) and value.isdigit() and len(value) <= 6):
            return str(value)
    return None


def message_text(body: object) -> str:
    if isinstance(body, dict):
        value = body.get("message", body.get("msg", ""))
        return value if isinstance(value, str) else ""
    return ""
