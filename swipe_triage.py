"""
Swpify — Swipe your Spotify Liked Songs (Streamlit + Spotipy)

What it does
------------
• Swipe through Liked Songs with KEEP / REMOVE / SKIP / KEEP→Keepers / ⭐ FAVOURITE.
• Undo last action, hotkeys, filters, shuffle, progress & ETA, CSV export.
• Robust Spotify rate-limit backoff.
• Works with Streamlit Cloud secrets or local .env.

Streamlit Secrets (Cloud)
-------------------------
SPOTIPY_CLIENT_ID = "..."
SPOTIPY_CLIENT_SECRET = "..."
SPOTIPY_REDIRECT_URI = "https://<your-app>.streamlit.app/callback"

Local .env (optional for local runs)
------------------------------------
SPOTIPY_CLIENT_ID=...
SPOTIPY_CLIENT_SECRET=...
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8501/callback
"""

import json
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import streamlit as st
import streamlit.components.v1 as components
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth
from spotipy.exceptions import SpotifyException

# ---------- Branding / Config ----------
STATE_FILE = Path("swpify_state.json")
DEFAULT_KEEPERS_PLAYLIST_NAME = "💚 Keepers (Swpify)"
DEFAULT_FAVOURITES_PLAYLIST_NAME = "⭐ Favourites (Swpify)"
PAGE_SIZE = 50
SCOPES = [
    "user-library-read",
    "user-library-modify",
    "playlist-modify-private",
]

# ---------- Env / Auth ----------
@st.cache_data(show_spinner=False)
def load_env() -> Tuple[str, str, str]:
    if "SPOTIPY_CLIENT_ID" in st.secrets:
        cid = st.secrets["SPOTIPY_CLIENT_ID"]
        secret = st.secrets["SPOTIPY_CLIENT_SECRET"]
        redirect = st.secrets["SPOTIPY_REDIRECT_URI"]
    else:
        from dotenv import load_dotenv  # local only
        load_dotenv()
        cid = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
        secret = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()
        redirect = os.getenv("SPOTIPY_REDIRECT_URI", "").strip()
    return cid, secret, redirect

@st.cache_resource(show_spinner=False)
def get_spotify_client() -> Spotify:
    cid, secret, redirect = load_env()
    if not (cid and secret and redirect):
        st.stop()
    auth = SpotifyOAuth(
        client_id=cid,
        client_secret=secret,
        redirect_uri=redirect,
        scope=" ".join(SCOPES),
        cache_path=str(Path(".cache_spotify_swpify")),
        show_dialog=False,
    )
    return Spotify(auth_manager=auth)

# ---------- State ----------
def now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"

def load_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {
        "queue": [],
        "seen": {},
        "keepers_playlist_id": None,
        "keepers_playlist_name": DEFAULT_KEEPERS_PLAYLIST_NAME,
        "favourites_playlist_id": None,
        "favourites_playlist_name": DEFAULT_FAVOURITES_PLAYLIST_NAME,
        "last_action": None,
        "queue_built_total": 0,
        "session_start": 0.0,
        "theme_dark": False,
        "swiped_today": 0,
        "swiped_day": datetime.utcnow().strftime("%Y-%m-%d"),
    }

def save_state(state: Dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ---------- Backoff wrapper ----------
def sp_call(fn: Callable[[], Any], *, max_tries: int = 5) -> Any:
    delay = 1.0
    for _ in range(max_tries - 1):
        try:
            return fn()
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = 0
                try:
                    retry_after = int(getattr(e, "headers", {}).get("Retry-After", "0"))
                except Exception:
                    pass
                time.sleep(retry_after if retry_after > 0 else delay)
            elif e.http_status in (500, 502, 503, 504):
                time.sleep(delay)
            else:
                raise
        except Exception:
            time.sleep(delay)
        delay = min(delay * 2, 16)
    return fn()  # final attempt

# ---------- Spotify helpers ----------
def ensure_playlist(sp: Spotify, name_key: str, id_key: str, name_val: str) -> str:
    state = load_state()
    if state.get(name_key) != name_val:
        state[id_key] = None
        state[name_key] = name_val
        save_state(state)

    if state.get(id_key):
        return state[id_key]

    me = sp_call(lambda: sp.current_user())["id"]
    results = sp_call(lambda: sp.current_user_playlists(limit=50))
    while results:
        for pl in results["items"]:
            if pl["name"] == name_val and pl["owner"]["id"] == me:
                state[id_key] = pl["id"]
                save_state(state)
                return pl["id"]
        results = sp_call(lambda: sp.next(results)) if results.get("next") else None

    created = sp_call(lambda: sp.user_playlist_create(me, name_val, public=False,
                                                      description="Saved via Swpify"))
    state[id_key] = created["id"]
    save_state(state)
    return created["id"]

def fetch_all_liked_ids(sp: Spotify) -> List[str]:
    ids: List[str] = []
    offset = 0
    while True:
        batch = sp_call(lambda: sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset))
        items = batch.get("items", [])
        if not items:
            break
        ids.extend(t["track"]["id"] for t in items if t.get("track") and t["track"].get("id"))
        offset += len(items)
        if offset >= batch.get("total", 0):
            break
    return ids

def refill_queue(sp: Spotify, *, shuffle: bool, filters: Dict) -> None:
    state = load_state()
    liked_ids = fetch_all_liked_ids(sp)
    liked_ids = [tid for tid in liked_ids if tid not in state["seen"]]

    if any(filters.values()):
        filtered: List[str] = []
        for i in range(0, len(liked_ids), 50):
            chunk = liked_ids[i : i + 50]
            tracks = sp_call(lambda: sp.tracks(chunk))["tracks"]
            for tr in tracks:
                if tr is None:
                    continue
                name = (tr.get("name") or "").lower()
                artists_names = ", ".join(a["name"] for a in tr.get("artists", []))
                album = (tr.get("album", {}).get("name") or "")
                year = (tr.get("album", {}).get("release_date") or "")[:4]
                ok = True
                if filters.get("term"):
                    term = filters["term"].lower()
                    ok &= (term in name) or (term in artists_names.lower()) or (term in album.lower())
                if filters.get("artist"):
                    ok &= filters["artist"].lower() in artists_names.lower()
                if filters.get("year"):
                    ok &= year == str(filters["year"]).strip()
                if ok:
                    filtered.append(tr["id"])
        liked_ids = filtered

    if shuffle:
        random.shuffle(liked_ids)

    state["queue"] = liked_ids
    state["queue_built_total"] = len(liked_ids) + len(state["seen"])
    state["session_start"] = time.time()
    save_state(state)

def get_track(sp: Spotify, tid: str) -> Optional[dict]:
    try:
        return sp_call(lambda: sp.track(tid))
    except Exception:
        return None

# ---------- UI helpers ----------
def render_track_card(tr: dict) -> None:
    name = tr.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in tr.get("artists", []))
    album = tr.get("album", {}).get("name", "")
    images = tr.get("album", {}).get("images", [])
    art_url = images[0]["url"] if images else None
    preview = tr.get("preview_url")

    col1, col2 = st.columns([1, 2], gap="large")
    with col1:
        if art_url:
            st.image(art_url, use_container_width=True)
        else:
            st.write("(no artwork)")
    with col2:
        st.markdown(f"### {name}\n**{artists}**\n\n_{album}_")
        if preview:
            st.audio(preview)
        else:
            st.caption("No 30s preview available for this track.")
        dur_ms = tr.get("duration_ms") or 0
        mins = dur_ms // 60000
        secs = (dur_ms % 60000) // 1000
        st.caption(f"Duration: {mins}:{secs:02d} • Popularity: {tr.get('popularity', '–')}")
        st.link_button("Open in Spotify", tr.get("external_urls", {}).get("spotify", "#"))

def action_remove(sp: Spotify, tid: str):                   sp_call(lambda: sp.current_user_saved_tracks_delete([tid]))
def action_readd(sp: Spotify, tid: str):                    sp_call(lambda: sp.current_user_saved_tracks_add([tid]))
def action_add_to_playlist(sp: Spotify, tid: str, pid: str): sp_call(lambda: sp.playlist_add_items(pid, [tid]))
def action_remove_from_playlist(sp: Spotify, tid: str, pid: str):
    sp_call(lambda: sp.playlist_remove_all_occurrences_of_items(pid, [tid]))

def fast_head_count(sp: Spotify) -> int:
    batch = sp_call(lambda: sp.current_user_saved_tracks(limit=1))
    return batch.get("total", 0)

# ---------- App ----------
st.set_page_config(page_title="Swpify — Spotify Liked Songs", page_icon="💚", layout="centered")
st.title("Swpify — Spotify Liked Songs")
st.caption("Swipe to keep, remove, or file songs to Keepers / Favourites. Built with Streamlit + Spotipy.")

cid, secret, redirect = load_env()
if not (cid and secret and redirect):
    st.error("Missing Spotify credentials. Add SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI via Streamlit secrets.")
    st.stop()

sp = get_spotify_client()
state = load_state()

# Theme toggle + daily stat
with st.sidebar:
    dark = st.checkbox("🌓 Dark theme", value=state.get("theme_dark", False))
    if dark != state.get("theme_dark"):
        state["theme_dark"] = dark
        save_state(state)
        st.rerun()

    st.markdown("---")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("swiped_day") != today:
        state["swiped_day"] = today
        state["swiped_today"] = 0
        save_state(state)
    st.metric("Swiped today", state.get("swiped_today", 0))

if state.get("theme_dark"):
    st.markdown("""
        <style>
        html, body, [data-testid="stAppViewContainer"] { background:#0e1117 !important; color:#e5e7eb !important; }
        .stButton>button, .stDownloadButton>button { background:#1f2937 !important; color:#e5e7eb !important; border:1px solid #334155 !important; }
        .stTextInput>div>div>input, .stTextArea textarea { background:#111827 !important; color:#e5e7eb !important; }
        .stExpander { background:#0b1220 !important; }
        audio { width: 100%; }
        </style>
    """, unsafe_allow_html=True)

# Hotkeys
components.html(
    """
    <script>
      const map = { 'k':'keep','r':'remove','p':'keepers','f':'favourite','s':'skip','u':'undo','?':'help' };
      document.addEventListener('keydown', (e) => {
        const a = map[e.key.toLowerCase()];
        if (!a) return;
        const t = document.activeElement?.tagName;
        if (t === 'INPUT' || t === 'TEXTAREA') return;
        const url = new URL(window.location);
        url.searchParams.set('hotkey', a);
        url.searchParams.set('_', Date.now());
        window.location = url;
      });
    </script>
    """,
    height=0,
)
params = st.query_params
hotkey_action = params.get("hotkey")
if hotkey_action:
    st.query_params.clear()

# Options
with st.expander("⚙️ Options", expanded=False):
    shuffle = st.checkbox("Shuffle order", value=True)
    colf1, colf2, colf3 = st.columns(3)
    with colf1:
        term = st.text_input("Filter by text (title/artist/album)")
    with colf2:
        artist = st.text_input("Filter by artist")
    with colf3:
        year = st.text_input("Filter by year (YYYY)")

    keepers_name = st.text_input("Keepers playlist name",
                                 value=state.get("keepers_playlist_name", DEFAULT_KEEPERS_PLAYLIST_NAME))
    if keepers_name != state.get("keepers_playlist_name"):
        state["keepers_playlist_name"] = keepers_name
        state["keepers_playlist_id"] = None
        save_state(state)

    favourites_name = st.text_input("Favourites playlist name",
                                    value=state.get("favourites_playlist_name", DEFAULT_FAVOURITES_PLAYLIST_NAME))
    if favourites_name != state.get("favourites_playlist_name"):
        state["favourites_playlist_name"] = favourites_name
        state["favourites_playlist_id"] = None
        save_state(state)

    if st.button("Build/Refresh Queue"):
        refill_queue(sp, shuffle=shuffle, filters={"term": term, "artist": artist, "year": year})
        st.success("Queue built from your current Liked Songs.")

if not state.get("queue"):
    st.info("No queue yet — click **Build/Refresh Queue** above to begin.")
    try:
        total_est = fast_head_count(sp)
        st.caption(f"You currently have approximately **{total_est}** Liked Songs.")
    except Exception:
        pass
    st.stop()

# Current track
current_id = state["queue"][0]
track = get_track(sp, current_id)
if not track:
    st.warning("This track could not be loaded. Skipping.")
    state["queue"].pop(0)
    save_state(state)
    st.rerun()

render_track_card(track)
curr_artists = ", ".join(a["name"] for a in track.get("artists", []))
if st.button(f"🎯 Filter queue to artist: {curr_artists}"):
    refill_queue(sp, shuffle=True, filters={"term": "", "artist": curr_artists, "year": ""})
    st.rerun()

# Action buttons
colk, cold, colp, colf, cols = st.columns(5)
keep_clicked      = colk.button("✅ KEEP", use_container_width=True)
remove_clicked    = cold.button("🗑️ REMOVE (unlike)", type="primary", use_container_width=True)
keepers_clicked   = colp.button("📁 KEEP → Keepers", use_container_width=True)
favourite_clicked = colf.button("⭐ FAVOURITE → Favourites", use_container_width=True)
skip_clicked      = cols.button("⏭️ SKIP", use_container_width=True)

# Hotkeys
if   hotkey_action == "keep":      keep_clicked = True
elif hotkey_action == "remove":    remove_clicked = True
elif hotkey_action == "keepers":   keepers_clicked = True
elif hotkey_action == "favourite": favourite_clicked = True
elif hotkey_action == "skip":      skip_clicked = True

def record_action(a: str):
    seen = state.get("seen", {})
    seen[current_id] = {"action": a, "ts": now_iso()}
    state["seen"] = seen
    state["last_action"] = {"track_id": current_id, "action": a}
    # daily metric
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if state.get("swiped_day") != today:
        state["swiped_day"] = today
        state["swiped_today"] = 0
    state["swiped_today"] = state.get("swiped_today", 0) + 1

# Actions
if keep_clicked:
    record_action("keep")
    state["queue"].pop(0)
    save_state(state)
    st.rerun()

if remove_clicked:
    try:
        action_remove(sp, current_id)
        record_action("remove")
        state["queue"].pop(0)
        save_state(state)
        st.rerun()
    except Exception as e:
        st.error(f"Failed to remove from Liked Songs: {e}")

if keepers_clicked:
    try:
        pl_id = ensure_playlist(sp, "keepers_playlist_name", "keepers_playlist_id",
                                state.get("keepers_playlist_name", DEFAULT_KEEPERS_PLAYLIST_NAME))
        action_add_to_playlist(sp, current_id, pl_id)
        record_action("keepers")
        state["queue"].pop(0)
        save_state(state)
        st.rerun()
    except Exception as e:
        st.error(f"Failed to add to Keepers: {e}")

if favourite_clicked:
    try:
        pl_id = ensure_playlist(sp, "favourites_playlist_name", "favourites_playlist_id",
                                state.get("favourites_playlist_name", DEFAULT_FAVOURITES_PLAYLIST_NAME))
        action_add_to_playlist(sp, current_id, pl_id)
        record_action("favourite")
        state["queue"].pop(0)
        save_state(state)
        st.rerun()
    except Exception as e:
        st.error(f"Failed to add to Favourites: {e}")

if skip_clicked:
    record_action("skip")
    state["queue"].append(state["queue"].pop(0))
    save_state(state)
    st.rerun()

# Undo
st.divider()
def undo_last_action(sp: Spotify, state: Dict):
    last = state.get("last_action")
    if not last:
        st.warning("No recent action to undo.")
        return
    tid = last["track_id"]; action = last["action"]
    try:
        if action == "remove":
            action_readd(sp, tid)
        elif action == "keepers":
            pid = ensure_playlist(sp, "keepers_playlist_name", "keepers_playlist_id",
                                  state.get("keepers_playlist_name", DEFAULT_KEEPERS_PLAYLIST_NAME))
            action_remove_from_playlist(sp, tid, pid)
        elif action == "favourite":
            pid = ensure_playlist(sp, "favourites_playlist_name", "favourites_playlist_id",
                                  state.get("favourites_playlist_name", DEFAULT_FAVOURITES_PLAYLIST_NAME))
            action_remove_from_playlist(sp, tid, pid)
        if tid in state["seen"]:
            del state["seen"][tid]
        if tid in state["queue"]:
            state["queue"].remove(tid)
        state["queue"].insert(0, tid)
        state["last_action"] = None
        state["swiped_today"] = max(0, state.get("swiped_today", 0) - 1)
        save_state(state)
        st.success("✅ Undone.")
        st.rerun()
    except Exception as e:
        st.error(f"Undo failed: {e}")

if st.button("↩️ Undo Last Action") or hotkey_action == "undo":
    undo_last_action(sp, state)

# Progress + ETA
processed = len(state.get("seen", {}))
remaining = len(state["queue"])
total = state.get("queue_built_total", processed + remaining) or (processed + remaining)
st.progress(0 if total == 0 else processed / total)
eta_txt = ""
if processed and state.get("session_start", 0.0) > 0:
    elapsed = max(time.time() - state["session_start"], 1)
    pace = elapsed / processed
    eta_s = int(pace * remaining)
    mins, secs = divmod(eta_s, 60)
    if mins or secs:
        eta_txt = f" • ETA ~{mins}m {secs}s"

st.caption("Hotkeys: K Keep • R Remove • P Keepers • F Favourite • S Skip • U Undo • ? Help")
st.caption(f"Processed: **{processed}/{total}** • Remaining: **{remaining}**{eta_txt}")

# Export decisions
def build_actions_csv(seen: Dict) -> str:
    rows = ["track_id,action,timestamp"]
    for tid, info in seen.items():
        rows.append(f"{tid},{info.get('action','')},{info.get('ts','')}")
    return "\n".join(rows)

csv_data = build_actions_csv(state.get("seen", {})).encode("utf-8")
st.download_button("⤓ Export actions (CSV)", data=csv_data,
                   file_name="swpify_actions.csv", mime="text/csv")

# Help
if hotkey_action == "help" or st.button("❓ Show hotkeys/help"):
    st.info(
        "Hotkeys:\n"
        "• K Keep\n• R Remove (un-like)\n• P Keep → Keepers\n• F Favourite → Favourites\n• S Skip\n• U Undo last action\n\n"
        "Tips:\n"
        "• Use Shuffle to avoid clumps.\n"
        "• Use the artist filter button to blitz one artist.\n"
        "• Export actions anytime; progress is saved in swpify_state.json."
    )

with st.expander("🧹 Utilities"):
    if st.button("Clear local state (does not affect Spotify)"):
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        st.query_params.clear()
        st.success("Local state cleared. Reload the page.")
