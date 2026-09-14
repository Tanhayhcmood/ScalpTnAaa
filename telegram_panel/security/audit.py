"""
Audit Decorator — convenience decorator for auto-logging handler actions.
Usage::

    @audit("RISK_UPDATE", target_from_arg="param")
    async def update_risk(self, update, context, param):
        ...
"""

import functools
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Fields whose values must be masked in audit logs.
# Any old_value / new_value whose key matches a field in this set will be
# replaced with "***MASKED***" before being written to the audit log.
# This prevents plaintext broker passwords appearing in the audit database
# (Security finding F-01 / M-04 — MEDIUM severity).
_SENSITIVE_FIELD_NAMES = frozenset({
    "password", "passwd", "secret", "token", "key", "api_key", "api_secret",
    "credential", "credentials", "encryption_key", "panel_encryption_key",
    "broker_password", "mt5_password", "account_password",
})


def _mask_if_sensitive(field_name: str, value: str) -> str:
    """Return '***MASKED***' if field_name is sensitive; otherwise return value unchanged."""
    if field_name is None:
        return value
    if str(field_name).lower() in _SENSITIVE_FIELD_NAMES:
        return "***MASKED***"
    return value


def audit(
    action: str,
    description: str = "",
    target_from_arg: Optional[str] = None,
    sensitive_fields: Optional[tuple] = None,
):
    """
    Decorator that automatically records an audit log entry.
    Requires that the decorated method's `self` has an `_auth` AuthMiddleware
    attribute and that `update` is the first positional argument.

    Args:
        action: Audit action label (e.g. "RISK_UPDATE", "ACCOUNT_ADD")
        description: Human-readable description (defaults to action)
        target_from_arg: Name of the function argument to use as the audit target
        sensitive_fields: Tuple of argument names whose values must be masked in
            the audit log. In addition, any field whose name appears in
            _SENSITIVE_FIELD_NAMES is automatically masked regardless of this list.
    """
    _extra_sensitive = frozenset(sensitive_fields or ())

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        async def wrapper(self, update, context, *args, **kwargs):
            result = await fn(self, update, context, *args, **kwargs)

            # Get user for audit
            try:
                if hasattr(self, "_auth"):
                    tg_user = update.effective_user
                    if tg_user:
                        user = await self._auth.get_user(tg_user.id, tg_user)
                        if user:
                            target = None
                            if target_from_arg:
                                all_args = dict(zip(fn.__code__.co_varnames[3:], args))
                                all_args.update(kwargs)
                                raw_target = all_args.get(target_from_arg, "")
                                # Mask target if it came from a sensitive argument
                                if target_from_arg.lower() in _SENSITIVE_FIELD_NAMES \
                                        or target_from_arg in _extra_sensitive:
                                    target = "***MASKED***"
                                else:
                                    target = str(raw_target)
                            desc = description or action
                            await self._auth.record_action(
                                user, action, desc, target=target
                            )
            except Exception as e:
                logger.debug(f"Audit decorator failed: {e}")

            return result
        return wrapper
    return decorator
