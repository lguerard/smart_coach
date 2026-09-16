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


def folder_of(remote: str) -> str:
    """The folder containing a remote that names a file.

    Parameters:
        remote (str): rclone path naming a file, e.g.
            ``"gdrive:Sante Connect.zip"``.

    Returns:
        str: The containing folder, e.g. ``"gdrive:"``.
    """
    parent, _, _ = remote.rpartition("/")
    if not parent:
        head, colon, _ = remote.partition(":")
        parent = head + colon
    return parent


def filename_of(remote: str) -> str:
    """The filename part of a remote that names a file.

    Parameters:
        remote (str): rclone path naming a file.

    Returns:
        str: The bare filename.
    """
    _, slash, tail = remote.rpartition("/")
    if slash:
        return tail
    return remote.partition(":")[2]


def _copy_args(remote: str, staging_dir: Path) -> list[str]:
    """Build the rclone argv for pulling the export.

    A remote naming the zip itself ("gdrive:Sante Connect.zip") is
    fetched as its parent folder filtered to that one name, so nothing
    else is transferred. It is expressed that way rather than by
    passing the file as the source because rclone's Drive backend
    reads a file path as a root directory and fails outright.

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
    if remote.lower().endswith(".zip"):
        # Handing rclone the file as the source looks like it should
        # work and doesn't: the Drive backend reads the path as a root
        # directory and fails with "directory not found". Copying the
        # parent folder with a filter for that one name is a plain
        # directory source, which every backend handles, and transfers
        # just as little.
        return [
            "rclone", "copy", folder_of(remote), str(staging_dir),
            "--include", filename_of(remote), "--max-depth", "1",
        ]
    return [
        "rclone", "copy", remote, str(staging_dir),
        "--include", "*.zip", "--max-depth", "1",
    ]


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
    remote = remote or os.environ.get("RCLONE_REMOTE")
    sync_remote(staging_dir, remote)
    latest = find_latest_zip(staging_dir)
    if latest is None and remote and remote.lower().endswith(".zip"):
        # The export's name is generated from the phone's locale and
        # has to survive a trip through .env, a settings field and
        # Drive intact -- an accent alone can end up normalised
        # differently at either end ("e" plus a combining mark rather
        # than a single codepoint), and then the filter quietly
        # matches nothing. Falling back to every zip in the folder
        # costs one wider transfer and lets find_latest_zip identify
        # the export the way it already does: by looking inside.
        sync_remote(staging_dir, folder_of(remote))
        latest = find_latest_zip(staging_dir)
    if latest is None:
        raise RuntimeError(f"No export zip found in {staging_dir}")
    return extract_export(latest, staging_dir / "extracted")


if __name__ == "__main__":
    import sys
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
    # A named zip is fetched via its parent + a filter, never as a
    # file source: rclone's Drive backend rejects that outright.
    file_args = _copy_args("gdrive:Sante Connect.zip", tmp)
    assert file_args[2] == "gdrive:", file_args
    assert file_args[-4:] == [
        "--include", "Sante Connect.zip", "--max-depth", "1",
    ], file_args
    nested = _copy_args("gdrive:Exports/Sante Connect.zip", tmp)
    assert nested[2] == "gdrive:Exports", nested
    assert nested[-3] == "Sante Connect.zip", nested
    # Case shouldn't decide which of the two shapes we get.
    assert _copy_args("gdrive:Export.ZIP", tmp)[-3] == "Export.ZIP"
    folder_args = _copy_args("gdrive:", tmp)
    assert folder_args[-4:] == ["--include", "*.zip", "--max-depth", "1"]

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

    # A named zip whose filter matches nothing (an accent normalised
    # differently at one end is enough) must fall back to the folder
    # rather than failing the run.
    module = sys.modules[__name__]

    fallback_dir = tmp / "fallback"
    fallback_dir.mkdir()
    attempts = []

    def fake_sync(staging, remote_arg=None):
        attempts.append(remote_arg)
        if remote_arg and remote_arg.lower().endswith(".zip"):
            return  # precise filter matched nothing
        with zipfile.ZipFile(staging / "Sante Connect.zip", "w") as archive:
            archive.writestr("health_connect_export.db", b"recovered")

    real_sync = module.sync_remote
    module.sync_remote = fake_sync
    try:
        found = module.sync_and_extract(
            fallback_dir, "gdrive:Sante Connect.zip",
        )
    finally:
        module.sync_remote = real_sync
    assert attempts == ["gdrive:Sante Connect.zip", "gdrive:"], attempts
    assert found.read_bytes() == b"recovered"

    print("sync_drive.py: all checks passed (no live rclone call made)")
