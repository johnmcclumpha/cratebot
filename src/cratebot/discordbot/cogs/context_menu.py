"""Message context menu -> "Add to playlist".

This is the no-intent fallback path from brief 2.3 and 5.3: a message
context menu interaction delivers the full target message, content
included, without requiring the privileged MESSAGE_CONTENT intent at all.
Keep this working even if the Gateway intent is ever revoked or disabled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from cratebot.discordbot.text_extract import collect_texts
from cratebot.discordbot.views import CandidatePickerView
from cratebot.links.parser import dedupe_by_normalized_url, parse_all
from cratebot.logging_setup import get_logger
from cratebot.pipeline import AddStatus

if TYPE_CHECKING:
    from cratebot.discordbot.bot import Cratebot

logger = get_logger(__name__)


def register_context_menu(bot: Cratebot) -> None:
    @app_commands.context_menu(name="Add to playlist")
    async def add_to_playlist(interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if interaction.guild_id is None:
            await interaction.followup.send("This only works inside a server.")
            return
        playlist_id = await bot.resolve_playlist_id(interaction.guild_id)
        if playlist_id is None:
            await interaction.followup.send("This server isn't configured for Cratebot yet.")
            return

        texts = collect_texts(message)
        parsed_links = [link for text in texts for link in parse_all(text)]
        if not parsed_links:
            await interaction.followup.send("No music links found in that message.")
            return
        parsed_links = dedupe_by_normalized_url(parsed_links)

        replies: list[str] = []
        for parsed in parsed_links:
            try:
                outcome = await bot.pipeline.process_link(
                    parsed,
                    playlist_id=playlist_id,
                    message_id=str(message.id),
                    requester_id=str(interaction.user.id),
                )
            except Exception:
                logger.exception("context_menu.process_link_failed", message_id=message.id)
                replies.append("Internal error while processing that link.")
                continue

            if outcome.status is AddStatus.AMBIGUOUS:
                view = CandidatePickerView(
                    bot.pipeline,
                    outcome.candidates,
                    playlist_id=playlist_id,
                    normalized_url=outcome.source_url or parsed.normalized_url,
                    message_id=str(message.id),
                    requester_id=str(interaction.user.id),
                )
                await interaction.followup.send(outcome.message, view=view, ephemeral=True)
            else:
                replies.append(outcome.message)

        if replies:
            await interaction.followup.send("\n".join(replies), ephemeral=True)

    bot.tree.add_command(add_to_playlist)
