# Raphael: Auto-Role Discord Bot for a Last Asylum Alliance

A Python Discord bot that keeps server roles in sync with player power from the Last Asylum Tracker API. It runs an hourly check, ranks members into role tiers, posts promotion shoutouts, and publishes a weekly top-gainers leaderboard.

**Built with:** Python 3.12+, discord.py (slash commands, scheduled tasks), aiohttp, REST API integration, JSON state, `.env` config, a dry-run safety mode.

**Highlights**
- Pure, unit-testable role logic separated from the Discord layer (`roles_logic.py`)
- Dry-run mode that reports changes before touching any roles
- Name matching that ignores case, spacing, symbols and alliance tags
- Handles API rate limits, auth errors and Cloudflare blocks gracefully

---

# Raphael: JTF Auto-Role Bot

Every hour Raphael checks the [JTF] roster on the Last Asylum tracker and keeps
everyone's Discord role matched to their might:

| Role | Might |
|---|---|
| Arch Demon | 22M+ |
| Greater Demon | 16M to 22M |
| Lesser Demon | under 16M |

It also:
- Never touches Demon Lord, Primordial or Envoy.
- Keeps anyone with the **Named Monster** role at Greater Demon or higher.
- Lets the members listed in `GRANDFATHER_ARCH` keep Arch Demon.
- Leaves Goblins and Wanderers for a Primordial to verify, and lists the ones ready to go.
- Posts once a day in your log channel with anyone whose nickname doesn't match the roster.
- Posts **RANK UP!** in main chat when someone is promoted.
- Posts the week's **Biggest Growers** every Monday at 10 AM Central.
- Adds two slash commands: `/syncroles` (check now; admins and officers only) and `/might <name>`.

---

## One-time setup (Windows, about 15 minutes)

### 1. Install Python
1. Download Python 3.12 or newer from python.org.
2. Run the installer. **Tick "Add python.exe to PATH"** at the bottom of the first screen, then click Install.

### 2. Put the bot files in a folder
Unzip this folder somewhere simple, like `C:\RaphaelBot`.

### 3. Install the bot's libraries
1. Open the folder in File Explorer.
2. Click the address bar, type `cmd` and press Enter.
3. In the black window that opens, run:
   ```
   pip install -r requirements.txt
   ```

### 4. Fill in your settings
1. Make a copy of `config.example.env` and rename the copy to exactly `.env`. If Windows warns you about changing the extension, click Yes.
2. Open `.env` in Notepad and fill in:
   - `DISCORD_TOKEN`: your bot token from the Developer Portal's Bot tab.
   - `TRACKER_API_KEY`: your `lap_…` key.
   - `GUILD_ID`, `LOG_CHANNEL_ID`, `SHOUTOUT_CHANNEL_ID`: turn on **Discord Settings → Advanced → Developer Mode**. Then right-click the server icon and choose **Copy Server ID**, and right-click a channel and choose **Copy Channel ID**.
3. Save the file. **Never share `.env`.** It holds your secrets.

### 5. Prepare the server
1. **Role order** (Server Settings → Roles): drag **Raphael** so it sits **below Primordial** and **above Arch Demon**.
2. **Named Monster role**: create a role called exactly `Named Monster` (no color, no permissions). Give it to the in-game Named Monsters who are in the Discord.
3. **Log channel access**: #executive-chat is private. Go to Edit Channel → Permissions, add the Raphael role, and allow View Channel and Send Messages.

### 6. Run it in test mode
In the black window, run:
```
python bot.py
```
You should see `Logged in as Raphael | DRY_RUN=True`. Type `/syncroles` in your server. Raphael will post what it **would** change in your log channel.

### 7. Switch it on for real
Once a few test reports look right, open `.env`, change `DRY_RUN=true` to `DRY_RUN=false`, save, and restart the bot. To restart, close the black window and run `python bot.py` again.

---

## Keeping it running
- The bot only works while that black window is open and the PC is awake.
- For 24/7, move the same folder (with `.env`) to any always-on host that runs Python, then start it with `python bot.py`.
- `data.json` stores the daily might history used for the weekly leaderboard. Keep it with the bot.

## Fixing name mismatches
Raphael matches each member's **server nickname** to their in-game name. Capitals, spaces and symbols are ignored. If someone can't change their nickname to match, add them to `name_overrides.json`:
```json
{ "their_discord_username": "TheirInGameName" }
```

## Changing the rules
Edit `.env` and restart the bot:
- `ARCH_MIN` / `GREATER_MIN`: the might cutoffs.
- `GRANDFATHER_ARCH`: Discord usernames that always keep Arch Demon.
- `AUTO_VERIFY=true`: Goblins and Wanderers whose nicknames match get their role automatically.
- `WEEKLY_DAY` / `WEEKLY_HOUR`: when the Biggest Growers post goes out.

## Troubleshooting
| Message | Fix |
|---|---|
| `Missing DISCORD_TOKEN in .env` | The file must be named exactly `.env`, not `.env.txt`, and must be in the same folder as `bot.py`. |
| `Tracker API refused the key` | Re-copy your `lap_` key into `.env`. |
| `my role is too low` | Drag Raphael's role higher, but keep it below Primordial. |
| `Privileged intent` error on start | Switch on **Server Members Intent** in the Developer Portal's Bot tab. |
| `/syncroles` doesn't show up | Check that `GUILD_ID` is set, then restart the bot. |
