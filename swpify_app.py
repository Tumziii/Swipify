# Swpify — Spotify Liked Songs (mobile-first + compact mode + date filter + progress)
# Streamlit 1.38+, Spotipy 2.23+

from __future__ import annotations
import os
from datetime import datetime
from typing import Dict, List, Optional

import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# --------------------------- App Config --------------------------- #
APP_TITLE = "Swpify — Spotify Liked Songs"
CACHE_PATH = ".cache_swpify"
DEFAULT_FAVOURITES = "Favourites (Swpify)"
PAGE_FETCH = 50
SCOPES = "user-library-read user-library-modify playlist-modify-private playlist-modify-public"

st.set_page_config(
    page_title="Swpify",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# --------------------------- Styles --------------------------- #
st.markdown(
    """
<style>
html, body, [class*=css] { font-size: 18px; }

/* Buttons */
.stButton > button {
  width: 100%;
  padding: 14px 18px;
  font-size: 18px;
  border-radius: 12px;
}

/* Compact row buttons (desktop) */
.compact-row .stButton > button {
  padding: 10px 12px;
  font-size: 16px;
  border-radius: 10px;
}

/* Card */
.swpify-card {
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 16px;
  padding: 14px;
  margin-top: 10px;
}

.block-container { padding-top: 1rem; padding-bottom: 3.5rem; }

/* Mobile stacking */
@media (max-width: 500px) {
  .stColumns, .stColumn { display: block !important; width: 100% !important; }
  .swpify-actions .stButton { margin-bottom: 10px; }
}
</style>
""",
    unsafe_allow_html=True,
)

# --------------------------- Secrets --------------------------- #
# IMPORTANT: Use the app ROOT as redirect to avoid /callback 404 confusion.
# Spotify Dashboard → Redirect URIs: https://swpify.streamlit.app
# Streamlit Secrets:
#   SPOTIPY_CLIENT_ID = "..."
#   SPOTIPY_CLIENT_SECRET = "..."
#   SPOTIPY_REDIRECT_URI = "https://swpify.streamlit.app"
CLIENT_ID = st.secrets.get("SPOTIPY_CLIENT_ID", "")
CLIENT_SECRET = st.secrets.get("SPOTIPY_CLIENT_SECRET", "")
REDIRECT_URI = st.secrets.get("SPOTIPY_REDIRECT_URI", "")

# --------------------------- Session --------------------------- #
def init_state() -> None:
    defaults = {
        "queue": [],                 # list[str] of track IDs to work through (filtered slice)
        "seen": {},                  # track_id -> "keep"|"favourite"|"remove"|"skip"
        "favourites_name": DEFAULT_FAVOURITES,
        "favourites_id": None,
        "liked_total": 0,            # total liked across library (for global progress)
        "swiped_today": 0,
        "filter_start": None,        # date
        "filter_end": None,          # date
        "compact_mode": False,       # UI layout toggle
        "build_request": False,      # flag to trigger building outside expander
        "token_info": None,          # OAuth tokens
        "me_id": None,               # current user id
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)

init_state()

# --------------------------- OAuth (explicit, in-app) --------------------------- #
def ensure_spotify_client() -> Optional[Spotify]:
    if not (CLIENT_ID and CLIENT_SECRET and REDIRECT_URI):
        st.error("Missing Spotify credentials in Secrets. Please set SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, SPOTIPY_REDIRECT_URI.")
        return None

    auth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,     # app root (works fine with query param handling)
        scope=SCOPES,
        cache_path=None,               # avoid file cache on Cloud
        open_browser=False,            # NEVER try console/browser flow here
        show_dialog=False,
        requests_timeout=15,
    )

    # Handle return from Spotify: ?code=...
    params = st.query_params
    if "code" in params:
        try:
            token_info = auth.get_access_token(code=params["code"])
            st.session_state["token_info"] = token_info
            st.query_params.clear()
            st.rerun()
        except Exception as e:
            st.error(f"Authentication error: {e}")
            return None

    # Already have tokens?
    ti = st.session_state.get("token_info")
    if ti:
        try:
            if auth.is_token_expired(ti):
                ti = auth.refresh_access_token(ti["refresh_token"])
                st.session_state["token_info"] = ti
        except Exception:
            st.session_state["token_info"] = None
            st.warning("Session expired — please log in again.")
            st.rerun()
        return Spotify(auth=ti["access_token"])

    # No token yet → show login button
    auth_url = auth.get_authorize_url()
    st.title(APP_TITLE)
    st.info("Tap below to connect your Spotify account.")
    st.link_button("🔐 Log in with Spotify", auth_url, use_container_width=True)
    return None

# --------------------------- Spotify helpers --------------------------- #
def get_me_id(sp: Spotify) -> str:
    mid = st.session_state.get("me_id")
    if mid:
        return mid
    uid = sp.current_user()["id"]
    st.session_state["me_id"] = uid
    return uid

def total_liked(sp: Spotify) -> int:
    try:
        return sp.current_user_saved_tracks(limit=1).get("total", 0)
    except Exception:
        return 0

def fetch_liked_with_dates(sp: Spotify) -> List[Dict]:
    """Return liked songs as dicts: {"id", "track", "added_at"} (ISO)."""
    out: List[Dict] = []
    offset = 0
    while True:
        batch = sp.current_user_saved_tracks(limit=PAGE_FETCH, offset=offset)
        items = batch.get("items", [])
        if not items:
            break
        for it in items:
            tr = it.get("track")
            if tr and tr.get("id"):
                out.append({"id": tr["id"], "track": tr, "added_at": it.get("added_at")})
        offset += len(items)
        if offset >= batch.get("total", 0):
            break
    return out

def ensure_playlist(sp: Spotify, name: str) -> str:
    uid = get_me_id(sp)
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results["items"]:
            if pl["name"] == name and pl["owner"]["id"] == uid:
                return pl["id"]
        results = sp.next(results) if results.get("next") else None
    created = sp.user_playlist_create(uid, name, public=False, description="Created by Swpify")
    return created["id"]

def add_to_playlist(sp: Spotify, pid: str, track_id: str) -> None:
    try:
        sp.playlist_add_items(pid, [track_id])
    except Exception:
        pass

def unlike(sp: Spotify, track_id: str) -> None:
    try:
        sp.current_user_saved_tracks_delete([track_id])
    except Exception:
        pass

def relike(sp: Spotify, track_id: str) -> None:
    try:
        sp.current_user_saved_tracks_add([track_id])
    except Exception:
        pass

# --------------------------- UI blocks --------------------------- #
def header():
    st.markdown(f"### {APP_TITLE}")
    st.sidebar.metric("Swiped today", st.session_state["swiped_today"])

def progress_bar():
    total = st.session_state["liked_total"]
    done = len(st.session_state["seen"])
    if total > 0:
        pct = int((done / total) * 100)
        st.progress(pct / 100, text=f"{pct}% complete ({done}/{total})")
    else:
        st.progress(0, text="0% complete")

def build_controls(sp: Spotify):
    with st.expander("⚙️ Options", expanded=True):
        c1, c2 = st.columns(2)
        with c1:
            st.session_state["favourites_name"] = st.text_input(
                "Favourites playlist name", st.session_state["favourites_name"]
            )
        with c2:
            st.session_state["compact_mode"] = st.toggle("🖥️ Compact Desktop Mode")

        c3, c4 = st.columns(2)
        with c3:
            st.session_state["filter_start"] = st.date_input(
                "Added After",
                st.session_state["filter_start"] or datetime(2020, 1, 1).date(),
            )
        with c4:
            st.session_state["filter_end"] = st.date_input(
                "Added Before",
                st.session_state["filter_end"] or datetime.now().date(),
            )

        # set a flag (we build outside to avoid nested containers)
        if st.button("Build / Refresh Queue", use_container_width=True):
            st.session_state["build_request"] = True

        # convenience: logout button
        if st.button("Log out (clear token)", use_container_width=True):
            st.session_state["token_info"] = None
            st.session_state["me_id"] = None
            st.toast("Session cleared — please log in again.")
            st.rerun()

def do_build_queue(sp: Spotify):
    """Run outside expander; safe spinner, no nested containers."""
    if not st.session_state.get("build_request"):
        return
    st.session_state["build_request"] = False

    with st.spinner("Fetching liked songs…"):
        liked = fetch_liked_with_dates(sp)
        st.session_state["liked_total"] = len(liked)

        start = st.session_state["filter_start"]
        end = st.session_state["filter_end"]

        filtered_ids: List[str] = []
        for t in liked:
            added_at = t.get("added_at")
            try:
                d = datetime.fromisoformat(added_at.replace("Z", "+00:00")).date()
            except Exception:
                continue
            if start <= d <= end:
                filtered_ids.append(t["id"])

        st.session_state["queue"] = filtered_ids

    st.toast(f"Queue ready: {len(filtered_ids)} of {len(liked)} songs match date filter ✅")

def render_track(sp: Spotify, track_id: str) -> bool:
    """Return False if track couldn't be loaded."""
    try:
        tr = sp.track(track_id)
    except Exception:
        tr = None
    if not tr:
        return False

    name = tr.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in tr.get("artists", [])) or "Unknown"
    album = (tr.get("album") or {}).get("name", "")
    imgs = (tr.get("album") or {}).get("images", [])
    cover = imgs[0]["url"] if imgs else None
    preview = tr.get("preview_url")
    dur_ms = tr.get("duration_ms", 0)
    mins, secs = divmod(dur_ms // 1000, 60)
    popularity = tr.get("popularity", 0)
    link = (tr.get("external_urls") or {}).get("spotify")

    st.markdown('<div class="swpify-card">', unsafe_allow_html=True)
    if cover:
        st.image(cover, use_column_width=True)
    st.subheader(name)
    st.write(f"**{artists}**")
    if album:
        st.caption(album)
    st.caption(f"Duration: {mins}:{secs:02d} • Popularity: {popularity}")
    if link:
        st.link_button("Open in Spotify", link, use_container_width=True)
    if preview:
        st.audio(preview)
    st.markdown("</div>", unsafe_allow_html=True)
    return True

def action_row(sp: Spotify, track_id: str):
    compact = st.session_state["compact_mode"]
    wrap = st.container()
    if compact:
        wrap = st.container()
        cols = st.columns(4)
        containers = cols
        wrap.markdown('<div class="compact-row">', unsafe_allow_html=True)
    else:
        containers = [st.container(), st.container(), st.container(), st.container()]

    with containers[0]:
        if st.button("✅ Keep", use_container_width=True):
            st.session_state["seen"][track_id] = "keep"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with containers[1]:
        if st.button("⭐ Favourite", use_container_width=True):
            pid = st.session_state.get("favourites_id") or ensure_playlist(sp, st.session_state["favourites_name"])
            st.session_state["favourites_id"] = pid
            add_to_playlist(sp, pid, track_id)
            st.session_state["seen"][track_id] = "favourite"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with containers[2]:
        if st.button("🗑 Remove", use_container_width=True):
            unlike(sp, track_id)
            st.session_state["seen"][track_id] = "remove"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with containers[3]:
        if st.button("⏭ Skip", use_container_width=True):
            q = st.session_state["queue"]
            q.append(q.pop(0))
            st.session_state["seen"][track_id] = "skip"
            st.rerun()

    if compact:
        wrap.markdown("</div>", unsafe_allow_html=True)

def undo(sp: Spotify):
    if not st.session_state["seen"]:
        st.info("Nothing to undo.")
        return
    # last seen entry
    last_id, last_action = list(st.session_state["seen"].items())[-1]
    # put back to front of queue
    st.session_state["queue"].insert(0, last_id)
    if last_action == "remove":
        relike(sp, last_id)
    st.session_state["seen"].pop(last_id)
    st.toast("↩️ Undone last action.")

# --------------------------- Main --------------------------- #
def main():
    sp = ensure_spotify_client()
    if not sp:
        # login UI already shown
        return

    header()
    progress_bar()
    build_controls(sp)
    do_build_queue(sp)

    q = st.session_state["queue"]
    if not q:
        # Show full count as reassurance if queue empty
        if st.session_state["liked_total"] == 0:
            st.session_state["liked_total"] = total_liked(sp)
        st.info(f"🎵 No queue yet — tap **Build / Refresh Queue** above. Total liked: {st.session_state['liked_total']}")
        return

    tid = q[0]
    ok = render_track(sp, tid)
    if not ok:
        q.pop(0)
        st.warning("Could not load this track; skipped.")
        st.rerun()

    # Actions
    st.markdown('<div class="swpify-actions">', unsafe_allow_html=True)
    action_row(sp, tid)
    st.markdown('</div>', unsafe_allow_html=True)

    # Footer
    st.divider()
    c1, c2 = st.columns(2)
    with c1:
        if st.button("↩️ Undo", use_container_width=True):
            undo(sp)
            st.rerun()
    with c2:
        st.caption(f"Remaining in queue: **{len(q)}**")

if __name__ == "__main__":
    main()
