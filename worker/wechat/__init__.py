"""Personal WeChat backend, ported from ProxyTalk V0.0.3.

The module exposes one entry point, :func:`worker.wechat.actions.handle_wechat_action`,
which the Python worker dispatch calls for ``wechat_action`` requests.  All
WeChat access is read-only; writing into the WeChat input box is owned by the
desktop shell's Rust input helper.
"""

from __future__ import annotations

from .actions import handle_wechat_action

__all__ = ["handle_wechat_action"]
__version__ = "0.1.0"
