#!/usr/bin/env python3
"""
Merge a valid Rate Your Music CSV export and public Last.fm listening data into
an Albums app backup JSON.

No third-party packages required.

Examples:
  export LASTFM_API_KEY='replace-with-your-new-key'
  python3 sync_albums_from_rym_lastfm.py \
      --albums-backup albums_backup_2026-06-23.json \
      --rym-export rym_music_export.csv \
      --lastfm-user ian_curtis_wish \
      --output albums_merged.json

Optional:
  --lastfm-top-limit 100
  --no-lastfm
  --dry-run
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import json
import os
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen


RATING_TAGS = [f"{n / 2:.1f}" for n in range(1, 11)]
LASTFM_API = "9c2959483b6ff40db3a698f40ce935d1""


def now_apple_reference_seconds() -> float:
    """Albums backup timestamps appear to use seconds since 2001-01-01 UTC."""
    epoch = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)
    return (dt.datetime.now(tz=dt.timezone.utc) - epoch).total_seconds()


def canonical(text: Any) -> str:
    text = "" if text is None else str(text)
    text = text.casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"\b(feat(?:uring)?|ft)\.?\b.*$", "", text)
    text = re.sub(
        r"\s*[\(\[\{]\s*(?:deluxe|expanded|anniversary|remaster(?:ed)?|reissue|bonus|mono|stereo|live).*?[\)\]\}]",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s*-\s*(?:single|ep)\s*$", "", text, flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_rating(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("★", "").replace("stars", "").replace("star", "").strip()
    try:
        n = float(s)
    except ValueError:
        return None
    if 0.5 <= n <= 5.0:
        rounded = round(n * 2) / 2
        return f"{rounded:.1f}"
    return None


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    preview = raw.lstrip()[:400].lower()
    if preview.startswith("<!doctype html") or "<title>just a moment" in preview or "cloudflare" in preview:
        raise ValueError(
            "The supplied RYM file is a Cloudflare HTML challenge page, not a CSV export. "
            "Re-download the export, then confirm its first line contains CSV column headers."
        )
    sniffed = csv.Sniffer().sniff(raw[:8192], delimiters=",;\t")
    reader = csv.DictReader(raw.splitlines(), dialect=sniffed)
    if not reader.fieldnames:
        raise ValueError("The RYM file has no header row.")
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    if not rows:
        raise ValueError("The RYM export has no rows.")
    return rows


def choose_column(fieldnames: Iterable[str], candidates: Iterable[str]) -> str | None:
    lower = {f.casefold().strip(): f for f in fieldnames}
    for candidate in candidates:
        if candidate.casefold() in lower:
            return lower[candidate.casefold()]
    for candidate in candidates:
        needle = candidate.casefold()
        for low, original in lower.items():
            if needle in low:
                return original
    return None


def parse_rym_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    fields = list(rows[0].keys())
    cols = {
        "artist": choose_column(fields, ["artist", "artist name", "primary artist"]),
        "album": choose_column(fields, ["release", "album", "title", "release title"]),
        "rating": choose_column(fields, ["rating", "your rating", "rating value"]),
        "review": choose_column(fields, ["review", "your review", "comment", "notes"]),
        "list": choose_column(fields, ["list", "list name", "lists"]),
        "year": choose_column(fields, ["year", "release year"]),
    }
    if not cols["artist"] or not cols["album"]:
        raise ValueError(
            "Could not identify artist and album columns. Found headers: "
            + ", ".join(repr(x) for x in fields)
        )

    parsed: list[dict[str, Any]] = []
    for row in rows:
        artist = row.get(cols["artist"], "").strip()
        album = row.get(cols["album"], "").strip()
        if not artist or not album:
            continue
        list_value = row.get(cols["list"], "") if cols["list"] else ""
        list_names = [
            item.strip()
            for item in re.split(r"\s*(?:\||;|\n)\s*", list_value)
            if item.strip()
        ]
        parsed.append(
            {
                "artist": artist,
                "album": album,
                "rating": norm_rating(row.get(cols["rating"])) if cols["rating"] else None,
                "review": row.get(cols["review"], "").strip() if cols["review"] else "",
                "lists": list_names,
                "year": row.get(cols["year"], "").strip() if cols["year"] else "",
            }
        )
    if not parsed:
        raise ValueError("No usable artist/album rows were found in the RYM export.")
    return parsed, {k: (v or "") for k, v in cols.items()}


def album_ref(album: dict[str, Any]) -> dict[str, str]:
    ref = {"uuid": album["uuid"]}
    if album.get("appleMusicID"):
        ref["appleMusicID"] = str(album["appleMusicID"])
    return ref


def build_album_index(albums: list[dict[str, Any]]) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    exact: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for album in albums:
        exact[(canonical(album.get("artist")), canonical(album.get("name")))].append(album)
    return exact, albums


def match_album(
    artist: str,
    title: str,
    exact: dict[tuple[str, str], list[dict[str, Any]]],
    albums: list[dict[str, Any]],
) -> dict[str, Any] | None:
    key = (canonical(artist), canonical(title))
    candidates = exact.get(key, [])
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        return sorted(candidates, key=lambda a: (a.get("isCompleteAlbum", False), a.get("tracksInAlbum", 0)), reverse=True)[0]

    ca, ct = key
    # Constrain fuzzy comparison by artist, then compare title.
    same_artist = [a for a in albums if canonical(a.get("artist")) == ca]
    pool = same_artist or albums
    best: tuple[float, dict[str, Any]] | None = None
    for album in pool:
        artist_score = difflib.SequenceMatcher(None, ca, canonical(album.get("artist"))).ratio()
        title_score = difflib.SequenceMatcher(None, ct, canonical(album.get("name"))).ratio()
        score = 0.42 * artist_score + 0.58 * title_score
        if best is None or score > best[0]:
            best = (score, album)
    if best and best[0] >= 0.91:
        return best[1]
    return None


def get_or_create_tag(tags: list[dict[str, Any]], name: str, hex_value: str, stamp: float) -> dict[str, Any]:
    for tag in tags:
        if tag.get("name") == name:
            tag.setdefault("albumsAndIDs", [])
            tag.setdefault("sessionIDs", [])
            return tag
    tag = {
        "name": name,
        "hex": hex_value,
        "lastModified": stamp,
        "albumsAndIDs": [],
        "sessionIDs": [],
    }
    tags.append(tag)
    return tag


def refs_as_uuid_set(tag: dict[str, Any]) -> set[str]:
    return {str(ref.get("uuid")) for ref in tag.get("albumsAndIDs", []) if ref.get("uuid")}


def add_ref(tag: dict[str, Any], album: dict[str, Any], stamp: float) -> bool:
    existing = refs_as_uuid_set(tag)
    if album["uuid"] in existing:
        return False
    tag.setdefault("albumsAndIDs", []).append(album_ref(album))
    tag["lastModified"] = stamp
    return True


def remove_ref(tag: dict[str, Any], album_uuid: str, stamp: float) -> bool:
    old = tag.get("albumsAndIDs", [])
    new = [ref for ref in old if str(ref.get("uuid")) != str(album_uuid)]
    if len(new) != len(old):
        tag["albumsAndIDs"] = new
        tag["lastModified"] = stamp
        return True
    return False


def upsert_note(notes: list[dict[str, Any]], album_uuid: str, label: str, text: str, stamp: float) -> bool:
    block = f"{label}\n{text.strip()}".strip()
    for note in notes:
        if note.get("albumID") == album_uuid:
            prior = (note.get("text") or "").rstrip()
            if block in prior:
                return False
            note["text"] = f"{prior}\n\n{block}".strip() if prior else block
            note["lastModified"] = stamp
            return True
    notes.append(
        {
            "uuid": str(uuid.uuid4()).upper(),
            "text": block,
            "lastModified": stamp,
            "albumID": album_uuid,
        }
    )
    return True


def call_lastfm(method: str, api_key: str, **params: Any) -> dict[str, Any]:
    query = {"method": method, "api_key": api_key, "format": "json", **params}
    req = Request(
        LASTFM_API + "?" + urlencode(query),
        headers={"User-Agent": "Albums-RYM-Lastfm-Local-Sync/1.0"},
    )
    with urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"Last.fm API error {payload['error']}: {payload.get('message', '')}")
    return payload


def fetch_top_albums(user: str, api_key: str, limit: int) -> list[dict[str, Any]]:
    payload = call_lastfm("user.gettopalbums", api_key, user=user, period="overall", limit=limit)
    return payload.get("topalbums", {}).get("album", [])


def fetch_loved_tracks(user: str, api_key: str, max_pages: int = 10) -> list[dict[str, Any]]:
    first = call_lastfm("user.getlovedtracks", api_key, user=user, limit=200, page=1)
    root = first.get("lovedtracks", {})
    tracks = list(root.get("track", []))
    total_pages = int(root.get("@attr", {}).get("totalPages", 1) or 1)
    for page in range(2, min(total_pages, max_pages) + 1):
        time.sleep(0.15)
        payload = call_lastfm("user.getlovedtracks", api_key, user=user, limit=200, page=page)
        tracks.extend(payload.get("lovedtracks", {}).get("track", []))
    return tracks


def top_album_match(
    item: dict[str, Any],
    exact: dict[tuple[str, str], list[dict[str, Any]]],
    albums: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return match_album(item.get("artist", {}).get("name", ""), item.get("name", ""), exact, albums)


def beloved_album_matches(
    loved_tracks: list[dict[str, Any]],
    albums: list[dict[str, Any]],
) -> set[str]:
    # Albums backup has track titles but Last.fm's loved-track response does not
    # reliably include album metadata. Match artist + track against existing album tracks.
    matched: set[str] = set()
    by_artist: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for album in albums:
        by_artist[canonical(album.get("artist"))].append(album)
    for track in loved_tracks:
        artist = canonical(track.get("artist", {}).get("name", ""))
        name = canonical(track.get("name", ""))
        if not artist or not name:
            continue
        for album in by_artist.get(artist, []):
            tracks = [canonical(x) for x in str(album.get("trackTitlesAsLowercase", "")).split("|")]
            if name in tracks:
                matched.add(album["uuid"])
    return matched


def apply_rym(
    backup: dict[str, Any],
    rym_rows: list[dict[str, Any]],
    stamp: float,
) -> dict[str, int]:
    tags = backup.setdefault("misc", {}).setdefault("tags", [])
    notes = backup["misc"].setdefault("notes", [])
    albums = backup["albumsAndArtists"]["albums"]
    exact, all_albums = build_album_index(albums)

    rating_tags = {rating: get_or_create_tag(tags, rating, "#ff7f001f", stamp) for rating in RATING_TAGS}
    list_tags: dict[str, dict[str, Any]] = {}
    matched = unmatched = rating_updates = review_updates = list_updates = 0

    for entry in rym_rows:
        album = match_album(entry["artist"], entry["album"], exact, all_albums)
        if not album:
            unmatched += 1
            continue
        matched += 1

        if entry["rating"]:
            # RYM should be the single source of truth for rating tags on matched albums.
            for rating, tag in rating_tags.items():
                if rating != entry["rating"]:
                    remove_ref(tag, album["uuid"], stamp)
            if add_ref(rating_tags[entry["rating"]], album, stamp):
                rating_updates += 1

        if entry["review"]:
            if upsert_note(notes, album["uuid"], "RYM review:", entry["review"], stamp):
                review_updates += 1

        for list_name in entry["lists"]:
            tag_name = f"RYM — {list_name}"
            tag = list_tags.setdefault(tag_name, get_or_create_tag(tags, tag_name, "#bf5af2", stamp))
            if add_ref(tag, album, stamp):
                list_updates += 1

    return {
        "rym_rows": len(rym_rows),
        "rym_matched": matched,
        "rym_unmatched": unmatched,
        "rating_updates": rating_updates,
        "review_updates": review_updates,
        "list_updates": list_updates,
    }


def apply_lastfm(
    backup: dict[str, Any],
    user: str,
    api_key: str,
    top_limit: int,
    stamp: float,
) -> dict[str, int]:
    tags = backup.setdefault("misc", {}).setdefault("tags", [])
    notes = backup["misc"].setdefault("notes", [])
    albums = backup["albumsAndArtists"]["albums"]
    exact, all_albums = build_album_index(albums)

    top_albums = fetch_top_albums(user, api_key, top_limit)
    top_25 = get_or_create_tag(tags, "Last.fm — Top 25 (All Time)", "#ff453a", stamp)
    top_100 = get_or_create_tag(tags, "Last.fm — Top 100 (All Time)", "#ff9f0a", stamp)
    lastfm_updates = top_matches = top_unmatched = note_updates = 0

    for item in top_albums:
        album = top_album_match(item, exact, all_albums)
        if not album:
            top_unmatched += 1
            continue
        top_matches += 1
        rank = int(item.get("@attr", {}).get("rank", top_matches) or top_matches)
        if rank <= 100 and add_ref(top_100, album, stamp):
            lastfm_updates += 1
        if rank <= 25 and add_ref(top_25, album, stamp):
            lastfm_updates += 1
        count = str(item.get("playcount", "")).strip()
        if count and upsert_note(notes, album["uuid"], "Last.fm all-time album scrobbles:", count, stamp):
            note_updates += 1

    loved_tracks = fetch_loved_tracks(user, api_key)
    loved_album_uuids = beloved_album_matches(loved_tracks, all_albums)
    loved_tag = get_or_create_tag(tags, "Last.fm — ♥ Loved Track", "#ff2d55", stamp)
    loved_added = 0
    albums_by_uuid = {a["uuid"]: a for a in all_albums}
    for album_uuid in loved_album_uuids:
        if add_ref(loved_tag, albums_by_uuid[album_uuid], stamp):
            loved_added += 1

    return {
        "lastfm_top_fetched": len(top_albums),
        "lastfm_top_matched": top_matches,
        "lastfm_top_unmatched": top_unmatched,
        "lastfm_tag_updates": lastfm_updates,
        "lastfm_note_updates": note_updates,
        "lastfm_loved_tracks_fetched": len(loved_tracks),
        "lastfm_loved_album_tags": loved_added,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge valid RYM and Last.fm data into an Albums app JSON backup."
    )
    parser.add_argument("--albums-backup", required=True, type=Path)
    parser.add_argument("--rym-export", type=Path)
    parser.add_argument("--lastfm-user")
    parser.add_argument("--lastfm-key", default=os.environ.get("LASTFM_API_KEY"))
    parser.add_argument("--lastfm-top-limit", type=int, default=100)
    parser.add_argument("--no-lastfm", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if not args.albums_backup.exists():
        parser.error(f"Albums backup does not exist: {args.albums_backup}")

    backup = json.loads(args.albums_backup.read_text(encoding="utf-8"))
    if not isinstance(backup.get("albumsAndArtists", {}).get("albums"), list):
        parser.error("This does not appear to be an Albums app backup JSON.")

    if not args.rym_export and args.no_lastfm:
        parser.error("Supply --rym-export and/or use Last.fm (omit --no-lastfm).")
    if not args.no_lastfm and (not args.lastfm_user or not args.lastfm_key):
        parser.error(
            "Last.fm requires --lastfm-user plus --lastfm-key or the LASTFM_API_KEY environment variable."
        )

    stamp = now_apple_reference_seconds()
    report: dict[str, Any] = {
        "albums_in_backup": len(backup["albumsAndArtists"]["albums"]),
        "timestamp_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
    }

    if args.rym_export:
        rows = read_csv_rows(args.rym_export)
        rym_rows, discovered_columns = parse_rym_rows(rows)
        report["rym_columns"] = discovered_columns
        report.update(apply_rym(backup, rym_rows, stamp))

    if not args.no_lastfm:
        report.update(
            apply_lastfm(
                backup,
                user=args.lastfm_user,
                api_key=args.lastfm_key,
                top_limit=max(1, min(args.lastfm_top_limit, 1000)),
                stamp=stamp,
            )
        )

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.dry_run:
        print("\nDry run: no JSON written.", file=sys.stderr)
        return 0

    args.output.write_text(json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote: {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
