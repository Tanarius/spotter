"""Telegram bot wiring for Spotter.

Step 4 scope: long-polling bot whose single message handler routes each
allow-listed text message through the :class:`Brain` and replies with Claude's
answer. Access is restricted to the single allow-listed user; any other sender
is silently ignored.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from .brain import Brain
from .config import Config

logger = logging.getLogger(__name__)


def build_application(config: Config, brain: Brain) -> Application:
    """Construct the Telegram ``Application`` with handlers wired in."""
    application = Application.builder().token(config.telegram_bot_token).build()
    # Stash dependencies on bot_data so handlers can reach them without globals.
    application.bot_data["config"] = config
    application.bot_data["brain"] = brain
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message)
    )
    return application


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route allow-listed text through the brain; silently ignore everyone else."""
    config: Config = context.bot_data["config"]
    brain: Brain = context.bot_data["brain"]

    user = update.effective_user
    if user is None or user.id != config.telegram_allowed_user_id:
        # Pull-only, single-user assistant: no reply, no acknowledgement.
        return

    message = update.effective_message
    if message is None or message.text is None:
        return

    # Brain.respond is synchronous (blocking API + DB calls); run it off the
    # event loop so long-polling stays responsive.
    reply = await asyncio.to_thread(brain.respond, message.text)
    await message.reply_text(reply)


def run_bot(config: Config, brain: Brain) -> None:
    """Start long polling. Blocks until the process is interrupted."""
    application = build_application(config, brain)
    logger.info("Starting Telegram long polling")
    application.run_polling()
