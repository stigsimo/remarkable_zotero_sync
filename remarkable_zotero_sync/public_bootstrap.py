from __future__ import annotations

import getpass
import json
import re
import shutil
import tomllib
from pathlib import Path

DEFAULT_CONFIG_NAME = "config.toml"
CONFIG_TEMPLATE_NAME = "config.template.toml"
USER_DATA_DIR_NAME = "user_data"
SECRETS_FILE_NAME = "secrets.json"


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_config_path(raw_value: str | Path | None) -> Path:
    if raw_value in {None, ""}:
        return project_root() / DEFAULT_CONFIG_NAME

    path = Path(raw_value).expanduser()
    if not path.is_absolute():
        path = (project_root() / path).resolve()
    return path


def config_template_path() -> Path:
    return project_root() / CONFIG_TEMPLATE_NAME


def user_data_dir(config_path: Path) -> Path:
    return config_path.parent / USER_DATA_DIR_NAME


def secrets_path(config_path: Path) -> Path:
    return user_data_dir(config_path) / SECRETS_FILE_NAME


def ensure_public_config(config_path: Path) -> bool:
    if config_path.exists():
        return False

    template_path = config_template_path()
    if not template_path.exists():
        raise FileNotFoundError(
            f"Config template not found: {template_path}"
        )

    config_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template_path, config_path)
    print(f"Created a starter config at {config_path}.")
    print(
        "If your Zotero library is not stored in the default ~/Zotero folder, "
        "edit db_path and storage_dir in that file before syncing."
    )
    return True


def read_raw_config(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def load_saved_password(config_path: Path) -> str:
    path = secrets_path(config_path)
    if not path.exists():
        return ""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""

    return str(payload.get("remarkable_password", "")).strip()


def save_saved_password(config_path: Path, password: str) -> None:
    path = secrets_path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"remarkable_password": password}
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def remarkable_host_user(config_path: Path) -> tuple[str, str]:
    raw_config = read_raw_config(config_path)
    remarkable_data = raw_config.get("remarkable", {})
    if not isinstance(remarkable_data, dict):
        remarkable_data = {}
    host = str(remarkable_data.get("host", "10.11.99.1")).strip() or "10.11.99.1"
    user = str(remarkable_data.get("user", "root")).strip() or "root"
    return host, user


def password_from_config(config_path: Path) -> str:
    raw_config = read_raw_config(config_path)
    remarkable_data = raw_config.get("remarkable", {})
    if not isinstance(remarkable_data, dict):
        return ""
    return str(remarkable_data.get("password", "")).strip()


def prompt_for_password(config_path: Path) -> str:
    host, user = remarkable_host_user(config_path)
    print()
    print("reMarkable SSH setup")
    print(
        "The script needs your tablet's SSH password one time so it can talk to "
        "the device over USB."
    )
    print(
        "1. Turn on Developer mode on the reMarkable. The exact menu names can "
        "vary a little by software version, but Developer mode is available in "
        "the tablet's Settings."
    )
    print(
        "2. After Developer mode is enabled, find the screen on the tablet that "
        "shows the SSH password. That on-device SSH password is the one this "
        "script needs."
    )
    print(
        "3. Connect the reMarkable to your computer over USB before syncing."
    )
    print()
    password = getpass.getpass(
        prompt=f"Enter the reMarkable SSH password for {user}@{host}: "
    ).strip()
    if not password:
        raise RuntimeError("No SSH password was entered.")

    save_saved_password(config_path, password)
    print(
        f"Saved the SSH password locally in {secrets_path(config_path)}. "
        "You will not be asked for it again unless you run --reset."
    )
    return password


def ensure_remarkable_password(config_path: Path) -> str:
    config_password = password_from_config(config_path)
    if config_password:
        return config_password

    saved_password = load_saved_password(config_path)
    if saved_password:
        return saved_password

    return prompt_for_password(config_path)


def _render_toml_value(value: str | bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value)


def set_zotero_config_value(
    config_path: Path,
    *,
    key: str,
    value: str | bool,
) -> None:
    text = config_path.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[zotero\]\n(?P<body>.*?)(?=^\[|\Z)", text)
    if match is None:
        raise RuntimeError(f"Could not find the [zotero] section in {config_path}")

    body = match.group("body")
    rendered = _render_toml_value(value)
    pattern = rf"(?m)^{re.escape(key)}\s*=.*$"
    replacement = f"{key} = {rendered}"
    if re.search(pattern, body):
        body = re.sub(pattern, replacement, body, count=1)
    else:
        body = body.rstrip() + f"\n{replacement}\n"

    updated = text[: match.start("body")] + body + text[match.end("body") :]
    config_path.write_text(updated, encoding="utf-8")


def choose_and_save_library_scope(config_path: Path) -> bool:
    print("Choose what you want to sync from Zotero:")
    print("1. Only the collection named 'reMarkable Sync' and its subcollections (Recommended)")
    print("2. Your entire Zotero library")

    selection_map = {
        "1": ("collection", "Only 'reMarkable Sync' and its subcollections"),
        "2": ("library", "Your entire Zotero library"),
    }

    while True:
        choice = input("Enter 1 or 2: ").strip()
        if choice in selection_map:
            break
        print("Please enter 1 or 2.")

    scope_value, scope_label = selection_map[choice]
    print(f"You selected: {scope_label}")
    print("Type yes to save this choice.")
    confirmation = input("Type yes to continue: ").strip()
    if confirmation != "yes":
        print("No changes were made.")
        return False

    set_zotero_config_value(
        config_path,
        key="library_scope",
        value=scope_value,
    )
    print(f"Saved sync scope: {scope_label}")
    return True


def remove_path(target: Path) -> None:
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target)
        return
    target.unlink()


def reset_public_workspace(config_path: Path) -> bool:
    print("Reset requested.")
    print(
        "This will remove the local config, the saved SSH password, cached sync "
        "state, and downloaded/exported annotated PDFs inside this zotero_sync folder."
    )
    confirmation = input("Type yes to continue: ").strip()
    if confirmation != "yes":
        print("Reset cancelled.")
        return False

    remove_path(config_path)
    remove_path(config_path.parent / ".cache")
    remove_path(config_path.parent / "annotated_exports")
    remove_path(user_data_dir(config_path))
    ensure_public_config(config_path)
    print("Reset complete.")
    return True
