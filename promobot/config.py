import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Package:
    key: str
    name: str
    medal: str
    hours: int
    price: int  # Telegram Stars


PACKAGES: dict[str, Package] = {
    "basic": Package("basic", "BASIC", "🥉", 3, 49),
    "standard": Package("standard", "STANDARD", "🥈", 12, 99),
    "premium": Package("premium", "PREMIUM", "🥇", 24, 149),
}

CURRENCY = "XTR"


def _parse_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Config:
    bot_token: str
    group_id: int
    admin_ids: frozenset[int]
    mongodb_uri: str = ""
    mongodb_db: str = "promotion_bot"
    port: int = 8000
    admin_contact: str = ""
    require_approval_default: bool = False
    invite_interval_default: int = 6  # hours, 0 = disabled
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("BOT_TOKEN environment variable is required")
        group = os.getenv("GROUP_ID", "").strip()
        if not group:
            raise RuntimeError("GROUP_ID environment variable is required")
        return cls(
            bot_token=token,
            group_id=int(group),
            admin_ids=_parse_ids(os.getenv("ADMIN_IDS", "")),
            mongodb_uri=os.getenv("MONGODB_URI", "").strip(),
            mongodb_db=os.getenv("MONGODB_DB", "promotion_bot").strip() or "promotion_bot",
            port=int(os.getenv("PORT", "8000") or 8000),
            admin_contact=os.getenv("ADMIN_CONTACT", "").strip().lstrip("@"),
            require_approval_default=os.getenv("REQUIRE_APPROVAL", "false").lower() in ("1", "true", "yes"),
            invite_interval_default=int(os.getenv("INVITE_INTERVAL_HOURS", "6") or 6),
        )

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.admin_ids
