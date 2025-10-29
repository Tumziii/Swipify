import os
import datetime as dt
import time
from typing import List, Dict

import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# ---------------------- Streamlit Page Setup ----------------------
st.set_page_config(
    page_title="Swpify — Spotify Liked Songs",
    layout="centered",
    initial_sidebar_state="expanded",
)

st.title("🎧 Swpify — Spotify Liked Songs")
st.caption("Swipe to keep, remove, or file songs to Favourites. Built with Streamlit + Spotipy.")

# ---------------------- Constants ----------------------
SCOPES = "user-library-read user-library-modify playlist-modify-public playlist-modify-private"
CACHE_FILE = ".cache_spotify_swpify"

# ---------------------- Session State ----------------------
def init_state():
    ss = st.session_state
    ss.setdefault("queue", [])                 # list of track dicts
    ss.setdefault("seen", {})                  # track_id -> action
    ss.setdefault("last_actions", [])          # stack for undo (optional extension)
    ss.setdefault("keepers_playlist", "Keepers (Swpify)")
    ss.setdefault("favourites_playlist", "Favourites (Swpify)")
    ss.setdefault("swiped_today", 0)
    ss.setdefault("token_info", None)          # store Spotipy token_info
    ss.setdefault("me_id", None)               # current user id (cache)

init_state()

# ---------------------- OAuth Helpers ----------------------
def oauth() -> SpotifyOAuth:
    try:
        return SpotifyOAuth(
            client_id=st.secrets["SPOTIPY_CLIENT_ID"],
            client_secret=st.secrets["SPOTIPY_CLIENT_SECRET"],
            redirect_uri=st.secrets["SPOTIPY_REDIRECT_URI"],  # <- app root URL
            scope=SCOPES,
            cache_path=CACHE_FILE,
        )
    except KeyError as e:
        st.error(
            "Missing Spotify credentials. Add SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET, "
            "and SPOTIPY_REDIRECT_URI to Streamlit Secrets."
        )
        st.stop()

def get_sp_client() -> Spotify | None:
    """Return an authenticated Spotify client or None if user not logged in yet."""
    auth = oauth()

    # 1) If we already have a token in the session, try to refresh if needed
    if st.session_state["token_info"]:
        if auth.is_token_expired(st.session_state["token_info"]):
            try:
                st.session_state["token_info"] = auth.refresh_access_token(
                    st.session_state["token_info"]["refresh_token"]
                )
            except Exception as e:
                # fall back to login
                st.session_state["token_info"] = None
                st.warning("Session expired — please log in again.")
                return None
        return Spotify(auth=st.session_state["token_info"]["access_token"])

    # 2) If we just returned from Spotify, the URL contains ?code=...
    qp = st.query_params
    if "code" in qp:
        try:
            token_info = auth.get_access_token(code=qp["code"])
            st.session_state["token_info"] = token_info
            # clean URL params
            st.query_params.clear()
            return Spotify(auth=token_info["access_token"])
        except Exception as e:
            st.error(f"Authentication error: {e}")
            return None

    # 3) Not logged in — show login button
    auth_url = auth.get_authorize_url()
    st.link_button("🔐 Log in with Spotify", auth_url, use_container_width=True)
    return None

# ---------------------- Spotify Helpers ----------------------
def me_id(sp: Spotify) -> str:
    if st.session_state["me_id"]:
        return st.session_state["me_id"]
    _id = sp.current_user()["id"]
    st.session_state["me_id"] = _id
    return _id

def fetch_all_liked(sp: Spotify) -> List[dict]:
    """Return list of track dicts for all liked songs."""
    items = []
    results = sp.current_user_saved_tracks(limit=50)
    items.extend(results["items"])
    while results.get("next"):
        results = sp.next(results)
        items.extend(results["items"])
    # flatten to tracks
    tracks = [it["track"] for it in items if it and it.get("track") and it["track"].get("id")]
    return tracks

def ensure_playlist(sp: Spotify, name: str) -> str:
    """Return playlist id for given name, creating if needed."""
    uid = me_id(sp)
    # search first pages only (sufficient for personal use)
    results = sp.current_user_playlists(limit=50)
    while True:
        for pl in results["items"]:
            if pl["name"] == name and pl["owner"]["id"] == uid:
                return pl["id"]
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    created = sp.user_playlist_create(uid, name, public=False, description="Created by Swpify")
    return created["id"]

def add_to_playlist(sp: Spotify, track_id: str, playlist_name: str):
    pid = ensure_playlist(sp, playlist_name)
    sp.playlist_add_items(pid, [track_id])

# ---------------------- UI Pieces ----------------------
def sidebar_stats():
    st.sidebar.metric("Swiped today", st.session_state["swiped_today"])

def card(track: dict):
    """Render a single track card."""
    name = track.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    album = track.get("album", {}).get("name", "")
    imgs = track.get("album", {}).get("images", [])
    cover = imgs[0]["url"] if imgs else None
    preview = track.get("preview_url")
    popularity = track.get("popularity", 0)
    dur_ms = int(track.get("duration_ms") or 0)
    mins, secs = divmod(dur_ms // 1000, 60)

    left, right = st.columns([1, 2], vertical_alignment="top")
    with left:
        if cover:
            # IMPORTANT: Streamlit 1.38 expects use_column_width (not use_container_width)
            st.image(cover, use_column_width=True)
        else:
            st.caption("(No artwork)")

    with right:
        st.subheader(name)
        st.write(f"**{artists}**")
        if album:
            st.caption(album)
        if preview:
            st.audio(preview)
        st.caption(f"Duration: {mins}:{secs:02d} • Popularity: {popularity}")

def actions(sp: Spotify, track_id: str):
    keep, fav, rmv = st.columns(3)
    if keep.button("✅ Keep", use_container_width=True):
        st.session_state["seen"][track_id] = "keep"
        st.session_state["swiped_today"] += 1
        st.session_state["queue"].pop(0)
        st.rerun()

    if fav.button("⭐ Favourite", use_container_width=True):
        try:
            add_to_playlist(sp, track_id, st.session_state["favourites_playlist"])
            st.session_state["seen"][track_id] = "favourite"
            st.session_state["swiped_today"] += 1
            st.session_state["queue"].pop(0)
            st.success("Added to Favourites")
        except Exception as e:
            st.error(f"Failed to add to Favourites: {e}")
        st.rerun()

    if rmv.button("🗑 Remove (unlike)", use_container_width=True):
        try:
            sp.current_user_saved_tracks_delete([track_id])
            st.session_state["seen"][track_id] = "remove"
            st.session_state["swiped_today"] += 1
            st.session_state["queue"].pop(0)
            st.warning("Removed from Liked Songs")
        except Exception as e:
            st.error(f"Failed to remove: {e}")
        st.rerun()

# ---------------------- Main ----------------------
def main():
    sidebar_stats()

    sp = get_sp_client()
    if not sp:
        # Not logged in yet, the login button is shown
        return

    with st.expander("⚙ Options", expanded=True):
        st.session_state["keepers_playlist"] = st.text_input(
            "Keepers playlist name", st.session_state["keepers_playlist"]
        )
        st.session_state["favourites_playlist"] = st.text_input(
            "Favourites playlist name", st.session_state["favourites_playlist"]
        )
        if st.button("Build / Refresh Queue", use_container_width=True):
            with st.spinner("Loading liked songs…"):
                liked = fetch_all_liked(sp)
                unseen = [t for t in liked if t["id"] not in st.session_state["seen"]]
                st.session_state["queue"] = unseen
                st.success(f"Queue ready: {len(unseen)} songs")

    q = st.session_state["queue"]
    if not q:
        # show approximate liked count for reassurance
        try:
            total = sp.current_user_saved_tracks(limit=1).get("total", 0)
            st.info(
                f"🎵 No queue yet — click **Build / Refresh Queue** above to begin.\n\n"
                f"You currently have approximately **{total}** liked songs."
            )
        except Exception:
            st.info("🎵 No queue yet — click **Build / Refresh Queue** above to begin.")
        return

    tr = q[0]
    tid = tr["id"]

    try:
        card(tr)
    except Exception:
        st.warning("Could not load this track; skipping.")
        q.pop(0)
        st.rerun()

    actions(sp, tid)

    st.divider()
    st.caption(f"Remaining in queue: **{len(q)}**")

if __name__ == "__main__":
    main()
