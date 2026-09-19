"""
Model checkpoint discovery and download.

Demucs downloads its weights lazily, from inside Separator(), through
torch.hub.load_state_dict_from_url(). That is a problem for us twice over:

- There is no progress callback, so the UI would sit on a frozen "Loading
  model" bar for a 300 MB (htdemucs) to 1 GB (htdemucs_ft bag of four)
  download.
- torch.hub announces the download with sys.stdout.write() (torch/hub.py:866),
  and in the daemon stdout is the JSON protocol channel.

So we fetch the checkpoints ourselves, first, into the exact cache location
torch.hub would have used. Demucs then finds them cached and never reaches
its own download path.

Cache layout, matching torch/hub.py:861-871 :
    <torch.hub.get_dir()>/checkpoints/<basename of the URL>
with the expected sha256 prefix embedded in the file name ("<sig>-<hash>.th").
"""

import hashlib
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

from .presets import MODEL_CONFIGS

# demucs.pretrained.ROOT_URL. Hard-coded rather than imported because
# importing demucs.pretrained drags in torch and the model classes, and this
# module is also used for a cheap "is it already downloaded?" check.
ROOT_URL = "https://dl.fbaipublicfiles.com/demucs/"

# torch.hub.HASH_REGEX (torch/hub.py:79).
HASH_REGEX = re.compile(r"-([a-f0-9]*)\.")

DOWNLOAD_BLOCK_BYTES = 1 << 20      # 1 MB: ~1000 progress ticks for a 1 GB bag
DOWNLOAD_TIMEOUT_S = 60
PROGRESS_MIN_DELTA = 0.005          # Emit at most ~200 events per file

ProgressHook = Callable[[dict], None]


class CheckpointError(RuntimeError):
    """Raised when a checkpoint cannot be located, downloaded or verified."""


@dataclass(frozen=True)
class Checkpoint:
    """One weights file a model needs."""

    signature: str
    url: str
    path: Path

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def present(self) -> bool:
        return self.path.is_file()


# =============================================================================
# Discovery
# =============================================================================

def _demucs_remote_dir() -> Path:
    """
    Locate demucs/remote/, which holds files.txt and the bag YAMLs.

    Importing `demucs` itself is cheap (its __init__ only sets __version__);
    it is demucs.pretrained that pulls in torch.
    """
    import demucs

    remote = Path(demucs.__file__).resolve().parent / "remote"
    if not remote.is_dir():
        raise CheckpointError(f"demucs model registry not found at {remote}")
    return remote


def _parse_files_txt(path: Path) -> Dict[str, str]:
    """
    Map checkpoint signature -> download URL.

    Mirrors demucs.pretrained._parse_remote_files. Reimplemented here (it is
    a dozen lines over a stable package data file) so that asking "do I
    already have these weights?" does not have to import torch.
    """
    models: Dict[str, str] = {}
    root = ""
    for raw in path.read_text(encoding="utf-8").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("root:"):
            root = line.split(":", 1)[1].strip()
            continue
        signature = line.split("-", 1)[0]
        models[signature] = ROOT_URL + root + line
    return models


def _signatures_for(model: str, remote: Path) -> List[str]:
    """The checkpoint signatures a model name resolves to (1, or N for a bag)."""
    bag = remote / f"{model}.yaml"
    if bag.is_file():
        import yaml

        loaded = yaml.safe_load(bag.read_text(encoding="utf-8")) or {}
        signatures = loaded.get("models") or []
        if not signatures:
            raise CheckpointError(f"Bag {model}.yaml lists no models")
        return [str(sig) for sig in signatures]
    # Not a bag: the name may itself be a signature.
    return [model]


def cache_dir() -> Path:
    """The directory torch.hub caches checkpoints in."""
    import torch

    return Path(torch.hub.get_dir()) / "checkpoints"


def cache_path(url: str) -> Path:
    """Where torch.hub would cache this URL (torch/hub.py:861-864)."""
    return cache_dir() / os.path.basename(urlparse(url).path)


def checkpoints_for(model: str) -> List[Checkpoint]:
    """
    Every weights file `model` needs, in bag order.

    Raises:
        CheckpointError: unknown model, or a signature missing from files.txt.
    """
    if model not in MODEL_CONFIGS:
        raise CheckpointError(f"Unknown model: {model}")
    remote = _demucs_remote_dir()
    urls = _parse_files_txt(remote / "files.txt")

    result: List[Checkpoint] = []
    for signature in _signatures_for(model, remote):
        url = urls.get(signature)
        if not url:
            raise CheckpointError(
                f"Model {model} needs checkpoint {signature}, which is not in files.txt"
            )
        result.append(Checkpoint(signature=signature, url=url, path=cache_path(url)))
    return result


def missing_checkpoints(model: str) -> List[Checkpoint]:
    """The subset of checkpoints_for(model) that is not in the cache yet."""
    return [c for c in checkpoints_for(model) if not c.present]


# =============================================================================
# Download
# =============================================================================

def _expected_hash(filename: str) -> Optional[str]:
    match = HASH_REGEX.search(filename)
    return match.group(1) if match else None


def _download_one(
    checkpoint: Checkpoint,
    file_index: int,
    file_count: int,
    on_progress: Optional[ProgressHook],
    cancel_check: Optional[Callable[[], None]],
) -> None:
    """
    Fetch one checkpoint into the torch.hub cache.

    Downloads to a sibling .part file, verifies the sha256 prefix carried in
    the file name (same contract as torch.hub's check_hash=True), and only
    then moves it into place, so an interrupted or corrupted download can
    never be picked up as a valid checkpoint.
    """
    checkpoint.path.parent.mkdir(parents=True, exist_ok=True)
    temp = checkpoint.path.with_name(f"{checkpoint.path.name}.part-{os.getpid()}")
    expected = _expected_hash(checkpoint.name)
    digest = hashlib.sha256()
    done = 0
    last_reported = -1.0

    def report(total: int, force: bool = False) -> None:
        nonlocal last_reported
        if on_progress is None:
            return
        fraction = (done / total) if total else 0.0
        if not force and fraction - last_reported < PROGRESS_MIN_DELTA:
            return
        last_reported = fraction
        on_progress({
            "name": checkpoint.name,
            "signature": checkpoint.signature,
            "fileIndex": file_index,
            "fileCount": file_count,
            "bytesDone": done,
            "bytesTotal": total,
            # Progress across the whole set, not just this file.
            "percent": round(100.0 * (file_index - 1 + fraction) / max(1, file_count), 1),
        })

    request = urllib.request.Request(
        checkpoint.url, headers={"User-Agent": "poligono-ai-hub"}
    )
    try:
        with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_S) as response:
            total = int(response.headers.get("Content-Length") or 0)
            report(total, force=True)
            with open(temp, "wb") as handle:
                while True:
                    if cancel_check:
                        cancel_check()
                    block = response.read(DOWNLOAD_BLOCK_BYTES)
                    if not block:
                        break
                    handle.write(block)
                    digest.update(block)
                    done += len(block)
                    report(total)
            report(total or done, force=True)
    except (urllib.error.URLError, OSError) as exc:
        _unlink(temp)
        raise CheckpointError(f"Could not download {checkpoint.name}: {exc}") from exc
    except BaseException:
        # Cancelled, or anything else: leave no half-written file behind.
        _unlink(temp)
        raise

    if expected:
        actual = digest.hexdigest()
        if not actual.startswith(expected):
            _unlink(temp)
            raise CheckpointError(
                f"{checkpoint.name} failed its checksum "
                f"(expected {expected}..., got {actual[:len(expected)]}...)"
            )

    try:
        os.replace(temp, checkpoint.path)
    except OSError as exc:
        _unlink(temp)
        raise CheckpointError(f"Could not store {checkpoint.name}: {exc}") from exc


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def ensure_checkpoints(
    model: str,
    on_progress: Optional[ProgressHook] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    log: Optional[Callable[[str, str], None]] = None,
) -> List[Checkpoint]:
    """
    Make sure every checkpoint `model` needs is in the torch.hub cache.

    Args:
        model: A name from MODEL_CONFIGS.
        on_progress: Called with a dict per progress tick (name, fileIndex,
            fileCount, bytesDone, bytesTotal, percent).
        cancel_check: Called between blocks; raise from it to abort. The
            engine passes its own cancel check, so a download aborts the same
            way a separation does.
        log: (message, level) sink.

    Returns:
        The checkpoints that had to be downloaded (empty when all were cached).
    """
    missing = missing_checkpoints(model)
    if not missing:
        return []

    if log:
        log(
            f"Model {model} needs {len(missing)} checkpoint(s); downloading to {cache_dir()}",
            "info",
        )
    for position, checkpoint in enumerate(missing, start=1):
        if cancel_check:
            cancel_check()
        if log:
            log(f"Downloading {checkpoint.name} ({position}/{len(missing)})...", "info")
        _download_one(checkpoint, position, len(missing), on_progress, cancel_check)
    if log:
        log(f"Model {model}: {len(missing)} checkpoint(s) downloaded", "info")
    return missing
