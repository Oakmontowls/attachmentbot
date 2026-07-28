# AttachmentBot

AttachmentBot is a Discord moderation bot focused on detecting image-based scam messages and controlling message spam. Each server has its own settings, keywords, channels, roles, and moderation options stored in SQLite.

## Features

### OCR image scanning

- Scans messages containing two or more image attachments.
- Can also scan single-image messages from low-activity users.
- Uses Tesseract OCR to extract text from images.
- Scores detected text against configurable keywords and aliases.
- Logs detections to a configured Discord channel.
- Reuploads detected images in a separate log message.
- Saves images under `detected images/` if Discord rejects the reupload.
- Can delete detected messages and assign the configured timeout role.
- Supports channel blacklists.

### Pressure moderation

- Tracks pressure separately for each member and channel.
- Adds pressure for message activity such as attachments, mentions, links, repeated messages, line breaks, solo emotes, GIFs, and banned words.
- Gradually reduces pressure over time.
- Supports a global threshold and per-channel threshold overrides, including forum channels and their threads.
- Can delete messages posted at or above the threshold.
- Can temporarily assign the timeout role and remove it after a configurable duration.
- Restores expired temporary-role state after the bot has been offline.
- Supports a separate pressure log channel with fallback to the shared server log channel.

### Server configuration

- Settings are stored per server in `attachmentbot.sqlite3`.
- New servers receive disabled default OCR and pressure settings with a built-in keyword configuration.
- Administrators can designate manager roles that are allowed to configure the bot.
- Moderation features are structured separately so additional moderation systems can reuse shared settings and temporary-role handling.

### Territory tracking

- Tracks each user's message count separately in every channel over a rolling 24-hour window.
- Builds the initial counts from accessible message history when the bot starts.
- Updates counts as new messages are posted and excludes bot messages.
- Provides a top-five channel leaderboard through `/territory leaderboard`.
- Can be explicitly enabled to maintain a territory line at the top of each text channel topic.
- Uses `Unclaimed territory` when a channel has no user messages in the rolling window.
- Preserves existing topic text below the managed line and removes bot-created lines when disabled.
- Supports a channel blacklist and a configurable update interval that defaults to 60 minutes.

### Recycle duplicate detection

- Can monitor one configured text channel per server when explicitly enabled.
- Builds a persistent index using a configurable rolling repost window from 1 to 365 days, defaulting to 30.
- Detects normalized duplicate links and perceptually similar image attachments.
- Perceptually hashes a representative frame from video attachments using FFmpeg.
- Treats Twitter/X and Instagram embed-fixer domains as the same underlying post link.
- Ignores Tenor, Klipy, and Giphy links.
- Can avoid flagging reply messages while still indexing them for future repost matches.
- Logs historical and new matches to the console.
- Adds Discord's standard `:recycle:` reaction to new duplicate-image and duplicate-link messages.
- Can reply with `:recycle:` and ping the author when Discord rejects the reaction.
- Resumes from its saved checkpoint instead of repeating completed history scans.

### News relay

- Relays new messages from a configured text or announcement channel into an existing forum post thread.
- Supports source channels in another server when the bot and configuring manager have access to both servers.
- Copies message text, embeds, and downloadable attachments without forwarding user, role, or everyone mentions.
- Handles attachment and embed limits with follow-up batches.
- Attempts to reopen archived forum posts and records deliveries to prevent duplicate relays.
- Supports multiple source-to-thread routes per destination server and is disabled by default.
- Relays only messages received after a route is configured; it does not backfill channel history.

## Slash commands

All commands begin with `/ab`.

- `/ab status` shows the server configuration.
- `/ab manager` manages roles allowed to configure the bot.
- `/ab timeout-role` selects the role used for moderation timeouts.
- `/ab ocr` contains OCR settings, keyword and alias management, channel blacklists, moderation actions, and single-image scanning settings.
- `/ab pressure` contains pressure settings, channel thresholds, banned words, current-pressure viewing, and pressure resets.
- `/territory leaderboard` shows the public rolling 24-hour message leaderboard.
- `/ab territory` contains manager-only territory-description settings.
- `/ab recycle` enables duplicate detection and selects its monitored channel.
- `/ab relay` enables news relaying and manages source-to-forum-thread routes.

Configuration commands require either the Discord Administrator permission or a manager role configured by an administrator.

## Optional modules

OCR, pressure, territory, recycle, and relay are independent optional cogs. They share database, permission, activity-cache, and temporary-role services from `core/`, but none imports or requires another feature cog.

All five modules load by default. Set `ATTACHMENTBOT_MODULES` to a comma-separated subset to run only selected features:

```powershell
$env:ATTACHMENTBOT_MODULES="ocr,pressure"
python main.py
```

The available names are `ocr`, `pressure`, `territory`, `recycle`, and `relay`. A module omitted from this setting is not loaded and its `/ab` command group is not registered. Optional-module load failures are logged without stopping the other modules. The shared `base` and `activity` cogs remain loaded because they provide `/ab`, permissions, and the rolling message cache.

### Configuring a news relay

Copy the source channel ID using Discord Developer Mode, then create and enable a route from the destination server:

```text
/ab relay add source_channel_id:123456789012345678 target_thread:#existing-forum-post
/ab relay enabled enabled:true
```

Use `/ab relay list` to view route IDs and `/ab relay remove` to delete one. For a cross-server source, the configuring user must be an Administrator or approved AttachmentBot manager in both servers. The bot needs View Channel access in the source, plus View Channel, Send Messages in Threads, Embed Links, and Attach Files in the destination. Manage Threads is needed to reopen an archived forum post automatically.

## Running the bot

The bot requires Python, the packages imported by the project, and a working Tesseract OCR installation. Recycle video hashing also requires the `ffmpeg` executable to be available on `PATH`.

Set the bot token using the `DISCORD_TOKEN` environment variable. A token can also be placed in `config.py` as `TOKEN`, though an environment variable is preferred.

```powershell
$env:DISCORD_TOKEN="your-token"
python main.py
```

Invite the bot with the `bot` and `applications.commands` OAuth scopes. It needs access to read channel history and messages, send messages and attachments, embed links, manage messages, and manage the configured timeout role. The bot's role must be above the timeout role in Discord's role hierarchy.

Do not commit bot tokens, the SQLite database, logs, or locally saved detected images.

## Standalone duplicate detector

`duplicate_link_filename_bot.py` is a separate bot for indexing one channel and detecting repeated links or perceptually similar image attachments. Its initial scan covers the last 30 days, runs oldest-to-newest, prints historical duplicates with links to both messages, saves progress in SQLite, and resumes from its last checkpoint after a restart. Historical matches are console-only. New messages with duplicate images are logged and receive a reaction; URL-only duplicates are only logged. Tenor and Klipy links are ignored.

```powershell
$env:DUPLICATE_BOT_TOKEN="your-separate-bot-token"
$env:DUPLICATE_CHANNEL_ID="123456789012345678"
python duplicate_link_filename_bot.py
```

Set `DUPLICATE_DATABASE` to change the index database path. `DUPLICATE_PHASH_DISTANCE` controls image similarity from `0` (identical pHash) through `7`; it defaults to `6`. `DUPLICATE_REACTION` changes the reaction and defaults to Discord's standard `:recycle:` reaction. The bot needs View Channel, Read Message History, Add Reactions, and the Message Content intent for the target channel.
