# swpify_app.py
# Swpify — Spotify Liked Songs with Compact Mode + Date Filter + Progress Bar

import os
from datetime import datetime
from typing import Dict, List, Optional

import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# --------------------------- Config --------------------------- #
APP_TITLE = "Swpify — Spotify Liked Songs"
CACHE_PATH = ".cache_swpify"
DEFAULT_FAVOURITES = "Favourites (Swpify)"
PAGE_FETCH = 50

SCOPES = [
    "user-library-read",
    "user-library-modify",
    "playlist-modify-private",
    "playlist-modify-public",
]

# ------------------------ Page settings ----------------------- #
st.set_page_config(
    page_title="Swpify",
    page_icon="🎧",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------- Custom CSS ---------------------- #
st.markdown(
    """
<style>
html, body, [class*=css] { font-size: 18px; }

/* Core button style */
.stButton > button {
  width: 100%;
  padding: 14px 18px;
  font-size: 18px;
  border-radius: 12px;
}

/* Compact toggle style */
.compact-btn > button {
  font-size: 16px !important;
  padding: 6px 10px !important;
  border-radius: 8px !important;
}

/* Cards and layout */
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

# ------------------------ Secrets & OAuth ---------------------- #
CLIENT_ID = st.secrets["SPOTIPY_CLIENT_ID"]
CLIENT_SECRET = st.secrets["SPOTIPY_CLIENT_SECRET"]
REDIRECT_URI = st.secrets["SPOTIPY_REDIRECT_URI"]


def spotify_client() -> Optional[Spotify]:
    auth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=" ".join(SCOPES),
        cache_path=CACHE_PATH,
        show_dialog=False,
    )
    try:
        return Spotify(auth_manager=auth)
    except Exception as e:
        st.error(f"Could not connect to Spotify: {e}")
        return None


# ------------------------ Session Keys ------------------------- #
def init_state():
    defaults = {
        "queue": [],
        "seen": {},
        "stack": [],
        "favourites_name": DEFAULT_FAVOURITES,
        "favourites_id": None,
        "liked_total": 0,
        "swiped_today": 0,
        "added_filter_start": None,  # date
        "added_filter_end": None,    # date
        "compact_mode": False,
        "build_request": False,      # trigger building outside expander
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)


# ------------------------ Spotify helpers ---------------------- #
def total_liked(sp: Spotify) -> int:
    try:
        return sp.current_user_saved_tracks(limit=1).get("total", 0)
    except Exception:
        return 0


def fetch_liked_with_dates(sp: Spotify) -> List[Dict]:
    """Return list of liked tracks with added_at timestamps."""
    results = []
    offset = 0
    while True:
        batch = sp.current_user_saved_tracks(limit=PAGE_FETCH, offset=offset)
        items = batch.get("items", [])
        if not items:
            break
        for item in items:
            tr = item.get("track")
            if tr and tr.get("id"):
                results.append(
                    {
                        "id": tr["id"],
                        "track": tr,
                        "added_at": item.get("added_at"),
                    }
                )
        offset += len(items)
        if offset >= batch.get("total", 0):
            break
    return results


def ensure_playlist(sp: Spotify, name: str) -> str:
    uid = sp.current_user()["id"]
    results = sp.current_user_playlists(limit=50)
    while True:
        for pl in results["items"]:
            if pl["name"] == name and pl["owner"]["id"] == uid:
                return pl["id"]
        if results.get("next"):
            results = sp.next(results)
        else:
            break
    created = sp.user_playlist_create(
        uid, name, public=False, description="Created by Swpify"
    )
    return created["id"]


def add_to_playlist(sp: Spotify, track_id: str, playlist_id: str):
    try:
        sp.playlist_add_items(playlist_id, [track_id])
    except Exception:
        pass


def unlike(sp: Spotify, track_id: str):
    try:
        sp.current_user_saved_tracks_delete([track_id])
    except Exception:
        pass


def relike(sp: Spotify, track_id: str):
    try:
        sp.current_user_saved_tracks_add([track_id])
    except Exception:
        pass


# -------------------------- UI elements -------------------------- #
def header():
    st.markdown(f"### {APP_TITLE}")


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
            st.session_state["added_filter_start"] = st.date_input(
                "Added After",
                st.session_state["added_filter_start"] or datetime(2020, 1, 1).date(),
            )
        with c4:
            st.session_state["added_filter_end"] = st.date_input(
                "Added Before",
                st.session_state["added_filter_end"] or datetime.now().date(),
            )

        # NOTE: We only set a flag in the expander to avoid nested containers.
        if st.button("Build / Refresh Queue", use_container_width=True):
            st.session_state["build_request"] = True


def do_build_queue(sp: Spotify):
    """Runs outside the expander so we can safely use spinner/toast."""
    if not st.session_state.get("build_request"):
        return
    st.session_state["build_request"] = False

    with st.spinner("Fetching liked songs…"):
        all_liked = fetch_liked_with_dates(sp)
        total = len(all_liked)

        start = st.session_state["added_filter_start"]
        end = st.session_state["added_filter_end"]
        # Compare by date object
        filtered = []
        for t in all_liked:
            added_at = t.get("added_at")
            try:
                d = datetime.fromisoformat(added_at.replace("Z", "+00:00")).date()
            except Exception:
                continue
            if start <= d <= end:
                filtered.append(t)

        st.session_state["queue"] = [t["id"] for t in filtered]
        st.session_state["liked_total"] = total

    st.toast(f"Queue ready: {len(filtered)} of {total} songs match date filter ✅")


def track_card(sp: Spotify, track_id: str):
    tr = sp.track(track_id)
    if not tr:
        return
    name = tr.get("name", "Unknown")
    artists = ", ".join(a["name"] for a in tr.get("artists", [])) or "Unknown"
    album = (tr.get("album") or {}).get("name", "")
    imgs = (tr.get("album") or {}).get("images", [])
    cover = imgs[0]["url"] if imgs else None
    preview = tr.get("preview_url")
    dur_ms = tr.get("duration_ms", 0)
    mins, secs = divmod(dur_ms // 1000, 60)
    popularity = tr.get("popularity", 0)

    st.markdown('<div class="swpify-card">', unsafe_allow_html=True)
    if cover:
        st.image(cover, use_column_width=True)
    st.subheader(name)
    st.write(f"**{artists}**")
    if album:
        st.caption(album)
    st.caption(f"Duration: {mins}:{secs:02d} • Popularity: {popularity}")
    if preview:
        st.audio(preview)
    st.markdown("</div>", unsafe_allow_html=True)


def actions(sp: Spotify, track_id: str):
    compact = st.session_state["compact_mode"]

    if compact:
        c1, c2, c3, c4 = st.columns(4)
    else:
        c1 = c2 = c3 = c4 = st.container()

    with c1:
        if st.button("✅ Keep", use_container_width=True):
            st.session_state["seen"][track_id] = "keep"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with c2:
        if st.button("⭐ Favourite", use_container_width=True):
            pid = st.session_state.get("favourites_id") or ensure_playlist(
                sp, st.session_state["favourites_name"]
            )
            st.session_state["favourites_id"] = pid
            add_to_playlist(sp, track_id, pid)
            st.session_state["seen"][track_id] = "favourite"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with c3:
        if st.button("🗑 Remove", use_container_width=True):
            unlike(sp, track_id)
            st.session_state["seen"][track_id] = "remove"
            st.session_state["queue"].pop(0)
            st.session_state["swiped_today"] += 1
            st.rerun()

    with c4:
        if st.button("⏭ Skip", use_container_width=True):
            q = st.session_state["queue"]
            q.append(q.pop(0))
            st.session_state["seen"][track_id] = "skip"
            st.rerun()


def undo(sp: Spotify):
    if not st.session_state["seen"]:
        st.info("Nothing to undo.")
        return
    last_id, last_action = list(st.session_state["seen"].items())[-1]
    st.session_state["queue"].insert(0, last_id)
    if last_action == "remove":
        relike(sp, last_id)
    st.session_state["seen"].pop(last_id)
    st.toast("↩️ Undone last action.")


# ---------------------------- Main ----------------------------- #
def main():
    init_state()
    sp = spotify_client()
    if not sp:
        st.stop()

    header()
    st.sidebar.metric("Swiped today", st.session_state["swiped_today"])
    progress_bar()

    # Controls inside expander
    build_controls(sp)
    # Build queue outside (no nested containers)
    do_build_queue(sp)

    q = st.session_state["queue"]
    if not q:
        total = total_liked(sp)
        st.session_state["liked_total"] = total
        st.info(
            f"🎵 No queue yet — tap **Build / Refresh Queue** above. Total liked: {total}"
        )
        st.stop()

    track_card(sp, q[0])
    actions(sp, q[0])

    st.divider()
    col1, col2 = st.columns(2)
    with col1:
        if st.button("↩️ Undo", use_container_width=True):
            undo(sp)
            st.rerun()
    with col2:
        st.caption(f"Remaining in queue: **{len(q)}**")


if __name__ == "__main__":
    main()
