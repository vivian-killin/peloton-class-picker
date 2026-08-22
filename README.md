# peloton-picker

Finds Peloton classes whose playlist actually has music you like.

There are two versions in here.

## 1. The website — anyone can use it, no login

**→ https://vivian-killin.github.io/peloton-class-picker/**

Type in a few artists, songs, or genres you love. It searches a snapshot of the Peloton
class library — about 20,000 classes and their playlists, the best-rated slice of
Peloton's 43,000 — and ranks the ones whose music matches. Results are ranked by how much of your list a class covers — a class with one song from
each of three artists you named beats one that plays a single artist six times. Every
result explains *why* it matched: how many of the things you named it hits, which songs,
and which genre tags landed. Expanding **Full playlist** shows every track in the class,
with the ones you asked for highlighted.

Filters: **class type** (multi-select — pick Cycling and Pilates together), **category**
(a sub-filter that narrows within the types you picked — Power Zone, Intervals, Barre,
Full Body, and so on), **length**, and **instructor**. "Surprise me" picks one at random
from the top matches.

It's a single static page — one `index.html`, plain CSS custom properties, vanilla DOM JS,
no build step. Everything runs in your browser: no account, no server, no data leaves the
page. Classes you mark "Done / hide" are remembered in your browser only.

The holographic look is built from the `design_handoff_neon_picker` spec (Claude Design):
original CSS gradients and text glyphs only — no images, no icon set, no Peloton branding.
Fonts are Baloo 2 and Nunito from Google Fonts.

The class library is a snapshot baked into the repo (`classes.json`), not a live feed —
Peloton's catalogue needs a member login, so a public site can't query it directly.
**It refreshes itself every Monday morning** (see below), and the footer shows the size
and date of the snapshot you're looking at.

Coverage is the point of the per-category page counts in `refresh.sh`: a query like
"Hilary Duff and Miley Cyrus" only finds the seven classes with both if the library is
deep enough to contain them. Raise the numbers there to go deeper, at the cost of a
bigger download — the file is currently about 1.7 MB over the wire.

## 2. The command-line tool — your account, your history

This is the personal version. It signs into *your* Peloton account, so it knows every
class you've already taken and never recommends one twice. It also learns your taste from
the playlists of classes you've done.

Python 3 standard library only. Nothing to install.

## Setup

1. Give it your Peloton login. It reads `PELOTON_USERNAME` / `PELOTON_PASSWORD`
   from the environment, or from a `.env` file in this folder:

   ```bash
   cp .env.example .env && open -e .env
   ```

   The credentials stay on your machine. They go to Peloton's own sign-in service
   (`auth.onepeloton.com`) and nowhere else. `.env` is gitignored; the bearer token is
   cached in `~/.peloton-picker/session.json` with owner-only permissions.

   Peloton retired its old password-login endpoint, so this signs in the way the website
   does: an Auth0 authorization-code flow with PKCE. Tokens last 48 hours and refresh
   themselves, so you'll rarely re-authenticate. If your account uses two-factor or a
   social login, this can't sign in for you — grab a bearer token from your browser's
   DevTools (Network tab, any `api.onepeloton.com` request, the `Authorization` header)
   and set `PELOTON_BEARER_TOKEN` instead. That one expires every 48 hours.

2. Pull your workout history, so it stops recommending classes you've done:

   ```bash
   python3 peloton_picker.py sync-history
   ```

3. Build a first draft of your music taste from classes you've already taken:

   ```bash
   python3 peloton_picker.py bootstrap-taste --top 60
   ```

   This writes `taste_seed.json` — the artists that appear most across your history,
   weighted toward cycling. It's a starting point, not a verdict: delete the ones you
   don't actually like, then paste the keepers into `liked_artists` in `prefs.json`.
   (Or run it with `--merge` to write them in directly and prune afterwards.)

   The song matching is only as good as this list, so it's worth ten minutes.

## Using it

**Double-click `Peloton Class Picker.command`.** A Terminal window opens and the
interface appears in your browser. Leave that window open while you use it; close it when
you're done. (There's a copy on the Desktop too.)

### A note on GitHub

This repo stores the code — it does not run it. If you turn on GitHub Pages, the page at
`vivian-killin.github.io/peloton-class-picker` is just this README rendered as a webpage,
not the app.

The picker can't run on GitHub Pages: Pages only serves static files and this is a Python
program, and more importantly the interface works by calling Peloton's API with *your*
login. That has to stay on your own machine. The UI exists only while the program is
running locally.

### Or start it from the command line

```bash
python3 peloton_picker.py serve
```

Either way, that opens a page at `http://127.0.0.1:8765` where you choose a class type, lengths,
optionally an instructor, and how many songs you want to like — then it hands back one
class you haven't taken, with the matching songs and a link to open it. "Not this one"
picks again. The page is served from your own machine only; nothing is exposed to the
network and your credentials never reach the browser. Leave the terminal open while you
use it, and press Ctrl-C to stop.

The first search of a given class type takes about a minute while it reads playlists;
after that they're cached and it's instant.

Or from the command line:

```bash
python3 peloton_picker.py pick                          # your default tracks
python3 peloton_picker.py pick -t cycling -n 5          # five rides
python3 peloton_picker.py pick -t strength core -m 20   # 20-min strength + core
python3 peloton_picker.py week                          # a full week, one class per day
```

Each result gives you the title, instructor, length, why it scored well, which of your
songs are in it, and a direct link to open the class.

Run `sync-history` again every couple of weeks so the no-repeats filter stays current.

### The website's class library refreshes itself

A macOS scheduled job runs `refresh.sh` **every Monday at 7am** (or at the next wake, if
the Mac is asleep). It re-exports the catalogue, and if anything actually changed it
commits and pushes — GitHub Pages then republishes within a couple of minutes. An
unchanged catalogue makes no commit, so the history stays meaningful.

Run it by hand any time:

```bash
cd ~/peloton-picker && ./refresh.sh
```

Logs are at `~/.peloton-picker/refresh.log`. The export contains only Peloton's public
class metadata — no history, no preferences, nothing about your account.

To change the schedule, edit `StartCalendarInterval` in
`~/Library/LaunchAgents/com.peloton-picker.refresh.plist` (`Weekday` 0–6, Sunday is 0),
then:

```bash
launchctl bootout gui/$(id -u)/com.peloton-picker.refresh && launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.peloton-picker.refresh.plist
```

To stop the automation entirely:

```bash
launchctl bootout gui/$(id -u)/com.peloton-picker.refresh && rm ~/Library/LaunchAgents/com.peloton-picker.refresh.plist
```

**Why the project lives at `~/peloton-picker`:** macOS blocks scheduled background jobs
from reading protected folders like `~/Documents`, so the weekly refresh could not run
from there. A shortcut at the old `~/Documents/Claude/Projects/peloton-picker` path still
works for opening the folder.

## Publishing it to GitHub

```bash
./setup-git.sh YOUR_GITHUB_USERNAME
```

Commits are authored as `YOUR_GITHUB_USERNAME@users.noreply.github.com` using a
repo-local git identity, so your real email never enters the history and your global git
config is left alone. `.env` (your Peloton login) and `taste_seed.json` are gitignored,
and the script refuses to run if that ever stops being true. Your workout history and
cached playlists live in `~/.peloton-picker/`, outside the repo entirely.

The one thing the repo does reveal is your taste: `prefs.json` holds your liked artists
and favourite instructors. Nothing identifies you by name. If you'd rather keep even that
private, make the GitHub repo private, or add `prefs.json` to `.gitignore` and commit a
copy with the personal lists emptied.

## Tuning it — `prefs.json`

Everything lives in `prefs.json`. The parts you'll actually touch:

- **`music.liked_artists`** — the whole basis of song matching. Add freely.
- **`music.liked_songs`** — exact titles, for one-off songs by artists you're lukewarm on.
- **`music.disliked_artists`** — each appearance subtracts from a class's score.
- **`music.genre_weights`** — `pop` is set highest. Matched against class titles and types.
- **`instructors`** — a name-to-bonus map. Rebecca Kennedy is at `3.0`. Add a negative
  number for an instructor you'd rather skip.
- **`tracks`** — one entry per kind of class, with its own durations and music weight:
  - `cycling` is set to 15/20/30 min, requires **4 liked songs**, and weights music
    heavily (`3.0`).
  - `strength` caps at 30 min; `stretching` is 5–10 min.
  - `strength` excludes cardio-flavoured classes via `exclude_type_keywords`
    (cardio, HIIT, plyo, tabata, dance, bootcamp, boxing).
  - `core`, `pilates`, `barre`, and `hikes` filter by class-type keyword.
  - `stretching` barely cares about music (`0.25`).
- **`week_plan`** — which track lands on which day for the `week` command.

Note on the 4-song bar: if too few classes clear it, the tool says so and relaxes it for
that run rather than handing you nothing. That usually means `liked_artists` is too short.

## Two things that may need adjusting

This uses Peloton's internal API, which isn't documented or supported, so two spots are
built to be repaired rather than to be permanently right:

- **Sign-in.** The Auth0 flow imitates a browser, so Peloton changing its login page can
  break it. Symptoms: "did not hand out a CSRF token" or "never reached the OAuth
  callback". `PELOTON_BEARER_TOKEN` is the manual way through.
- **Class-type names.** If `pilates`, `barre`, or `hikes` come back empty, the keywords
  in `tracks` may not match what Peloton calls them. Run
  `python3 peloton_picker.py categories` to see the real browse categories.
- **Playlist shape.** If everything reports `0/0 songs`, the `/details` response has
  moved. Run `python3 peloton_picker.py debug-details <ride_id>` (the `classId` from any
  class URL) to see the raw JSON, and adjust `extract_songs()`.

Playlists are cached in `~/.peloton-picker/cache.sqlite` for 120 days, so repeat runs are
fast — the first run on a new track fetches up to 90 class detail pages and takes a minute.
