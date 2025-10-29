import os
import time
import datetime as dt
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import streamlit as st

# ---------------------- Streamlit Page Setup ----------------------
st.set_page_config(
    page_title="Swpify — Spotify Liked Songs",
    layout="centered",
    initial_sidebar_state="expanded"
)

st.title("🎧 Swpify — Spotify Liked Songs")
st.caption("Swipe to keep, remove, or file songs to Favourites. Built with Streamlit + Spotipy.")


# ---------------------- Spotify Authentication ----------------------
def get_spotify_client() -> spotipy.Spotify:
    try:
        auth_manager = SpotifyOAuth(
            client_id=st.secrets["SPOTIPY_CLIENT_ID"],
            client_secret=st.secrets["SPOTIPY_CLIENT_SECRET"],
            redirect_uri=st.secrets["SPOTIPY_REDIRECT_URI"],
            scope="user-library-read user-library-modify playlist-modify-public playlist-modify-private",
            cache_path=".cache_spotify_swpify"
        )
        token_info = auth_manager.get_access_token(as_dict=False)
        return spotipy.Spotify(auth=token_info)
    except Exception as e:
        st.error(f"Authentication failed: {e}")
        st.stop()


# ---------------------- Helpers ----------------------
def today():
    return dt.date.today().isoformat()


def init_state():
    """Initialize all session state keys."""
    defaults = {
        "queue": [],
        "seen": {},
        "last_action": [],
        "keepers_playlist": "Keepers (Swpify)",
        "favourites_playlist": "Favourites (Swpify)",
        "swiped_today": 0
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def sidebar_stats():
    st.sidebar.metric("Swiped today", st.session_state["swiped_today"])


def get_liked_songs(sp):
    """Return full list of liked songs (simplified objects)."""
    tracks = []
    results = sp.current_user_saved_tracks(limit=50)
    tracks.extend(results["items"])

    while results["next"]:
        results = sp.next(results)
        tracks.extend(results["items"])

    return [t["track"] for t in tracks if t and t["track"]]


# ---------------------- UI Components ----------------------
def song_card(track: dict):
    """Render a track card."""
    name = track.get("name", "Unknown Title")
    artists = ", ".join(a["name"] for a in track.get("artists", []))
    album = track.get("album", {}).get("name", "")
    images = track.get("album", {}).get("images", [])
    cover = images[0]["url"] if images else None
    preview = track.get("preview_url")
    popularity = track.get("popularity", 0)
    duration_ms = track.get("duration_ms") or 0
    mins, secs = divmod(duration_ms // 1000, 60)

    left, right = st.columns([1, 2], vertical_alignment="top")

    with left:
        if cover:
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


def song_actions(sp, track_id):
    """Provide buttons to Keep / Remove / Add to Favourites."""
    keep_col, fav_col, rm_col = st.columns(3)

    with keep_col:
        if st.button("✅ Keep", use_container_width=True):
            st.session_state["seen"][track_id] = "kept"
            st.session_state["swiped_today"] += 1
            st.session_state["last_action"].append(("keep", track_id))
            st.success("Song kept")
            st.rerun()

    with fav_col:
        if st.button("⭐ Favourite", use_container_width=True):
            st.session_state["seen"][track_id] = "favourite"
            st.session_state["swiped_today"] += 1
            add_to_playlist(sp, track_id, st.session_state["favourites_playlist"])
            st.success("Added to Favourites")
            st.rerun()

    with rm_col:
        if st.button("🗑 Remove", use_container_width=True):
            st.session_state["seen"][track_id] = "removed"
            st.session_state["swiped_today"] += 1
            try:
                sp.current_user_saved_tracks_delete([track_id])
                st.warning("Removed from Liked Songs")
            except Exception as e:
                st.error(f"Error removing song: {e}")
            st.rerun()


def add_to_playlist(sp, track_id, playlist_name):
    """Create or add song to playlist."""
    user_id = sp.current_user()["id"]
    playlists = sp.current_user_playlists()["items"]

    playlist = next((pl for pl in playlists if pl["name"] == playlist_name), None)
    if not playlist:
        playlist = sp.user_playlist_create(user=user_id, name=playlist_name)
    sp.playlist_add_items(playlist["id"], [track_id])


# ---------------------- Main ----------------------
def main():
    init_state()
    sidebar_stats()

    # Authenticate Spotify
    sp = get_spotify_client()

    # Build Queue
    with st.expander("⚙ Options", expanded=True):
        st.session_state["keepers_playlist"] = st.text_input("Keepers playlist name", st.session_state["keepers_playlist"])
        st.session_state["favourites_playlist"] = st.text_input("Favourites playlist name", st.session_state["favourites_playlist"])
        if st.button("Build / Refresh Queue"):
            with st.spinner("Loading liked songs…"):
                liked = get_liked_songs(sp)
                st.session_state["queue"] = [t for t in liked if t["id"] not in st.session_state["seen"]]
                st.success(f"Queue built with {len(st.session_state['queue'])} songs.")

    queue = st.session_state["queue"]
    if not queue:
        st.info("🎵 No queue yet — click **Build/Refresh Queue** above to begin.")
        return

    # Display top of queue
    track = queue[0]
    tid = track["id"]

    song_card(track)
    song_actions(sp, tid)

    st.divider()
    st.caption(f"You currently have approximately **{len(queue)}** songs remaining.")


# ---------------------- Run App ----------------------
if __name__ == "__main__":
    main()
