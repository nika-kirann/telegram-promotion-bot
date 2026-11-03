"""All user-facing copy (HTML parse mode)."""
from html import escape

from .config import PACKAGES, Package

DIV = "━━━━━━━━━━━━━━━━"

WELCOME = (
    "<b>Welcome to Promotion Center! 🚀</b>\n\n"
    "Want to promote your channel, group, business, or services in our community?\n\n"
    "Choose your preferred promotion package below and get your advertisement featured in our group."
)


def packages_text() -> str:
    lines = ["<b>📦 PROMOTION PACKAGES</b>", DIV]
    for p in PACKAGES.values():
        lines.append(f"{p.medal} <b>{p.name} PROMOTION</b>\n     ⏳ {p.hours} Hours  ·  ⭐ {p.price} Stars")
    lines += [DIV, "Your ad is published in our group for the full duration and removed automatically afterwards.",
              "", "👇 <i>Select a package to continue.</i>"]
    return "\n".join(lines)


def ask_ad(pkg: Package) -> str:
    return (
        f"{pkg.medal} <b>{pkg.name} PROMOTION</b>  ·  {pkg.hours} Hours  ·  ⭐ {pkg.price}\n{DIV}\n"
        "<b>✍️ Send your advertisement now.</b>\n\n"
        "Accepted formats:\n"
        "• Text advertisement\n"
        "• Photo with caption\n"
        "• Telegram channel / group links\n"
        "• Website links\n\n"
        "<i>Text up to 3500 characters · captions up to 900 characters.</i>\n"
        "Send /cancel to abort."
    )


def preview_header(pkg: Package) -> str:
    return (
        f"<b>👀 ADVERTISEMENT PREVIEW</b>\n{DIV}\n"
        f"📦 Package: <b>{pkg.name}</b>\n⏳ Duration: <b>{pkg.hours} Hours</b>\n⭐ Price: <b>{pkg.price} Stars</b>\n{DIV}\n"
        "Your ad will appear in the group exactly as shown above.\n"
        "Confirm to receive your secure Telegram Stars invoice."
    )


def invoice_description(pkg: Package) -> str:
    return f"{pkg.hours}-hour featured advertisement in our community group. Removed automatically after expiry."


def payment_success(pkg: Package) -> str:
    return (
        "<b>🎉 PAYMENT SUCCESSFUL!</b>\n\n"
        "Your promotion has been activated successfully.\n\n"
        f"📦 Package: {pkg.name}\n"
        f"⏳ Duration: {pkg.hours} Hours\n"
        f"⭐ Paid: {pkg.price} Stars\n\n"
        "Your advertisement is now live in our community.\n\n"
        "Thank you for supporting us! ❤️"
    )


EXPIRED = (
    "<b>⏰ YOUR PROMOTION HAS EXPIRED!</b>\n\n"
    "Your advertisement promotion period has ended.\n\n"
    "Thank you for advertising with us!\n\n"
    "Want to promote again?\n"
    "Tap /start to purchase another package."
)


def payment_pending_review(pkg: Package, promo_id: str) -> str:
    return (
        "<b>✅ PAYMENT RECEIVED</b>\n\n"
        f"📦 Package: {pkg.name}\n⭐ Paid: {pkg.price} Stars\n🆔 Promotion ID: <code>{promo_id}</code>\n\n"
        "Your advertisement is now in review by our team. Your promotion time starts only when it goes live, "
        "and you'll get a full refund if it isn't approved."
    )


def rejected(promo_id: str, refunded: bool) -> str:
    r = "Your Stars have been refunded to your account." if refunded else "Please contact an admin regarding your refund."
    return (f"<b>❌ ADVERTISEMENT NOT APPROVED</b>\n\nPromotion <code>{promo_id}</code> did not meet our guidelines.\n{r}\n\n"
            "Tap /start to submit a new advertisement.")


def removed_by_admin(promo_id: str) -> str:
    return f"<b>🗑️ PROMOTION REMOVED</b>\n\nPromotion <code>{promo_id}</code> was removed by an administrator. Contact admin for details."


def extended(promo_id: str, hours: int, expires_str: str) -> str:
    return f"<b>🔄 PROMOTION EXTENDED</b>\n\nPromotion <code>{promo_id}</code> got <b>+{hours}h</b>.\nNew expiry: <b>{expires_str}</b>"


RULES = (
    f"<b>📜 RULES & GUIDELINES</b>\n{DIV}\n"
    "1. No adult, gambling, scam, or illegal content.\n"
    "2. No misleading claims, phishing, or malware links.\n"
    "3. No hate speech, harassment, or political propaganda.\n"
    "4. One advertisement per order; keep it clear and honest.\n"
    "5. Ads may be reviewed. Non-compliant paid ads are refunded.\n"
    "6. Promotion time starts when your ad goes live.\n"
    "7. Ads are removed automatically when the package expires.\n"
    f"{DIV}\n<i>By purchasing, you agree to these rules.</i>"
)

HELP = (
    f"<b>ℹ️ HOW IT WORKS</b>\n{DIV}\n"
    "1️⃣ Tap <b>Buy Promotion</b> and choose a package\n"
    "2️⃣ Send your ad (text, photo + caption, links)\n"
    "3️⃣ Review the preview and confirm\n"
    "4️⃣ Pay securely with Telegram Stars ⭐\n"
    "5️⃣ Your ad goes live and is removed automatically on expiry\n"
    f"{DIV}\n<b>Commands</b>\n"
    "/start – Open promotion center\n/buy – Purchase a promotion\n/myorders – View promotion history\n"
    "/packages – View available packages\n/help – Bot instructions\n/cancel – Cancel current action"
)


def invitation() -> str:
    lines = ["<b>🚀 WANT TO PROMOTE YOUR CHANNEL OR BUSINESS?</b>", "", "Reach our community with a paid promotional post!", ""]
    for p in PACKAGES.values():
        lines.append(f"{p.medal} {p.hours} Hours – {p.price} ⭐")
    lines += ["", "Interested?", "👇 Start your promotion now!"]
    return "\n".join(lines)


STATUS_LABEL = {
    "draft": "📝 Draft",
    "awaiting_payment": "💳 Awaiting payment",
    "pending_approval": "🕒 In review",
    "active": "🟢 Active",
    "expired": "⏳ Expired",
    "deleted": "🗑️ Removed",
    "rejected": "❌ Rejected",
    "publish_failed": "⚠️ Publish failed",
}


def esc(s) -> str:
    return escape(str(s or ""))
