#!/usr/bin/env python3
"""Pull the Health Connect export zip from Google Drive via rclone.

One-time setup (outside this script, done once interactively):
    rclone config   # create a remote named e.g. "gdrive", scope
                     # drive.readonly, config cached to
                     # ~/.config/rclone/rclone.conf

Then RCLONE_REMOTE (e.g. "gdrive:HealthConnectExports") points at the
Drive folder the phone's automated export writes into. Only *.zip is
copied, so a folder shared with unrelated files is fine; of the zips
found, the newest by mtime wins, which handles both a single
repeatedly-overwritten export and timestamped/rotating ones.

RCLONE_REMOTE may name either the folder holding the export or the
export file itself ("gdrive:Sante Connect.zip"). Naming the file is
the better option whenever the folder isn't dedicated to the export
-- which it often can't be, since some Android builds give no choice
of destination and always write to the Drive root. It fetches that
one file and touches nothing else.

Given a folder, only the zips directly inside it are fetched, and the
export is then identified by its contents rather than by name or
timestamp, so unrelated archives sharing the folder are ignored
rather than mistaken for it.

The file is named after the phone's locale ("Sante Connect.zip" on a
French device), so don't go looking for a predictable name --
`rclone lsf <remote> --max-depth 1 --include "*.zip"` shows what is
actually there. Note `rclone lsd` lists directories only and will
never show it.
"""

import os
import subprocess
import zipfile
from pathlib import Path
from typing import Optional


def sync_remote(staging_dir: Path, remote: Optional[str] = None) -> None:
    """rclone-copy the Drive export folder to a local staging dir.

    Parameters:
        staging_dir (Path): Local directory to copy into (created if
            missing).
        remote (str | None): rclone remote path, e.g.
            ``"gdrive:HealthConnectExports"``. Defaults to the
            ``RCLONE_REMOTE`` env var.

    Raises:
        RuntimeError: ``RCLONE_REMOTE`` unset, or rclone exits nonzero.
    """
    remote = remote or os.environ.get("RCLONE_REMOTE")
    if not remote:
        raise RuntimeError("RCLONE_REMOTE is not set.")
    staging_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        _copy_args(remote, staging_dir),
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rclone copy failed: {result.stderr[:500]}")


def _copy_args(remote: str, staging_dir: Path) -> list[str]:
    """Build the rclone argv for pulling the export.

    A remote naming the zip itself ("gdrive:Sante Connect.zip") is
    copied directly -- nothing else is fetched, and nothing else has
    to be stored or sifted through afterwards. This is the precise
    option, and the only one that avoids touching the neighbours
    entirely when the export shares a busy folder.

    A remote naming a *folder* is restricted to the zips directly
    inside it. Both restrictions matter there: without ``--include``
    the whole folder is mirrored, and without ``--max-depth`` the copy
    descends into every subfolder, so an unrelated backup directory
    would be re-fetched as it grows.

    Parameters:
        remote (str): rclone remote path, folder or file.
        staging_dir (Path): Local directory to copy into.

    Returns:
        list[str]: Full argv for ``subprocess.run``.
    """
    args = ["rclone", "copy", remote, str(staging_dir)]
    if remote.lower().endswith(".zip"):
        return args
    return args + ["--include", "*.zip", "--max-depth", "1"]


def find_latest_zip(staging_dir: Path) -> Optional[Path]:
    """Newest zip in the staging dir that actually holds an export.

    Newest-by-mtime alone is not enough to identify the export. The
    folder Health Connect writes to is picked in Android's file
    picker, so it is regularly a general-purpose folder (or the Drive
    root) holding unrelated zips -- and any of those being more
    recent would otherwise win, leaving the run to either fail on a
    missing .db or, worse, feed some unrelated database to the
    parser. Checking the contents makes the choice of folder
    irrelevant.

    Parameters:
        staging_dir (Path): Directory to scan (non-recursive).

    Returns:
        Path | None: Newest zip containing a ``.db`` member, or
        ``None`` if the directory holds no such zip.
    """
    candidates = sorted(
        staging_dir.glob("*.zip"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for candidate in candidates:
        try:
            with zipfile.ZipFile(candidate) as archive:
                if any(_db_members(archive)):
                    return candidate
        except zipfile.BadZipFile:
            # A half-synced or simply not-a-zip file shouldn't stop us
            # reaching the real export behind it.
            continue
    return None


def _db_members(archive: zipfile.ZipFile) -> list[str]:
    """``.db`` members of an archive, the export's own name first.

    Parameters:
        archive (zipfile.ZipFile): Open archive to inspect.

    Returns:
        list[str]: Matching member names, ``health_connect_export.db``
        ordered ahead of any other ``.db`` found.
    """
    members = [name for name in archive.namelist() if name.endswith(".db")]
    return sorted(
        members,
        key=lambda name: not name.endswith("health_connect_export.db"),
    )


def extract_export(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the Health Connect sqlite export from a zip.

    Parameters:
        zip_path (Path): The export zip (e.g. "Sante Connect.zip").
        dest_dir (Path): Directory to extract into.

    Returns:
        Path: Path to the extracted ``.db`` file.

    Raises:
        RuntimeError: No ``.db`` member found in the zip.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        db_members = _db_members(archive)
        if not db_members:
            raise RuntimeError(f"No .db file found inside {zip_path}")
        archive.extract(db_members[0], dest_dir)
        return dest_dir / db_members[0]


def sync_and_extract(
    staging_dir: Path, remote: Optional[str] = None,
) -> Path:
    """Full pull: rclone sync, pick newest zip, extract the export db.

    Parameters:
        staging_dir (Path): Local working directory.
        remote (str | None): rclone remote, see ``sync_remote``.

    Returns:
        Path: Path to the extracted Health Connect export db.

    Raises:
        RuntimeError: No zip found after syncing.
    """
    sync_remote(staging_dir, remote)
    latest = find_latest_zip(staging_dir)
    if latest is None:
        raise RuntimeError(f"No export zip found in {staging_dir}")
    return extract_export(latest, staging_dir / "extracted")


if __name__ == "__main__":
    import tempfile
    import time

    tmp = Path(tempfile.mkdtemp())
    older = tmp / "export-2026-07-01.zip"
    newer = tmp / "export-2026-07-13.zip"
    with zipfile.ZipFile(older, "w") as archive:
        archive.writestr("health_connect_export.db", b"old")
    time.sleep(0.01)
    with zipfile.ZipFile(newer, "w") as archive:
        archive.writestr("health_connect_export.db", b"new")
    os.utime(newer, (time.time() + 100, time.time() + 100))

    # Pointing straight at the export fetches it alone; pointing at a
    # folder stays inside it rather than sweeping the whole account.
    file_args = _copy_args("gdrive:Sante Connect.zip", tmp)
    assert file_args[:3] == ["rclone", "copy", "gdrive:Sante Connect.zip"]
    assert "--include" not in file_args and "--max-depth" not in file_args
    folder_args = _copy_args("gdrive:", tmp)
    assert folder_args[-4:] == ["--include", "*.zip", "--max-depth", "1"]
    # Case shouldn't decide which of the two shapes we get.
    assert "--include" not in _copy_args("gdrive:Export.ZIP", tmp)

    assert find_latest_zip(tmp) == newer
    assert find_latest_zip(tmp / "empty") is None

    extracted = extract_export(newer, tmp / "out")
    assert extracted.read_bytes() == b"new"

    # The export commonly shares a folder (or a Drive root) with
    # unrelated zips, so a newer one must not be mistaken for it --
    # nor must a corrupt file hide the real export behind it.
    decoy = tmp / "KMSpico_setup.zip"
    with zipfile.ZipFile(decoy, "w") as archive:
        archive.writestr("setup.exe", b"not an export")
    corrupt = tmp / "Contrats_signes.zip"
    corrupt.write_bytes(b"not a zip at all")
    for path in (decoy, corrupt):
        os.utime(path, (time.time() + 500, time.time() + 500))
    assert find_latest_zip(tmp) == newer, find_latest_zip(tmp)

    # A locale-named export with no health_connect_export.db member
    # still resolves through the generic .db fallback.
    accented = tmp / "Sante Connect.zip"
    with zipfile.ZipFile(accented, "w") as archive:
        archive.writestr("export.db", b"locale-named")
    os.utime(accented, (time.time() + 900, time.time() + 900))
    assert find_latest_zip(tmp) == accented
    assert extract_export(accented, tmp / "out2").read_bytes() == b"locale-named"

    print("sync_drive.py: all checks passed (no live rclone call made)")
