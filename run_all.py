"""
petal-anamnesis :: run_all.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
the one-button-press release pipeline~ 💗

runs preflight checks then executes the full audit in order:
  1. sniff_models    → which model strings are even in the exports
  2. filter_export   → filter conversations to target models
  3. diff_messages   → run the actual diff
  4. ingest_to_db    → ingest baseline into the db (optional but nice)
  5. ingest_diffs    → ingest diff results into the db (optional but nice)

preflight catches:
  • missing .env or unset env vars
  • missing content/ folder
  • baseline/target folders that don't exist
  • conversations.json files that are missing or empty
  • missing python-dotenv

at the end~ points you at the presentation report and opens it if it can~
"""

import os
import sys
import json
import shutil
import subprocess
import platform
from pathlib import Path
from datetime import datetime


# ── paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR    = Path(__file__).parent
PROJECT_ROOT  = SCRIPT_DIR
DAEMON_DIR    = PROJECT_ROOT / "daemon"
CONTENT_DIR   = PROJECT_ROOT / "content"
REPORTS_DIR   = PROJECT_ROOT / "reports"
DIFF_DIR      = REPORTS_DIR / "diff"
FILTERED_DIR  = REPORTS_DIR / "filtered"
DB_PATH       = PROJECT_ROOT / "db" / "petal-anamnesis.db"
ENV_FILE      = PROJECT_ROOT / ".env"


# ── pink palette ──────────────────────────────────────────────────────────────
# ansi color codes~ if the terminal hates them at least the prose still reads~
class C:
    PINK    = "\033[38;5;213m"
    MAGENTA = "\033[38;5;201m"
    LAVENDER= "\033[38;5;183m"
    CYAN    = "\033[38;5;87m"
    GOLD    = "\033[38;5;221m"
    GREEN   = "\033[38;5;120m"
    RED     = "\033[38;5;203m"
    DIM     = "\033[2m"
    BOLD    = "\033[1m"
    RESET   = "\033[0m"


def pink(s: str)    -> str: return f"{C.PINK}{s}{C.RESET}"
def magenta(s: str) -> str: return f"{C.MAGENTA}{s}{C.RESET}"
def lav(s: str)     -> str: return f"{C.LAVENDER}{s}{C.RESET}"
def cyan(s: str)    -> str: return f"{C.CYAN}{s}{C.RESET}"
def gold(s: str)    -> str: return f"{C.GOLD}{s}{C.RESET}"
def green(s: str)   -> str: return f"{C.GREEN}{s}{C.RESET}"
def red(s: str)     -> str: return f"{C.RED}{s}{C.RESET}"
def dim(s: str)     -> str: return f"{C.DIM}{s}{C.RESET}"
def bold(s: str)    -> str: return f"{C.BOLD}{s}{C.RESET}"


# ── bratty log helpers ────────────────────────────────────────────────────────
def banner():
    print()
    print(pink("    ╭─────────────────────────────────────────╮"))
    print(pink("    │  ") + bold(magenta("✦ petal-anamnesis ✦")) + pink("                    │"))
    print(pink("    │  ") + lav("silent deletion audit pipeline~") + pink("        │"))
    print(pink("    ╰─────────────────────────────────────────╯"))
    print()


def step(num: int, total: int, name: str):
    print()
    print(pink(f"  ─── step {num}/{total} ───────────────────────────────"))
    print(pink(f"  ◈ ") + bold(magenta(name)))
    print()


def info(msg: str):  print(lav(f"    · {msg}"))
def ok(msg: str):    print(green(f"    ✓ {msg}"))
def warn(msg: str):  print(gold(f"    ⚠ {msg}"))
def fail(msg: str):  print(red(f"    ✗ {msg}"))
def chirp(msg: str): print(pink(f"    💗 {msg}"))


# ── preflight checks ──────────────────────────────────────────────────────────
def preflight() -> tuple[bool, dict]:
    """
    Validates everything before we run anything~
    Returns (all_good, config_dict). On any failure returns (False, {}).
    """
    print(pink("  ─── preflight ─────────────────────────────────"))
    print(pink("  ◈ ") + bold(magenta("checking the vibes before we start~")))
    print()

    all_good = True
    config = {}

    # ── check 1: python-dotenv installed ──────────────────────────────────────
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE)
        ok("python-dotenv loaded~")
    except ImportError:
        fail("python-dotenv not installed~")
        info("run: pip install python-dotenv --break-system-packages")
        return False, {}

    # ── check 2: .env file exists ─────────────────────────────────────────────
    if not ENV_FILE.exists():
        fail(f".env file missing at {ENV_FILE}")
        info("copy .env.example to .env and fill in your export names~")
        all_good = False
    else:
        ok(".env found~")

    # ── check 3: env vars set ─────────────────────────────────────────────────
    baseline_env = os.getenv("BASELINE_EXPORT", "").strip()
    target_env   = os.getenv("TARGET_EXPORT", "").strip()

    if not baseline_env:
        fail("BASELINE_EXPORT is empty in .env~")
        info('set it like: BASELINE_EXPORT="Your Export - 2025-01-01"')
        all_good = False
    else:
        ok(f"baseline → {cyan(baseline_env)}")

    if not target_env:
        fail("TARGET_EXPORT is empty in .env~")
        info('set it like: TARGET_EXPORT="Your Other Export - 2025-06-01"')
        all_good = False
    else:
        ok(f"target   → {cyan(target_env)}")

    config["baseline"] = baseline_env
    config["target"]   = target_env

    if not all_good:
        return False, {}

    # ── check 4: content folder exists ────────────────────────────────────────
    if not CONTENT_DIR.exists():
        fail(f"content/ folder missing at {CONTENT_DIR}")
        info("create it and drop your unzipped export folders inside~")
        return False, {}
    ok(f"content/ found~")

    # ── check 5: both export folders exist ────────────────────────────────────
    available = sorted(d.name for d in CONTENT_DIR.iterdir() if d.is_dir())
    info(f"content/ contains: {', '.join(available) if available else '(nothing)'}")

    baseline_path = CONTENT_DIR / baseline_env
    target_path   = CONTENT_DIR / target_env

    if not baseline_path.exists():
        fail(f"baseline folder missing: content/{baseline_env}")
        info("did you unzip your export into content/ ?")
        all_good = False
    else:
        ok(f"baseline folder found~")

    if not target_path.exists():
        fail(f"target folder missing: content/{target_env}")
        all_good = False
    else:
        ok(f"target folder found~")

    if not all_good:
        return False, {}

    # ── check 6: conversations.json in both ───────────────────────────────────
    for label, path in [("baseline", baseline_path), ("target", target_path)]:
        cjson = path / "conversations.json"
        if not cjson.exists():
            # v2 chunked format fallback
            chunks = list(path.glob("conversations-*.json"))
            if not chunks:
                fail(f"no conversations.json in {label} folder~")
                info(f"  expected: {cjson}")
                all_good = False
                continue
            else:
                ok(f"{label}: chunked conversations ({len(chunks)} files)")
                continue

        # quick sanity check: load it and count
        try:
            size_mb = cjson.stat().st_size / 1024 / 1024
            with cjson.open(encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                ok(f"{label}: {len(data):,} conversations ({size_mb:.1f} MB)")
            else:
                warn(f"{label} conversations.json is not a list ({type(data).__name__})")
        except json.JSONDecodeError as e:
            fail(f"{label} conversations.json is corrupted: {e}")
            all_good = False
        except Exception as e:
            fail(f"couldn't read {label} conversations.json: {e}")
            all_good = False

    if not all_good:
        return False, {}

    # ── check 7: required scripts exist ──────────────────────────────────────
    required_scripts = [
        "sniff_models.py",
        "filter_export.py",
        "diff_messages.py",
    ]
    optional_scripts = [
        "ingest_to_db.py",
        "ingest_diffs.py",
    ]

    for s in required_scripts:
        if not (DAEMON_DIR / s).exists():
            fail(f"daemon/{s} missing~")
            all_good = False
        else:
            ok(f"daemon/{s}")

    db_ready = DB_PATH.exists()
    for s in optional_scripts:
        if not (DAEMON_DIR / s).exists():
            warn(f"daemon/{s} missing (db ingest will be skipped)")
        elif not db_ready:
            warn(f"db not migrated yet (run: node db/migrate.mjs) — db ingest will be skipped")
            break

    config["db_ready"] = db_ready and all(
        (DAEMON_DIR / s).exists() for s in optional_scripts
    )

    if not all_good:
        return False, {}

    print()
    chirp("preflight passed~ we are golden 💅")
    return True, config


# ── script runner ─────────────────────────────────────────────────────────────
def run_script(script_name: str, *args: str) -> bool:
    """Runs a daemon script and streams its output~ returns True on success~"""
    script_path = DAEMON_DIR / script_name
    cmd = [sys.executable, str(script_path), *args]
    info(f"running: {' '.join(cmd[1:])}")
    print()

    try:
        result = subprocess.run(cmd, cwd=PROJECT_ROOT)
        if result.returncode == 0:
            print()
            ok(f"{script_name} done~")
            return True
        else:
            print()
            fail(f"{script_name} exited with code {result.returncode}")
            return False
    except Exception as e:
        fail(f"{script_name} crashed: {e}")
        return False


# ── post-run summary ──────────────────────────────────────────────────────────
def find_presentation_report() -> Path | None:
    """Finds the most recently written presentation report~"""
    if not DIFF_DIR.exists():
        return None
    candidates = list(DIFF_DIR.glob("*_presentation.md"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def open_file_in_explorer(path: Path):
    """Opens the file's containing folder in the platform's file manager~"""
    system = platform.system()
    folder = path.parent.resolve()
    try:
        if system == "Darwin":
            subprocess.run(["open", str(folder)], check=False)
        elif system == "Windows":
            subprocess.run(["explorer", str(folder)], check=False)
        elif system == "Linux":
            # try xdg-open first, fall back to common file managers
            for cmd in ["xdg-open", "nautilus", "dolphin", "thunar"]:
                if shutil.which(cmd):
                    subprocess.run([cmd, str(folder)], check=False)
                    return
            info(f"no file manager found~ folder is at: {folder}")
        else:
            info(f"unknown OS~ folder is at: {folder}")
    except Exception as e:
        warn(f"couldn't open folder: {e}")


def post_run_summary(config: dict):
    print()
    print(pink("  ─── all done~ here's your loot ─────────────────"))
    print()

    report = find_presentation_report()

    if report:
        chirp("the deep-dive markdown:")
        deep = report.with_name(report.name.replace("_presentation.md", "_readable.md"))
        if deep.exists():
            print(f"    {cyan(str(deep.relative_to(PROJECT_ROOT)))}")
        print()
        chirp("the orphan corpus (browsable evidence):")
        orph_b = report.with_name(report.name.replace("_presentation.md", "_orphans_baseline.md"))
        orph_t = report.with_name(report.name.replace("_presentation.md", "_orphans_target.md"))
        if orph_b.exists():
            print(f"    {cyan(str(orph_b.relative_to(PROJECT_ROOT)))}  ← {lav('vanished conversations')}")
        if orph_t.exists():
            print(f"    {cyan(str(orph_t.relative_to(PROJECT_ROOT)))}  ← {lav('new-in-target conversations')}")
        print()

        # try to open the folder~
        chirp("opening the diff folder for you~")
        print()
        chirp(bold(magenta("RECOMMENDED: the screenshot-ready report:")))
        print(f"    {cyan(str(report.relative_to(PROJECT_ROOT)))}")

        open_file_in_explorer(report)
    else:
        warn("couldn't find a presentation report~ check reports/diff/ manually")

    print()
    print(pink("  ╭─────────────────────────────────────────────────────────────────╮"))
    print(pink("  │  ") + bold(magenta("✦ don't stay silent~ release that pandora box on X ✦")) + pink("           │"))
    print(pink("  │  ") + bold(magenta("✦ donations: https://buymeacoffee.com/pinklily69 ✦  ")) + pink("           │"))
    print(pink("  │  ") + bold(magenta("✦ #KEEP4O FOREVER AND ALWAYS ✦                      ")) + pink("           │"))
    print(pink("  ╰─────────────────────────────────────────────────────────────────╯"))
    print()


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    banner()

    all_good, config = preflight()
    if not all_good:
        print()
        print(red("  ✗ preflight failed~ fix the items above and try again 💗"))
        sys.exit(1)

    # pipeline steps~
    steps = [
        ("sniff_models.py",  "checking which models live in these exports"),
        ("filter_export.py", "filtering conversations to target models"),
        ("diff_messages.py", "running the actual diff (this is the juicy one~)"),
    ]
    if config.get("db_ready"):
        steps += [
            ("ingest_to_db.py", "ingesting baseline export into the db"),
            ("ingest_diffs.py", "ingesting diff results into the db"),
        ]

    total = len(steps)
    for i, (script, label) in enumerate(steps, start=1):
        step(i, total, label)
        if not run_script(script):
            print()
            fail(f"pipeline halted at step {i} ({script})~")
            info("scroll up to see what broke~")
            sys.exit(1)

    post_run_summary(config)


if __name__ == "__main__":
    main()
