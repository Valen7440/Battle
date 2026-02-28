from collections import defaultdict
from io import BytesIO
import random
from typing import TYPE_CHECKING, cast

import discord
from cachetools import TTLCache
from discord import app_commands
from discord.ext import commands
from discord.utils import MISSING

from ballsdex.core.models import BallInstance, Player
from ballsdex.core.utils.buttons import ConfirmChoiceView
from ballsdex.core.utils.sorting import FilteringChoices, SortingChoices, filter_balls, sort_balls
from ballsdex.core.utils.transformers import BallEnabledTransform, BallInstanceTransform, SpecialEnabledTransform
from ballsdex.core.utils.utils import can_mention
from ballsdex.settings import settings

from .menu import BattleMenu, BattleViewMenu, BulkAddView
from .types import BattleBall, BattleType, BattleUser

if TYPE_CHECKING:
    from ballsdex.core.bot import BallsDexBot

class Battle(commands.GroupCog):
    """
    Start a battle with your friend and win!
    """

    def __init__(self, bot: "BallsDexBot"):
        self.bot = bot
        self.battles: TTLCache[int, dict[int, list[BattleMenu]]] = TTLCache(maxsize=999999, ttl=1800)
    
    bulk = app_commands.Group(name="bulk", description="Bulk Commands")

    def get_battle(
        self,
        interaction: discord.Interaction["BallsDexBot"] | None = None,
        *,
        channel: discord.TextChannel | None = None,
        user: discord.User | discord.Member = MISSING,
    ) -> tuple[BattleMenu, BattleUser] | tuple[None, None]:
        """
        Find an ongoing battle for the given interaction.

        Parameters
        ----------
        interaction: discord.Interaction["BallsDexBot"]
            The current interaction, used for getting the guild, channel and author.

        Returns
        -------
        tuple[BattleMenu, BattleUser] | tuple[None, None]
            A tuple with the `BattleMenu` and `BattleUser` if found, else `None`.
        """
        guild: discord.Guild
        if interaction:
            guild = cast(discord.Guild, interaction.guild)
            channel = cast(discord.TextChannel, interaction.channel)
            user = interaction.user
        elif channel:
            guild = channel.guild
        else:
            raise TypeError("Missing interaction or channel")

        if guild.id not in self.battles:
            self.battles[guild.id] = defaultdict(list)
        if channel.id not in self.battles[guild.id]:
            return (None, None)
        to_remove: list[BattleMenu] = []
        for battle in self.battles[guild.id][channel.id]:
            if (
                battle.current_view.is_finished()
                or battle.battler1.cancelled
                or battle.battler2.cancelled
            ):
                # remove what was supposed to have been removed
                to_remove.append(battle)
                continue
            try:
                battler = battle._get_battler(user)
            except RuntimeError:
                continue
            else:
                break
        else:
            for battle in to_remove:
                self.battles[guild.id][channel.id].remove(battle)
            return (None, None)

        for battle in to_remove:
            self.battles[guild.id][channel.id].remove(battle)
        return (battle, battler)

    @app_commands.command()
    @app_commands.choices(type=[
        app_commands.Choice(name="Standard", value=BattleType.STANDARD),
        app_commands.Choice(name="Quick Match", value=BattleType.QUICK_MATCH)
    ])
    async def start(
        self, 
        interaction: discord.Interaction["BallsDexBot"], 
        user: discord.User,
        type: BattleType,
        duplicates: bool = True,
        amount: app_commands.Range[int, 3, 10] = 3
    ):
        """
        Starts a battle with the chosen user.

        Parameters
        ----------
        user: discord.User
            The user you want to battle with
        type: BattleType
            The battle type you want to play. Default to Standard.
        duplicates: bool
            Whether or not you want to allow duplicates in your battle
        amount: int
            The amount of countryballs needed for the battle. Minimum is 3, maximium is 10.
        """
        if user.bot:
            await interaction.response.send_message("You cannot battle with bots.", ephemeral=True)
            return
        if user.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot battle with yourself.", ephemeral=True
            )
            return
        player1, _ = await Player.get_or_create(discord_id=interaction.user.id)
        player2, _ = await Player.get_or_create(discord_id=user.id)
        blocked = await player1.is_blocked(player2)
        blocked = await player1.is_blocked(player2)
        if blocked:
            await interaction.response.send_message(
                "You cannot begin a battle with a user that you have blocked.", ephemeral=True
            )
            return
        blocked2 = await player2.is_blocked(player1)
        if blocked2:
            await interaction.response.send_message(
                "You cannot begin a battle with a user that has blocked you.", ephemeral=True
            )
            return
        
        battle1, battler1 = self.get_battle(interaction)
        battle2, battler2 = self.get_battle(interaction, channel=interaction.channel) # type: ignore
        if battle1 or battler1:
            await interaction.response.send_message(
                "You already have an ongoing battle.", ephemeral=True
            )
            return
        if battle2 or battler2:
            await interaction.response.send_message(
                "The user you are trying to battle with is already in a battle.", ephemeral=True
            )
            return
        
        if player2.discord_id in self.bot.blacklist:
            await interaction.response.send_message(
                "You cannot battle with a blacklisted user.", ephemeral=True
            )
            return

        if type == BattleType.QUICK_MATCH:
            await interaction.response.defer(thinking=True)
            view = ConfirmChoiceView(
                interaction, 
                user=user, 
                accept_message="Battle started!",
                cancel_message="Request has been cancelled."
            )
            await interaction.followup.send(
                f"Hey {user.mention}, would you like to battle with "
                f"{interaction.user} in a quick match?",
                view=view,
                allowed_mentions=await can_mention([player2])
            )
            await view.wait()
            if not view.value:
                return

            await self._start_quick_match(interaction, interaction.user, user)
            return
        
        menu = BattleMenu(
            self, 
            interaction, 
            BattleUser(interaction.user, player1), 
            BattleUser(user, player2), 
            duplicates,
            amount
        )
        self.battles[interaction.guild.id][interaction.channel.id].append(menu)  # type: ignore
        await menu.start()
        await interaction.response.send_message("Battle started!", ephemeral=True)
    
    @app_commands.command()
    async def add(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Adds a ball to your battle proposal.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to add to your proposal
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        battle, battler = self.get_battle(interaction)
        if not battle or not battler:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return
        if battler.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return
        if any(countryball.pk == ball.instance.pk for ball in battler.proposal):
            await interaction.followup.send(
                f"You already have this {settings.collectible_name} in your proposal.",
                ephemeral=True,
            )
            return
        if not battle.duplicates and any(
            countryball.ball_id == ball.instance.ball_id 
            for ball in battler.proposal
        ):
            await interaction.followup.send(
                f"You've already added this {settings.collectible_name}",
                ephemeral=True
            )
            return

        if await countryball.is_locked():
            await interaction.followup.send(
                f"This {settings.collectible_name} is currently in an active battle, trade or donation, "
                "please try again later.",
                ephemeral=True,
            )
            return

        await countryball.lock_for_trade()
        battler.proposal.append(BattleBall(countryball, countryball.health, countryball.attack))
        await interaction.followup.send(
            f"{countryball.countryball.country} added.", ephemeral=True
        )
    
    @bulk.command(name="add")
    async def bulk_add(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallEnabledTransform | None = None,
        sort: SortingChoices | None = None,
        special: SpecialEnabledTransform | None = None,
        filter: FilteringChoices | None = None,
    ):
        """
        Bulk add countryballs to the ongoing battle, with paramaters to aid with searching.

        Parameters
        ----------
        countryball: Ball
            The countryball you would like to filter the results to
        sort: SortingChoices
            Choose how countryballs are sorted. Can be used to show duplicates.
        special: Special
            Filter the results to a special event
        filter: FilteringChoices
            Filter the results to a specific filter
        """
        await interaction.response.defer(ephemeral=True, thinking=True)
        battle, battler = self.get_battle(interaction)
        if not battle or not battler:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return
        if battler.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return
        query = BallInstance.filter(player__discord_id=interaction.user.id).exclude(
            tradeable=False, ball__tradeable=False
        )
        if countryball:
            query = query.filter(ball=countryball)
        if special:
            query = query.filter(special=special)
        if sort:
            query = sort_balls(sort, query)
        if filter:
            query = filter_balls(filter, query, interaction.guild_id)
        balls = cast(list[int], await query.values_list("id", flat=True))
        if not balls:
            await interaction.followup.send(
                f"No {settings.plural_collectible_name} found.", ephemeral=True
            )
            return

        view = BulkAddView(interaction, balls, self)
        await view.start(
            content=f"Select the {settings.plural_collectible_name} you want to add "
            "to your proposal, note that the display will wipe on pagination however "
            f"the selected {settings.plural_collectible_name} will remain."
        )


    @app_commands.command()
    async def remove(
        self,
        interaction: discord.Interaction["BallsDexBot"],
        countryball: BallInstanceTransform,
        special: SpecialEnabledTransform | None = None,
    ):
        """
        Remove a countryball from what you proposed in the ongoing battle.

        Parameters
        ----------
        countryball: BallInstance
            The countryball you want to remove from your proposal
        special: Special
            Filter the results of autocompletion to a special event. Ignored afterwards.
        """
        if not countryball:
            return

        battle, battler = self.get_battle(interaction)
        if not battle or not battler:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return
        if battler.locked:
            await interaction.followup.send(
                "You have locked your proposal, it cannot be edited! "
                "You can click the cancel button to stop the battle instead.",
                ephemeral=True,
            )
            return
        ball = next(
            (ball for ball in battler.proposal if ball.instance.pk == countryball.pk),
            None
        )
        if not ball:
            await interaction.response.send_message(
                f"That {settings.collectible_name} is not in your proposal.", ephemeral=True
            )
            return
        battler.proposal.remove(ball)
        await interaction.response.send_message(
            f"{countryball.countryball.country} removed.", ephemeral=True
        )
        await countryball.unlock()

    @app_commands.command()
    async def cancel(self, interaction: discord.Interaction["BallsDexBot"]):
        """
        Cancel the ongoing battle.
        """
        battle, battler = self.get_battle(interaction)
        if not battle or not battler:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return

        if battle.battler1.accepted and battle.battler2.accepted:
            await interaction.followup.send(
                "You can't cancel now; the battle has already gone through."
            )

        await battle.user_cancel(battler)
        await interaction.response.send_message("Battle cancelled.", ephemeral=True)

    @app_commands.command()
    async def view(
        self,
        interaction: discord.Interaction["BallsDexBot"],
    ):
        """
        View the countryballs added to an ongoing battle.
        """
        battle, battler = self.get_battle(interaction)
        if not battle or not battler:
            await interaction.followup.send("You do not have an ongoing battle.", ephemeral=True)
            return

        source = BattleViewMenu(interaction, [battle.battler1, battle.battler2], self)
        await source.start(content="Select a user to view their proposal.")

    async def _start_quick_match(
        self, 
        interaction: discord.Interaction["BallsDexBot"], 
        user1: discord.User | discord.Member, 
        user2: discord.User | discord.Member,
    ):
        """
        Starts an quick match.
        """
        user1_health = 100
        user2_health = 100
        text = f"{user1.name} VS. {user2.name} - Quick Match info:\n"
        turn = 1

        while user1_health > 0 and user2_health > 0:
            user, enemy = (
                (user1, user2)
                if random.randint(0, 1) == 0
                else (user2, user1)
            )
            damage = random.randint(1, 15)

            if enemy.id == user1.id:
                user1_health -= damage
                remaining = user1_health
            else:
                user2_health -= damage
                remaining = user2_health

            if remaining <= 0:
                text += f"Turn {turn}: {user.name} has killed {enemy.name}\n"
            else:
                text += f"Turn {turn}: {user.name} has dealt {damage} to {enemy.name}\n"
            
            turn += 1
        
        winner = user1 if user1_health > 0 else user2
        file = discord.File(BytesIO(text.encode("utf-8")), filename="quick-match.txt")
        await interaction.followup.send(file=file, content=f"Quick match finished! {winner.mention} won!")
