# TV Show Organizer

Renames and reorganizes messy TV show folders (torrents, rips, etc.) into a clean Plex/Jellyfin-compatible library structure.

**Output format:**
```
Show Name (Year)/
  Season 01/
    Show Name - S01E01 - Episode Title.mkv
    Show Name - S01E02 - Episode Title.mkv
  Season 02/
    ...
```

Works on **local folders** and **SMB/network shares** (including Nautilus-mounted GVFS paths).

Episode titles are fetched from [TMDB](https://www.themoviedb.org/). Supports anime group tags, multi-season packs, OVAs, specials, and most common release naming conventions.

---

## Requirements

- Python 3.10+
- `requests` library: `pip install requests`
- A free TMDB API token (see below)

---

## Setup

### 1. Get a TMDB API token

1. Create a free account at [themoviedb.org](https://www.themoviedb.org/)
2. Go to **Settings → API**
3. Create an API key (choose "Developer")
4. Copy your **API Read Access Token** (the long JWT string, not the short API key)

### 2. Set your token

Either export it in your shell (add to `~/.bashrc` or `~/.zshrc`):

```bash
export TMDB_TOKEN="your_token_here"
```

Or pass it directly with `--token` each time you run.

---

## Usage

```bash
python3 organize.py [options] <path> [<path> ...]
```

### Options

| Flag | Description |
|------|-------------|
| `--commit` | Actually move files. Without this, it's a dry run — nothing is touched. |
| `--reset` | Reprocess folders already marked as done |
| `--output <dir>` | Write organized files to a different directory (default: same as input) |
| `--token <token>` | TMDB API bearer token (or set `TMDB_TOKEN` env var) |

---

## Examples

### Local folder — dry run (safe, nothing moves)
```bash
python3 organize.py /path/to/Torrents
```

### Local folder — commit
```bash
python3 organize.py --commit /path/to/Torrents
```

### Output to a different location (e.g. separate Plex library)
```bash
python3 organize.py --commit --output /mnt/plex/TV\ Shows /path/to/Torrents
```

### SMB/network share (Nautilus GVFS mount)
```bash
python3 organize.py "/run/user/1000/gvfs/smb-share:server=192.168.1.x,share=plex/TV Shows"
```

### Multiple paths at once
```bash
python3 organize.py --commit /mnt/Torrents "/run/user/1000/gvfs/smb-share:server=192.168.1.x,share=plex/TV Shows"
```

### Reprocess folders already marked as done
```bash
python3 organize.py --reset --commit /path/to/Torrents
```

---

## Interactive Prompts

For each folder, the script searches TMDB and asks you to confirm the match:

```
─────────────────────────────────────────────────────────────────
  Folder : [Judas] Some Show (Season 1) [BD 1080p][x265]
  Parsed : Some Show

  [1] Some Show (2021)  ★4200
  [2] Some Other Show (2019)  ★800
  ...

  [1-6] pick  [m] manual search  [s] skip  [q] quit
  >
```

After the file plan is shown, you confirm before anything moves:
```
  Execute move? [y/n]:
```

---

## Notes

- **Dry run by default** — `--commit` is required to actually move anything
- **State file** at `~/.tv-organizer-state.json` tracks processed folders; use `--reset` to reprocess
- **Log file** at `~/tv-organizer.log` records every file move
- OVAs and specials are placed in `Season 00 (Specials)/`
- Creditless OP/ED, menus, and trailers are automatically skipped
