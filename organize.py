#!/usr/bin/env python3
"""
TV Show Library Organizer
Output format: Show Name (Year)/Season XX/Show Name - SXXEXX - Episode Title.ext
"""

import os
import re
import sys
import json
import shutil
import logging
import argparse
import requests
from pathlib import Path
from difflib import SequenceMatcher

# ── Constants ─────────────────────────────────────────────────────────────────
TMDB_BASE = "https://api.themoviedb.org/3"
STATE_FILE = Path.home() / ".tv-organizer-state.json"
CACHE_FILE = Path.home() / ".tv-organizer-cache.json"
LOG_FILE   = Path.home() / "tv-organizer.log"

VIDEO_EXTS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.wmv'}

SKIP_DIRS = {
    'extras', 'featurettes', 'behind the scenes', 'deleted scenes',
    'trailers', 'shorts', 'nc', 'ncop', 'nced', 'other',
    'bonus', 'bdmenu', 'sample', 'cm', 'creditless',
}

# ── Name cleaning ─────────────────────────────────────────────────────────────
# Strip leading group tag: [Group] or (Group)
RE_GROUP_PREFIX = re.compile(r'^\[[\w\s\-]+\]\s*|^\([\w]+\)\s*')

# Patterns that mark END of show name (stop here, strip everything after)
NAME_STOP_PATTERNS = [
    re.compile(r'\s+S\d{1,2}(?:[\+\-]S?\d{1,2})*(?:\s|$)', re.I),  # S01, S01-S04, S1+S2
    re.compile(r'\s+Season\s*\d', re.I),
    re.compile(r'\s+\(Season', re.I),
    re.compile(r'\s+\{Complete\}', re.I),
    re.compile(r'\s+Complete\s+Series', re.I),
    re.compile(r'\s+\d{3,4}p\b'),                   # 1080p
    re.compile(r'\s+(?:BluRay|BDRip|WEB-?DL|WEBRip|HDTV|DVDRip|REMUX)\b', re.I),
    re.compile(r'\s+(?:HEVC|x265|x264|AVC|H\.264|H\.265)\b', re.I),
]

# Trailing bracket junk to strip after stop patterns
RE_TRAILING_BRACKET = re.compile(r'\s*[\[\({][^\]\)}{]*[\]\)}]\s*$')


def clean_name(raw: str) -> str:
    name = raw.strip()

    # Strip leading [Group]
    name = RE_GROUP_PREFIX.sub('', name)

    # Dot-separated filename? replace dots with spaces
    if name.count('.') > max(name.count(' '), 2):
        name = name.replace('.', ' ')

    # Replace underscores between words
    name = name.replace('_', ' ')

    # Stop at quality/season markers
    for pat in NAME_STOP_PATTERNS:
        m = pat.search(name)
        if m:
            name = name[:m.start()]
            break

    # Strip trailing brackets (quality tags)
    for _ in range(3):
        prev = name
        name = RE_TRAILING_BRACKET.sub('', name).strip()
        if name == prev:
            break

    return name.strip(' -_[](){}')


# ── Episode parsing ───────────────────────────────────────────────────────────
RE_EP_SXXEXX     = re.compile(r'[Ss](\d{1,2})\s*[Ee](\d{2,3})')               # S01E01 or S01 E01
RE_EP_MULTI      = re.compile(r'[Ee](\d{2,3})[-–][Ee]\d{2,3}')                # E01-E03 → take first
RE_EP_NXNN       = re.compile(r'(?<!\d)\[?(\d{1,2})x(\d{2,3})\]?(?!\d)')      # [1x07], not 1280x720
RE_EP_SXXOVA     = re.compile(r'[Ss](\d{1,2})(?:OVA|OAD)(\d{2,3})', re.I)    # S01OVA01/S01OAD01 → S00
RE_EP_SXXSP      = re.compile(r'[Ss](\d{1,2})SP(\d{2,3})', re.I)              # S01SP01 → S00
RE_EP_BARE_SP    = re.compile(r'(?<![A-Za-z])SP(\d{2,3})(?![A-Za-z])', re.I)  # SP00, SP01 → S00
RE_EP_OVA_INLINE = re.compile(r'\bOVA\b\s*[-–]\s*(\d{2,3})', re.I)            # OVA - 01 → S00
RE_EP_OVA_NUM    = re.compile(r'(?<![A-Za-z])OVA(\d{1,3})(?![A-Za-z])', re.I) # OVA1, OVA2 → S00
RE_EP_EPISODE    = re.compile(r'\bEpisode[\s.]*(\d{1,3})\b', re.I)            # Episode 01 / Episode.01
RE_EP_EPWORD     = re.compile(r'(?<![A-Za-z])Ep(\d{2,3})(?![A-Za-z])', re.I)  # Ep01 (Exiled-Destiny)
RE_EP_EONLY      = re.compile(r'(?<!\w)[Ee](\d{2,3})(?!\w)')                  # E01 without S prefix
# Allow: - 01 [hash], _01_(quality), - 01$, - 01 - Title, 01 Prologue, 01. Title
RE_EP_BARE       = re.compile(r'(?:^|[\s\-_])(\d{2,3})(?:[\s_]*[\[\(]|\s+-|\s*$|\s+(?=[A-Za-z])|\.\s)')
RE_HALF_EP       = re.compile(r'(\d{1,3})\.5\b')                               # 7.5, 12.5 → treat floor as ep
RE_EP_SEASONED3  = re.compile(r'(?:^|[\s\-_])([2-9])(\d{2})(?:[\s_]*[\[\(]|\s+-|\s*$|\s+(?=[A-Za-z])|\.\s)')  # 201→S2E01
RE_VERSION       = re.compile(r'v\d+$', re.I)                                  # strip trailing v2, v3
RE_HASH          = re.compile(r'\s*[\[\(][0-9A-Fa-f]{6,8}[\]\)]\s*')          # [A1B2C3D4]

# Files to skip regardless of directory (creditless OP/ED, menus, extras, promos)
RE_SKIP_FILE = re.compile(
    r'(?<![A-Za-z])NC(?:OP|ED|OED)\w*'        # NCOP, NCED, NCEDv01, NCOP01b
    r'|Main[\W_]?Menu'
    r'|\bCreditless\b'
    r'|[-–_]\s*(?:Opening|Ending)\s*\d*\s*$'  # "- Opening 1" at end (not mid-title)
    r'|\bTrailer\b'
    r'|\bPromo\b'
    r'|(?<![A-Za-z])(?:OP|ED)(?:v\d+|[\s_]+\d+)?[\s_]*$'  # OP, OPv2, OP 2, ED 1 / _ED_ at end
    r'|(?<![A-Za-z])(?:OP|ED)[\s_]*\d*[\s_]*[\[\(]',      # OP/ED followed by bracket (with _ ok)
    re.I
)


def is_skip_file(filename: str) -> bool:
    return bool(RE_SKIP_FILE.search(Path(filename).stem))


def _prep_stem(filename: str) -> str:
    """Strip hash, version suffix, and normalise half-episodes from stem."""
    stem = Path(filename).stem
    stem = RE_HASH.sub('', stem)
    # 7.5 / 12.5 → treat as ep 07 / 12 (half-episodes), zero-pad to 2 digits
    stem = RE_HALF_EP.sub(lambda m: str(int(m.group(1))).zfill(2), stem)
    # strip trailing version tag: "01v2" → "01", "02v3" → "02"
    stem = re.sub(r'(\d+)v\d+\b', r'\1', stem, flags=re.I)
    return stem


def parse_episode(filename: str, season_hint: int = None):
    """Return (season, episode) or None."""
    stem = _prep_stem(filename)

    # Multi-episode range E01-E03 → first ep (check before SxxExx so we capture season too)
    mm = RE_EP_MULTI.search(stem)
    m = RE_EP_SXXEXX.search(stem)
    if m:
        ep = int(mm.group(1)) if mm else int(m.group(2))
        return int(m.group(1)), ep

    m = RE_EP_NXNN.search(stem)
    if m:
        return int(m.group(1)), int(m.group(2))

    # S01OVA01, S01OAD01, S01SP01 → season 0
    m = RE_EP_SXXOVA.search(stem)
    if m:
        return 0, int(m.group(2))
    m = RE_EP_SXXSP.search(stem)
    if m:
        return 0, int(m.group(2))

    # Bare SP00 / SP01 (no season prefix) → season 0
    m = RE_EP_BARE_SP.search(stem)
    if m:
        ep = int(m.group(1))
        if 0 <= ep <= 200:
            return 0, ep

    # OVA - 01 inline → season 0
    m = RE_EP_OVA_INLINE.search(stem)
    if m:
        ep = int(m.group(1))
        if 1 <= ep <= 200:
            return 0, ep

    # OVA1 / OVA2 attached digit → season 0
    m = RE_EP_OVA_NUM.search(stem)
    if m:
        ep = int(m.group(1))
        if 1 <= ep <= 200:
            return 0, ep

    if season_hint is not None:
        # "Episode NN" / "Episode.NN"
        m = RE_EP_EPISODE.search(stem)
        if m:
            ep = int(m.group(1))
            if 0 <= ep <= 200:
                return season_hint, ep

        # "Ep01" short form (Exiled-Destiny style)
        m = RE_EP_EPWORD.search(stem)
        if m:
            ep = int(m.group(1))
            if 0 <= ep <= 200:
                return season_hint, ep

        # Bare E01 without S prefix (Prison School style)
        m = RE_EP_EONLY.search(stem)
        if m:
            ep = int(m.group(1))
            if 0 <= ep <= 200:
                return season_hint, ep

        # Season-encoded 3-digit number: 201→S2E01, 312→S3E12
        m = RE_EP_SEASONED3.search(stem)
        if m:
            return int(m.group(1)), int(m.group(2))

        # Bare episode number
        m = RE_EP_BARE.search(stem)
        if m:
            ep = int(m.group(1))
            if 0 <= ep <= 200:
                return season_hint, ep

    return None


RE_SEASON_DIR = re.compile(r'(?:Season\s*|[Ss])(\d{1,2})', re.I)
RE_OVA_DIR    = re.compile(r'\b(OVA|OAD|Special|SP|Bonus)\b', re.I)
RE_OVA_FILE   = re.compile(r'\b(OVA|OAD|Special|SP)\b', re.I)


def detect_season(dirname: str) -> int | None:
    """Extract season number from a directory name. OVA/Special dirs → 0."""
    m = RE_SEASON_DIR.search(dirname)
    if m:
        return int(m.group(1))
    # bare s01
    m = re.match(r'^s(\d{2})$', dirname, re.I)
    if m:
        return int(m.group(1))
    # OVA/Special dir with no season number → Season 0
    if RE_OVA_DIR.search(dirname):
        return 0
    # "01. Show Name" or "1 Show Name" numbered-season prefix
    m = re.match(r'^0*(\d{1,2})[.\s]', dirname)
    if m:
        return int(m.group(1))
    return None


def is_ova_file(filename: str) -> bool:
    return bool(RE_OVA_FILE.search(Path(filename).stem))


# ── TMDB client ───────────────────────────────────────────────────────────────
class TMDB:
    def __init__(self, token: str, cache_file: Path):
        self.token = token
        self.session = requests.Session()
        self.session.headers['Authorization'] = f'Bearer {token}'
        self.cache_file = cache_file
        self.cache: dict = json.loads(cache_file.read_text()) if cache_file.exists() else {}

    def _save_cache(self):
        self.cache_file.write_text(json.dumps(self.cache, indent=2))

    def search(self, name: str, year: int = None) -> list:
        key = f"search:{name}:{year}"
        if key in self.cache:
            return self.cache[key]
        params = {'query': name, 'include_adult': 'false'}
        if year:
            params['first_air_date_year'] = year
        try:
            r = self.session.get(f"{TMDB_BASE}/search/tv", params=params, timeout=10)
            r.raise_for_status()
            results = r.json().get('results', [])
            self.cache[key] = results
            self._save_cache()
            return results
        except Exception as e:
            logging.warning(f"TMDB search error '{name}': {e}")
            return []

    def get_episode_title(self, tmdb_id: int, season: int, episode: int) -> str:
        key = f"ep:{tmdb_id}:{season}"
        if key not in self.cache:
            try:
                r = self.session.get(
                    f"{TMDB_BASE}/tv/{tmdb_id}/season/{season}",
                    timeout=10
                )
                r.raise_for_status()
                self.cache[key] = r.json().get('episodes', [])
                self._save_cache()
            except Exception as e:
                logging.warning(f"TMDB season fetch error: {e}")
                return ''
        for ep in self.cache[key]:
            if ep.get('episode_number') == episode:
                return ep.get('name', '')
        return ''


# ── Show matching ─────────────────────────────────────────────────────────────
def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def pick_show(tmdb: TMDB, folder: str, candidate: str) -> dict | None:
    """Interactively confirm TMDB match. Returns TMDB result dict or None."""
    year_m = re.search(r'\b(19\d{2}|20\d{2})\b', folder)
    year_hint = int(year_m.group(1)) if year_m else None

    results = tmdb.search(candidate, year_hint)
    if not results and year_hint:
        results = tmdb.search(candidate)

    if not results:
        print(f"  ! No TMDB results for: {candidate!r}")
        return _manual_search(tmdb, folder)

    def score(r):
        names = [r.get('name', ''), r.get('original_name', '')]
        best = max(similarity(candidate, n) for n in names if n)
        pop = min(r.get('vote_count', 0) / 2000, 1.0) * 0.15
        return best * 0.85 + pop

    results.sort(key=score, reverse=True)

    print(f"\n{'─'*65}")
    print(f"  Folder : {folder}")
    print(f"  Parsed : {candidate}")
    print()

    top = results[:6]
    for i, r in enumerate(top):
        yr = (r.get('first_air_date') or '')[:4] or '????'
        orig = r.get('original_name', '')
        orig_str = f"  [{orig}]" if orig and orig != r['name'] else ''
        votes = r.get('vote_count', 0)
        print(f"  [{i+1}] {r['name']} ({yr}){orig_str}  ★{votes}")

    print()
    print("  [1-6] pick  [m] manual search  [s] skip  [q] quit")

    while True:
        try:
            raw = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if raw == 'q':
            sys.exit(0)
        elif raw == 's':
            return None
        elif raw == 'm':
            return _manual_search(tmdb, folder)
        elif raw.isdigit() and 1 <= int(raw) <= len(top):
            return top[int(raw) - 1]


def _manual_search(tmdb: TMDB, folder: str) -> dict | None:
    try:
        name = input("  Search name: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not name:
        return None
    return pick_show(tmdb, folder, name)


# ── File discovery ────────────────────────────────────────────────────────────
def find_episodes(show_dir: Path) -> list[tuple[Path, int, int]]:
    """Return list of (path, season, episode)."""
    found = []

    for root, dirs, files in os.walk(show_dir):
        root_path = Path(root)
        rel = root_path.relative_to(show_dir)

        # Skip junk dirs
        dirs[:] = [
            d for d in dirs
            if d.lower() not in SKIP_DIRS
            and not d.lower().startswith('.')
        ]

        # Season hint from any part of the relative path
        season_hint = None
        for part in rel.parts:
            s = detect_season(part)
            if s is not None:
                season_hint = s
                break
        # No season info anywhere in path → assume S01
        if season_hint is None:
            season_hint = 1

        for fname in sorted(files):
            if Path(fname).suffix.lower() not in VIDEO_EXTS:
                continue
            if is_skip_file(fname):
                continue
            # If dir is OVA but file has no explicit season, force season 0
            effective_hint = season_hint
            if season_hint is None and is_ova_file(fname):
                effective_hint = 0
            result = parse_episode(fname, effective_hint)
            if result is None and is_ova_file(fname):
                # OVA with no parseable episode number — assign incrementally
                ep_num = sum(1 for x in found if x[1] == 0) + 1
                result = (0, ep_num)
            if result:
                found.append((root_path / fname, result[0], result[1]))

    return sorted(found, key=lambda x: (x[1], x[2]))


# ── Already-organized detection ───────────────────────────────────────────────
RE_ORGANIZED_FOLDER = re.compile(r'^.+ \(\d{4}\)$')
RE_SEASON_SUBDIR    = re.compile(r'^Season \d{2}', re.I)


def is_already_organized(show_dir: Path) -> bool:
    """True if folder name is 'Title (YYYY)' and all video files are in Season XX/ with SxxExx names."""
    if not RE_ORGANIZED_FOLDER.match(show_dir.name):
        return False
    video_files = [
        p for p in show_dir.rglob('*')
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    ]
    if not video_files:
        return False
    return all(
        RE_SEASON_SUBDIR.match(p.parent.name) and RE_EP_SXXEXX.search(p.stem)
        for p in video_files
    )


# ── Empty folder cleanup ──────────────────────────────────────────────────────
def cleanup_empty_dirs(root: Path) -> int:
    """Remove dirs with no media files bottom-up. Returns count removed."""
    removed = 0
    for dirpath, dirs, files in os.walk(root, topdown=False):
        p = Path(dirpath)
        if p == root:
            continue
        has_media = any(Path(dirpath, f).suffix.lower() in VIDEO_EXTS for f in files)
        if not has_media:
            try:
                p.rmdir()  # only succeeds if dir is empty after subdirs removed
                logging.info(f"RMDIR {p}")
                removed += 1
            except OSError:
                pass  # not empty (has non-media files) — leave it
    # Remove root itself if now empty
    try:
        root.rmdir()
        logging.info(f"RMDIR {root}")
        removed += 1
    except OSError:
        pass
    return removed


# ── Rename plan ───────────────────────────────────────────────────────────────
RE_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name: str) -> str:
    return RE_INVALID_CHARS.sub('', name).strip()


MAX_FILENAME = 240  # NTFS limit is 255; leave headroom for extension + safety


def _make_ep_filename(show_name: str, season: int, episode: int, title: str, ext: str) -> str:
    prefix = safe(f"{show_name} - S{season:02d}E{episode:02d}")
    suffix = safe(ext)
    if title:
        title_clean = safe(title)
        # Reserve space for " - " separator
        max_title = MAX_FILENAME - len(prefix) - len(suffix) - 3
        if len(title_clean) > max_title:
            title_clean = title_clean[:max_title - 1].rstrip() + '…'
        return f"{prefix} - {title_clean}{suffix}"
    return f"{prefix}{suffix}"


def build_plan(show_dir: Path, tmdb_result: dict, tmdb: TMDB, output_base: Path) -> list[tuple[Path, Path]]:
    show_name = tmdb_result['name']
    year = (tmdb_result.get('first_air_date') or '')[:4]
    folder_name = safe(f"{show_name} ({year})" if year else show_name)

    episodes = find_episodes(show_dir)
    if not episodes:
        return []

    ops = []
    for src, season, episode in episodes:
        title = tmdb.get_episode_title(tmdb_result['id'], season, episode)
        season_dir = "Season 00 (Specials)" if season == 0 else f"Season {season:02d}"
        ep_file = _make_ep_filename(show_name, season, episode, title, src.suffix)
        dst = output_base / folder_name / season_dir / ep_file
        ops.append((src, dst))

    return ops


# ── State ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description='TV Show Library Organizer')
    ap.add_argument('paths', nargs='+', help='TV show root directories to process')
    ap.add_argument('--output', help='Output base dir (default: same as input path)')
    ap.add_argument('--commit', action='store_true', help='Actually move files (default: dry run)')
    ap.add_argument('--reset', action='store_true', help='Reprocess already-done shows')
    ap.add_argument('--token', default=os.environ.get('TMDB_TOKEN', ''),
                    help='TMDB API Bearer token (or set TMDB_TOKEN env var)')
    args = ap.parse_args()

    if not args.token:
        print("ERROR: TMDB bearer token required. Use --token or set TMDB_TOKEN env var.")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
    )

    tmdb  = TMDB(args.token, CACHE_FILE)
    state = {} if args.reset else load_state()
    dry   = not args.commit

    if dry:
        print("DRY RUN — files will not be moved. Use --commit to execute.\n")

    total_ops = []

    for path_str in args.paths:
        base = Path(path_str)
        if not base.exists():
            logging.error(f"Path not found: {base}")
            continue

        out = Path(args.output) if args.output else base

        show_dirs = sorted(d for d in base.iterdir() if d.is_dir())

        for show_dir in show_dirs:
            folder  = show_dir.name
            skey    = str(show_dir)

            if not args.reset and state.get(skey, {}).get('status') == 'done':
                print(f"  [skip] {folder}  (already done)")
                continue

            if not args.reset and is_already_organized(show_dir):
                print(f"  [skip] {folder}  (already organized)")
                state[skey] = {'status': 'done', 'folder': folder, 'auto_detected': True}
                save_state(state)
                continue

            candidate = clean_name(folder)
            result    = pick_show(tmdb, folder, candidate)

            if result is None:
                state[skey] = {'status': 'skipped', 'folder': folder}
                save_state(state)
                continue

            ops = build_plan(show_dir, result, tmdb, out)

            if not ops:
                print(f"  ! No parseable episode files found in: {folder}")
                state[skey] = {'status': 'no_episodes', 'folder': folder}
                save_state(state)
                continue

            yr = (result.get('first_air_date') or '')[:4]
            print(f"\n  → {result['name']} ({yr})  [{len(ops)} episodes]")
            for src, dst in ops[:4]:
                print(f"    {src.name}")
                print(f"      → {dst.relative_to(out)}")
            if len(ops) > 4:
                print(f"    ... and {len(ops) - 4} more")

            if dry:
                total_ops.extend(ops)
                continue

            try:
                ans = input("\n  Execute move? [y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if ans != 'y':
                state[skey] = {'status': 'skipped_at_confirm', 'folder': folder}
                save_state(state)
                continue

            moved = 0
            for src, dst in ops:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
                logging.info(f"MOVE {src} → {dst}")
                moved += 1

            state[skey] = {
                'status':    'done',
                'folder':    folder,
                'tmdb_id':   result['id'],
                'name':      result['name'],
                'year':      yr,
                'moved':     moved,
            }
            save_state(state)
            removed = cleanup_empty_dirs(show_dir)
            print(f"  ✓ Moved {moved} files" + (f", removed {removed} empty dirs" if removed else ""))

    if dry and total_ops:
        print(f"\nDry run: {len(total_ops)} files would be moved across {len(args.paths)} path(s).")
        print("Run with --commit to execute.")


if __name__ == '__main__':
    main()
