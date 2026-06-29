"""Telegram bot wiring for Spotter.

Step 2 scope: long-polling bot with a single message handler that echoes back
``received: <text>``. Access is restricted to the single allow-listed user; any
other sender is silently ignored.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config

logger = logging.getLogger(__name__)


def build_application(config: Config) -> Application:
    """Construct the Telegram ``Application`` with handlers wired in."""
    application = Application.builder().token(config.telegram_bot_token).build()
    # Stash config on bot_data so handlers can reach it without globals.
    application.bot_data["config"] = config
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message)
    )
    return application


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Echo allow-listed text messages; silently ignore everyone else."""
    config: Config = context.bot_data["config"]

    user = update.effective_user
    if user is None or user.id != config.telegram_allowed_user_id:
        # Pull-only, single-user assistant: no reply, no acknowledgement.
        return

    message = update.effective_message
    if message is None or message.text is None:
        return

    await message.reply_text(f"received: {message.text}")


def run_bot(config: Config) -> None:
    """Start long polling. Blocks until the process is interrupted."""
    application = build_application(config)
    logger.info("Starting Telegram long polling")
    application.run_polling()
