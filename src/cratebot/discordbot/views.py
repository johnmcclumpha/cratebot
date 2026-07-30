"""Button-based disambiguation UI: when the matcher can't clear the
confidence threshold, ask a human to pick from the top candidates rather
than silently adding the wrong song (brief 5.2)."""

from __future__ import annotations

import discord

from cratebot.matching import Candidate
from cratebot.pipeline import AddPipeline, AddStatus
from cratebot.logging_setup import get_logger

logger = get_logger(__name__)


class CandidatePickerView(discord.ui.View):
    def __init__(
        self,
        pipeline: AddPipeline,
        candidates: list[tuple[Candidate, float]],
        *,
        normalized_url: str,
        message_id: str,
        requester_id: str,
        timeout: float = 600.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self._pipeline = pipeline
        self._normalized_url = normalized_url
        self._message_id = message_id
        self._requester_id = requester_id

        for candidate, score in candidates[:3]:
            label = f"{candidate.title} - {candidate.artist}"[:80]
            button = discord.ui.Button(
                label=label or "(unknown)", style=discord.ButtonStyle.secondary
            )
            button.callback = self._make_callback(candidate)
            self.add_item(button)

        none_button = discord.ui.Button(label="None of these", style=discord.ButtonStyle.danger)
        none_button.callback = self._decline
        self.add_item(none_button)

    def _make_callback(self, candidate: Candidate):
        async def callback(interaction: discord.Interaction) -> None:
            outcome = await self._pipeline.confirm_candidate(
                candidate,
                normalized_url=self._normalized_url,
                message_id=self._message_id,
                requester_id=self._requester_id,
            )
            if outcome.status is AddStatus.ADDED:
                await interaction.response.edit_message(
                    content=f"Added: {outcome.title} - {outcome.artist}", view=None
                )
            else:
                await interaction.response.edit_message(content=f"Couldn't add it: {outcome.message}", view=None)

        return callback

    async def _decline(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content="No match picked; skipped.", view=None)
