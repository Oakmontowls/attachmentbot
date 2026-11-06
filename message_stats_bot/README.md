# Message Stats Bot

A standalone, collection-only Discord statistics bot for a separate backend
viewer. It tracks non-bot messages in:

- text channels
- announcement channels
- text chat attached to voice channels
- public and joined private threads
- forum post threads

Forum posts are stored as individual channels. Each record also includes the
parent forum ID, allowing the backend to report on one post or roll up all posts
inside a forum.

The bot has no commands. It requests Message Content only to parse the explicitly
scoped legacy migration embeds and never stores message text, voice activity, or
game activity.

## Stored data

The SQLite database contains two tables:

- `channels`: channel/thread identity, type, parent, owner, creation time, and
  archive state.
- `messages`: message ID, server ID, channel/thread ID, parent forum/channel ID,
  author ID, and creation time.

Useful indexes are included for server, channel, parent forum, member, and time
range queries. SQLite WAL mode is enabled so a backend can read the database while
the collector is running.

Deleted messages remain in the database because this records historical activity,
not the current contents of Discord channels.

## Setup

1. Create a separate application and bot in the Discord Developer Portal.
2. Invite it with only the `bot` scope; there are no application commands.
3. Give it View Channels and Read Message History everywhere it should collect.
4. Install the dependency and run it from the workspace root:

```powershell
python -m pip install -r message_stats_bot/requirements.txt
$env:MESSAGE_STATS_BOT_TOKEN="your-new-bot-token"
python -m message_stats_bot.bot
```

On the first startup, the bot imports all history it can access from regular text
channels, voice-channel text chat, active threads, archived public threads, archived
forum posts, and joined private threads. Discord does not expose private threads the
bot cannot access.

History progress is tracked with a dedicated per-location cursor. Live messages do
not advance that cursor, so a message arriving today cannot cause older channel or
forum-post history to be skipped. Interrupted imports resume from the last history
page successfully traversed. Forum-post threads are imported before ordinary text
channels so forum coverage fills in early during a large initial backfill.

Bot-authored messages remain excluded except for migrated forum history with the
recognized embed format. Each qualifying embed is counted as one original post.
By default this applies only to forum `1445647609342726194`. Migration bot
`1410698528627490818` is checked when Discord exposes its author, application, or
webhook ID. Webhook copies that hide the creating bot's ID are recognized by the
same forum restriction plus their avatar-ID and legacy-timestamp embed structure.
Override the configured IDs with
`MESSAGE_STATS_MIGRATION_FORUM_ID` and `MESSAGE_STATS_MIGRATION_BOT_ID` if needed.
The original member ID comes from the embed author's Discord avatar URL, the embed
author name becomes member metadata, and the embedded timestamp becomes the
activity date. Content is inspected in memory but is not stored. Set
`MESSAGE_STATS_MIGRATION_TIMEZONE` to the IANA timezone used by those displayed
legacy timestamps; it defaults to `America/Los_Angeles`.
Consecutive embeds from the same original member may omit repeated timestamps;
those posts inherit that member's preceding timestamp. The context is saved with
the thread cursor so an interrupted import resumes with the same result.

On later startups, each channel resumes after its dedicated history cursor, while
newly accessible channels and threads receive a complete import. Message IDs are
unique, so live events and concurrent history import cannot double-count a message.

History rows are committed in batches of 5,000 to keep large imports efficient.
Live messages are committed immediately. Set `MESSAGE_STATS_BATCH_SIZE` to a
different positive integer if the host needs smaller or larger transactions.
History scans use eight concurrent location workers by default so one very large
channel cannot block all others; discord.py still enforces Discord's route and
global rate limits. Set `MESSAGE_STATS_IMPORT_CONCURRENCY` to tune that worker count.
Transient Discord/API failures are retried five times with exponential backoff and
resume from the last committed cursor. Set `MESSAGE_STATS_HISTORY_RETRY_ATTEMPTS`
to change that limit. Final logs distinguish completed, inaccessible, and failed
locations; an import is never labeled complete while a retryable scan is unfinished.
Partially traversed locations are prioritized on the next startup so large channels
resume before already-complete locations are checked for incremental messages.
On Windows the collector uses the selector event loop to avoid Python 3.11
Proactor socket-shutdown errors during gateway reconnects.

Set `MESSAGE_STATS_DATABASE=C:\path\to\stats.sqlite3` to change the database
location.

## Statistics dashboard

Run the read-only local dashboard in a second terminal while the collector is
running:

```powershell
python -m message_stats_bot.viewer
```

Open `http://127.0.0.1:8765`. The dashboard provides message totals, contributors,
daily/weekly/monthly charts, hour-of-week activity, member and channel rankings,
forum rollups, and individual forum-post thread drilldowns. Filters cover preset or
custom time ranges, channels, forums, threads, and members.

Daily aggregates are maintained automatically by SQLite so long-range reports do
not repeatedly scan every raw message. The first collector startup after this
upgrade builds the aggregate once from existing history.

The viewer binds only to localhost by default. For a private network or eventual
reverse-proxy deployment, set both a non-local host and a strong access token:

```powershell
$env:MESSAGE_STATS_VIEWER_HOST="0.0.0.0"
$env:MESSAGE_STATS_VIEWER_TOKEN="generate-a-long-random-secret"
python -m message_stats_bot.viewer
```

The server refuses a non-local bind without a token. An internet-facing deployment
should additionally terminate HTTPS at a reverse proxy and replace the shared token
with proper user authentication before opening access broadly.
