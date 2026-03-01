from __future__ import annotations

import asyncio
from io import BytesIO
import logging
from datetime import timedelta
import random
from typing import TYPE_CHECKING, AsyncIterator, List, Set, cast

import discord
from discord.ui import Button, View, button
from discord.utils import format_dt, utcnow

from ballsdex.core.models import BallInstance, Player
from ballsdex.core.utils import menus
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.paginator import Pages
from ballsdex.core.utils.utils import can_mention
from ballsdex.packages.balls.countryballs_paginator import CountryballsSource, CountryballsViewer
from ballsdex.packages.battle.display import fill_battle_embed_fields
from ballsdex.packages.battle.types import BattleBall, BattleUser, BattleType
from ballsdex.settings import settings

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot
    from ballsdex.packages.battle.cog import Battle as BattleCog

log = logging.getLogger("ballsdex.packages.battle.menu")
BATTLE_TIMEOUT = 30


class InvalidBattleOperation(Exception):
    pass


class BattleView(View):
    def __init__(self, battle: BattleMenu):
        super().__init__(timeout=60 * BATTLE_TIMEOUT + 1)
        self.battle = battle

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
        try:
            self.battle._get_battler(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this battle.", ephemeral=True
            )
            return False
        else:
            return True

    @button(label="Lock proposal", emoji="\N{LOCK}", style=discord.ButtonStyle.primary)
    async def lock(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        battler = self.battle._get_battler(interaction.user)
        if battler.locked:
            await interaction.response.send_message(
                "You have already locked your proposal!", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        proposal_size = len(battler.proposal)
        if proposal_size < self.battle.amount:
            await interaction.response.send_message(
                f"Your proposal doesn't have **{self.battle.amount}** {settings.plural_collectible_name}. "
                f"It only have **{proposal_size}**",
                ephemeral=True
            )
            return
        if proposal_size > self.battle.amount:
            await interaction.response.send_message(
                f"Your proposal exceed the allowed amount ({self.battle.amount})!\n"
                f"Please remove **{proposal_size - self.battle.amount}** {settings.plural_collectible_name}.",
                ephemeral=True
            )
            return

        await self.battle.lock(battler)
        if self.battle.battler1.locked and self.battle.battler2.locked:
            await interaction.followup.send(
                "Your proposal has been locked. Now confirm again to end the battle.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Your proposal has been locked. "
                "You can wait for the other user to lock their proposal.",
                ephemeral=True,
            )

    @button(label="Reset", emoji="\N{DASH SYMBOL}", style=discord.ButtonStyle.secondary)
    async def clear(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        battler = self.battle._get_battler(interaction.user)
        await interaction.response.defer(thinking=True, ephemeral=True)

        if battler.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return

        view = ConfirmChoiceView(
            interaction,
            accept_message="Clearing your proposal...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to clear your proposal?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        if battler.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return

        for countryball in battler.proposal:
            await countryball.instance.unlock()

        battler.proposal.clear()
        await interaction.followup.send("Proposal cleared.", ephemeral=True)

    @button(
        label="Cancel battle",
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
        style=discord.ButtonStyle.danger,
    )
    async def cancel(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)

        view = ConfirmChoiceView(
            interaction,
            accept_message="Cancelling the battle...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to cancel this battle?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        await self.battle.user_cancel(self.battle._get_battler(interaction.user))
        await interaction.followup.send("Battle has been cancelled.", ephemeral=True)


class ConfirmView(View):
    def __init__(self, battle: BattleMenu):
        super().__init__(timeout=60 * 14 + 55)
        self.battle = battle

    async def on_timeout(self):
        """
        When the view times out, we cancel the battle.
        """
        if self.battle.task:
            self.battle.task.cancel()
        await self.battle.cancel("The battle has timed out.")

    async def interaction_check(self, interaction: discord.Interaction["BallsDexBot"], /) -> bool:
        try:
            self.battle._get_battler(interaction.user)
        except RuntimeError:
            await interaction.response.send_message(
                "You are not allowed to interact with this battle.", ephemeral=True
            )
            return False
        else:
            return True

    @discord.ui.button(
        style=discord.ButtonStyle.success, emoji="\N{HEAVY CHECK MARK}\N{VARIATION SELECTOR-16}"
    )
    async def accept_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        battler = self.battle._get_battler(interaction.user)
        await interaction.response.defer(ephemeral=True, thinking=True)
        if battler.accepted:
            await interaction.response.send_message(
                "You have already accepted this battle.", ephemeral=True
            )
            return
        result = await self.battle.confirm(battler)
        if self.battle.battler1.accepted and self.battle.battler2.accepted:
            if result:
                await interaction.followup.send("The battle is now concluded.", ephemeral=True)
            else:
                await interaction.followup.send(
                    ":warning: An error occurred while concluding the battle.", ephemeral=True
                )
        else:
            await interaction.followup.send(
                "You have accepted the battle, waiting for the other user...", ephemeral=True
            )

    @discord.ui.button(
        style=discord.ButtonStyle.danger,
        emoji="\N{HEAVY MULTIPLICATION X}\N{VARIATION SELECTOR-16}",
    )
    async def deny_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)

        view = ConfirmChoiceView(
            interaction,
            accept_message="Cancelling the battle...",
            cancel_message="This request has been cancelled.",
        )
        await interaction.followup.send(
            "Are you sure you want to cancel this battle?", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return

        if self.battle.battler1.accepted and self.battle.battler2.accepted:
            await interaction.followup.send(
                "You can't cancel now; the battle has already gone through."
            )
            return

        await self.battle.user_cancel(self.battle._get_battler(interaction.user))
        await interaction.followup.send("Battle has been cancelled.", ephemeral=True)


class BattleMenu:
    def __init__(
        self,
        cog: BattleCog,
        interaction: discord.Interaction["BallsDexBot"],
        battler1: BattleUser,
        battler2: BattleUser,
        duplicates: bool,
        amount: int
    ):
        self.cog = cog
        self.bot = interaction.client
        self.channel: discord.TextChannel = cast(discord.TextChannel, interaction.channel)
        self.battler1 = battler1
        self.battler2 = battler2
        self.embed = discord.Embed()
        self.task: asyncio.Task | None = None
        self.current_view: BattleView | ConfirmView = BattleView(self)
        self.message: discord.Message
        self.duplicates = duplicates
        self.amount = amount

    def _get_battler(self, user: discord.User | discord.Member) -> BattleUser:
        if user.id == self.battler1.user.id:
            return self.battler1
        elif user.id == self.battler2.user.id:
            return self.battler2
        raise RuntimeError(f"User with ID {user.id} cannot be found in the battle")

    def _generate_embed(self):
        add_command = self.cog.add.extras.get("mention", "`/battle add`")
        remove_command = self.cog.remove.extras.get("mention", "`/battle remove`")
        view_command = self.cog.view.extras.get("mention", "`/battle view`")

        self.embed.title = f"{settings.plural_collectible_name.title()} fighting"
        self.embed.color = discord.Colour.blurple()
        self.embed.description = (
            f"Add or remove {settings.plural_collectible_name} you want to propose "
            f"to the other player using the {add_command} and {remove_command} commands.\n"
            "Once you're finished, click the lock button below to confirm your proposal.\n"
            "*This battle proposal will timeout "
            f"{format_dt(utcnow() + timedelta(minutes=BATTLE_TIMEOUT), style='R')}.*\n\n"
            f"Use the {view_command} command to see the full"
            f" list of {settings.plural_collectible_name}.\n\n"
            f"- Amount: {self.amount}\n"
            f"- Duplicates: {'Yes' if self.duplicates else 'No'}"
        )
        self.embed.set_footer(
            text="This message is updated every 15 seconds, "
            "but you can keep on editing your proposal."
        )

    async def update_message_loop(self):
        """
        A loop task that updates every 15 seconds with the new content.
        """

        assert self.task
        start_time = utcnow()

        while True:
            await asyncio.sleep(15)
            if utcnow() - start_time > timedelta(minutes=BATTLE_TIMEOUT):
                self.bot.loop.create_task(self.cancel("The battle timed out"))
                return

            try:
                fill_battle_embed_fields(self.embed, self.bot, self.battler1, self.battler2)
                await self.message.edit(embed=self.embed)
            except Exception:
                log.exception(
                    "Failed to refresh the battle menu "
                    f"guild={self.message.guild.id} "  # type: ignore
                    f"battler1={self.battler1.user.id} battler2={self.battler2.user.id}"
                )
                self.bot.loop.create_task(self.cancel("The battle errored"))
                return

    async def start(self):
        """
        Start the battle by sending the initial message and opening up the proposals.
        """
        self._generate_embed()
        fill_battle_embed_fields(self.embed, self.bot, self.battler1, self.battler2)
        self.message = await self.channel.send(
            content=f"Hey {self.battler2.user.mention}, {self.battler1.user.name} "
            "is proposing a battle with you!",
            embed=self.embed,
            view=self.current_view,
            allowed_mentions=await can_mention([self.battler2.player]),
        )
        self.task = self.bot.loop.create_task(self.update_message_loop())

    async def cancel(self, reason: str = "The battle proposal has been cancelled."):
        """
        Cancel the battle immediately.
        """
        if self.task:
            self.task.cancel()
        self.current_view.stop()

        for countryball in self.battler1.proposal + self.battler2.proposal:
            await countryball.instance.unlock()

        for item in self.current_view.children:
            item.disabled = True  # type: ignore

        fill_battle_embed_fields(self.embed, self.bot, self.battler1, self.battler2)
        self.embed.colour = discord.Colour.dark_red()
        self.embed.description = f"**{reason}**"
        if getattr(self, "message", None):
            await self.message.edit(content=None, embed=self.embed, view=self.current_view)

    async def lock(self, battler: BattleUser):
        """
        Mark a user's proposal as locked, ready for next stage
        """
        battler.locked = True
        if self.battler1.locked and self.battler2.locked:
            if self.task:
                self.task.cancel()
            if not self.battler1.proposal and not self.battler2.proposal:
                await self.cancel("Nothing has been proposed in the battle, it has been cancelled.")
                return
            self.current_view.stop()
            fill_battle_embed_fields(self.embed, self.bot, self.battler1, self.battler2)

            self.embed.colour = discord.Colour.yellow()
            self.embed.description = (
                "Both users locked their propositions! Now confirm to conclude this battle proposal."
            )
            self.current_view = ConfirmView(self)
            await self.message.edit(content=None, embed=self.embed, view=self.current_view)

    async def user_cancel(self, battler: BattleUser):
        """
        Register a user request to cancel the battle
        """
        battler.cancelled = True
        await self.cancel()

    async def perform_battle(self) -> tuple[discord.Embed, str]:
        text = f"{self.battler1.user.name} VS. {self.battler2.user.name} - Battle info:\n"
        turn = 1
        
        for ball in self.battler1.proposal + self.battler2.proposal:
            await ball.instance.unlock()

        battler1_copy = self.battler1.copy()
        battler2_copy = self.battler2.copy()
        while len(self.battler1.proposal) > 0 and len(self.battler2.proposal) > 0:
            player = random.choice([self.battler1, self.battler2])
            enemy = self.battler2 if player.user.id == self.battler1.user.id else self.battler1
            if turn == 1:
                await self.message.channel.send(
                    f"Starting with the battle! {player.user.mention} will start."
                )
            else:
                await self.message.channel.send(
                    f"Round #{turn}: turn of {player.user.mention}."
                )
            await asyncio.sleep(3)

            ball = random.choice(player.proposal)
            target = random.choice(enemy.proposal)

            dealt = random.randint(1, ball.attack)
            target.health -= dealt
            if target.health <= 0:
                enemy.proposal.remove(target)
                proposal_size = len(enemy.proposal)
                grammar = settings.collectible_name if proposal_size == 1 else settings.plural_collectible_name
                text += (
                    f"Turn {turn}: {player.user.name}'s {ball.instance.countryball.country} has killed "
                    f"{enemy.user.name}'s {target.instance.countryball.country}\n"
                )
                await self.message.channel.send(
                    f"{player.user.name}'s {ball.instance.countryball.country} has killed "
                    f"{enemy.user.name}'s {target.instance.countryball.country}!\n"
                    f"{enemy.user.name} now has **{proposal_size}.** {grammar}"
                )
            else:
                proposal_size = len(enemy.proposal)
                grammar = settings.collectible_name if proposal_size == 1 else settings.plural_collectible_name
                text += (
                    f"Turn {turn}: {player.user.name}'s {ball.instance.countryball.country} has dealt {dealt} "
                    f"to {enemy.user.name}'s {target.instance.countryball.country}\n"
                )
                await self.message.channel.send(
                    f"{player.user.name}'s {ball.instance.countryball.country} has dealt **{dealt}** "
                    f"damage to {enemy.user.name}'s {target.instance.countryball.country}.\n"
                    f"{target.instance.countryball.country} now has **{target.health}** HP."
                )
            turn += 1
            await asyncio.sleep(5)
        
        winner = self.battler1 if len(self.battler2.proposal) == 0 else self.battler2
        embed = discord.Embed(
            title=f"Battle Between {self.battler1.user.name} and {self.battler2.user.name}",
            color=discord.Color.blurple()
        )
        embed.description = (
            "**__Settings:__**\n"
            f"Duplicate: {'Yes' if self.duplicates else 'No'}\n"
            f"Amount: {self.amount}\n"
        )
        fill_battle_embed_fields(embed, self.bot, battler1_copy, battler2_copy, is_final=True)
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        embed.add_field(name="Winner", value=f"{winner.user.name} - Turn: {turn}", inline=False)
        return embed, text


    async def confirm(self, battler: BattleUser) -> bool:
        """
        Mark a user's proposal as accepted. If both user accept, end the battle now

        If the battle proposal is concluded, return True, otherwise if an error occurs, return False
        """
        result = True
        battler.accepted = True
        fill_battle_embed_fields(self.embed, self.bot, self.battler1, self.battler2)
        if self.battler1.accepted and self.battler2.accepted:
            if self.task and not self.task.cancelled():
                # shouldn't happen but just in case
                self.task.cancel()

            self.embed.description = "Battle Proposal concluded!"
            self.embed.colour = discord.Colour.green()
            self.current_view.stop()
            for item in self.current_view.children:
                item.disabled = True  # type: ignore

            try:
                embed, text = await self.perform_battle()
                file = discord.File(BytesIO(text.encode("utf-8")), filename="log.txt")
                await self.message.reply(file=file, embed=embed)
            except InvalidBattleOperation:
                log.warning(f"Illegal battle operation between {self.battler1=} and {self.battler2=}")
                self.embed.description = (
                    f":warning: An attempt to modify the {settings.plural_collectible_name} "
                    "during the battle was detected and the battle was cancelled."
                )
                self.embed.colour = discord.Colour.red()
                result = False
            except Exception:
                log.exception(f"Failed to conclude battle {self.battler1=} {self.battler2=}")
                self.embed.description = "An error occured when concluding the battle."
                self.embed.colour = discord.Colour.red()
                result = False

        await self.message.edit(content=None, embed=self.embed, view=self.current_view)
        return result


class CountryballsSelector(Pages):
    def __init__(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        balls: List[int],
        cog: BattleCog,
    ):
        self.bot = interaction.client
        self.interaction = interaction
        source = CountryballsSource(balls)
        super().__init__(source, interaction=interaction)
        self.add_item(self.select_ball_menu)
        self.add_item(self.confirm_button)
        self.add_item(self.select_all_button)
        self.add_item(self.clear_button)
        self.balls_selected: Set[BallInstance] = set()
        self.cog = cog

    async def set_options(self, balls: AsyncIterator[BallInstance]):
        options: List[discord.SelectOption] = []
        async for ball in balls:
            if ball.is_tradeable is False:
                continue
            emoji = self.bot.get_emoji(int(ball.countryball.emoji_id))
            favorite = f"{settings.favorited_collectible_emoji} " if ball.favorite else ""
            special = ball.special_emoji(self.bot, True)
            options.append(
                discord.SelectOption(
                    label=f"{favorite}{special}#{ball.pk:0X} {ball.countryball.country}",
                    description=f"ATK: {ball.attack_bonus:+d}% • HP: {ball.health_bonus:+d}% • "
                    f"Caught on {ball.catch_date.strftime('%d/%m/%y %H:%M')}",
                    emoji=emoji,
                    value=f"{ball.pk}",
                    default=ball in self.balls_selected,
                )
            )
        self.select_ball_menu.options = options
        self.select_ball_menu.max_values = len(options)

    @discord.ui.select(min_values=1, max_values=25)
    async def select_ball_menu(
        self, interaction: discord.Interaction["BallsDexBot"], item: discord.ui.Select
    ):
        for value in item.values:
            ball_instance = await BallInstance.get(id=int(value)).prefetch_related(
                "ball", "player"
            )
            self.balls_selected.add(ball_instance)
        await interaction.response.defer()

    @discord.ui.button(label="Select Page", style=discord.ButtonStyle.secondary)
    async def select_all_button(
        self, interaction: discord.Interaction["BallsDexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        for ball in self.select_ball_menu.options:
            ball_instance = await BallInstance.get(id=int(ball.value)).prefetch_related(
                "ball", "player"
            )
            if ball_instance not in self.balls_selected:
                self.balls_selected.add(ball_instance)
        await interaction.followup.send(
            (
                f"All {settings.plural_collectible_name} on this page have been selected.\n"
                "Note that the menu may not reflect this change until you change page."
            ),
            ephemeral=True,
        )

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm_button(
        self, interaction: discord.Interaction["BallsDexBot"], button: Button
    ):
        await interaction.response.defer(thinking=True, ephemeral=True)
        battle, battler = self.cog.get_battle(interaction)
        if battle is None or battler is None:
            return await interaction.followup.send(
                "The battle has been cancelled or the user is not part of the battle.",
                ephemeral=True,
            )
        if battler.locked:
            return await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
        if any(
            ball.pk == battleball.instance.pk 
            for ball in self.balls_selected
            for battleball in battler.proposal
        ):
            return await interaction.followup.send(
                "You have already added some of the "
                f"{settings.plural_collectible_name} you selected.",
                ephemeral=True,
            )
        if not battle.duplicates and any(
            ball.ball_id in {b.instance.ball_id for b in battler.proposal}
            for ball in self.balls_selected
        ):
            await interaction.followup.send(
                "You've selected a ball that is already in your proposal.",
                ephemeral=True
            )
            return

        balls_selected_size = len(self.balls_selected)
        if balls_selected_size == 0:
            return await interaction.followup.send(
                f"You have not selected any {settings.plural_collectible_name} "
                "to add to your proposal.",
                ephemeral=True,
            )
        if balls_selected_size > battle.amount:
            return await interaction.followup.send(
                f"Your selected balls exceed allowed amount! ({battle.amount})"
                f"Please remove {balls_selected_size - battle.amount} from selected balls.",
                ephemeral=True
            )
        for ball in self.balls_selected:
            if await ball.is_locked():
                return await interaction.followup.send(
                    f"{settings.collectible_name.title()} #{ball.pk:0X} is locked "
                    "for battle and won't be added to the proposal.",
                    ephemeral=True,
                )
            battler.proposal.append(BattleBall(ball, ball.health, ball.attack))
            await ball.lock_for_trade()
        grammar = (
            f"{settings.collectible_name}"
            if len(self.balls_selected) == 1
            else f"{settings.plural_collectible_name}"
        )
        await interaction.followup.send(
            f"{len(self.balls_selected)} {grammar} added to your proposal.", ephemeral=True
        )
        self.balls_selected.clear()

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger)
    async def clear_button(self, interaction: discord.Interaction["BallsDexBot"], button: Button):
        await interaction.response.defer(thinking=True, ephemeral=True)
        self.balls_selected.clear()
        await interaction.followup.send(
            f"You have cleared all currently selected {settings.plural_collectible_name}."
            f"This does not affect {settings.plural_collectible_name} within your battle.\n"
            f"There may be an instance where it shows {settings.plural_collectible_name} on the"
            " current page as selected, this is not the case - "
            "changing page will show the correct state.",
            ephemeral=True,
        )


class BulkAddView(CountryballsSelector):
    async def on_timeout(self) -> None:
        return await super().on_timeout()


class BattleViewSource(menus.ListPageSource):
    def __init__(self, entries: List[BattleUser]):
        super().__init__(entries, per_page=25)

    async def format_page(self, menu, players: List[BattleUser]):
        menu.set_options(players)
        return True  # signal to edit the page


class BattleViewMenu(Pages):
    def __init__(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        proposal: List[BattleUser],
        cog: BattleCog,
    ):
        self.bot = interaction.client
        source = BattleViewSource(proposal)
        super().__init__(source, interaction=interaction)
        self.add_item(self.select_player_menu)
        self.cog = cog

    def set_options(self, players: List[BattleUser]):
        options: List[discord.SelectOption] = []
        for player in players:
            user_obj = player.user
            plural_check = (
                f"{settings.collectible_name}"
                if len(player.proposal) == 1
                else f"{settings.plural_collectible_name}"
            )
            options.append(
                discord.SelectOption(
                    label=f"{user_obj.display_name}",
                    description=(f"ID: {user_obj.id} | {len(player.proposal)} {plural_check}"),
                    value=f"{user_obj.id}",
                )
            )
        self.select_player_menu.options = options

    @discord.ui.select()
    async def select_player_menu(
        self, interaction: discord.Interaction["BallsDexBot"], item: discord.ui.Select
    ):
        await interaction.response.defer(thinking=True)
        player = await Player.get(discord_id=int(item.values[0]))
        battle, battler = self.cog.get_battle(interaction)
        if battle is None or battler is None:
            return await interaction.followup.send(
                "The battle has been cancelled or the user is not part of the battle.",
                ephemeral=True,
            )
        battle_player = (
            battle.battler2 if battle.battler2.user.id == player.discord_id else battle.battler1
        )
        ball_instances = battle_player.proposal
        if len(ball_instances) == 0:
            return await interaction.followup.send(
                f"{battle_player.user} has not added any {settings.plural_collectible_name}.",
                ephemeral=True,
            )

        paginator = CountryballsViewer(interaction, [x.instance.pk for x in ball_instances])
        await paginator.start()
