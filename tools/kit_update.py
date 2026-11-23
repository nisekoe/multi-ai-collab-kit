#!/usr/bin/env python3
"""Install releases of the kit with Python 3.9 or later.

The release manifest is unsigned. Use a trusted HTTPS source. The updater
checks the archive against the manifest and runs a bundled CLI with --help
before moving anything into the installation directory.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import http.client
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4
import zipfile
import zlib


KIT = "multi-ai-collab-kit"
DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/nisekoe/multi-ai-collab-kit/"
    "main/kit-manifest.json"
)
VERSION = re.compile(
    r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
ZIP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.zip")
NETWORK_ERRORS = (URLError, TimeoutError, ConnectionError, http.client.HTTPException, ssl.SSLError)
CHUNK_SIZE = 1024 * 1024
MAX_MANIFEST_BYTES = CHUNK_SIZE


class UpdateError(Exception):
    """An update could not proceed safely."""


def version_key(value, label):
    match = VERSION.fullmatch(value) if isinstance(value, str) and len(value) <= 128 else None
    if not match:
        raise UpdateError(f"{label} must be a version such as 0.4.0 or 0.4.0-rc.1")
    major, minor, patch, prerelease = match.groups()
    identifiers = []
    for part in prerelease.split(".") if prerelease else ():
        if part.isdecimal():
            if len(part) > 1 and part.startswith("0"):
                raise UpdateError(f"{label} has a prerelease number with a leading zero")
            identifiers.append((0, int(part)))
        else:
            identifiers.append((1, part))
    return int(major), int(minor), int(patch), prerelease is None, tuple(identifiers)


def https_url(url):
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UpdateError("Invalid HTTPS URL") from exc
    if parsed.scheme != "https" or not parsed.hostname or parsed.username is not None:
        raise UpdateError("Manifest and asset URLs must use HTTPS without embedded credentials")
    return parsed


class HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    https_url(url)
    return build_opener(HttpsRedirect).open(url, timeout=15)


def select_asset(manifest):
    if not isinstance(manifest, dict) or manifest.get("kit") != KIT:
        raise UpdateError(f"The manifest must identify the kit as {KIT}")
    version_key(manifest.get("version"), "Manifest version")
    version_key(manifest.get("minDsh"), "minDsh")
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not all(isinstance(asset, dict) for asset in assets):
        raise UpdateError("The manifest must contain a list of release assets")
    archives = [
        asset for asset in assets
        if isinstance(asset.get("name"), str) and ZIP_NAME.fullmatch(asset["name"])
    ]
    expected = f"kit-{manifest['version'].removeprefix('v')}.zip"
    preferred = [asset for asset in archives if asset["name"] == expected]
    choices = preferred or archives
    if len(choices) != 1:
        raise UpdateError(f"The manifest must identify one ZIP asset or name it {expected}")
    asset = choices[0]
    digest = asset.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9A-Fa-f]{64}", digest):
        raise UpdateError("The ZIP asset must have a 64-digit SHA-256 digest")
    if type(asset.get("size")) is not int or asset["size"] <= 0:
        raise UpdateError("The ZIP asset must have a positive size in bytes")
    return asset


def asset_url(manifest_url, version, name, base_url):
    if base_url:
        https_url(base_url)
        return urljoin(base_url.rstrip("/") + "/", quote(name))
    source = https_url(manifest_url)
    parts = source.path.strip("/").split("/")
    if source.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
        owner, repo = parts[:2]
        tag = quote("v" + version.removeprefix("v"), safe="")
        return f"https://github.com/{owner}/{repo}/releases/download/{tag}/{quote(name)}"
    return urljoin(manifest_url, quote(name))


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_state(path, install):
    try:
        with path.open(encoding="utf-8-sig") as stream:
            state = json.load(stream)
    except FileNotFoundError:
        state = {}
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise UpdateError(f"Invalid local version record at {path}") from exc
    if not isinstance(state, dict):
        raise UpdateError(f"Invalid local version record at {path}")
    recorded = state.get("installDir")
    if recorded is not None and (not isinstance(recorded, str) or Path(recorded) != install):
        raise UpdateError("The version record belongs to another installation. Select its --state-file")
    for key in ("lastCheck", "previous", "pending"):
        if state.get(key) is not None and not isinstance(state[key], dict):
            raise UpdateError(f"Invalid {key} entry in the local version record")
    state["installDir"] = str(install)
    return state


def save_state(path, state, **changes):
    updated = dict(state, **changes)
    fd, temporary = tempfile.mkstemp(prefix=".kit-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    state.update(changes)


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Closing the descriptor releases the lock even after a process crash.
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UpdateError("Cannot lock the updater. Another update may be running") from exc
        yield


def release_for_today(path, state, manifest_url):
    https_url(manifest_url)
    stamp = now()
    source = hashlib.sha256(manifest_url.encode("utf-8")).hexdigest()
    previous_check = state.get("lastCheck") or {}
    if str(previous_check.get("at", ""))[:10] == stamp[:10]:
        if previous_check.get("source") != source:
            print("Already checked today. No cached result for this manifest URL")
            return None
        return previous_check.get("manifest")

    # Count attempts including offline checks so a failed connection cannot cause a retry storm.
    check = {"at": stamp, "source": source}
    save_state(path, state, lastCheck=check)
    with open_url(manifest_url) as response:
        body = response.read(MAX_MANIFEST_BYTES + 1)
    if len(body) > MAX_MANIFEST_BYTES:
        raise UpdateError("The release manifest is too large")
    try:
        manifest = json.loads(body)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise UpdateError("The release manifest is not valid JSON") from exc
    select_asset(manifest)
    save_state(path, state, lastCheck=dict(check, manifest=manifest))
    return manifest


def directory_exists(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    reparse = getattr(attributes, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if not stat.S_ISDIR(attributes.st_mode) or reparse:
        raise UpdateError(f"Expected a regular directory at {path}")
    return True


def installed_version(install, state, supplied):
    recorded = state.get("version")
    if recorded is not None:
        recorded_key = version_key(recorded, "Recorded version")
        if supplied is not None and version_key(supplied, "Installed version") != recorded_key:
            raise UpdateError("--installed-version differs from the local version record")
        return recorded
    present = directory_exists(install)
    if supplied is not None:
        version_key(supplied, "Installed version")
        if not present:
            raise UpdateError("--installed-version requires an existing installation directory")
        return supplied
    if present and any(install.iterdir()):
        raise UpdateError("The existing installation has no version record. Supply --installed-version")
    return None


def download_archive(url, asset, target):
    count = 0
    digest = hashlib.sha256()
    with open_url(url) as response, target.open("xb") as stream:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            count += len(chunk)
            if count > asset["size"]:
                raise UpdateError("Asset size exceeds the manifest size")
            digest.update(chunk)
            stream.write(chunk)
    if count != asset["size"]:
        raise UpdateError("Asset size does not match the manifest")
    if digest.hexdigest() != asset["sha256"].lower():
        raise UpdateError("Asset SHA-256 does not match the manifest")


def extract_archive(archive, destination):
    destination.mkdir()
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                parts = member.orig_filename.removesuffix("/").split("/")
                if any(
                    part in ("", ".", "..")
                    or part.endswith((" ", "."))
                    or PureWindowsPath(part).is_reserved()
                    or any(char in '\\:<>"|?*' or ord(char) < 32 for char in part)
                    for part in parts
                ):
                    raise UpdateError("The ZIP contains an unsafe path")
                mode = member.external_attr >> 16
                expected_type = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
                if stat.S_IFMT(mode) not in (0, expected_type):
                    raise UpdateError("The ZIP contains a link or a special file")
                target = destination.joinpath(*parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as source, target.open("xb") as stream:
                        shutil.copyfileobj(source, stream, length=CHUNK_SIZE)
                    if os.name != "nt" and mode & 0o111:
                        target.chmod(target.stat().st_mode | (mode & 0o111))
    except (zipfile.BadZipFile, RuntimeError, EOFError, zlib.error) as exc:
        raise UpdateError(f"Cannot extract the release ZIP ({exc})") from exc


def self_check(unpacked):
    roots = [unpacked]
    entries = list(unpacked.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        roots.append(entries[0])
    for root in roots:
        for name in ("tools/kit_update.py", "kit_update.py", "tools/init_rules.py"):
            script = root / name
            if script.is_file():
                try:
                    subprocess.run(
                        [sys.executable, "-I", "-B", str(script), "--help"],
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True,
                        timeout=15,
                    )
                except subprocess.CalledProcessError as exc:
                    raise UpdateError(f"Release self-check failed ({name} --help exited {exc.returncode})") from exc
                except subprocess.TimeoutExpired as exc:
                    raise UpdateError(f"Release self-check timed out ({name} --help)") from exc
                return root
    raise UpdateError("The release ZIP contains no supported CLI for the --help self-check")


def managed_directory(install, name, kind):
    if not isinstance(name, str) or not re.fullmatch(rf"\.kit-{kind}-[a-z0-9_]{{8,32}}", name):
        raise UpdateError(f"Invalid {kind} directory in the local version record")
    path = install.parent / name
    directory_exists(path)
    return path


def discard(path):
    try:
        if directory_exists(path):
            shutil.rmtree(path)
    except (OSError, UpdateError) as exc:
        print(f"Could not clean up {path} ({exc})", file=sys.stderr)


def recover(path, state, install):
    pending = state["pending"]
    workspace = None
    if pending.get("operation") == "apply":
        workspace = managed_directory(install, pending.get("source"), "stage")
        incoming = workspace / "candidate"
    elif pending.get("operation") == "rollback":
        incoming = managed_directory(install, pending.get("source"), "backup")
    else:
        raise UpdateError("Invalid pending operation in the local version record")
    saved = managed_directory(install, pending.get("saved"), "backup")
    had_current = pending.get("hadCurrent")
    if type(had_current) is not bool or saved == incoming or saved == install or incoming == install:
        raise UpdateError("Invalid pending paths in the local version record")
    has_install = directory_exists(install)
    has_incoming = directory_exists(incoming)
    if saved.exists():
        if has_install:
            if has_incoming:
                raise UpdateError("Cannot recover the update because both installation paths exist")
            install.rename(incoming)
        saved.rename(install)
    elif had_current:
        if not has_install or not has_incoming:
            raise UpdateError("Cannot recover the update because a directory is missing")
    elif has_install:
        if has_incoming:
            raise UpdateError("Cannot recover the update because both installation paths exist")
        install.rename(incoming)

    save_state(path, state, pending=None)
    if workspace is not None:
        discard(workspace)


def switch_install(path, state, install, incoming, version, current_version, workspace=None):
    prior = state.get("previous")
    obsolete = None
    if workspace is not None and prior is not None:
        obsolete = managed_directory(install, prior.get("directory"), "backup")
        if obsolete == install:
            raise UpdateError("The previous version points to the active installation")
    saved = install.parent / f".kit-backup-{uuid4().hex}"
    had_current = directory_exists(install)
    if directory_exists(saved) or incoming == install or not directory_exists(incoming):
        raise UpdateError("The replacement or backup directory is unavailable")
    journal = {
        "operation": "apply" if workspace is not None else "rollback",
        "source": workspace.name if workspace is not None else incoming.name,
        "saved": saved.name,
        "hadCurrent": had_current,
    }
    previous = {"directory": saved.name, "version": current_version} if had_current and current_version else None
    # Persist the rename plan before moving either tree. A later invocation can undo a stopped update.
    try:
        save_state(path, state, pending=journal, version=current_version)
        if had_current:
            install.rename(saved)
        incoming.rename(install)
        save_state(path, state, version=version, previous=previous, pending=None, lastError=None)
    except BaseException:
        persisted = load_state(path, install)
        state.update(persisted)
        if state.get("pending"):
            recover(path, state, install)
        raise

    if obsolete is not None:
        discard(obsolete)
    if had_current and previous is None:
        discard(saved)


def confirm(question, assume_yes):
    if assume_yes:
        return True
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_release(args, path, state, install, install_lock, manifest, asset, current):
    version = manifest["version"]
    if not confirm(f"Install kit {version} into {install}?", args.yes):
        print("Update cancelled")
        return
    url = asset_url(args.manifest_url, version, asset["name"], args.asset_base_url)
    with lock(install_lock):
        if current is not None and not directory_exists(install):
            raise UpdateError("The directory for the recorded version is missing")
        if installed_version(install, state, args.installed_version) != current:
            raise UpdateError("The installed version changed during the check")
        workspace = Path(tempfile.mkdtemp(prefix=".kit-stage-", dir=install.parent))
        try:
            archive = workspace / "release.zip"
            unpacked = workspace / "unpacked"
            download_archive(url, asset, archive)
            extract_archive(archive, unpacked)
            ready = workspace / "candidate"
            self_check(unpacked).rename(ready)
            switch_install(path, state, install, ready, version, current, workspace)
        finally:
            if state.get("pending") is None:
                discard(workspace)
    print(f"Installed kit {version}. Restart DSH to load the new version")


def rollback(args, path, state, install, install_lock):
    previous = state.get("previous")
    if previous is None:
        raise UpdateError("There is no previous version to restore")
    version = previous.get("version")
    version_key(version, "Previous version")
    if not confirm(f"Restore kit {version} into {install}?", args.yes):
        print("Rollback cancelled")
        return
    with lock(install_lock):
        backup = managed_directory(install, previous.get("directory"), "backup")
        current = installed_version(install, state, args.installed_version)
        switch_install(path, state, install, backup, version, current)
    print(f"Restored kit {version}. Restart DSH to load the restored version")


def check_or_apply(args, path, state, install, install_lock):
    current = installed_version(install, state, args.installed_version)
    manifest = release_for_today(path, state, args.manifest_url)
    if manifest is None:
        return
    asset = select_asset(manifest)
    version = manifest["version"]
    if current is not None and version_key(version, "Manifest version") <= version_key(current, "Installed version"):
        print(f"No update. Release {version} is not newer than installed {current}")
        return
    minimum = manifest["minDsh"]
    if args.dsh_version is None:
        if args.apply:
            raise UpdateError(f"Kit {version} requires DSH >= {minimum}. Supply --dsh-version")
        print(f"Update available to {version}. It requires DSH >= {minimum}")
        return
    if version_key(args.dsh_version, "DSH version") < version_key(minimum, "minDsh"):
        raise UpdateError(f"Kit {version} requires DSH >= {minimum}. Current DSH is {args.dsh_version}")
    if args.check:
        print(f"Update available to {version} from {current or 'an empty installation'}. DSH meets minDsh {minimum}")
        return
    apply_release(args, path, state, install, install_lock, manifest, asset, current)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check, install or roll back a kit release",
        epilog="Use a trusted HTTPS source. The release manifest is unsigned. "
        "GitHub ZIP assets use tag vVERSION. With other sources the ZIP is next to the manifest "
        "unless --asset-base-url is supplied. Run apply and rollback from outside the installation directory.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="record one daily check without changing the installation")
    action.add_argument("--apply", action="store_true", help="install the selected update after confirmation")
    action.add_argument("--status", action="store_true", help="show the recorded version and last check offline")
    action.add_argument("--rollback", action="store_true", help="restore the previous version after confirmation")
    parser.add_argument("--install-dir", type=Path, required=True, help="directory containing the kit")
    parser.add_argument("--state-file", type=Path, default=Path.home() / ".dsh" / "kit-version.json",
                        help="local version and check record, one per installation (default ~/.dsh/kit-version.json)")
    parser.add_argument("--dsh-version", help="current DSH version, required when installing an update")
    parser.add_argument("--installed-version", help="current kit version for an existing installation with no record")
    parser.add_argument("--manifest-url", default=DEFAULT_MANIFEST_URL, help="HTTPS URL of kit-manifest.json")
    parser.add_argument("--asset-base-url", help="optional HTTPS directory containing the ZIP asset")
    parser.add_argument("--yes", action="store_true", help="skip confirmation for apply or rollback")
    args = parser.parse_args(argv)
    try:
        install = args.install_dir.expanduser().absolute()
        directory_exists(install)
        install = install.resolve()
        path = args.state_file.expanduser().resolve()
        if install == install.parent or path.is_relative_to(install):
            raise UpdateError("Choose an installation directory with the version record stored outside it")
        if args.status:
            state = load_state(path, install)
            print(f"Installed version {state.get('version') or 'unrecorded'}")
            print(f"Last checked {(state.get('lastCheck') or {}).get('at') or 'never'}")
            if state.get("pending"):
                print("An interrupted directory switch awaits recovery")
            if state.get("lastError"):
                print(f"Last error {state['lastError']}")
            return 0

        install_lock = install.with_name(f".{install.name}.kit-update.lock")
        state_lock = path.with_name(path.name + ".lock")
        if state_lock in (install, install_lock) or path == install_lock:
            raise UpdateError("Choose a version record path distinct from the installation lock")
        with lock(state_lock):
            state = load_state(path, install)
            try:
                if state.get("pending"):
                    if args.check:
                        raise UpdateError("An interrupted directory switch awaits recovery by --apply or --rollback")
                    with lock(install_lock):
                        recover(path, state, install)
                if args.rollback:
                    rollback(args, path, state, install, install_lock)
                else:
                    check_or_apply(args, path, state, install, install_lock)
            except NETWORK_ERRORS as exc:
                save_state(path, state, lastError=f"Network unavailable ({exc})")
            except (OSError, UpdateError) as exc:
                save_state(path, state, lastError=str(exc))
                raise
        return 0
    except (OSError, UpdateError) as exc:
        print(f"Update stopped. {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Update interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
