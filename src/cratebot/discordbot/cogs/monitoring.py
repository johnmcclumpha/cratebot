"""Live monitoring: on_message / on_message_edit -> parse -> pipeline -> react.

Scans message.content, embed URLs, and forwarded-message snapshots (brief
5.1) since a link that only exists in a snapshot would otherwise be missed
entirely, both live and on backfill.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from cratebot.discordbot.text_extract import collect_texts
from cratebot.discordbot.views import CandidatePickerView
from cratebot.links.parser import ParsedLink, parse_all
from cratebot.logging_setup import get_logger
from cratebot.pipeline import AddOutcome, AddStatus

if TYPE_CHECKING:
    from cratebot.config import GuildConfig
    from cratebot.discordbot.bot import Cratebot

logger = get_logger(__name__)

REACTION_ADDED = "✅"  # white_check_mark
REACTION_DUPLICATE = "↩️"  # leftwards_arrow_with_hook
REACTION_AMBIGUOUS = "❓"  # question
REACTION_FAILED = "⚠️"  # warning


class MonitoringCog(commands.Cog):
    def __init__(self, bot: Cratebot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.id == self.bot.user.id:
            return
        if message.author.bot and not self.bot.settings.allow_bot_authors:
            return
        guild_cfg = self.bot.is_monitored_channel(message.channel)
        if guild_cfg is None:
            return
        await self._handle_message(message, guild_cfg)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if after.author.id == self.bot.user.id:
            return
        if after.author.bot and not self.bot.settings.allow_bot_authors:
            return
        guild_cfg = self.bot.is_monitored_channel(after.channel)
        if guild_cfg is None:
            return
        await self._handle_message(after, guild_cfg)

    async def _handle_message(self, message: discord.Message, guild_cfg: GuildConfig) -> None:
        parsed_links: list[ParsedLink] = []
        for text in collect_texts(message):
            parsed_links.extend(parse_all(text))
        if not parsed_links:
            return

        playlist_id = await self.bot.resolve_playlist_id(guild_cfg.guild_id)
        if playlist_id is None:
            return  # shouldn't happen - guild_cfg only exists if the guild is configured

        seen_urls: set[str] = set()
        for parsed in parsed_links:
            if parsed.normalized_url in seen_urls:
                continue
            seen_urls.add(parsed.normalized_url)
            try:
                outcome = await self.bot.pipeline.process_link(
                    parsed,
                    playlist_id=playlist_id,
                    message_id=str(message.id),
                    requester_id=str(message.author.id),
                )
            except Exception:
                logger.exception("monitoring.process_link_failed", message_id=message.id)
                outcome = AddOutcome(AddStatus.FAILED, "Internal error while processing this link.")
            await self._report(message, parsed, outcome, playlist_id)

    async def _report(
        self, message: discord.Message, parsed: ParsedLink, outcome: AddOutcome, playlist_id: str
    ) -> None:
        try:
            if outcome.status is AddStatus.ADDED:
                await message.add_reaction(REACTION_ADDED)
            elif outcome.status is AddStatus.DUPLICATE:
                await message.add_reaction(REACTION_DUPLICATE)
            elif outcome.status is AddStatus.AMBIGUOUS:
                await message.add_reaction(REACTION_AMBIGUOUS)
                view = CandidatePickerView(
                    self.bot.pipeline,
                    outcome.candidates,
                    playlist_id=playlist_id,
                    normalized_url=outcome.source_url or parsed.normalized_url,
                    message_id=str(message.id),
                    requester_id=str(message.author.id),
                )
                await message.reply(outcome.message, view=view, mention_author=False)
            elif outcome.status is AddStatus.FAILED:
                await message.add_reaction(REACTION_FAILED)
                await message.reply(f"Couldn't add that link: {outcome.message}", mention_author=False)
            # SKIPPED / DRY_RUN / ALREADY_PROCESSED: deliberately quiet - avoids reaction
            # spam on albums/playlists, and avoids a spurious duplicate reaction when
            # Discord's own embed-attach re-fires on_message_edit for a link we already
            # handled on this exact message.
        except discord.HTTPException:
            logger.warning("monitoring.report_failed", message_id=message.id)
