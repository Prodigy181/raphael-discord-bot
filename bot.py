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

from roles_logic import Settings, days_offline, decide, fmt_power, inactivity_groups, normalize, weekly_gains

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
INACTIVE_TIERS = tuple(int(x) for x in env("INACTIVE_DAYS", "1,2,4,6").split(",") if x.strip())
INACTIVITY_HOUR = env_int("INACTIVITY_HOUR", 9)
# Optional daily standings post. Off until STANDINGS_CHANNEL_ID is set.
STANDINGS_CHANNEL_ID = env_int("STANDINGS_CHANNEL_ID")
STANDINGS_HOUR = env_int("STANDINGS_HOUR", 9)

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
    async with session.get(API_URL, params={"fresh": "1"}, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
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
    print("Online fields (first 3):", [(m.get("nick_name"), m.get("online"), m.get("last_online_at"), m.get("active")) for m in members[:3]])
    print("last_online_at filled for", sum(1 for m in members if m.get("last_online_at")), "of", len(members), "| online flag true for", sum(1 for m in members if m.get("online")))
    print("Roster sample:", [m.get("nick_name") or m.get("name") for m in members[:5]], "| count:", len(members))
    for m in members:
        name = m.get("nick_name") or m.get("name") or m.get("nickname") or m.get("player_name") or m.get("username")
        power = m.get("power")
        if name and power is not None:
            roster[normalize(name)] = {
                "name": name, "power": float(power),
                "last_online": m.get("last_online_at"), "online": bool(m.get("online")),
                "active": m.get("active"), "tech": m.get("daily_tech"), "honor": m.get("daily_honor"),
                "kills": m.get("kills"), "rank": m.get("rank_label") or m.get("rank_code") or "",
            }
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
        self.last_roster: dict[str, dict] = {}

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
            self.track_presence(roster)
            self.last_roster = roster
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
        # hourly stats snapshot: daily values keep their highest reading of the day
        stats = self.data.setdefault("stats", {}).setdefault(today, {})
        for p in roster.values():
            old = stats.get(p["name"], {})
            stats[p["name"]] = {
                "tech": max(p.get("tech") or 0, old.get("tech", 0)),
                "honor": max(p.get("honor") or 0, old.get("honor", 0)),
                "kills": p.get("kills") or old.get("kills", 0),
                "power": p["power"], "active": p.get("active") or 0,
            }
        for day in sorted(self.data["stats"])[:-15]:
            del self.data["stats"][day]
        save_json(DATA_FILE, self.data)

    def ranking_text(self, stat: str, top: int = 10) -> str:
        stats = self.data.get("stats", {})
        days = sorted(stats)
        if not days:
            return "No data yet. Try again after the next check."
        today = stats[days[-1]]
        week = days[-7:]
        label = {"tech": "Tech donated", "honor": "Honor", "kills": "Kills", "power": "Power", "active": "Weekly activity"}[stat]
        out = []
        def fmt(v):
            return fmt_power(v) if stat in ("power", "kills") else f"{int(v):,}"
        now_rows = sorted(((n, v.get(stat, 0)) for n, v in today.items()), key=lambda r: r[1], reverse=True)
        out.append(f"**{label}: top {top} (latest reading)**")
        out += [f"{i}. {n}: {fmt(v)}" for i, (n, v) in enumerate(now_rows[:top], 1)]
        if stat in ("tech", "honor") and len(week) > 1:
            tot = {}
            for d in week:
                for n, v in stats[d].items():
                    tot[n] = tot.get(n, 0) + v.get(stat, 0)
            rows = sorted(tot.items(), key=lambda r: r[1], reverse=True)[:top]
            out.append(f"\n**{label}: top {top} over the last {len(week)} tracked days**")
            out += [f"{i}. {n}: {int(v):,}" for i, (n, v) in enumerate(rows, 1)]
        if stat in ("kills", "power") and len(days) > 1:
            base = stats[days[0]]
            gains = sorted(((n, v.get(stat, 0) - base[n].get(stat, 0)) for n, v in today.items() if n in base), key=lambda r: r[1], reverse=True)[:top]
            out.append(f"\n**{label} gained since {days[0]}**")
            out += [f"{i}. {n}: +{fmt(g)}" for i, (n, g) in enumerate(gains, 1) if g > 0]
        if stat in ("tech", "honor"):
            zero = [n for n, v in today.items() if not v.get(stat)]
            out.append(f"\n_{len(zero)} members at 0 today._")
        return "\n".join(out)

    def standings_text(self, top: int = 3) -> str:
        stats = self.data.get("stats", {})
        days = sorted(stats)
        if not days:
            return ""
        today = stats[days[-1]]
        specs = [
            ("power", ":crossed_swords: Power", lambda v: fmt_power(v)),
            ("kills", ":dagger: Kills", lambda v: fmt_power(v)),
            ("honor", ":medal: Honor", lambda v: f"{int(v):,}"),
            ("active", ":fire: Weekly activity", lambda v: f"{int(v):,}"),
            ("tech", ":test_tube: Tech donated", lambda v: f"{int(v):,}"),
        ]
        lines = [":bar_chart: **TEMPEST STANDINGS (daily)**"]
        for key, label, fmt in specs:
            rows = sorted(((n, v.get(key, 0)) for n, v in today.items()), key=lambda r: r[1], reverse=True)
            rows = [(n, v) for n, v in rows[:top] if v]
            if rows:
                lines.append(f"{label}: " + ", ".join(f"{n} {fmt(v)}" for n, v in rows))
        if len(lines) == 1:
            return ""
        lines.append("\nCheck your own numbers with /might or /rankings. Keep climbing, Tempest!")
        return "\n".join(lines)

    async def maybe_standings(self):
        if not STANDINGS_CHANNEL_ID:
            return
        now = datetime.now(TZ)
        today = now.date().isoformat()
        if now.hour < STANDINGS_HOUR or self.data.get("last_standings") == today:
            return
        text = self.standings_text()
        if text:
            await self.post(STANDINGS_CHANNEL_ID, text)
        self.data["last_standings"] = today
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
        stats = self.data.get("stats", {})
        week = sorted(stats)[-7:]
        tot = {}
        for d in week:
            for n, v in stats[d].items():
                tot[n] = tot.get(n, 0) + v.get("tech", 0)
        top = [(n, t) for n, t in sorted(tot.items(), key=lambda r: r[1], reverse=True)[:5] if t > 0]
        if top and len(week) >= 2:
            lines = [f"{i}. **{n}**: {int(t):,} tech" for i, (n, t) in enumerate(top, 1)]
            await self.post(SHOUTOUT_CHANNEL_ID, ":gem: **TOP DONORS THIS WEEK** :gem:\n" + "\n".join(lines)
                            + "\n\nThank you for powering up the alliance!")
        self.data["last_weekly"] = today
        save_json(DATA_FILE, self.data)

    def track_presence(self, roster: dict[str, dict]):
        """The tracker sends no last-online times, so remember when each member was last seen online."""
        now_iso = datetime.now(timezone.utc).isoformat()
        seen = self.data.setdefault("seen", {})
        since = self.data.setdefault("tracking_since", now_iso)
        prev = self.data.setdefault("active_prev", {})
        for key, m in roster.items():
            act = m.get("active")
            changed = act is not None and key in prev and prev[key] != act
            if act is not None:
                prev[key] = act
            if m.get("online") or changed:
                seen[key] = now_iso
            if not m.get("last_online"):
                # never seen online yet: count from when tracking began (a minimum, not exact)
                m["last_online"] = seen.get(key, since)
        save_json(DATA_FILE, self.data)

    def inactivity_text(self, roster: dict[str, dict], always: bool = False) -> str:
        now_utc = datetime.now(timezone.utc)
        groups = inactivity_groups(roster, now_utc, INACTIVE_TIERS)
        tiers = sorted(groups)

        def label(i: int, t: int) -> str:
            top = i == len(tiers) - 1
            if top:
                return f"{t}+ days offline: leadership decides on a kick"
            nxt = tiers[i + 1] - 1
            span = f"{t} day" if nxt <= t else f"{t}-{nxt} days"
            note = "message them in-game" if i == len(tiers) - 2 else "heads up"
            return f"{span} offline: {note}"

        parts = []
        for i in reversed(range(len(tiers))):
            rows = groups[tiers[i]]
            if rows:
                lines = [f"- {n}{f' ({r})' if r else ''}: {d} day{'s' if d != 1 else ''}" for n, r, d in rows]
                parts.append(f"**{label(i, tiers[i])}** ({len(rows)})\n" + "\n".join(lines))
        known = sum(1 for m in roster.values() if days_offline(m, now_utc) is not None)
        unknown = len(roster) - known
        if not parts and (always or known == 0):
            if known == 0:
                sample = next((m.get("last_online") for m in roster.values() if m.get("last_online")), None)
                parts.append(f"I couldn't read anyone's last-online time. Sample value from the tracker: {sample!r}")
            else:
                parts.append(f"Everyone has been online within the last {tiers[0]} day(s).")
        if parts:
            parts.append(f"_Checked {known} of {len(roster)} members" + (f"; {unknown} {'has' if unknown == 1 else 'have'} no last-online data._" if unknown else "._"))
        return "\n\n".join(parts)

    async def maybe_inactivity(self):
        now = datetime.now(TZ)
        today = now.date().isoformat()
        if now.hour < INACTIVITY_HOUR or self.data.get("last_inactivity_report") == today or not self.last_roster:
            return
        text = self.inactivity_text(self.last_roster)
        if text and "Checked" in text and not text.startswith("Everyone"):
            await self.post(LOG_CHANNEL_ID, ":zzz: **Daily inactivity report**\n\n" + text)
        self.data["last_inactivity_report"] = today
        save_json(DATA_FILE, self.data)

    @tasks.loop(minutes=60)
    async def hourly(self):
        try:
            await self.sync_roles("hourly")
            await self.maybe_weekly()
            await self.maybe_inactivity()
            await self.maybe_standings()
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

    @bot.tree.command(name="inactive", description="Show members who have been offline 1, 2, 4 and 6+ days")
    @app_commands.default_permissions(manage_roles=True)
    async def inactive(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            roster = await fetch_roster(bot.session)
            bot.track_presence(roster)
            text = bot.inactivity_text(roster, always=True)
        except Exception as e:
            text = f"Failed: {e}"
        await interaction.followup.send(text[:1900], ephemeral=True)

    @bot.tree.command(name="rankings", description="Top members by tech donated, honor, kills, power or activity")
    @app_commands.describe(stat="Which ranking to show")
    @app_commands.choices(stat=[
        app_commands.Choice(name="Tech donated", value="tech"),
        app_commands.Choice(name="Honor", value="honor"),
        app_commands.Choice(name="Kills", value="kills"),
        app_commands.Choice(name="Power", value="power"),
        app_commands.Choice(name="Weekly activity", value="active"),
    ])
    async def rankings(interaction: discord.Interaction, stat: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            text = bot.ranking_text(stat.value)
        except Exception as e:
            text = f"Failed: {e}"
        await interaction.followup.send(text[:1900], ephemeral=False)

    @bot.tree.command(name="might", description="Look up a JTF member's might and role tier")
    @app_commands.describe(name="In-game name")
    async def might(interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=False)
        try:
            roster = await fetch_roster(bot.session)
        except Exception as e:
            await interaction.followup.send(f"Couldn't reach the tracker: {e}", ephemeral=False)
            return
        p = roster.get(normalize(name))
        if not p:
            sample = ", ".join(v["name"] for v in list(roster.values())[:5]) or "(empty)"
            await interaction.followup.send(f"No one named '{name}' on the JTF roster. Roster has {len(roster)} names, e.g. {sample}", ephemeral=False)
            return
        from roles_logic import role_for_power
        await interaction.followup.send(f"**{p['name']}**: {fmt_power(p['power'])} → {role_for_power(p['power'], SETTINGS)}", ephemeral=False)

    # --- Named Monster role (members of the in-game Named Monsters group chat) ---
    NM_ROLE = env("NAMED_MONSTER_ROLE", "Named Monster")
    nm = app_commands.Group(
        name="namedmonster",
        description="Manage the Named Monster role",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    async def nm_change(interaction: discord.Interaction, member: discord.Member, add: bool):
        await interaction.response.defer(ephemeral=True)
        role = discord.utils.get(interaction.guild.roles, name=NM_ROLE)
        if role is None:
            await interaction.followup.send(f"I can't find a role named '{NM_ROLE}'.", ephemeral=True)
            return
        if member.bot:
            await interaction.followup.send("Bots can't be Named Monsters.", ephemeral=True)
            return
        has_role = role in member.roles
        if add and has_role:
            await interaction.followup.send(f"{member.display_name} is already a Named Monster.", ephemeral=True)
            return
        if not add and not has_role:
            await interaction.followup.send(f"{member.display_name} isn't a Named Monster.", ephemeral=True)
            return
        try:
            reason = f"/namedmonster {'add' if add else 'remove'} by {interaction.user}"
            if add:
                await member.add_roles(role, reason=reason)
            else:
                await member.remove_roles(role, reason=reason)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I'm not allowed to change that role. Move my Raphael role above '{NM_ROLE}' in Server Settings > Roles.",
                ephemeral=True,
            )
            return
        verb = "added to" if add else "removed from"
        await interaction.followup.send(f"{member.display_name} {verb} {NM_ROLE}.", ephemeral=True)
        await bot.post(LOG_CHANNEL_ID, f":smiling_imp: **{NM_ROLE}**: {member.display_name} {verb} the role by {interaction.user.display_name}.")

    @nm.command(name="add", description="Give a member the Named Monster role")
    @app_commands.describe(member="Discord member who is in the in-game Named Monsters group")
    async def nm_add(interaction: discord.Interaction, member: discord.Member):
        await nm_change(interaction, member, True)

    @nm.command(name="remove", description="Remove the Named Monster role from a member")
    @app_commands.describe(member="Discord member to remove")
    async def nm_remove(interaction: discord.Interaction, member: discord.Member):
        await nm_change(interaction, member, False)

    @nm.command(name="list", description="List current Named Monsters")
    async def nm_list(interaction: discord.Interaction):
        role = discord.utils.get(interaction.guild.roles, name=NM_ROLE)
        if role is None:
            await interaction.response.send_message(f"I can't find a role named '{NM_ROLE}'.", ephemeral=True)
            return
        names = sorted(m.display_name for m in role.members)
        text = f"**{NM_ROLE}** ({len(names)}): " + (", ".join(names) or "nobody yet")
        await interaction.response.send_message(text[:1900], ephemeral=True)

    bot.tree.add_command(nm)


if __name__ == "__main__":
    Raphael().run(DISCORD_TOKEN, log_handler=None)
