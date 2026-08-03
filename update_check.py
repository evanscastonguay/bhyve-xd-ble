"""
Optional, opt-in update check. When enabled (top-level `"update_check": true` in config.json — default
OFF), the server asks GitHub **once on startup** for the latest released version and surfaces it in
`GET /api/version`. It sends **no data about you or your device** — it's a read-only "is there a newer
version?" fetch, like a package manager checking for updates. Best-effort: any failure is ignored.
"""
from __future__ import annotations

GITHUB_LATEST = "https://api.github.com/repos/evanscastonguay/bhyve-xd-ble/releases/latest"


def _tuple(v):
    """Parse a version like 'v1.4.0' / '1.4.0-beta' -> (1,4,0); None if unparseable."""
    core = (v or "").lstrip("vV").split("-")[0].strip()
    try:
        return tuple(int(x) for x in core.split(".")) if core else None
    except ValueError:
        return None


def is_newer(latest, current) -> bool:
    a, b = _tuple(latest), _tuple(current)
    return bool(a and b and a > b)


async def _default_fetch():
    """GET the latest release tag from GitHub (lazy aiohttp). Returns a version string or None."""
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(GITHUB_LATEST, headers={"Accept": "application/vnd.github+json"}) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return ((data.get("tag_name") or "").lstrip("vV")) or None


async def check(current: str, fetch=None) -> dict:
    """Return {latest, update_available}. Never raises."""
    fetch = fetch or _default_fetch
    try:
        latest = await fetch()
    except Exception:  # noqa: BLE001 — best-effort; an update check must never affect control
        latest = None
    return {"latest": latest, "update_available": is_newer(latest, current) if latest else False}
