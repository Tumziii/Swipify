from __future__ import annotations
import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple

import streamlit as st
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import streamlit.components.v1 as components


# --------------------------- Config --------------------------- #
st.set_page_config(
    page_title="Swpify — Spotify Liked Songs",
    page_icon="🎧",
    layout="wide",
)


# ---------- CSS (tighter, mobile-first; compact makes it extra tight) ---------- #
def base_css(compact: bool = False) -> str:
    return f"""
<style>
html, body, [class*="css"] {{
  font-size: {13 if compact else 15}px;
}}
.swpify-card {{
  background: rgba(255,255,255,0.03);
  border: 1px solid rgba(255,255,255,0.08);
  border-radius: 14px;
  padding: {6 if compact else 8}px;
  margin-top: {6 if compact else 8}px;
}}
.swpify-art img {{
  border-radius: 10px;
  width: 100%;
  height: auto;
  max-height: {28 if compact else 34}vh;
  object-fit: cover;
}}
.block-container .stColumns {{
  gap: {6 if compact else 8}px !important;
}}
.stButton > button {{
  width: 100%;
  padding: {7 if compact else 9}px {9 if compact else 11}px;
  font-size: {13 if compact else 15}px;
  border-radius: {7 if compact else 9}px;
  margin-top: {6 if compact else 8}px;
}}
.swpify-footer {{
  background: rgba(125,143,175,0.10);
  border: 1px solid rgba(255,255,255,0.08);
  padding: 10px;
  border-radius: 9px;
  margin-top: 8px;
  font-size: {12 if compact else 13}px;
  opacity: 0.95;
}}
.swpify-hotkeys {{
  font-size: {12 if compact else 13}px;
  opacity: 0.9;
  margin-top: 2px;
}}
@media (max-width: 640px) {{
  .stColumns, .stColumn {{ display:block !important; width:100% !important; }}
}}
</style>
"""


# --------------------------- Helpers & State --------------------------- #
@dataclass
class Keys:
    token: str = "token_info"
    queue: str = "queue"
    seen: str = "seen_ids"
    swiped: str = "swiped_today"
    favourites_pl: str = "favourites_playlist"
    added_start: str = "added_filter_start"
    added_end: str = "added_filter_end"
    compact: str = "compact_desktop"
    total_liked: str = "total_liked"

K = Keys()

def init_state():
    SS = st.session_state
    SS.setdefault(K.queue, [])
    SS.setdefault(K.seen, set())
    SS.setdefault(K.swiped, 0)
    SS.setdefault(K.favourites_pl, "Favourites (Swpify)")
    SS.setdefault(K.added_start, "2020/01/01")
    SS.setdefault(K.added_end, dt.date.today().strftime("%Y/%m/%d"))
    SS.setdefault(K.compact, False)
    SS.setdefault(K.total_liked, 0)

def date_from_str(s: str) -> Optional[dt.date]:
    s = (s or "").strip().replace("-", "/")
    try:
        y, m, d = [int(x) for x in s.split("/")]
        return dt.date(y, m, d)
    except Exception:
        return None


# --------------------------- Spotify Auth --------------------------- #
def auth() -> Optional[spotipy.Spotify]:
    cid = st.secrets.get("SPOTIPY_CLIENT_ID")
    sec = st.secrets.get("SPOTIPY_CLIENT_SECRET")
    redir = st.secrets.get("SPOTIPY_REDIRECT_URI")

    if not all([cid, sec, redir]):
        st.error("Missing Spotify secrets. Set SPOTIPY_CLIENT_ID / SECRET / REDIRECT_URI in Streamlit secrets.")
        return None

    oauth = SpotifyOAuth(
        client_id=cid,
        client_secret=sec,
        redirect_uri=redir,
        scope="user-library-read user-library-modify playlist-modify-public playlist-modify-private",
        cache_path=None,
        show_dialog=False,
    )

    token_info = st.session_state.get(K.token)
    if token_info and oauth.is_token_expired(token_info):
        try:
            token_info = oauth.refresh_access_token(token_info["refresh_token"])
            st.session_state[K.token] = token_info
        except Exception:
            token_info = None

    if not token_info:
        params = dict(st.query_params)  # new API
        code = params.get("code")
        if isinstance(code, list):  # defensive (older shapes)
            code = code[0]
        if code:
            try:
                token_info = oauth.get_access_token(code, as_dict=True)  # deprecation is fine here
                st.session_state[K.token] = token_info
                st.query_params.clear()  # clean URL
            except Exception:
                token_info = None

    if not token_info:
        st.title("Swpify — Spotify Liked Songs")
        st.write("Click below to connect your Spotify account.")
        st.link_button("🔐 Log in with Spotify", oauth.get_authorize_url(), use_container_width=True)
        return None

    return spotipy.Spotify(auth=token_info["access_token"])


# --------------------------- Spotify Ops --------------------------- #
def current_user_total_likes(sp: spotipy.Spotify) -> int:
    try:
        saved = sp.current_user_saved_tracks(limit=1, offset=0)
        return saved.get("total", 0) or 0
    except Exception:
        return 0

def create_or_get_playlist(sp: spotipy.Spotify, name: str) -> str:
    user = sp.current_user()["id"]
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results.get("items", []):
            if pl["name"].strip().lower() == name.strip().lower():
                return pl["id"]
        results = sp.next(results) if results.get("next") else None
    new_pl = sp.user_playlist_create(user, name, public=False, description="Curated by Swpify")
    return new_pl["id"]

def add_to_playlist(sp: spotipy.Spotify, track_id: str, playlist_name: str):
    pid = create_or_get_playlist(sp, playlist_name)
    sp.playlist_add_items(pid, [track_id])

def remove_like(sp: spotipy.Spotify, track_id: str):
    sp.current_user_saved_tracks_delete([track_id])

def fetch_liked_with_dates(sp: spotipy.Spotify) -> List[Tuple[str, dt.date]]:
    out: List[Tuple[str, dt.date]] = []
    limit = 50
    offset = 0
    while True:
        items = sp.current_user_saved_tracks(limit=limit, offset=offset)
        for it in items.get("items", []):
            tid = (it.get("track") or {}).get("id")
            added = it.get("added_at", "")
            if tid and added:
                d = dt.datetime.fromisoformat(added.replace("Z", "+00:00")).date()
                out.append((tid, d))
        offset += limit
        if not items.get("next"):
            break
    return out


# --------------------------- UI Blocks --------------------------- #
def header():
    total = st.session_state.get(K.total_liked, 0)
    q_len = len(st.session_state[K.queue])
    done = st.session_state[K.swiped]
    denom = max(total, 1)
    pct = int(round(100 * done / denom))
    st.write(f"**{pct}% complete ({done}/{total})**")
    st.progress(done / denom)
    st.caption(f"Queue: **{q_len}** remaining • Liked total: **{total}**")

def options_block():
    with st.expander("Options", expanded=True):
        st.session_state[K.favourites_pl] = st.text_input(
            "Favourites playlist name",
            value=st.session_state[K.favourites_pl],
        )
        left, right = st.columns(2)
        with left:
            st.session_state[K.added_start] = st.text_input("Added After", value=st.session_state[K.added_start])
        with right:
            st.session_state[K.added_end] = st.text_input("Added Before", value=st.session_state[K.added_end])

        st.session_state[K.compact] = st.toggle("🖥️ Compact Desktop Mode", value=st.session_state[K.compact])

        build_btn = st.button("Build / Refresh Queue", use_container_width=True)

        logout = st.button("Log out (clear token)", use_container_width=True)
        if logout:
            st.session_state.pop(K.token, None)
            st.rerun()

    return build_btn

def render_track(sp: spotipy.Spotify, track_id: str) -> bool:
    try:
        tr = sp.track(track_id)
    except Exception:
        return False
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
    c1, c2 = st.columns([1, 1.2], vertical_alignment="top")
    with c1:
        if cover:
            st.markdown('<div class="swpify-art">', unsafe_allow_html=True)
            st.image(cover, use_column_width=True)
            st.markdown('</div>', unsafe_allow_html=True)

    with c2:
        st.subheader(name)
        st.write(f"**{artists}**")
        if album:
            st.caption(album)
        st.caption(f"Duration: {mins}:{secs:02d} • Popularity: {popularity}")
        if link:
            st.link_button("🎧 Open in Spotify", link, use_container_width=True)
        if preview:
            st.audio(preview)

    st.markdown('</div>', unsafe_allow_html=True)
    return True

def act_and_next(action: str, sp: spotipy.Spotify, track_id: str):
    if action == "fav":
        add_to_playlist(sp, track_id, st.session_state[K.favourites_pl])
    elif action == "rm":
        remove_like(sp, track_id)
    st.session_state[K.seen].add(track_id)
    st.session_state[K.swiped] += 1
    if st.session_state[K.queue] and st.session_state[K.queue][0] == track_id:
        st.session_state[K.queue].pop(0)
    st.rerun()

def inject_hotkeys():
    """
    J = Keep, K = Favourite, L = Remove (unlike)
    Robust matching: click first button whose text CONTAINS the keyword
    (emoji / extra spaces won't break it).
    """
    components.html(
        """
<script>
(function() {
  function clickByIncludes(keyword) {
    const btns = Array.from(window.parent.document.querySelectorAll('button'));
    const key = keyword.toLowerCase();
    const target = btns.find(b => (b.innerText || '').toLowerCase().includes(key));
    if (target) { target.click(); }
  }
  window.addEventListener('keydown', (e) => {
    const k = (e.key || '').toLowerCase();
    if (['input','textarea'].includes((document.activeElement || {}).tagName?.toLowerCase())) return;
    if (k === 'j') { e.preventDefault(); clickByIncludes('keep'); }
    if (k === 'k') { e.preventDefault(); clickByIncludes('favourite') || clickByIncludes('favorite'); }
    if (k === 'l') { e.preventDefault(); clickByIncludes('remove'); }
  }, true);
})();
</script>
        """,
        height=0,
    )
    st.caption("**Hotkeys:** J = Keep, K = Favourite, L = Remove", help="Use your keyboard to fly through tracks.")
    st.markdown('<div class="swpify-hotkeys"></div>', unsafe_allow_html=True)

def actions_row(sp: spotipy.Spotify, track_id: str):
    inject_hotkeys()
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

def build_controls(sp: spotipy.Spotify):
    st.markdown(base_css(st.session_state[K.compact]), unsafe_allow_html=True)
    build_clicked = options_block()

    if build_clicked:
        with st.spinner("Fetching liked songs and building your queue…"):
            all_items = fetch_liked_with_dates(sp)
            st.session_state[K.total_liked] = current_user_total_likes(sp)

            start = date_from_str(st.session_state[K.added_start])
            end = date_from_str(st.session_state[K.added_end])
            q: List[str] = []
            for tid, added in all_items:
                if start and added < start:
                    continue
                if end and added > end:
                    continue
                if tid not in st.session_state[K.seen]:
                    q.append(tid)
            st.session_state[K.queue] = q

        st.success(f"Queue ready: {len(st.session_state[K.queue])} songs")

def footer():
    q_len = len(st.session_state[K.queue])
    total = st.session_state.get(K.total_liked, 0)
    st.markdown(
        f"""
<div class="swpify-footer">
  🎵 {"No queue yet — tap <b>Build / Refresh Queue</b> above." if q_len == 0 else f"{q_len} in queue."}
  &nbsp;&nbsp; Total liked: <b>{total}</b>
</div>
""",
        unsafe_allow_html=True,
    )


# --------------------------- Main --------------------------- #
def main():
    init_state()
    sp = auth()
    if not sp:
        return

    if not st.session_state.get(K.total_liked):
        st.session_state[K.total_liked] = current_user_total_likes(sp)

    header()
    build_controls(sp)

    q = st.session_state[K.queue]
    if not q:
        footer()
        return

    current_id = q[0]
    ok = render_track(sp, current_id)
    if not ok:
        st.warning("Could not load this track; skipped.")
        st.session_state[K.seen].add(current_id)
        st.session_state[K.queue].pop(0)
        st.rerun()
        return

    actions_row(sp, current_id)
    st.divider()
    footer()


if __name__ == "__main__":
    main()
