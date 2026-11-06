import os
import shutil
import tempfile
from pathlib import Path

from webdriver_manager.firefox import GeckoDriverManager


def _managed_driver_path() -> Path:
    if os.name == "nt":
        cache_root = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        cache_root = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache")
    binary_name = "geckodriver.exe" if os.name == "nt" else "geckodriver"
    return cache_root / "Autowell" / "drivers" / binary_name


def _legacy_wdm_driver_path() -> Path | None:
    binary_name = "geckodriver.exe" if os.name == "nt" else "geckodriver"
    driver_root = Path.home() / ".wdm" / "drivers" / "geckodriver"
    try:
        candidates = sorted(
            driver_root.rglob(binary_name),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for candidate in candidates:
        try:
            _validate_driver_file(candidate, "webdriver-manager's cached geckodriver")
            return candidate
        except (OSError, RuntimeError):
            continue
    return None


def _validate_driver_file(path: Path, source: str) -> str:
    if not path.is_file():
        raise RuntimeError(f"{source} does not point to a driver file: {path}")
    if path.stat().st_size == 0:
        raise RuntimeError(f"{source} points to an empty driver file: {path}")
    if os.name == "nt" and path.suffix.casefold() != ".exe":
        raise RuntimeError(f"{source} must point to geckodriver.exe: {path}")
    return str(path.resolve())


def _cache_downloaded_driver(downloaded_path: str, cache_path: Path) -> str:
    source = Path(downloaded_path)
    if not source.is_file():
        raise RuntimeError(
            f"GeckoDriver installer returned a missing driver file: {source}"
        )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=cache_path.parent,
            prefix=f"{cache_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
        shutil.copy2(source, temporary_path)
        if os.name != "nt":
            temporary_path.chmod(temporary_path.stat().st_mode | 0o111)
        os.replace(temporary_path, cache_path)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return _validate_driver_file(cache_path, "Autowell's cached geckodriver")


def resolve_geckodriver_path() -> str:
    configured_path = os.path.expandvars(
        os.getenv("GECKODRIVER_PATH", "").strip()
    )
    if configured_path:
        path = Path(configured_path.strip('"\'')).expanduser()
        return _validate_driver_file(path, "GECKODRIVER_PATH")

    installed_path = shutil.which("geckodriver")
    if installed_path:
        return _validate_driver_file(Path(installed_path), "geckodriver on PATH")

    cache_path = _managed_driver_path()
    if cache_path.is_file():
        try:
            return _validate_driver_file(cache_path, "Autowell's cached geckodriver")
        except RuntimeError:
            pass

    legacy_cache_path = _legacy_wdm_driver_path()
    if legacy_cache_path:
        try:
            return _cache_downloaded_driver(str(legacy_cache_path), cache_path)
        except OSError:
            # Use the valid webdriver-manager copy if the persistent cache cannot be written.
            return _validate_driver_file(
                legacy_cache_path, "webdriver-manager's cached geckodriver"
            )

    try:
        downloaded_path = GeckoDriverManager().install()
        try:
            return _cache_downloaded_driver(downloaded_path, cache_path)
        except OSError:
            return _validate_driver_file(
                Path(downloaded_path), "webdriver-manager's installed geckodriver"
            )
    except Exception as error:
        detail = str(error).splitlines()[0].strip()[:240]
        raise RuntimeError(
            "Could not install geckodriver automatically. Autowell checked "
            "GECKODRIVER_PATH, PATH, and its local driver cache before attempting "
            "the GitHub download. Check your network connection, or install "
            "geckodriver locally and set GECKODRIVER_PATH in .env."
            + (f" Download error: {detail}" if detail else "")
        ) from error
