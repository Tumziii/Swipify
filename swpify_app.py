"""
Swpify — Spotify Liked Songs (Streamlit + Spotipy)

Features
--------
• Log in with Spotify via an in-app OAuth button (no console prompts).
• Build a swipe queue from your Liked Songs (with live progress & filters).
• Actions: KEEP / REMOVE (unlike) / → Keepers / → Favourites / SKIP / UNDO.
• Shuffle, library size banner, and a small Troubleshoot panel.

Cloud secrets (Streamlit)
-------------------------
SPOTIPY_CLIENT_ID = "..."
SPOTIPY_CLIENT_SECRET = "..."
SPOTIPY_REDIRECT_URI = "https://<your-app>.streamlit.app/callback"
(Spotify Dashboard → Edit Settings → Redirect URIs must include that exact URL.)

Local .env (optional for local dev)
-----------------------------------
SPOTIPY_CLIENT_ID=...
SPOTIPY_CLIENT_SECRET=...
SPOTIPY_REDIRECT_URI=http://127.0.0.1:8501/callback
"""

from __future__ import annotations
import os
import json
import random
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import streamlit as st
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

# ----------------------------- App config ----------------------------- #
st.set_page_config(page_title="Swpify — Spotify Liked Songs", page_icon="🎧", layout="centered")

# Session keys
QUEUE = "queue_ids"                    # List[str]
SEEN = "seen_actions"                  # {track_id: {"action": str, "ts": str}}
LAST = "last_action_stack"             # list of {"id": str, "action": str, "payload": optional}
KEEPERS_ID = "keepers_playlist_id"
FAVS_ID = "favs_playlist_id"
BUILT = "queue_built_once"
SWIPED_TODAY = "swiped_today_count"
SWIPED_DATE = "swiped_today_ymd"
TOKEN_INFO = "token_info"

DEFAULT_KEEPERS = "Keepers (Swpify)"
DEFAULT_FAVS = "Favourites (Swpify)"
PAGE_SIZE = 50
SCOPES = "user-library-read user-library-modify playlist-modify-private"


# ----------------------------- Utilities ----------------------------- #
def today_ymd() -> str:
    import datetime as dt
    return dt.date.today().isoformat()

def now_iso() -> str:
    import datetime as dt
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def init_state() -> None:
    ss = st.session_state
    ss.setdefault(QUEUE, [])
    ss.setdefault(SEEN, {})
    ss.setdefault(LAST, [])
    ss.setdefault(KEEPERS_ID, None)
    ss.setdefault(FAVS_ID, None)
    ss.setdefault(BUILT, False)
    if ss.get(SWIPED_DATE) != today_ymd():
        ss[SWIPED_DATE] = today_ymd()
        ss[SWIPED_TODAY] = 0

def bump_swiped() -> None:
    ss = st.session_state
    if ss.get(SWIPED_DATE) != today_ymd():
        ss[SWIPED_DATE] = today_ymd()
        ss[SWIPED_TODAY] = 0
    ss[SWIPED_TODAY] = ss.get(SWIPED_TODAY, 0) + 1


# ----------------------------- OAuth (explicit, in-app) ----------------------------- #
def load_creds() -> Tuple[str, str, str]:
    # Prefer Streamlit Secrets; fall back to env for local dev
    cid = st.secrets.get("SPOTIPY_CLIENT_ID", os.getenv("SPOTIPY_CLIENT_ID", "")).strip()
    secret = st.secrets.get("SPOTIPY_CLIENT_SECRET", os.getenv("SPOTIPY_CLIENT_SECRET", "")).strip()
    redirect = st.secrets.get("SPOTIPY_REDIRECT_URI", os.getenv("SPOTIPY_REDIRECT_URI", "")).strip()
    if not (cid and secret and redirect):
        st.error("Missing Spotify credentials. Set SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET / SPOTIPY_REDIRECT_URI.")
        st.stop()
    return cid, secret, redirect

def ensure_spotify_client() -> Spotify:
    """
    Explicit web OAuth for Streamlit:
    - Shows 'Log in with Spotify' button
    - Handles callback ?code=...
    - Stores token in session, refreshes when needed
    """
    cid, secret, redirect = load_creds()
    auth = SpotifyOAuth(
        client_id=cid,
        client_secret=secret,
        redirect_uri=redirect,  # e.g. https://swpify-app.streamlit.app/callback
        scope=SCOPES,
        cache_path=None,        # avoid file cache on Streamlit Cloud
        open_browser=False,     # don't attempt local browser
        show_dialog=False,
    )

    # Handle callback: ?code=...
    params = st.query_params
    if "code" in params:
        code = params["code"]
        try:
            token_info = auth.get_access_token(code, as_dict=True)
        except TypeError:
            token_info = auth.get_access_token(code)  # older spotipy returns dict
        st.session_state[TOKEN_INFO] = token_info
        st.query_params.clear()
        st.rerun()

    # Token present?
    token_info = st.session_state.get(TOKEN_INFO)
    if token_info:
        try:
            if auth.is_token_expired(token_info):
                token_info = auth.refresh_access_token(token_info["refresh_token"])
                st.session_state[TOKEN_INFO] = token_info
        except Exception:
            # Token invalid/expired; force fresh login
            st.session_state.pop(TOKEN_INFO, None)
            st.warning("Session expired — please log in with Spotify again.")
            st.rerun()
        return Spotify(auth=token_info["access_token"])

    # No token yet → show login button
    auth_url = auth.get_authorize_url()
    st.title("Swpify — Spotify Liked Songs")
    st.write("Click below to connect your Spotify account.")
    st.link_button("🔐 Log in with Spotify", auth_url, use_container_width=True)
    st.stop()


# ----------------------------- Spotify helpers ----------------------------- #
def fast_liked_total(sp: Spotify) -> int:
    try:
        return int(sp.current_user_saved_tracks(limit=1).get("total", 0))
    except Exception:
        return 0

def ensure_playlist(sp: Spotify, name: str) -> str:
    me = sp.current_user()["id"]
    results = sp.current_user_playlists(limit=50)
    while results:
        for pl in results["items"]:
            if pl["name"] == name and pl["owner"]["id"] == me:
                return pl["id"]
        results = sp.next(results) if results.get("next") else None
    created = sp.user_playlist_create(me, name, public=False, description="Created by Swpify")
    return created["id"]

def fetch_all_liked_ids(sp: Spotify, status=None, bar=None) -> List[str]:
    ids: List[str] = []
    offset = 0
    total = None
    while True:
        batch = sp.current_user_saved_tracks(limit=PAGE_SIZE, offset=offset)
        if total is None:
            total = max(1, batch.get("total", 0))
        items = batch.get("items", [])
        if not items:
            break
        ids.extend(t["track"]["id"] for t in items if t.get("track") and t["track"].get("id"))
        offset += len(items)
        if status:
            status.write(f"Loading liked songs… **{min(offset, total):,}/{total:,}**")
        if bar:
            bar.progress(min(offset / total, 1.0))
        if offset >= total:
            break
    return ids

def apply_filters(sp: Spotify, ids: List[str], term: str, artist: str, year: str, status=None, bar=None) -> List[str]:
    term = (term or "").strip().lower()
    artist = (artist or "").strip().lower()
    year = (year or "").strip()
    if not (term or artist or year):
        return ids

    out: List[str] = []
    chunks = [ids[i:i+50] for i in range(0, len(ids), 50)]
    total = max(1, len(chunks))
    for i, chunk in enumerate(chunks, 1):
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
            if artist:
                names = ", ".join(a.get("name","") for a in tr.get("artists", []))
                ok &= (artist in names.lower())
            if year:
                y = (tr.get("album", {}).get("release_date","") or "")[:4]
                ok &= (y == year)
            if ok and tr.get("id"):
                out.append(tr["id"])
        if status:
            status.write(f"Filtering… **{i}/{total}**")
        if bar:
            bar.progress(min(i / total, 1.0))
    return out

def build_queue(sp: Spotify, *, shuffle: bool, term: str, artist: str, year: str) -> int:
    ss = st.session_state
    status = st.empty()
    bar = st.progress(0.0)
    with st.spinner("Building your queue…"):
        liked = fetch_all_liked_ids(sp, status=status, bar=bar)
        liked = [tid for tid in liked if tid not in ss[SEEN]]  # drop already processed
        if term or artist or year:
            status.write("Filtering…")
            bar.progress(0.0)
            liked = apply_filters(sp, liked, term, artist, year, status=status, bar=bar)
        if shuffle:
            random.shuffle(liked)
        ss[QUEUE] = liked
        ss[BUILT] = True
    status.empty(); bar.empty()
    return len(liked)

def get_track(sp: Spotify, tid: str) -> Optional[dict]:
    try:
        return sp.track(tid)
    except Exception:
        return None

def open_url(tr: dict) -> str:
    return tr.get("external_urls", {}).get("spotify", "#")

def unlike(sp: Spotify, tid: str): sp.current_user_saved_tracks_delete([tid])
def relike(sp: Spotify, tid: str): sp.current_user_saved_tracks_add([tid])
def add_to_playlist(sp: Spotify, pid: str, tid: str): sp.playlist_add_items(pid, [tid])
def remove_from_playlist(sp: Spotify, pid: str, tid: str): sp.playlist_remove_all_occurrences_of_items(pid, [tid])


# ----------------------------- UI blocks ----------------------------- #
def header(sp: Spotify):
    st.title("Swpify — Spotify Liked Songs")
    count = fast_liked_total(sp)
    st.caption(f"Swipe to keep, remove, or file songs to **Keepers / Favourites**. "
               f"Library size: **{count:,}** liked songs.")
    st.sidebar.metric("Swiped today", st.session_state.get(SWIPED_TODAY, 0))

def options(sp: Spotify):
    ss = st.session_state
    with st.expander("⚙️ Options", expanded=not ss[BUILT]):
        shuffle = st.checkbox("Shuffle order", value=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            term = st.text_input("Filter by text (title/artist/album)")
        with c2:
            artist = st.text_input("Filter by artist")
        with c3:
            year = st.text_input("Filter by year (YYYY)")

        keepers_name = st.text_input("Keepers playlist name", value=DEFAULT_KEEPERS)
        favs_name = st.text_input("Favourites playlist name", value=DEFAULT_FAVS)

        if st.button("Build / Refresh queue", type="primary"):
            if ss.get(KEEPERS_ID) is None:
                ss[KEEPERS_ID] = ensure_playlist(sp, keepers_name or DEFAULT_KEEPERS)
            if ss.get(FAVS_ID) is None:
                ss[FAVS_ID] = ensure_playlist(sp, favs_name or DEFAULT_FAVS)

            n = build_queue(sp, shuffle=shuffle, term=term, artist=artist, year=year)
            if n:
                st.success(f"Queue ready — {n:,} songs to swipe.")
            else:
                st.warning("No songs queued. (All processed or filters too strict.)")

def card(tr: dict):
    name = tr.get("name") or "Unknown"
    artists = ", ".join(a.get("name","") for a in tr.get("artists", []))
    album = (tr.get("album", {}) or {}).get("name", "") or ""
    images = (tr.get("album", {}) or {}).get("images", [])
    cover = images[0]["url"] if images else None
    preview = tr.get("preview_url")

    left, right = st.columns([1, 2], vertical_alignment="top")
    with left:
        if cover: st.image(cover, use_container_width=True)
        else: st.caption("(No artwork)")
    with right:
        st.subheader(name)
        st.write(f"**{artists}**")
        if album: st.caption(album)
        if preview: st.audio(preview)
        else: st.caption("No 30s preview available.")
        st.link_button("Open in Spotify", open_url(tr))

def actions(sp: Spotify, tid: str):
    ss = st.session_state
    colk, cold, colf, colp, cols, colu = st.columns(6)
    keep = colk.button("✅ Keep", use_container_width=True)
    remove = cold.button("🗑️ Remove", use_container_width=True, type="primary")
    fav = colf.button("⭐ → Favourites", use_container_width=True)
    keepers = colp.button("📁 → Keepers", use_container_width=True)
    skip = cols.button("⏭️ Skip", use_container_width=True)
    undo = colu.button("↩️ Undo", use_container_width=True, disabled=(len(ss[LAST]) == 0))

    if keep:
        ss[SEEN][tid] = {"action": "keep", "ts": now_iso()}
        ss[LAST].append({"id": tid, "action": "keep"})
        ss[QUEUE].pop(0); bump_swiped(); st.rerun()

    if remove:
        try:
            unlike(sp, tid)
            ss[SEEN][tid] = {"action": "remove", "ts": now_iso()}
            ss[LAST].append({"id": tid, "action": "remove"})
            ss[QUEUE].pop(0); bump_swiped(); st.rerun()
        except Exception as e:
            st.error(f"Failed to remove: {e}")

    if fav:
        try:
            add_to_playlist(sp, ss[FAVS_ID], tid)
            ss[SEEN][tid] = {"action": "favourite", "ts": now_iso()}
            ss[LAST].append({"id": tid, "action": "favourite", "payload": ss[FAVS_ID]})
            ss[QUEUE].pop(0); bump_swiped(); st.rerun()
        except Exception as e:
            st.error(f"Failed to add to Favourites: {e}")

    if keepers:
        try:
            add_to_playlist(sp, ss[KEEPERS_ID], tid)
            ss[SEEN][tid] = {"action": "keepers", "ts": now_iso()}
            ss[LAST].append({"id": tid, "action": "keepers", "payload": ss[KEEPERS_ID]})
            ss[QUEUE].pop(0); bump_swiped(); st.rerun()
        except Exception as e:
            st.error(f"Failed to add to Keepers: {e}")

    if skip:
        ss[SEEN][tid] = {"action": "skip", "ts": now_iso()}
        ss[QUEUE].append(ss[QUEUE].pop(0))
        st.rerun()

    if undo:
        perform_undo(sp)

def perform_undo(sp: Spotify):
    ss = st.session_state
    if not ss[LAST]:
        return
    last = ss[LAST].pop()
    tid = last["id"]; act = last["action"]
    try:
        if act == "remove":
            relike(sp, tid)
        elif act in ("favourite", "keepers"):
            pid = last.get("payload")
            if pid:
                remove_from_playlist(sp, pid, tid)
        ss[SEEN].pop(tid, None)
        if tid in ss[QUEUE]: ss[QUEUE].remove(tid)
        ss[QUEUE].insert(0, tid)
        st.success("Undone.")
        st.rerun()
    except Exception as e:
        st.error(f"Undo failed: {e}")

def troubleshoot(sp: Spotify):
    with st.expander("🔧 Troubleshoot"):
        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button("Test Spotify auth"):
                try:
                    me = sp.current_user()
                    st.success(f"Auth OK — **{me.get('display_name', me.get('id'))}**")
                except Exception as e:
                    st.error("Auth failed."); st.exception(e)
        with c2:
            if st.button("Force re-auth (clear session token)"):
                st.session_state.pop(TOKEN_INFO, None)
                st.success("Session token cleared. Reload the page and log in again.")
        with c3:
            if st.button("Quick fetch 1 track"):
                try:
                    batch = sp.current_user_saved_tracks(limit=1)
                    total = batch.get("total", 0)
                    item = (batch.get("items") or [{}])[0]
                    tr = (item.get("track") or {})
                    name = tr.get("name", "(no name)")
                    artists = ", ".join(a.get("name","") for a in tr.get("artists", []))
                    st.success(f"API OK — Total ≈ {total:,}. First: **{name}** — {artists}")
                except Exception as e:
                    st.error("Fetch failed."); st.exception(e)


# ----------------------------- Main ----------------------------- #
def main():
    init_state()

    # In-app OAuth (no console prompt), returns ready Spotify client
    sp = ensure_spotify_client()

    header(sp)
    options(sp)

    ss = st.session_state
    if not ss[QUEUE]:
        st.info("No queue yet — use **Build / Refresh queue** above.")
        troubleshoot(sp)
        return

    tid = ss[QUEUE][0]
    tr = get_track(sp, tid)
    if not tr:
        ss[QUEUE].pop(0)
        st.warning("Could not load this track; skipped.")
        st.rerun()

    card(tr)
    actions(sp, tid)

    st.divider()
    remaining = len(ss[QUEUE])
    processed = len(ss[SEEN]) if ss.get(SEEN) else 0
    st.caption(f"Remaining: **{remaining}** • Decisions this session: **{processed}**")


if __name__ == "__main__":
    main()
