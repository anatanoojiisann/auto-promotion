"""Platform implementations shipped with Auto Promotion."""

BACKENDS = {
    "telegram": ("Telethon / MTProto", "telethon"),
    "reddit": ("Local browser / Playwright", "playwright"),
    "instagram": ("Instagram Graph API", None),
    "x": ("Local browser / Playwright", "playwright"),
    "discord": ("Discord Bot / HTTP API", None),
    "tiktok": ("TikTok Accounts API / OAuth", None),
}
