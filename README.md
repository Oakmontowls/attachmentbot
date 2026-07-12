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
- Supports a separate pressure log channel with fallback to the OCR log channel.

### Server configuration

- Settings are stored per server in `attachmentbot.sqlite3`.
- New servers receive disabled default OCR and pressure settings with a built-in keyword configuration.
- Administrators can designate manager roles that are allowed to configure the bot.
- Moderation features are structured separately so additional moderation systems can reuse shared settings and temporary-role handling.

## Slash commands

All commands begin with `/ab`.

- `/ab status` shows the server configuration.
- `/ab manager` manages roles allowed to configure the bot.
- `/ab timeout-role` selects the role used for moderation timeouts.
- `/ab ocr` contains OCR settings, keyword and alias management, channel blacklists, moderation actions, and single-image scanning settings.
- `/ab pressure` contains pressure settings, channel thresholds, banned words, current-pressure viewing, and pressure resets.

Configuration commands require either the Discord Administrator permission or a manager role configured by an administrator.

## Running the bot

The bot requires Python, the packages imported by the project, and a working Tesseract OCR installation.

Set the bot token using the `DISCORD_TOKEN` environment variable. A token can also be placed in `config.py` as `TOKEN`, though an environment variable is preferred.

```powershell
$env:DISCORD_TOKEN="your-token"
python main.py
```

Invite the bot with the `bot` and `applications.commands` OAuth scopes. It needs access to read channel history and messages, send messages and attachments, embed links, manage messages, and manage the configured timeout role. The bot's role must be above the timeout role in Discord's role hierarchy.

Do not commit bot tokens, the SQLite database, logs, or locally saved detected images.
