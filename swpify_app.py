"""
Swpify — Spotify Liked Songs
Swipe to keep, remove, or file songs to Keepers / Favourites playlists.
Built with Streamlit + Spotipy.

Requirements (requirements.txt):
  streamlit==1.38.0
  spotipy==2.23.0
  pandas==2.2.2
  requests==2.31.0
  python-dotenv==1.0.1
  Pillow==10.3.0
"""

from __future__ import annotations
import os
import re
import json
import random
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# --------------------------- App config --------------------------- #
st.set_page_config(
    page_title="Swpify — Spotify Liked Songs",
    page_icon="🎧",
    layout="centered",
)

# Session keys used throughout
SS = st.session_state
KEYS = dict(
    sp="sp_client",
    queue="queue_ids",
    seen="seen_actions",
    keepers="keepers_pl_id",
    favs="favs_pl_id",
    last="last_actions_stack",
    count="swiped_today_count",
    count_date="swiped_today_date",
    built="queue_built_once",
)

# Playlists and scopes
DEFAULT_KEEPERS = "Keepers (Swpify)"
DEFAULT_FAVS = "Favourites (Swpify)"
PAGE_SIZE = 50
SCOPES = "user-library-read user-library-modify playlist-modify-private"

# --------------------------- Helpers --------------------------- #
def _today_str() -> str:
    return dt.date.today().isoformat()

def init_state():
    SS.setdefault(KEYS.queue, [])
    SS.setdefault(KEYS.seen, {})               # track_id -> {"action": ...}
    SS.setdefault(KEYS.last, [])               # stack of {"id", "action", "payload"}
    SS.setdefault(KEYS.keepers, None)
    SS.setdefault(KEYS.favs, None)
    SS.setdefault(KEYS.built, False)
    # swipe counter
    if SS.get(KEYS.count_date) != _today_str():
        SS[KEYS.count_date] = _today_str()
        SS[KEYS.count] = 0

def bump_swipe_counter():
    if SS.get(KEYS.count_date) != _today_str():
        SS[KEYS.count_date] = _today_str()
        SS[KEYS.count] = 0
    SS[KEYS.count] += 1

def read_secrets() -> Tuple[str, str, str]:
    """Read credentials from Streamlit secrets."""
    try:
        cid = st.secrets["SPOTIPY_CLIENT_ID"].strip()
        secret = st.secrets["SPOTIPY_CLIENT_SECRET"].strip()
        redirect = st.secrets["SPOTIPY_REDIRECT_URI"].strip()
        return cid, secret, redirect
    except Exception:
        st.error(
            "Missing Spotify credentials in **Secrets**. "
            "You must set `SPOTIPY_CLIENT_ID`, `SPOTIPY_CLIENT_SECRET`, `SPOTIPY_REDIRECT_URI`."
        )
        st.stop()

@st.cache_resource(show_spinner=False)
def get_spotify_client(cache_key: str = "swpify_token_cache") -> Spotify:
    cid, secret, redirect = read_secrets()
    auth = SpotifyOAuth(
        client_id=cid,
        client_secret=secret,
        redirect_uri=redirect,
        scope=SCOPES,
        cache_path=str(Path(f".cache_{cache_key}")),
        show_dialog=False,
    )
    return Spotify(auth_manager=auth)

def fast_liked_total(sp: Spotify) -> int:
    try:
        batch = sp.current_user_saved_tracks(limit=1)
        return int(batch.get("total", 0))
    except Exception:
        return 0

def fetch_all_liked_ids(sp: Spotify) -> List[str]:
    ids: List[str] = []
    offset = 0
    total = fast_liked_total(sp)
    progress = st.progress(0.0, text="Loading liked songs …")
    while True:
        batch = sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        items = batch.get("items", [])
        if not items:
            break
        for it in items:
            tr = it.get("track")
            if tr and tr.get("id"):
                ids.append(tr["id"])
        offset += len(items)
        if total:
            progress.progress(min(offset / total, 1.0), text=f"Loaded {offset}/{total} …")
        if offset >= batch.get("total", 0):
            break
    progress.empty()
    return ids

def ensure_playlist(sp: Spotify, name: str) -> str:
    """Get or create a private playlist with given name belonging to the current user."""
    me = sp.current_user()["id"]
    # List existing
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results["items"]:
            if pl["name"] == name and pl["owner"]["id"] == me:
                return pl["id"]
        results = sp.next(results) if results.get("next") else None
    # Create
    created = sp.user_playlist_create(
        me, name, public=False, description="Created by Swpify"
    )
    return created["id"]

def refill_queue(sp: Spotify, *, shuffle: bool, filters: Dict, keepers_name: str, favs_name: str):
    # Ensure helper playlists exist once at build time
    if SS.get(KEYS.keepers) is None:
        SS[KEYS.keepers] = ensure_playlist(sp, keepers_name or DEFAULT_KEEPERS)
    if SS.get(KEYS.favs) is None:
        SS[KEYS.favs] = ensure_playlist(sp, favs_name or DEFAULT_FAVS)

    liked_ids = fetch_all_liked_ids(sp)
    # Remove already handled
    liked_ids = [tid for tid in liked_ids if tid not in SS[KEYS.seen]]

    # Filters — evaluate in batches to avoid rate limits
    term = (filters.get("term") or "").strip().lower()
    artf = (filters.get("artist") or "").strip().lower()
    yearf = (filters.get("year") or "").strip()

    if term or artf or yearf:
        filtered: List[str] = []
        for i in stqdm(range(0, len(liked_ids), 50), desc="Filtering tracks …"):
            chunk = liked_ids[i : i + 50]
            tracks = sp.tracks(chunk)["tracks"]
            for tr in tracks:
                if not tr:
                    continue
                ok = True
                if term:
                    blob = " ".join([
                        tr.get("name") or "",
                        ", ".join(a.get("name","") for a in tr.get("artists", [])),
                        (tr.get("album", {}) or {}).get("name", "") or "",
                    ]).lower()
                    ok &= (term in blob)
                if artf:
                    artists = ", ".join(a.get("name","") for a in tr.get("artists", []))
                    ok &= (artf in artists.lower())
                if yearf:
                    y = (tr.get("album", {}).get("release_date","") or "")[:4]
                    ok &= (y == yearf)
                if ok and tr.get("id"):
                    filtered.append(tr["id"])
        liked_ids = filtered

    if shuffle:
        random.shuffle(liked_ids)

    SS[KEYS.queue] = liked_ids
    SS[KEYS.built] = True

# Light inline tqdm for Streamlit
def stqdm(iterable, desc="Working …"):
    placeholder = st.empty()
    total = len(iterable)
    for idx, x in enumerate(iterable, 1):
        placeholder.progress(idx / total, text=f"{desc} ({idx}/{total})")
        yield x
    placeholder.empty()

def get_track(sp: Spotify, track_id: str) -> Optional[dict]:
    try:
        return sp.track(track_id)
    except Exception:
        return None

def open_in_spotify_url(tr: dict) -> str:
    return tr.get("external_urls", {}).get("spotify", "#")

def action_unlike(sp: Spotify, track_id: str):
    sp.current_user_saved_tracks_delete([track_id])

def action_like(sp: Spotify, track_id: str):
    sp.current_user_saved_tracks_add([track_id])

def action_add_to_playlist(sp: Spotify, playlist_id: str, track_id: str):
    sp.playlist_add_items(playlist_id, [track_id])

def action_remove_from_playlist(sp: Spotify, playlist_id: str, track_id: str):
    sp.playlist_remove_all_occurrences_of_items(playlist_id, [track_id])

# --------------------------- UI parts --------------------------- #
def sidebar_stats():
    st.sidebar.metric("Swiped today", SS.get(KEYS.count, 0))

def header(sp: Spotify):
    st.title("Swpify — Spotify Liked Songs")
    total = fast_liked_total(sp)
    st.caption(
        f"Swipe to keep, remove, or file songs to **Keepers / Favourites**. "
        f"You currently have approximately **{total}** Liked Songs."
    )

def render_options(sp: Spotify):
    with st.expander("⚙️ Options", expanded=True if not SS[KEYS.built] else False):
        shuffle = st.checkbox("Shuffle order", value=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            term = st.text_input("Filter by text (title/artist/album)")
        with c2:
            artist = st.text_input("Filter by artist")
        with c3:
            year = st.text_input("Filter by year (YYYY)", help="Exact year, e.g. 2016")

        k_name = st.text_input("Keepers playlist name", value=DEFAULT_KEEPERS)
        f_name = st.text_input("Favourites playlist name", value=DEFAULT_FAVS)

        if st.button("Build/Refresh Queue", type="primary"):
            with st.spinner("Building your queue …"):
                refill_queue(
                    sp,
                    shuffle=shuffle,
                    filters={"term": term, "artist": artist, "year": year},
                    keepers_name=k_name,
                    favs_name=f_name,
                )
            st.success(f"Queue ready: {len(SS[KEYS.queue])} tracks")

def render_track_card(tr: dict):
    name = tr.get("name") or "Unknown"
    artists = ", ".join(a.get("name","") for a in tr.get("artists", []))
    album = (tr.get("album", {}) or {}).get("name", "") or ""
    images = (tr.get("album", {}) or {}).get("images", [])
    cover = images[0]["url"] if images else None
    preview = tr.get("preview_url")

    col1, col2 = st.columns([1, 2], vertical_alignment="top")
    with col1:
        if cover:
            st.image(cover, use_container_width=True)
        else:
            st.caption("(No artwork)")
    with col2:
        st.subheader(name)
        st.write(f"**{artists}**")
        if album:
            st.caption(album)
        if preview:
            st.audio(preview)
        else:
            st.caption("No 30s preview available.")
        st.link_button("Open in Spotify", open_in_spotify_url(tr))

def draw_buttons(sp: Spotify, current_id: str, current_tr: dict):
    colk, colr, colf, colp, colu = st.columns(5)
    keep = colk.button("✅ Keep", use_container_width=True)
    remove = colr.button("🗑️ Remove (unlike)", use_container_width=True, type="primary")
    fav = colf.button("⭐ Favourite → Favourites", use_container_width=True)
    keepers = colp.button("📁 File → Keepers", use_container_width=True)
    undo = colu.button("↩️ Undo", use_container_width=True, disabled=(len(SS[KEYS.last]) == 0))

    if keep:
        SS[KEYS.seen][current_id] = {"action": "keep"}
        SS[KEYS.last].append({"id": current_id, "action": "keep"})
        SS[KEYS.queue].pop(0)
        bump_swipe_counter()
        st.rerun()

    if remove:
        try:
            action_unlike(sp, current_id)
            SS[KEYS.seen][current_id] = {"action": "remove"}
            SS[KEYS.last].append({"id": current_id, "action": "remove"})
            SS[KEYS.queue].pop(0)
            bump_swipe_counter()
            st.rerun()
        except Exception as e:
            st.error(f"Failed to remove: {e}")

    if fav:
        try:
            action_add_to_playlist(sp, SS[KEYS.favs], current_id)
            SS[KEYS.seen][current_id] = {"action": "favourite"}
            SS[KEYS.last].append({"id": current_id, "action": "favourite", "payload": SS[KEYS.favs]})
            SS[KEYS.queue].pop(0)
            bump_swipe_counter()
            st.rerun()
        except Exception as e:
            st.error(f"Failed to add to Favourites: {e}")

    if keepers:
        try:
            action_add_to_playlist(sp, SS[KEYS.keepers], current_id)
            SS[KEYS.seen][current_id] = {"action": "keepers"}
            SS[KEYS.last].append({"id": current_id, "action": "keepers", "payload": SS[KEYS.keepers]})
            SS[KEYS.queue].pop(0)
            bump_swipe_counter()
            st.rerun()
        except Exception as e:
            st.error(f"Failed to add to Keepers: {e}")

    if undo:
        perform_undo(sp)

def perform_undo(sp: Spotify):
    if not SS[KEYS.last]:
        return
    action = SS[KEYS.last].pop()
    tid = action["id"]
    what = action["action"]
    try:
        if what == "remove":
            action_like(sp, tid)
        elif what in ("favourite", "keepers"):
            pl = action.get("payload")
            if pl:
                action_remove_from_playlist(sp, pl, tid)
        # put back to front of queue for immediate review
        SS[KEYS.queue].insert(0, tid)
        SS[KEYS.seen].pop(tid, None)
        st.success("Undone.")
        st.rerun()
    except Exception as e:
        st.error(f"Undo failed: {e}")

def troubleshoot(sp: Spotify):
    with st.expander("🔧 Troubleshoot"):
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Test Spotify auth"):
                me = sp.current_user()
                st.success(f"Authenticated as **{me.get('display_name', me.get('id'))}**")
        with c2:
            if st.button("Force re-auth (clear token)"):
                try:
                    # remove cache file and rerun
                    for p in Path(".").glob(".cache_*"):
                        p.unlink(missing_ok=True)
                    st.success("Token cache cleared. Please refresh the page.")
                except Exception as e:
                    st.error(f"Could not clear cache: {e}")
        with c3:
            if st.button("Reset local state"):
                for k in list(SS.keys()):
                    del SS[k]
                st.success("State cleared. Refresh the page.")

# --------------------------- Main --------------------------- #
def main():
    init_state()
    sidebar_stats()

    # Spotify client
    sp = get_spotify_client()
    header(sp)
    render_options(sp)

    # If no queue, stop here
    if not SS[KEYS.queue]:
        st.info("No queue yet — click **Build/Refresh Queue** above to begin.")
        troubleshoot(sp)
        return

    # Current track
    current_id = SS[KEYS.queue][0]
    tr = get_track(sp, current_id)
    if not tr:
        # skip invalid
        SS[KEYS.queue].pop(0)
        st.warning("This track could not be loaded and was skipped.")
        st.rerun()

    render_track_card(tr)
    draw_buttons(sp, current_id, tr)

    st.divider()
    remaining = len(SS[KEYS.queue])
    processed = len(SS[KEYS.seen]) if SS.get(KEYS.seen) else 0
    st.caption(f"Remaining: **{remaining}** • Decisions this session: **{processed}**")
    troubleshoot(sp)

if __name__ == "__main__":
    main()
