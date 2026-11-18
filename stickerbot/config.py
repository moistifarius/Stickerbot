"""Configuration from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _ids(raw: str) -> set[int]:
    return {int(x) for x in raw.replace(" ", "").split(",") if x}


def _list(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Config:
    bot_token: str
    owner_id: int
    pack_basename: str = "stickies"
    pack_title: str = ""  # "" -> "<Basename> <n>"
    data_dir: Path = Path("/data")
    extra_packs: list[str] = field(default_factory=list)
    luke_id: int = 0
    troll_chance: float = 1.0
    allowed_users: set[int] = field(default_factory=set)
    allowed_chats: set[int] = field(default_factory=set)
    hoover_groups: bool = True
    default_emoji: str = "🙂"
    pack_limit: int = 120
    legacy_state: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "stickerbot.db"

    @property
    def heartbeat_path(self) -> Path:
        return self.data_dir / "heartbeat"

    def is_trusted(self, user_id: int | None) -> bool:
        return user_id is not None and (user_id == self.owner_id or user_id in self.allowed_users)

    def title_for(self, index: int) -> str:
        base = self.pack_title or self.pack_basename.capitalize()
        return f"{base} {index}"[:64]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ if env is None else env)
        token, owner = env.get("BOT_TOKEN", ""), env.get("OWNER_ID", "")
        if not token or not owner:
            raise SystemExit("Must set BOT_TOKEN and OWNER_ID env vars")
        data_dir = Path(env.get("DATA_DIR", "/data"))
        legacy = env.get("DATA_FILE") or str(data_dir / "pack_state.json")
        return cls(
            bot_token=token,
            owner_id=int(owner),
            pack_basename=env.get("PACK_BASENAME", "stickies"),
            pack_title=env.get("PACK_TITLE", ""),
            data_dir=data_dir,
            extra_packs=_list(env.get("EXTRA_PACKS", "")),
            luke_id=int(env.get("LUKE_ID", "0") or 0),
            troll_chance=float(env.get("TROLL_CHANCE", "1.0")),
            allowed_users=_ids(env.get("ALLOWED_USERS", "")),
            allowed_chats=_ids(env.get("ALLOWED_CHATS", "")),
            hoover_groups=env.get("HOOVER_GROUPS", "1").lower() not in ("0", "false", "no"),
            default_emoji=env.get("DEFAULT_EMOJI", "🙂"),
            pack_limit=int(env.get("PACK_LIMIT", "120")),
            legacy_state=Path(legacy),
        )
