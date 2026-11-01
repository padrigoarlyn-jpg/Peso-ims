"""Migrate existing resumes out of website/static/uploads/resumes.

Usage: python migrate_private_resumes.py --dry-run
       python migrate_private_resumes.py --copy
       python migrate_private_resumes.py --verify
       python migrate_private_resumes.py --remove-public

Run from PESO-IMS project root, after making a backup. Do not delete public
files until --verify succeeds. Stop all web processes during final migration.
"""
import argparse
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "website" / "static" / "uploads" / "resumes"
PRIVATE = Path(os.environ.get("PRIVATE_RESUME_DIR") or ROOT / "instance" / "private" / "resumes").resolve()


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.digest()


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    for name in ("dry-run", "copy", "verify", "remove-public"):
        g.add_argument("--" + name, action="store_true")
    args = p.parse_args()
    if PRIVATE == PUBLIC.resolve() or (ROOT / "website" / "static").resolve() in PRIVATE.parents:
        p.error("Private directory must not be inside website/static")
    files = [f for f in PUBLIC.iterdir() if f.is_file() and not f.is_symlink()] if PUBLIC.exists() else []
    if args.dry_run:
        print(f"Public files: {len(files)}; private target: {PRIVATE}")
        return
    if args.copy:
        PRIVATE.mkdir(parents=True, exist_ok=True)
        for f in files:
            target = PRIVATE / f.name
            if target.exists():
                if digest(f) != digest(target):
                    raise RuntimeError(f"Different file already exists: {f.name}")
            else:
                import shutil
                shutil.copy2(f, target)
        print(f"Copied/verified {len(files)} files; originals retained")
        return
    mismatches = [f.name for f in files if not (PRIVATE / f.name).is_file() or digest(f) != digest(PRIVATE / f.name)]
    if mismatches:
        raise RuntimeError(f"Migration verification FAILED for {len(mismatches)} file(s): {mismatches[:10]}")
    print(f"Verified {len(files)} files")
    if args.remove_public:
        for f in files:
            f.unlink()
        print("Public copies removed. Confirm static URLs return 404.")

if __name__ == "__main__":
    main()
