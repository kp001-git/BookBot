import re
import logging
from typing import Optional, Union, Dict, Any, List
from aiogram.filters import Filter
from aiogram.types import Message

logger = logging.getLogger(__name__)


class IsAllowedGroupTopic(Filter):
    """
    Filter that validates messages from group or supergroup chats.
    If the chat has forum topics enabled (chat.is_forum is True), only allows
    execution if message_thread_id is None or 1 (i.e. the General topic).
    Silently ignores all other forum topics.
    """

    async def __call__(self, message: Message) -> bool:
        if not message.chat or message.chat.type not in ("group", "supergroup"):
            return False

        is_forum = getattr(message.chat, "is_forum", False)
        if is_forum:
            # General topic in Telegram has message_thread_id of None or 1
            if message.message_thread_id not in (None, 1):
                logger.debug(
                    f"Ignored message from forum chat {message.chat.id} "
                    f"in non-general topic {message.message_thread_id}"
                )
                return False

        return True


class GroupCommandFilter(Filter):
    """
    Filter that matches zero-spam trigger commands:
    Matches messages starting with /<cmd>, #<cmd>, /<cmd>@<bot_username>, or #<cmd>@<bot_username>.
    Returns a dict with 'command_name' and 'query' to be injected into the handler.
    """

    def __init__(self, commands: Union[str, List[str], tuple]):
        if isinstance(commands, str):
            commands = [commands]
        self.commands = [c.lower().lstrip("/#") for c in commands]
        cmds_pattern = "|".join(re.escape(c) for c in self.commands)
        # Matches: ^(?:/|#)(command)(?:@\w+)?(?:\s+(.*))?$
        self.pattern = re.compile(
            rf"^(?:/|#)({cmds_pattern})(?:@\w+)?(?:\s+(.*))?$",
            re.IGNORECASE | re.DOTALL
        )

    async def __call__(self, message: Message) -> Union[bool, Dict[str, Any]]:
        text = message.text or message.caption or ""
        text = text.strip()
        if not text:
            return False

        match = self.pattern.match(text)
        if not match:
            return False

        command_name = match.group(1).lower()
        raw_query = match.group(2) or ""
        query = raw_query.strip()

        return {
            "command_name": command_name,
            "query": query
        }
