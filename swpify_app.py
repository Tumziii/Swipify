# swpify_app.py
# Swpify — Spotify Liked Songs Swipe (mobile-first)
# Requires: streamlit==1.38.0, spotipy==2.23.0, pandas

import datetime as dt
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple

import streamlit as st
import spotipy
from spotipy.oauth2 import SpotifyOAuth


# --------------------------- Config --------------------------- #
st.set_page_config(
    page_title="Swpify — Spotify Liked Songs",
    page_icon="🎧",
    layout="wide",          # we still manage compact via our own styles
    initial_sidebar_state="collapsed",
)

# Small CSS to tighten layout on mobile by default
MOBILE_CSS = """
<style>
/* Reduce paddings */
.block-container {padding-top: 0.8rem; padding-bottom: 2rem; max-width: 1200px;}
/* Buttons full width and chunkier on phones */
.stButton>button {height: 3.2rem; font-size: 1.05rem;}
/* Make the image column narrower on compact */
.swpify-card .leftcol img {border-radius: 10px;}
/* Tighten subheaders */
h2, h3 { margin-bottom: 0.2rem; }
/* Sticky footer actions on very small screens */
@media (max-width: 480px) {
  .swpify-actions { position: sticky; bottom: 0; background: var(--background-color); padding-top: 0.5rem; padding-bottom: 0.5rem; z-index: 50; }
}
</style>
"""
st.markdown(MOBILE_CSS, unsafe_allow_html=True)


# --------------------------- Helpers & State --------------------------- #
@dataclass(frozen=True)
class K:
    queue    : str = "queue"          # list[dict] of track payloads
    swiped   : str = "swiped_today"   # int
    favourites: str = "favourites_playlist"  # str
    total    : str = "total_liked"    # int (all liked tracks in library)
    compact  : str = "compact"        # bool (mobile-friendly layout)
    added_after : str = "added_filter_start"  # str 'YYYY/MM/DD'
    added_before: str = "added_filter_end"    # str
    token_info: str = "token_info"    # dict from SpotifyOAuth
    seen_ids : str = "seen_ids"       # set of track ids already swiped this session


def init_state():
    if K.queue not in st.session_state:
        st.session_state[K.queue] = []
    if K.swiped not in st.session_state:
        st.session_state[K.swiped] = 0
    if K.favourites not in st.session_state:
        st.session_state[K.favourites] = "Favourites (Swpify)"
    if K.total not in st.session_state:
        st.session_state[K.total] = 0
    if K.seen_ids not in st.session_state:
        st.session_state[K.seen_ids] = set()
    # Make COMPACT the default (mobile-first).
    # Allow override with ?compact=0 | 1
    qp_val = str(st.query_params.get("compact", "1")).lower()
    default_compact = not (qp_val in ("0", "false"))
    if K.compact not in st.session_state:
        st.session_state[K.compact] = default_compact
    if K.added_after not in st.session_state:
        st.session_state[K.added_after] = "2020/01/01"
    if K.added_before not in st.session_state:
        # default to “today” for convenience
        st.session_state[K.added_before] = dt.date.today().strftime("%Y/%m/%d")


def parse_date(s: str) -> Optional[dt.date]:
    if not s:
        return None
    s = s.strip().replace("-", "/")
    try:
        y, m, d = [int(x) for x in s.split("/")]
        return dt.date(y, m, d)
    except Exception:
        return None


def fmt_ms(ms: int) -> str:
    sec = int(round(ms / 1000))
    m = sec // 60
    s = sec % 60
    return f"{m}:{s:02d}"


def make_oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=st.secrets["SPOTIPY_CLIENT_ID"],
        client_secret=st.secrets["SPOTIPY_CLIENT_SECRET"],
        redirect_uri=st.secrets["SPOTIPY_REDIRECT_URI"],
        scope="user-library-read user-library-modify playlist-modify-private playlist-modify-public",
        cache_path=None,
        show_dialog=False,
    )


def token_to_client() -> Optional[spotipy.Spotify]:
    oauth = make_oauth()
    token_info = st.session_state.get(K.token_info)

    if not token_info:
        # first-time: check for redirect code
        code = st.query_params.get("code")
        if code:
            token_info = oauth.get_access_token(code, as_dict=True)  # deprecation warned but works on 2.23
            st.session_state[K.token_info] = token_info
            # clean URL (remove ?code=...)
            st.query_params.clear()
        else:
            return None

    # refresh if expired
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
        st.session_state[K.token_info] = token_info

    return spotipy.Spotify(auth=token_info["access_token"])


# --------------------------- Spotify Actions --------------------------- #
def fetch_all_liked(sp: spotipy.Spotify) -> List[Dict]:
    """Get ALL liked tracks with added_at."""
    items: List[Dict] = []
    limit = 50
    offset = 0
    while True:
        resp = sp.current_user_saved_tracks(limit=limit, offset=offset)
        for it in resp["items"]:
            t = it["track"]
            if not t:
                continue
            album_images = t["album"]["images"] or []
            image_url = album_images[-1]["url"] if album_images else None
            items.append({
                "id": t["id"],
                "name": t["name"],
                "artist": ", ".join(a["name"] for a in t["artists"]),
                "album": t["album"]["name"],
                "duration_ms": t.get("duration_ms", 0),
                "popularity": t.get("popularity", 0),
                "image": image_url,
                "url": t["external_urls"]["spotify"],
                "added_at": it.get("added_at"),
            })
        offset += len(resp["items"])
        if not resp["next"]:
            break
    # store total size for progress (100% = all-time liked size)
    st.session_state[K.total] = len(items)
    return items


def ensure_playlist(sp: spotipy.Spotify, name: str) -> str:
    me = sp.current_user()["id"]
    # quick lookup: get first 50 playlists
    results = sp.current_user_playlists(limit=50)
    for pl in results["items"]:
        if pl["name"] == name:
            return pl["id"]
    # not found -> create private
    created = sp.user_playlist_create(me, name, public=False, description="Made with Swpify")
    return created["id"]


def add_to_playlist(sp: spotipy.Spotify, track_id: str, playlist_name: str):
    pid = ensure_playlist(sp, playlist_name)
    sp.playlist_add_items(pid, [track_id])


def unlike_track(sp: spotipy.Spotify, track_id: str):
    sp.current_user_saved_tracks_delete([track_id])


# --------------------------- UI Pieces --------------------------- #
def header():
    # progress across entire library (not only filtered queue)
    total = max(1, st.session_state.get(K.total, 0))
    seen = len(st.session_state[K.seen_ids])
    pct = int(100 * seen / total)
    st.subheader(f"{pct}% complete ({seen}/{total})")
    st.progress(min(1.0, seen / total))


def controls(sp: Optional[spotipy.Spotify]):
    with st.expander("Options", expanded=True):
        # compact toggle (default ON). Also rewrite query param so reopened links preserve state.
        col_t, col_toggle = st.columns([1, 1])
        with col_t:
            st.text_input("Favourites playlist name", key=K.favourites)
        with col_toggle:
            compact_now = st.toggle("🖥️ Compact Desktop Mode", value=st.session_state[K.compact], help="Turn OFF for a wider desktop layout")
            st.session_state[K.compact] = compact_now
            st.query_params["compact"] = "1" if compact_now else "0"

        c1, c2 = st.columns(2)
        with c1:
            st.text_input("Added After", key=K.added_after, help="YYYY/MM/DD")
        with c2:
            st.text_input("Added Before", key=K.added_before, help="YYYY/MM/DD")

        # Build/Refresh queue
        if sp and st.button("Build / Refresh Queue", use_container_width=True):
            all_liked = fetch_all_liked(sp)
            start = parse_date(st.session_state[K.added_after])
            end   = parse_date(st.session_state[K.added_before])

            def in_range(added_at: Optional[str]) -> bool:
                if not added_at:
                    return True
                d = dt.datetime.fromisoformat(added_at.replace("Z", "+00:00")).date()
                if start and d < start:
                    return False
                if end and d > end:
                    return False
                return True

            filtered = [t for t in all_liked if in_range(t.get("added_at"))]
            # Shuffle option could be added here if wanted
            # Reset queue + seen-set only for the filtered portion;
            # seen_ids persists to compute global progress.
            st.session_state[K.queue] = filtered
            st.toast(f"Queue ready: {len(filtered)} song(s)", icon="🎵")
            st.rerun()

        # Logout (clear token)
        if st.button("Log out (clear token)", use_container_width=True):
            if K.token_info in st.session_state:
                st.session_state.pop(K.token_info)
            st.success("Cleared token; please log in again.")
            st.rerun()


def card(track: Dict):
    # Responsive card: compact uses narrower artwork
    compact = st.session_state[K.compact]
    ratios = [1, 2] if compact else [4, 7]
    left, right = st.columns(ratios, vertical_alignment="top", gap="medium")
    with left:
        st.markdown('<div class="swpify-card leftcol">', unsafe_allow_html=True)
        if track.get("image"):
            # Constrain image a bit (smaller on compact)
            max_w = 260 if compact else 420
            st.image(track["image"], width=max_w)
        else:
            st.caption("(no artwork)")
        st.markdown("</div>", unsafe_allow_html=True)

    with right:
        st.subheader(track["name"])
        st.write(track["artist"])
        if track.get("album"):
            st.caption(track["album"])
        meta = f"Duration: {fmt_ms(track.get('duration_ms', 0))} • Popularity: {track.get('popularity', 0)}"
        st.caption(meta)

        if track.get("url"):
            st.link_button("🎧 Open in Spotify", track["url"], use_container_width=True)


def actions_row(sp: spotipy.Spotify, track_id: str):
    st.markdown('<div class="swpify-actions">', unsafe_allow_html=True)
    a, b, c = st.columns(3)
    with a:
        if st.button("✅ Keep", use_container_width=True):
            act_and_next("keep", sp, track_id)
    with b:
        if st.button("⭐ Favourite", use_container_width=True):
            act_and_next("fav", sp, track_id)
    with c:
        if st.button("🗑️ Remove (unlike)", use_container_width=True):
            act_and_next("rm", sp, track_id)
    st.markdown("</div>", unsafe_allow_html=True)


def act_and_next(action: str, sp: spotipy.Spotify, track_id: str):
    try:
        if action == "fav":
            add_to_playlist(sp, track_id, st.session_state[K.favourites])
        elif action == "rm":
            unlike_track(sp, track_id)
        # 'keep' does nothing with library but marks as processed
    finally:
        st.session_state[K.swiped] += 1
        st.session_state[K.seen_ids].add(track_id)
        # advance queue if current is same head
        if st.session_state[K.queue] and st.session_state[K.queue][0]["id"] == track_id:
            st.session_state[K.queue].pop(0)
        st.rerun()


def login_view(oauth: SpotifyOAuth):
    st.title("Swpify — Spotify Liked Songs")
    auth_url = oauth.get_authorize_url()
    st.link_button("🔐 Log in with Spotify", auth_url, use_container_width=True)
    st.info("Tip: on iPhone, open this link in Safari (not an in-app browser).")


def main():
    init_state()

    oauth = make_oauth()
    sp = token_to_client()

    if not sp:
        login_view(oauth)
        return

    header()
    controls(sp)

    q = st.session_state[K.queue]
    if not q:
        # Empty queue banner
        st.info(f"🎵 No queue yet — tap **Build / Refresh Queue** above. Total liked: {st.session_state.get(K.total, 0)}")
        return

    # Display current head of queue
    current = q[0]
    card(current)
    actions_row(sp, current["id"])

    st.divider()
    st.caption(f"Remaining in queue: **{len(q)}**")


if __name__ == "__main__":
    main()
