"""Raphael - JTF auto-role bot.

Every hour it pulls the [JTF] roster from the Last Asylum tracker API, matches
Discord members to in-game names by their server nickname, and keeps the
might-based roles (Arch Demon / Greater Demon / Lesser Demon) up to date.

Start in DRY_RUN=true: it only reports what it WOULD change.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from roles_logic import Settings, decide, fmt_power, normalize, weekly_gains

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("raphael")


def env(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise SystemExit(f"Missing {name} in .env - see README.md")
    return val or ""


def env_int(name: str, default: int = 0) -> int:
    raw = env(name, str(default)).strip()
    return int(raw) if raw else default


DISCORD_TOKEN = env("DISCORD_TOKEN", required=True)
TRACKER_API_KEY = env("TRACKER_API_KEY", required=True)
GUILD_ID = env_int("GUILD_ID")
GAME_SERVER = env("GAME_SERVER", "369")
ALLIANCE_TAG = env("ALLIANCE_TAG", "JTF")
LOG_CHANNEL_ID = env_int("LOG_CHANNEL_ID")
SHOUTOUT_CHANNEL_ID = env_int("SHOUTOUT_CHANNEL_ID")
DRY_RUN = env("DRY_RUN", "true").lower() != "false"
CHECK_MINUTES = env_int("CHECK_MINUTES", 60)
TZ = ZoneInfo(env("TIMEZONE", "America/Chicago"))
WEEKLY_DAY = env_int("WEEKLY_DAY", 0)  # 0 = Monday
WEEKLY_HOUR = env_int("WEEKLY_HOUR", 10)

SETTINGS = Settings(
    arch_min=env_int("ARCH_MIN", 22_000_000),
    greater_min=env_int("GREATER_MIN", 16_000_000),
    named_monster_role=env("NAMED_MONSTER_ROLE", "Named Monster"),
    grandfather_arch={u.strip().lower() for u in env("GRANDFATHER_ARCH", "").split(",") if u.strip()},
    auto_verify=env("AUTO_VERIFY", "false").lower() == "true",
)

API_URL = f"https://api.lastasylumtracker.com/v1/alliances/{GAME_SERVER}/{ALLIANCE_TAG}"
DATA_FILE = BASE / "data.json"
OVERRIDES_FILE = BASE / "name_overrides.json"


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        log.error("Could not read %s (bad JSON) - using defaults", path.name)
        return default


def save_json(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def find_members(payload) -> list[dict]:
    """The API nests the roster under 'members'; look a level down too, just in case."""
    if isinstance(payload, dict):
        if isinstance(payload.get("members"), list):
            return payload["members"]
        for key in ("alliance", "data", "result"):
            inner = payload.get(key)
            if isinstance(inner, dict) and isinstance(inner.get("members"), list):
                return inner["members"]
    return []


async def fetch_roster(session: aiohttp.ClientSession) -> dict[str, dict]:
    headers = {"X-API-Key": TRACKER_API_KEY, "User-Agent": "JTF-Raphael-Bot/1.0 (Discord role sync)"}
    async with session.get(API_URL, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 429:
            raise RuntimeError("Tracker API daily limit reached (429). Will try again next check.")
        if resp.status in (401, 403):
            raise RuntimeError(f"Tracker API refused the key ({resp.status}). Check TRACKER_API_KEY in .env.")
        resp.raise_for_status()
        payload = await resp.json()
    members = find_members(payload)
    if not members:
        raise RuntimeError("Tracker API answered but no 'members' list was found.")
    roster = {}
    print("Roster sample:", [m.get("name") for m in members[:5]], "| fields:", sorted(members[0].keys()))
    for m in members:
        name = m.get("name") or m.get("nickname") or m.get("player_name") or m.get("username")
        power = m.get("power")
        if name and power is not None:
            roster[normalize(name)] = {"name": name, "power": float(power)}
    return roster


class Raphael(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # needs "Server Members Intent" switched on in the Developer Portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.session: aiohttp.ClientSession | None = None
        self.data = load_json(DATA_FILE, {"history": {}, "last_weekly": "", "last_unmatched_report": ""})
        self.lock = asyncio.Lock()

    async def setup_hook(self):
        self.session = aiohttp.ClientSession()
        guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
        register_commands(self)
        if guild:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        self.hourly.change_interval(minutes=CHECK_MINUTES)
        self.hourly.start()

    async def close(self):
        if self.session:
            await self.session.close()
        await super().close()

    def guild_obj(self) -> discord.Guild | None:
        if GUILD_ID:
            return self.get_guild(GUILD_ID)
        return self.guilds[0] if self.guilds else None

    async def post(self, channel_id: int, text: str):
        if not channel_id:
            log.info("(no channel set) %s", text)
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            log.warning("Channel %s not found or no access", channel_id)
            return
        for i in range(0, len(text), 1900):  # Discord 2000-char limit
            await channel.send(text[i:i + 1900], allowed_mentions=discord.AllowedMentions.none())

    async def sync_roles(self, trigger: str = "hourly") -> str:
        async with self.lock:
            guild = self.guild_obj()
            if guild is None:
                return "I'm not in a server yet."
            roster = await fetch_roster(self.session)
            overrides = {k.lower(): v for k, v in load_json(OVERRIDES_FILE, {}).items()}
            self.record_history(roster)

            role_by_name = {r.name: r for r in guild.roles}
            missing = [n for n in SETTINGS.might_roles if n not in role_by_name]
            if missing:
                return f"Can't find these roles in the server: {', '.join(missing)}"

            changes, unverified, unmatched, errors = [], [], [], []
            for member in guild.members:
                d = decide(
                    username=member.name, display_name=member.display_name,
                    current_roles=[r.name for r in member.roles], is_bot=member.bot,
                    roster=roster, overrides=overrides, s=SETTINGS,
                )
                if d.action == "unmatched":
                    unmatched.append(member.display_name)
                elif d.action == "unverified":
                    unverified.append(f"{member.display_name} ({fmt_power(d.power)} → {d.target})")
                elif d.action == "change":
                    line = f"{member.display_name}: {', '.join(d.remove) or 'no role'} → {d.target} ({fmt_power(d.power)})"
                    if not DRY_RUN:
                        try:
                            if d.remove:
                                await member.remove_roles(*[role_by_name[r] for r in d.remove if r in role_by_name], reason="Raphael might sync")
                            if d.add:
                                await member.add_roles(*[role_by_name[r] for r in d.add], reason="Raphael might sync")
                            if d.is_promotion:
                                await self.post(SHOUTOUT_CHANNEL_ID,
                                                f":crown: **RANK UP!** {member.mention} has reached **{d.target}** with {fmt_power(d.power)} might! Keep growing, Tempest!")
                        except discord.Forbidden:
                            errors.append(f"{member.display_name} (my role is too low to change theirs)")
                            continue
                    changes.append(line)

            mode = "DRY RUN - nothing was changed" if DRY_RUN else "applied"
            parts = []
            if changes:
                parts.append(f"**Role changes ({mode}):**\n" + "\n".join(f"- {c}" for c in changes))
            if errors:
                parts.append("**Couldn't change:**\n" + "\n".join(f"- {e}" for e in errors))
            today = datetime.now(TZ).date().isoformat()
            report_unmatched = trigger == "manual" or self.data.get("last_unmatched_report") != today
            if report_unmatched and unverified:
                parts.append("**Waiting for verification (Goblin/Wanderer, name matches the roster):**\n" + "\n".join(f"- {u}" for u in unverified))
            if report_unmatched and unmatched:
                parts.append("**Nickname doesn't match anyone on the JTF roster:**\n" + ", ".join(sorted(unmatched))
                             + "\nFix their server nickname, or add them to name_overrides.json.")
            if report_unmatched:
                self.data["last_unmatched_report"] = today
                save_json(DATA_FILE, self.data)
            summary = "\n\n".join(parts)
            if summary:
                await self.post(LOG_CHANNEL_ID, f":gear: **Raphael role check ({trigger})**\n\n{summary}")
            return summary or "Everyone's role already matches their might."

    def record_history(self, roster: dict[str, dict]):
        today = datetime.now(TZ).date().isoformat()
        self.data.setdefault("history", {})[today] = {p["name"]: p["power"] for p in roster.values()}
        for day in sorted(self.data["history"])[:-15]:  # keep ~2 weeks
            del self.data["history"][day]
        save_json(DATA_FILE, self.data)

    async def maybe_weekly(self):
        now = datetime.now(TZ)
        today = now.date().isoformat()
        if now.weekday() != WEEKLY_DAY or now.hour < WEEKLY_HOUR or self.data.get("last_weekly") == today:
            return
        gains = weekly_gains(self.data.get("history", {}), today)
        if gains:
            lines = [f"{i}. **{n}** +{fmt_power(g)} (now {fmt_power(p)})" for i, (n, g, p) in enumerate(gains, 1)]
            await self.post(SHOUTOUT_CHANNEL_ID, ":chart_with_upwards_trend: **BIGGEST GROWERS THIS WEEK** :chart_with_upwards_trend:\n" + "\n".join(lines)
                            + "\n\nArch Demon 22M+ | Greater Demon 16M+. Who's next?")
        self.data["last_weekly"] = today
        save_json(DATA_FILE, self.data)

    @tasks.loop(minutes=60)
    async def hourly(self):
        try:
            await self.sync_roles("hourly")
            await self.maybe_weekly()
        except Exception as e:  # keep the loop alive
            log.exception("Check failed")
            await self.post(LOG_CHANNEL_ID, f":warning: Raphael check failed: {e}")

    @hourly.before_loop
    async def before_hourly(self):
        await self.wait_until_ready()
        log.info("Logged in as %s | DRY_RUN=%s | checking every %s min", self.user, DRY_RUN, CHECK_MINUTES)


def register_commands(bot: Raphael):
    @bot.tree.command(name="syncroles", description="Check the tracker now and update might roles")
    @app_commands.default_permissions(manage_roles=True)
    async def syncroles(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await bot.sync_roles("manual")
        except Exception as e:
            result = f"Failed: {e}"
        await interaction.followup.send(result[:1900] or "Done.", ephemeral=True)

    @bot.tree.command(name="might", description="Look up a JTF member's might and role tier")
    @app_commands.describe(name="In-game name")
    async def might(interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            roster = await fetch_roster(bot.session)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the tracker: {e}", ephemeral=True)
            return
        p = roster.get(normalize(name))
        if not p:
            sample = ", ".join(v["name"] for v in list(roster.values())[:5]) or "(empty)"
            await interaction.followup.send(f"No one named '{name}' on the JTF roster. Roster has {len(roster)} names, e.g. {sample}", ephemeral=True)
            return
        from roles_logic import role_for_power
        await interaction.followup.send(f"**{p['name']}**: {fmt_power(p['power'])} → {role_for_power(p['power'], SETTINGS)}", ephemeral=True)


if __name__ == "__main__":
    Raphael().run(DISCORD_TOKEN, log_handler=None)
