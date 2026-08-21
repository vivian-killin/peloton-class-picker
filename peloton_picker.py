#!/usr/bin/env python3
"""
peloton_picker — pick Peloton classes worth taking, based on your preferences.

Standard library only. Talks to the unofficial Peloton API at api.onepeloton.com.

Credentials come from the environment (never from this file):
    PELOTON_USERNAME, PELOTON_PASSWORD
or from a .env file next to this script with those two keys.

Commands:
    pick              recommend classes (the main event)
    week              recommend a varied week of classes
    sync-history      pull your workout history (powers "no repeats")
    bootstrap-taste   seed your liked-artist list from artists in classes you've taken
    categories        list the browse categories the API actually accepts
    debug-details     dump one class's raw /details JSON (for adapting the parser)
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

API = "https://api.onepeloton.com"
HERE = Path(__file__).resolve().parent
PREFS_PATH = HERE / "prefs.json"
STATE_DIR = Path(os.environ.get("PELOTON_PICKER_HOME", Path.home() / ".peloton-picker"))
SESSION_PATH = STATE_DIR / "session.json"
DB_PATH = STATE_DIR / "cache.sqlite"
UA = "peloton-picker/1.0 (personal use)"


# --------------------------------------------------------------------------
# credentials + auth
#
# Peloton retired the old POST /auth/login endpoint (it now answers 403
# "Endpoint no longer accepting requests"). The members site authenticates
# through Auth0 with a PKCE authorization-code flow, so that is what we do:
# request an authorize URL, post the credentials to Auth0's login endpoint,
# ride the redirect chain to the ?code=, and exchange it for a bearer token.
# Access tokens last 48h and carry a refresh token, so a full login is rare.
# --------------------------------------------------------------------------

AUTH_DOMAIN = "auth.onepeloton.com"
AUTH_CLIENT_ID = "WVoJxVDdPoFx4RNewvvg6ch2mZ7bwnsM"
AUTH_AUDIENCE = "https://api.onepeloton.com/"
AUTH_SCOPE = "offline_access openid peloton-api.members:default"
AUTH_REDIRECT_URI = "https://members.onepeloton.com/callback"
AUTH0_CLIENT = "eyJuYW1lIjoiYXV0aDAuanMtdWxwIiwidmVyc2lvbiI6IjkuMTQuMyJ9"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def load_dotenv() -> None:
    env_file = HERE / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


class PelotonError(RuntimeError):
    pass


class _CodeFound(Exception):
    def __init__(self, code: str):
        self.code = code


class _CaptureRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects, but stops the moment the OAuth ?code= shows up."""

    def __init__(self):
        self.chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append(newurl)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(newurl).query)
        if "code" in params and newurl.startswith(AUTH_REDIRECT_URI):
            raise _CodeFound(params["code"][0])
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _FormParser(HTMLParser):
    """Auth0 replies with a self-submitting hidden form; pull out its fields."""

    def __init__(self):
        super().__init__()
        self.action: str | None = None
        self.fields: dict[str, str] = {}

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = d.get("action")
        elif tag == "input" and d.get("name"):
            self.fields[d["name"]] = d.get("value") or ""


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Client:
    def __init__(self, verbose: bool = False):
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.expires_at: float = 0.0
        self.user_id: str | None = None
        self.verbose = verbose
        self.jar = http.cookiejar.CookieJar()
        self._load_session()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}", file=sys.stderr)

    # -- stored session ---------------------------------------------------
    def _load_session(self) -> None:
        if not SESSION_PATH.exists():
            return
        try:
            d = json.loads(SESSION_PATH.read_text())
        except json.JSONDecodeError:
            return
        self.access_token = d.get("access_token")
        self.refresh_token = d.get("refresh_token")
        self.expires_at = d.get("expires_at", 0)
        self.user_id = d.get("user_id")

    def _save_session(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps({
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "user_id": self.user_id,
        }))
        os.chmod(SESSION_PATH, 0o600)

    def clear_session(self) -> None:
        self.access_token = self.refresh_token = None
        self.expires_at = 0
        if SESSION_PATH.exists():
            SESSION_PATH.unlink()

    # -- low-level http ---------------------------------------------------
    def _open(self, url: str, *, data: bytes | None = None, headers: dict | None = None,
              follow: bool = True, capture: bool = False):
        """Returns (status, headers, body_bytes, final_url)."""
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        redirector = _CaptureRedirects() if capture else None
        handlers.append(redirector if capture else
                        (urllib.request.HTTPRedirectHandler() if follow else _NoRedirect()))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=data, headers=headers or {},
                                     method="POST" if data is not None else "GET")
        try:
            with opener.open(req, timeout=45) as resp:
                return resp.status, resp.headers, resp.read(), resp.url
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read(), exc.url

    # -- the OAuth dance --------------------------------------------------
    def _full_login(self) -> None:
        email = os.environ.get("PELOTON_USERNAME")
        password = os.environ.get("PELOTON_PASSWORD")
        if not email or not password:
            raise PelotonError(
                "Set PELOTON_USERNAME and PELOTON_PASSWORD in the environment or in a .env "
                f"file at {HERE / '.env'} (see README.md)."
            )

        verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        state = _b64url(secrets.token_bytes(24))
        nonce = _b64url(secrets.token_bytes(24))

        # 1. hit /authorize and land on the hosted login page
        authorize = f"https://{AUTH_DOMAIN}/authorize?" + urllib.parse.urlencode({
            "client_id": AUTH_CLIENT_ID,
            "audience": AUTH_AUDIENCE,
            "scope": AUTH_SCOPE,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": AUTH_REDIRECT_URI,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "auth0Client": AUTH0_CLIENT,
        })
        self._log("GET /authorize")
        status, _, _, login_url = self._open(authorize, headers={"User-Agent": BROWSER_UA})
        if status >= 400:
            raise PelotonError(f"Peloton's authorize step failed ({status}).")

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(login_url).query)
        state = (qs.get("state") or [state])[0]

        csrf = next((c.value for c in self.jar
                     if c.name == "_csrf" and AUTH_DOMAIN in (c.domain or "")), None)
        if not csrf:
            raise PelotonError(
                "Peloton's login page did not hand out a CSRF token. The sign-in flow has "
                "probably changed again — see the troubleshooting notes in README.md."
            )

        # 2. post the credentials to Auth0
        payload = {
            "client_id": AUTH_CLIENT_ID,
            "redirect_uri": AUTH_REDIRECT_URI,
            "tenant": "peloton-prod",
            "response_type": "code",
            "scope": AUTH_SCOPE,
            "audience": AUTH_AUDIENCE,
            "_csrf": csrf,
            "state": state,
            "_intstate": "deprecated",
            "nonce": nonce,
            "username": email,
            "password": password,
            "connection": "pelo-user-password",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        self._log("POST /usernamepassword/login")
        status, hdrs, body, _ = self._open(
            f"https://{AUTH_DOMAIN}/usernamepassword/login",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Accept": "*/*",
                "Origin": f"https://{AUTH_DOMAIN}",
                "Referer": login_url,
                "Auth0-Client": AUTH0_CLIENT,
                "User-Agent": BROWSER_UA,
            },
            follow=False,
        )
        if status in (401, 403):
            raise PelotonError("Peloton rejected that email/password. Check .env — and if the "
                               "account uses two-factor or a social login, this tool can't sign "
                               "in for you (see README.md).")
        if status >= 400 and not hdrs.get("Location"):
            raise PelotonError(f"Login POST failed ({status}): "
                               f"{body.decode('utf-8', 'replace')[:300]}")

        # 3. Auth0 answers with a self-submitting form; submit it and ride the
        #    redirects until the callback hands back ?code=
        location = hdrs.get("Location")
        if location:
            code = self._chase_code(urllib.parse.urljoin(f"https://{AUTH_DOMAIN}", location))
        else:
            parser = _FormParser()
            parser.feed(body.decode("utf-8", "replace"))
            if not parser.action:
                raise PelotonError("Auth0 returned no continuation form; the flow has changed.")
            action = urllib.parse.urljoin(f"https://{AUTH_DOMAIN}", parser.action)
            self._log(f"POST {action}")
            code = self._chase_code(action, form=parser.fields)

        # 4. swap the code for a bearer token
        self._log("POST /oauth/token")
        status, _, body, _ = self._open(
            f"https://{AUTH_DOMAIN}/oauth/token",
            data=json.dumps({
                "grant_type": "authorization_code",
                "client_id": AUTH_CLIENT_ID,
                "code_verifier": verifier,
                "code": code,
                "redirect_uri": AUTH_REDIRECT_URI,
            }).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json",
                     "User-Agent": BROWSER_UA},
        )
        if status >= 400:
            raise PelotonError(f"Token exchange failed ({status}): "
                               f"{body.decode('utf-8', 'replace')[:300]}")
        self._store_token(json.loads(body))

    def _chase_code(self, url: str, form: dict | None = None) -> str:
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            status, hdrs, body, final_url = self._open(url, data=data, headers=headers,
                                                       capture=True)
        except _CodeFound as found:
            return found.code
        for candidate in (final_url, hdrs.get("Location") or ""):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(candidate).query)
            if "code" in params:
                return params["code"][0]
        raise PelotonError(f"Never reached the OAuth callback (status {status}): "
                           f"{body.decode('utf-8', 'replace')[:300]}")

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        self._log("refreshing bearer token")
        status, _, body, _ = self._open(
            f"https://{AUTH_DOMAIN}/oauth/token",
            data=urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "client_id": AUTH_CLIENT_ID,
                "refresh_token": self.refresh_token,
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                     "User-Agent": BROWSER_UA},
        )
        if status >= 400:
            return False
        self._store_token(json.loads(body))
        return True

    def _store_token(self, token: dict) -> None:
        self.access_token = token.get("access_token")
        if not self.access_token:
            raise PelotonError("Peloton returned no access token.")
        self.refresh_token = token.get("refresh_token") or self.refresh_token
        # refresh an hour early rather than discovering expiry mid-run
        self.expires_at = time.time() + int(token.get("expires_in", 172800)) - 3600
        self._save_session()

    def login(self) -> None:
        manual = os.environ.get("PELOTON_BEARER_TOKEN")
        if manual:
            self.access_token = manual
            self.expires_at = time.time() + 3600
            return
        if self.access_token and time.time() < self.expires_at:
            return
        if self._refresh():
            return
        self._full_login()

    # -- api requests -----------------------------------------------------
    def get(self, path: str, params: dict | None = None, _retry: bool = True):
        self.login()
        url = f"{API}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        self._log(f"GET {url}")
        status, _, body, _ = self._open(url, headers={
            "Authorization": f"Bearer {self.access_token}",
            "User-Agent": BROWSER_UA,
            "peloton-platform": "web",
            "Accept": "application/json",
        })
        if status in (401, 403) and _retry:
            self.clear_session()
            return self.get(path, params, _retry=False)
        if status == 429 and _retry:
            time.sleep(5)
            return self.get(path, params, _retry=False)
        if status >= 400:
            raise PelotonError(f"GET {path} failed ({status}): "
                               f"{body.decode('utf-8', 'replace')[:200]}")
        return json.loads(body)

    def me(self) -> dict:
        data = self.get("/api/me")
        self.user_id = data.get("id") or self.user_id
        self._save_session()
        return data


# --------------------------------------------------------------------------
# local cache
# --------------------------------------------------------------------------

def db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False so the web UI's request threads can share one handle;
    # every caller that does is serialised behind a lock.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS playlists (ride_id TEXT PRIMARY KEY, songs TEXT, fetched_at REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS history (ride_id TEXT PRIMARY KEY, title TEXT, "
                 "instructor TEXT, discipline TEXT, taken_at INTEGER)")
    conn.commit()
    return conn


def cached_songs(conn, ride_id: str, max_age_days: int = 120):
    row = conn.execute("SELECT songs, fetched_at FROM playlists WHERE ride_id = ?", (ride_id,)).fetchone()
    if not row:
        return None
    if time.time() - row[1] > max_age_days * 86400:
        return None
    return json.loads(row[0])


def store_songs(conn, ride_id: str, songs: list) -> None:
    conn.execute("INSERT OR REPLACE INTO playlists VALUES (?, ?, ?)",
                 (ride_id, json.dumps(songs), time.time()))


# --------------------------------------------------------------------------
# preferences
# --------------------------------------------------------------------------

@dataclass
class Prefs:
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path = PREFS_PATH) -> "Prefs":
        if not path.exists():
            raise PelotonError(f"No preferences file at {path}.")
        return cls(json.loads(path.read_text()))

    def save(self, path: Path = PREFS_PATH) -> None:
        path.write_text(json.dumps(self.raw, indent=2) + "\n")

    @property
    def tracks(self) -> dict:
        return self.raw["tracks"]

    @property
    def liked_artists(self) -> list[str]:
        return self.raw["music"]["liked_artists"]

    @property
    def liked_songs(self) -> list[str]:
        return self.raw["music"].get("liked_songs", [])

    @property
    def disliked_artists(self) -> list[str]:
        return self.raw["music"].get("disliked_artists", [])


def norm(s: str) -> str:
    """Loose normalisation so 'The Weeknd' == 'weeknd' == 'the  weeknd'."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^\s*the\s+", "", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------

@dataclass
class Klass:
    ride_id: str
    title: str
    instructor: str
    discipline: str
    duration_min: int
    class_types: list[str]
    difficulty: float
    rating: float
    total_workouts: int
    air_time: int
    explicit: bool
    track: str = ""
    liked_songs: list[str] = field(default_factory=list)
    n_songs: int = 0
    score: float = 0.0
    why: list[str] = field(default_factory=list)

    @property
    def url(self) -> str:
        return (f"https://members.onepeloton.com/classes/{self.discipline or 'cycling'}"
                f"?modal=classDetailsModal&classId={self.ride_id}")


def fetch_catalog(client: Client, browse_category: str, pages: int, sort_by: str,
                  per_page: int = 100) -> list[Klass]:
    out: list[Klass] = []
    instructors: dict[str, str] = {}
    types: dict[str, str] = {}
    for page in range(pages):
        data = client.get("/api/v2/ride/archived", {
            "browse_category": browse_category,
            "limit": per_page,
            "page": page,
            "sort_by": sort_by,
            "desc": "true",
            "content_format": "audio,video",
        })
        for ins in data.get("instructors", []):
            instructors[ins.get("id", "")] = ins.get("name") or ins.get("first_name", "")
        for ct in list(data.get("class_types", [])) + list(data.get("ride_types", [])):
            types[ct.get("id", "")] = ct.get("name", "")
        rides = data.get("data", [])
        if not rides:
            break
        for r in rides:
            ct_ids = list(r.get("class_type_ids") or []) + list(r.get("ride_type_ids") or [])
            if r.get("ride_type_id"):
                ct_ids.append(r["ride_type_id"])
            out.append(Klass(
                ride_id=r.get("id", ""),
                title=r.get("title", ""),
                instructor=instructors.get(r.get("instructor_id", ""), ""),
                discipline=r.get("fitness_discipline", "") or browse_category,
                duration_min=round((r.get("duration") or 0) / 60),
                class_types=list(dict.fromkeys(types[i] for i in ct_ids if types.get(i))),
                difficulty=float(r.get("difficulty_rating_avg") or 0),
                rating=float(r.get("overall_estimate") or 0),
                total_workouts=int(r.get("total_workouts") or 0),
                air_time=int(r.get("original_air_time") or 0),
                explicit=bool(r.get("is_explicit")),
            ))
        if page + 1 >= (data.get("page_count") or 1):
            break
    return out


def extract_songs(details: dict) -> list[dict]:
    """The /details payload has moved around over the years; try the known shapes."""
    playlist = details.get("playlist") or (details.get("ride") or {}).get("playlist") or {}
    raw = playlist.get("songs") or details.get("songs") or []
    songs = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        title = s.get("title") or s.get("name") or ""
        artists = []
        for a in (s.get("artists") or s.get("artist") or []):
            if isinstance(a, dict):
                artists.append(a.get("artist_name") or a.get("name") or "")
            elif isinstance(a, str):
                artists.append(a)
        songs.append({"title": title, "artists": [a for a in artists if a]})
    if not songs:
        for a in playlist.get("top_artists", []):
            name = a.get("artist_name") if isinstance(a, dict) else a
            if name:
                songs.append({"title": "", "artists": [name]})
    return songs


def fetch_playlists(client: Client, conn, ride_ids: list[str], workers: int = 5) -> dict[str, list]:
    result: dict[str, list] = {}
    missing = []
    for rid in ride_ids:
        hit = cached_songs(conn, rid)
        if hit is None:
            missing.append(rid)
        else:
            result[rid] = hit

    if missing:
        print(f"  fetching playlists for {len(missing)} classes "
              f"({len(result)} already cached)...", file=sys.stderr)

    def one(rid: str):
        try:
            return rid, extract_songs(client.get(f"/api/ride/{rid}/details"))
        except PelotonError:
            return rid, []

    if missing:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rid, songs in pool.map(one, missing):
                result[rid] = songs
                store_songs(conn, rid, songs)
        conn.commit()
    return result


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

def sync_history(client: Client, conn, limit_pages: int = 300) -> int:
    if not client.user_id:
        client.me()
    added = 0
    for page in range(limit_pages):
        data = client.get(f"/api/user/{client.user_id}/workouts", {
            "joins": "ride,ride.instructor", "limit": 100, "page": page,
        })
        workouts = data.get("data", [])
        if not workouts:
            break
        for w in workouts:
            ride = w.get("ride") or {}
            rid = ride.get("id")
            if not rid:
                continue
            instructor = (ride.get("instructor") or {}).get("name", "")
            conn.execute(
                "INSERT OR REPLACE INTO history VALUES (?, ?, ?, ?, ?)",
                (rid, ride.get("title", ""), instructor,
                 ride.get("fitness_discipline", ""), int(w.get("created_at") or 0)),
            )
            added += 1
        if page + 1 >= (data.get("page_count") or 1):
            break
    conn.commit()
    return added


def taken_ride_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT ride_id FROM history")}


def taken_titles(conn) -> set[tuple[str, str]]:
    return {(norm(t), norm(i)) for t, i in conn.execute("SELECT title, instructor FROM history")}


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

def count_liked_songs(songs: list[dict], liked_artists: set[str], liked_songs: set[str],
                      disliked: set[str]) -> tuple[list[str], int]:
    hits, penalty = [], 0
    for s in songs:
        artists = [norm(a) for a in s.get("artists", [])]
        title = norm(s.get("title", ""))
        if any(a in disliked for a in artists):
            penalty += 1
            continue
        if any(a in liked_artists for a in artists) or (title and title in liked_songs):
            # Peloton's data often repeats an artist under different casings
            names = list(dict.fromkeys(norm(a) or a for a in s.get("artists", [])))
            pretty = [next(a for a in s["artists"] if (norm(a) or a) == n) for n in names]
            hits.append(f"{s.get('title', '?')} — {', '.join(pretty) or '?'}")
    return hits, penalty


def matches_keywords(klass: Klass, keywords: list[str]) -> bool:
    haystack = norm(klass.title) + " " + " ".join(norm(t) for t in klass.class_types)
    return any(norm(k) in haystack for k in keywords)


def score_class(klass: Klass, track_cfg: dict, prefs: Prefs, songs: list[dict],
                liked_artists: set, liked_song_set: set, disliked: set) -> Klass:
    w = prefs.raw["weights"]
    hits, penalty = count_liked_songs(songs, liked_artists, liked_song_set, disliked)
    klass.liked_songs = hits
    klass.n_songs = len(songs)

    music_w = track_cfg.get("music_weight", w["music_default"])
    cap = w.get("liked_song_cap", 8)
    score = music_w * min(len(hits), cap)
    if hits:
        klass.why.append(f"{len(hits)} liked song{'s' if len(hits) != 1 else ''}")
    score -= w.get("disliked_song_penalty", 1.0) * penalty

    # instructors
    ins_scores = {norm(k): v for k, v in prefs.raw["instructors"].items()}
    bonus = ins_scores.get(norm(klass.instructor))
    if bonus:
        score += bonus
        klass.why.append(f"instructor {klass.instructor}")

    # genre keywords (Peloton puts the genre in the title / class type)
    for genre, weight in prefs.raw["music"]["genre_weights"].items():
        if matches_keywords(klass, [genre]):
            score += weight
            klass.why.append(genre.lower())
            break

    score += w.get("rating", 4.0) * klass.rating
    score += w.get("popularity", 0.4) * math.log10(max(klass.total_workouts, 1))

    diff_target = track_cfg.get("difficulty_target")
    if diff_target and klass.difficulty:
        score -= w.get("difficulty_miss", 0.6) * abs(klass.difficulty - diff_target)

    klass.score = score
    return klass


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

def candidates_for_track(client: Client, conn, prefs: Prefs, track: str, minutes: list[int],
                         pool_pages: int, verbose: bool,
                         min_liked_override: int | None = None,
                         instructor: str = "") -> list[Klass]:
    cfg = dict(prefs.tracks[track])
    if min_liked_override is not None:
        cfg["min_liked_songs"] = min_liked_override
    classes = fetch_catalog(client, cfg["browse_category"], pool_pages,
                            prefs.raw.get("catalog_sort", "top_rated"))
    for k in classes:
        k.track = track

    inc = cfg.get("include_type_keywords")
    exc = cfg.get("exclude_type_keywords", [])
    allowed_minutes = minutes or cfg.get("durations") or []

    kept = []
    for k in classes:
        if allowed_minutes and k.duration_min not in allowed_minutes:
            continue
        if inc and not matches_keywords(k, inc):
            continue
        if exc and matches_keywords(k, exc):
            continue
        if prefs.raw.get("skip_explicit") and k.explicit:
            continue
        # Filter by instructor here, before the shortlist is trimmed — otherwise the
        # trim throws away every class by that instructor and we come back empty.
        if instructor and norm(k.instructor) != norm(instructor):
            continue
        kept.append(k)

    if prefs.raw.get("avoid_repeats", True):
        taken = taken_ride_ids(conn)
        titles = taken_titles(conn)
        kept = [k for k in kept
                if k.ride_id not in taken and (norm(k.title), norm(k.instructor)) not in titles]

    # cheap pre-rank so we only pay for playlists on plausible classes
    kept.sort(key=lambda k: (k.rating, k.total_workouts), reverse=True)
    depth = cfg.get("playlist_depth", prefs.raw.get("playlist_depth", 60))
    shortlist = kept[:depth]

    playlists = fetch_playlists(client, conn, [k.ride_id for k in shortlist])
    liked_artists = {norm(a) for a in prefs.liked_artists}
    liked_song_set = {norm(s) for s in prefs.liked_songs}
    disliked = {norm(a) for a in prefs.disliked_artists}

    scored = [score_class(k, cfg, prefs, playlists.get(k.ride_id, []),
                          liked_artists, liked_song_set, disliked) for k in shortlist]

    min_liked = cfg.get("min_liked_songs", 0)
    strict = [k for k in scored if len(k.liked_songs) >= min_liked]
    if min_liked and len(strict) < 3:
        print(f"  note: only {len(strict)} {track} class(es) cleared the "
              f"{min_liked}-liked-song bar — relaxing it for this run. "
              f"Add artists to prefs.json to tighten it back up.", file=sys.stderr)
        strict = scored
    strict.sort(key=lambda k: k.score, reverse=True)
    return strict


def allocate(total: int, buckets: int) -> list[int]:
    """Spread `total` picks across `buckets` tracks as evenly as possible."""
    if buckets <= 0:
        return []
    base, extra = divmod(total, buckets)
    return [base + (1 if i < extra else 0) for i in range(buckets)]


def diversify(pool: list[Klass], count: int, prefs: Prefs) -> list[Klass]:
    """Greedy pick that penalises repeating an instructor or a class type."""
    pen_ins = prefs.raw["weights"].get("repeat_instructor_penalty", 2.5)
    pen_type = prefs.raw["weights"].get("repeat_type_penalty", 1.2)
    pen_track = prefs.raw["weights"].get("repeat_track_penalty", 3.0)
    chosen: list[Klass] = []
    remaining = list(pool)
    while remaining and len(chosen) < count:
        seen_ins = {norm(k.instructor) for k in chosen}
        seen_types = {norm(t) for k in chosen for t in k.class_types}
        track_counts: dict[str, int] = {}
        for k in chosen:
            track_counts[k.track] = track_counts.get(k.track, 0) + 1
        best, best_val = None, -1e9
        for k in remaining:
            val = k.score
            if norm(k.instructor) in seen_ins:
                val -= pen_ins
            if any(norm(t) in seen_types for t in k.class_types):
                val -= pen_type
            val -= pen_track * track_counts.get(k.track, 0)
            if val > best_val:
                best, best_val = k, val
        chosen.append(best)
        remaining.remove(best)
    return chosen


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def render(classes: list[Klass], show_songs: bool) -> None:
    if not classes:
        print("No classes matched. Loosen the filters in prefs.json or widen --minutes.")
        return
    for i, k in enumerate(classes, 1):
        aired = datetime.fromtimestamp(k.air_time, timezone.utc).strftime("%b %Y") if k.air_time else "?"
        print(f"\n{i}. {k.title}")
        print(f"   {k.instructor or '?'} · {k.duration_min} min · {k.track} · aired {aired}")
        bits = []
        if k.class_types:
            bits.append("/".join(k.class_types[:3]))
        if k.rating:
            bits.append(f"{k.rating * 100:.0f}% liked")
        if k.difficulty:
            bits.append(f"difficulty {k.difficulty:.1f}")
        if bits:
            print(f"   {' · '.join(bits)}")
        print(f"   score {k.score:.1f} — {', '.join(k.why) if k.why else 'baseline match'}"
              f" ({len(k.liked_songs)}/{k.n_songs} songs you like)")
        if show_songs and k.liked_songs:
            for s in k.liked_songs[:6]:
                print(f"     ♫ {s}")
        print(f"   {k.url}")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_pick(args) -> int:
    prefs = Prefs.load()
    client = Client(verbose=args.verbose)
    conn = db()
    tracks = args.tracks or prefs.raw.get("default_tracks", list(prefs.tracks))
    minutes = [int(m) for m in args.minutes.split(",")] if args.minutes else []

    for track in tracks:
        if track not in prefs.tracks:
            print(f"Unknown track '{track}'. Known: {', '.join(prefs.tracks)}", file=sys.stderr)
            return 2

    # Slots are allocated per track rather than by one global ranking: music weights
    # differ per track by design (a ride lives or dies on its playlist, a stretch does
    # not), so scores are only comparable within a track.
    quota = allocate(args.count, len(tracks))
    picks: list[Klass] = []
    for track, n in zip(tracks, quota):
        if n == 0:
            continue
        print(f"Scanning {track}...", file=sys.stderr)
        pool = candidates_for_track(client, conn, prefs, track, minutes,
                                    args.pages, args.verbose)
        picks.extend(diversify(pool, n, prefs))
    picks.sort(key=lambda k: (k.track, -k.score))
    if args.json:
        print(json.dumps([{**k.__dict__, "url": k.url} for k in picks], indent=2))
    else:
        render(picks, show_songs=not args.no_songs)
    return 0


def cmd_week(args) -> int:
    prefs = Prefs.load()
    client = Client(verbose=args.verbose)
    conn = db()
    plan = prefs.raw.get("week_plan", [])
    if not plan:
        print("No week_plan in prefs.json.", file=sys.stderr)
        return 2

    by_track: dict[str, list[Klass]] = {}
    for slot in plan:
        track = slot["track"]
        if track not in by_track:
            print(f"Scanning {track}...", file=sys.stderr)
            mins = [int(m) for m in str(slot.get("minutes", "")).split(",") if m]
            by_track[track] = candidates_for_track(client, conn, prefs, track, mins,
                                                   args.pages, args.verbose)
    used: set[str] = set()
    print("\n=== Your week ===")
    for slot in plan:
        pool = [k for k in by_track[slot["track"]] if k.ride_id not in used]
        if slot.get("minutes"):
            wanted = [int(m) for m in str(slot["minutes"]).split(",")]
            filtered = [k for k in pool if k.duration_min in wanted]
            pool = filtered or pool
        pick = diversify(pool, 1, prefs)
        print(f"\n--- {slot['day']} · {slot['track']} ---")
        if pick:
            used.add(pick[0].ride_id)
            render(pick, show_songs=not args.no_songs)
        else:
            print("  nothing matched this slot.")
    return 0


def cmd_sync(args) -> int:
    client = Client(verbose=args.verbose)
    conn = db()
    n = sync_history(client, conn)
    total = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    print(f"Synced {n} workout records. {total} unique classes marked as already taken.")
    return 0


def cmd_bootstrap(args) -> int:
    prefs = Prefs.load()
    client = Client(verbose=args.verbose)
    conn = db()
    if not taken_ride_ids(conn):
        print("No history yet — running sync-history first.", file=sys.stderr)
        sync_history(client, conn)

    rows = conn.execute("SELECT ride_id, discipline FROM history ORDER BY taken_at DESC "
                        "LIMIT ?", (args.limit,)).fetchall()
    playlists = fetch_playlists(client, conn, [r[0] for r in rows])
    weight_by_ride = {r[0]: (2.0 if r[1] == "cycling" else 1.0) for r in rows}

    tally: dict[str, float] = {}
    display: dict[str, str] = {}
    for rid, songs in playlists.items():
        for s in songs:
            for a in s.get("artists", []):
                key = norm(a)
                if not key:
                    continue
                tally[key] = tally.get(key, 0) + weight_by_ride.get(rid, 1.0)
                display.setdefault(key, a)

    top = sorted(tally.items(), key=lambda kv: kv[1], reverse=True)[:args.top]
    names = [display[k] for k, _ in top]
    out = HERE / "taste_seed.json"
    out.write_text(json.dumps({"suggested_liked_artists": names,
                               "counts": {display[k]: v for k, v in top}}, indent=2) + "\n")
    print(f"Wrote {len(names)} candidate artists to {out}")
    print("These are the artists that show up most across classes you've taken — a starting")
    print("point, not a verdict. Edit the list, then run with --merge to add them to prefs.json.")
    if args.merge:
        existing = {norm(a) for a in prefs.liked_artists}
        added = [n for n in names if norm(n) not in existing]
        prefs.raw["music"]["liked_artists"] = prefs.liked_artists + added
        prefs.save()
        print(f"Merged {len(added)} new artists into prefs.json.")
    return 0


def cmd_categories(args) -> int:
    client = Client(verbose=args.verbose)
    data = client.get("/api/browse_categories")
    for c in data.get("browse_categories", []):
        print(f"{c.get('slug', '?'):<24} {c.get('name', '')}")
    return 0


def cmd_debug(args) -> int:
    client = Client(verbose=args.verbose)
    details = client.get(f"/api/ride/{args.ride_id}/details")
    print(json.dumps(details, indent=2)[:args.chars])
    print(f"\n--- parsed songs ---")
    print(json.dumps(extract_songs(details), indent=2))
    return 0


def cmd_serve(args) -> int:
    """Local web UI: choose preferences in the browser, get one unseen class back."""
    import web

    prefs = Prefs.load()
    client = Client(verbose=args.verbose)
    conn = db()
    pools: dict[tuple, list[Klass]] = {}
    lock = threading.Lock()

    def instructor_names() -> list[str]:
        try:
            data = client.get("/api/instructor", {"limit": 100, "page": 0})
            names = sorted({i.get("name", "").strip() for i in data.get("data", [])
                            if i.get("name")})
            if names:
                return names
        except PelotonError:
            pass
        return sorted(prefs.raw["instructors"])

    def config_fn() -> dict:
        return {
            "tracks": {
                name: {
                    "durations": cfg.get("durations", []),
                    "all_durations": web.ALL_DURATIONS,
                    "min_liked_songs": cfg.get("min_liked_songs", 0),
                }
                for name, cfg in prefs.tracks.items()
            },
            "instructors": instructor_names(),
            "default_track": (prefs.raw.get("default_tracks") or ["cycling"])[0],
        }

    def pick_fn(body: dict) -> dict:
        with lock:  # one catalogue scan at a time; also guards the shared db handle
            return _pick(body)

    def _pick(body: dict) -> dict:
        track = body.get("track") or "cycling"
        if track not in prefs.tracks:
            return {"error": f"Unknown class type: {track}"}
        minutes = [int(m) for m in body.get("minutes") or []]
        min_liked = int(body.get("min_liked_songs") or 0)
        exclude = set(body.get("exclude") or [])
        instructor = (body.get("instructor") or "").strip()

        key = (track, tuple(sorted(minutes)), min_liked, instructor)
        if key not in pools:
            # a named instructor needs a wider sweep to find enough of their classes
            pages = args.pages + 3 if instructor else args.pages
            pools[key] = candidates_for_track(client, conn, prefs, track, minutes,
                                              args.pages if not instructor else pages,
                                              args.verbose,
                                              min_liked_override=min_liked,
                                              instructor=instructor)
        pool = [k for k in pools[key] if k.ride_id not in exclude]
        if not pool:
            return {"error": "Nothing left that matches. Try another length, a different "
                             "instructor, or fewer required songs."}

        # Favour the best matches without always handing back the same class.
        top = pool[:12]
        chosen = random.choices(top, weights=[len(top) - i for i in range(len(top))])[0]
        return {
            "ride_id": chosen.ride_id,
            "title": chosen.title,
            "instructor": chosen.instructor,
            "duration_min": chosen.duration_min,
            "aired": (datetime.fromtimestamp(chosen.air_time, timezone.utc).strftime("%b %Y")
                      if chosen.air_time else "?"),
            "rating": chosen.rating,
            "difficulty": chosen.difficulty,
            "liked_songs": chosen.liked_songs,
            "url": chosen.url,
        }

    web.serve(pick_fn, config_fn, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_export(args) -> int:
    """Build the static dataset the public web app runs on.

    This is Peloton's public class catalogue — titles, instructors, lengths, playlists.
    Nothing personal goes in: no workout history, no preferences, no account details.
    """
    prefs = Prefs.load()
    client = Client(verbose=args.verbose)
    conn = db()

    categories = args.categories or sorted(
        {cfg["browse_category"] for cfg in prefs.tracks.values()})

    seen: dict[str, Klass] = {}
    browse: dict[str, str] = {}  # Peloton's own nav category — pilates and hiking are
    for cat in categories:       # their own, but report a discipline of strength/walking
        print(f"Fetching {cat}...", file=sys.stderr)
        for k in fetch_catalog(client, cat, args.pages, "top_rated"):
            if k.ride_id and k.ride_id not in seen:
                seen[k.ride_id] = k
                browse[k.ride_id] = cat
    print(f"{len(seen)} unique classes.", file=sys.stderr)

    playlists = fetch_playlists(client, conn, list(seen))

    artist_index: dict[str, int] = {}

    def artist_id(name: str) -> int:
        if name not in artist_index:
            artist_index[name] = len(artist_index)
        return artist_index[name]

    classes = []
    for rid, k in seen.items():
        songs = []
        for song in playlists.get(rid, []):
            # Peloton repeats artists under different casings ("JAY-Z" / "Jay-Z")
            by_norm: dict[str, str] = {}
            for a in song.get("artists", []):
                by_norm.setdefault(norm(a) or a, a)
            names = list(by_norm.values())
            if not (song.get("title") or names):
                continue
            songs.append([song.get("title", ""), [artist_id(n) for n in names]])
        classes.append({
            "i": rid,
            "t": k.title,
            "n": k.instructor,
            "d": k.duration_min,
            "f": k.discipline,
            "b": browse.get(rid, k.discipline),
            "c": k.class_types,
            "r": round(k.rating, 3),
            "x": round(k.difficulty, 1),
            "a": (datetime.fromtimestamp(k.air_time, timezone.utc).strftime("%Y-%m")
                  if k.air_time else ""),
            "s": songs,
        })

    classes.sort(key=lambda c: (-c["r"], c["t"]))
    payload = {
        "generated": args.generated or "",
        "artists": [name for name, _ in sorted(artist_index.items(), key=lambda kv: kv[1])],
        "classes": classes,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    if args.only_if_changed and out.exists():
        try:
            old = json.loads(out.read_text())
        except json.JSONDecodeError:
            old = {}
        # ignore the date stamp, or the weekly job would commit an identical file
        if (old.get("classes"), old.get("artists")) == (payload["classes"], payload["artists"]):
            print(f"{out} is already up to date — nothing written.")
            return 3

    out.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    kb = out.stat().st_size / 1024
    with_songs = sum(1 for c in classes if c["s"])
    print(f"Wrote {out} — {len(classes)} classes ({with_songs} with playlists), "
          f"{len(artist_index)} artists, {kb:,.0f} KB")
    return 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("--pages", type=int, default=3,
                        help="catalogue pages to scan per track (100 classes each)")
        sp.add_argument("--no-songs", action="store_true", help="hide the matched song list")

    sp = sub.add_parser("pick", help="recommend classes")
    sp.add_argument("-t", "--tracks", nargs="*", help="e.g. cycling strength core")
    sp.add_argument("-m", "--minutes", help="comma-separated durations, e.g. 20,30")
    sp.add_argument("-n", "--count", type=int, default=5)
    sp.add_argument("--json", action="store_true")
    add_common(sp)
    sp.set_defaults(func=cmd_pick)

    sp = sub.add_parser("week", help="recommend a varied week from prefs.json week_plan")
    add_common(sp)
    sp.set_defaults(func=cmd_week)

    sp = sub.add_parser("sync-history", help="pull workout history so repeats get filtered")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("bootstrap-taste", help="seed liked artists from your history")
    sp.add_argument("--limit", type=int, default=250, help="how many past classes to mine")
    sp.add_argument("--top", type=int, default=60)
    sp.add_argument("--merge", action="store_true", help="write results straight into prefs.json")
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser("serve", help="open the browser interface")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--pages", type=int, default=3)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("export", help="build the static dataset for the public web app")
    sp.add_argument("--out", default="docs/classes.json")
    sp.add_argument("--pages", type=int, default=2, help="pages per category (100 each)")
    sp.add_argument("--categories", nargs="*")
    sp.add_argument("--generated", default="", help="date stamp to embed")
    sp.add_argument("--only-if-changed", action="store_true",
                    help="exit 3 without writing if the catalogue hasn't changed")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("categories", help="list valid browse categories")
    sp.set_defaults(func=cmd_categories)

    sp = sub.add_parser("debug-details", help="dump raw class details JSON")
    sp.add_argument("ride_id")
    sp.add_argument("--chars", type=int, default=4000)
    sp.set_defaults(func=cmd_debug)

    args = p.parse_args()
    try:
        return args.func(args)
    except PelotonError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
