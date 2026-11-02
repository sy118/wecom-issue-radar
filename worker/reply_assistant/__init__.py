"""Reply assistant backend: the ``eq_reply`` engine behind the assistant action."""

from .actions import handle_assistant_action

__all__ = ["handle_assistant_action"]
