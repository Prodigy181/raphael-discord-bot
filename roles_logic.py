"""Pure role-decision logic for the Raphael bot (no Discord calls, easy to test)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from dataclasses import dataclass, field


def normalize(name: str) -> str:
    """Lowercase and drop spaces/punctuation so 'DrAmy68' == 'dramy 68'."""
    name = re.sub(r"^\s*[\[(][^\])]*[\])]\s*", "", name or "")  # drop a leading [TAG]
    return re.sub(r"[^0-9a-z]", "", name.lower())


def fmt_power(power: float) -> str:
    return f"{power / 1_000_000:.1f}M"


@dataclass
class Settings:
    arch_role: str = "Arch Demon"
    greater_role: str = "Greater Demon"
    lesser_role: str = "Lesser Demon"
    arch_min: int = 22_000_000
    greater_min: int = 16_000_000
    exempt_roles: tuple = ("Demon Lord", "Primordial", "Envoy")
    unverified_roles: tuple = ("Goblin", "Wanderer")
    named_monster_role: str = "Named Monster"
    grandfather_arch: set = field(default_factory=set)  # Discord usernames that keep Arch Demon
    auto_verify: bool = False  # if True, matched Goblins/Wanderers get their might role

    @property
    def might_roles(self) -> list[str]:
        return [self.arch_role, self.greater_role, self.lesser_role]

    def rank_of(self, role: str) -> int:
        return {self.lesser_role: 0, self.greater_role: 1, self.arch_role: 2}[role]


@dataclass
class Decision:
    action: str  # "skip" | "unmatched" | "unverified" | "ok" | "change"
    target: str | None = None
    add: list = field(default_factory=list)
    remove: list = field(default_factory=list)
    power: float | None = None
    game_name: str | None = None
    reason: str = ""

    @property
    def is_promotion(self) -> bool:
        return self.action == "change" and bool(self.add) and self.reason.startswith("promote")


def role_for_power(power: float, s: Settings) -> str:
    if power >= s.arch_min:
        return s.arch_role
    if power >= s.greater_min:
        return s.greater_role
    return s.lesser_role


def decide(
    *,
    username: str,
    display_name: str,
    current_roles: list[str],
    is_bot: bool,
    roster: dict[str, dict],
    overrides: dict[str, str],
    s: Settings,
) -> Decision:
    """Work out what a single member's might role should be.

    roster: normalized in-game name -> {"name": str, "power": number}
    overrides: Discord username (lowercase) -> in-game name, for nicknames that can't match.
    """
    if is_bot or any(r in current_roles for r in s.exempt_roles):
        return Decision("skip", reason="bot or exempt role")

    game_name = overrides.get(username.lower()) or display_name
    player = roster.get(normalize(game_name))
    if player is None:
        return Decision("unmatched", reason=f"nickname '{display_name}' not on the roster")

    power = float(player.get("power") or 0)
    target = role_for_power(power, s)

    # Floors: Named Monsters never drop below Greater Demon; grandfathered keep Arch Demon.
    if s.named_monster_role and s.named_monster_role in current_roles and s.rank_of(target) < s.rank_of(s.greater_role):
        target = s.greater_role
    if username.lower() in s.grandfather_arch:
        target = s.arch_role

    unverified = [r for r in s.unverified_roles if r in current_roles]
    if unverified and not s.auto_verify:
        return Decision("unverified", target=target, power=power, game_name=player["name"],
                        reason=f"has {', '.join(unverified)}; needs a Primordial to verify")

    current_might = [r for r in s.might_roles if r in current_roles]
    remove = [r for r in current_might if r != target] + unverified
    add = [] if target in current_roles else [target]

    if not add and not remove:
        return Decision("ok", target=target, power=power, game_name=player["name"])

    if current_might:
        old = max(current_might, key=s.rank_of)
        direction = "promote" if s.rank_of(target) > s.rank_of(old) else ("demote" if s.rank_of(target) < s.rank_of(old) else "fix")
    else:
        direction = "promote"  # first might role (new verified member)
    return Decision("change", target=target, add=add, remove=remove, power=power,
                    game_name=player["name"], reason=f"{direction}: {fmt_power(power)}")


def weekly_gains(history: dict[str, dict[str, float]], today: str, days: int = 7, top: int = 5):
    """history: 'YYYY-MM-DD' -> {name: power}. Returns [(name, gain, now_power)] best first."""
    from datetime import date, timedelta

    dates = sorted(history)
    if today not in history or len(dates) < 2:
        return []
    target = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    older = [d for d in dates if d <= target] or [dates[0]]
    base_day = older[-1]
    if base_day == today:
        return []
    base, now = history[base_day], history[today]
    gains = [(n, now[n] - base[n], now[n]) for n in now if n in base]
    gains.sort(key=lambda g: g[1], reverse=True)
    return [g for g in gains if g[1] > 0][:top]


def parse_last_online(raw, now: datetime) -> datetime | None:
    """Accept epoch seconds, epoch milliseconds, or an ISO date string. Returns an aware UTC time, or None."""
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().replace(".", "", 1).isdigit()):
            value = float(raw)
            if value > 1e11:  # milliseconds
                value /= 1000
            return datetime.fromtimestamp(value, tz=timezone.utc)
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def days_offline(member: dict, now: datetime) -> float | None:
    """Days since the member was last online. 0 if online now, None if unknown."""
    if member.get("online"):
        return 0.0
    seen = parse_last_online(member.get("last_online"), now)
    if seen is None:
        return None
    return max((now - seen).total_seconds() / 86400, 0.0)


def inactivity_groups(roster: dict, now: datetime, tiers=(2, 4, 6)):
    """Return {tier_days: [(name, rank, whole_days), ...]} with each member in the highest tier they reach."""
    tiers = sorted(tiers)
    groups = {t: [] for t in tiers}
    for m in roster.values():
        d = days_offline(m, now)
        if d is None:
            continue
        hit = [t for t in tiers if d >= t]
        if hit:
            groups[hit[-1]].append((m["name"], m.get("rank") or "", int(d)))
    for t in groups:
        groups[t].sort(key=lambda x: -x[2])
    return groups
