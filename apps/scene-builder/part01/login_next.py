"""Safe same-origin return URLs for Scene and independent app sign-in.

Never accept an absolute URL, protocol-relative reference, backslash alias,
control character or traversal path from an untrusted browser query/form.
"""
from urllib.parse import unquote, urlencode, urlsplit


def safe_login_target(candidate: str | None) -> str:
    if not isinstance(candidate, str) or not candidate or len(candidate) > 2048:
        return "/"
    if candidate != candidate.strip() or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return "/"
    if "\\" in candidate or "#" in candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return "/"
    if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith("/"):
        return "/"
    path = parsed.path
    # Reject encoded //, backslashes and dot segments at each decoding depth.
    for _ in range(3):
        if path.startswith("//") or "\\" in path or any(
            part in (".", "..") for part in path.split("/")
        ) or any(ord(char) < 32 or ord(char) == 127 for char in path):
            return "/"
        path = unquote(path)
    if path.startswith(("/login", "/logout", "/internal/")) or path in ("/internal",):
        return "/"
    return candidate


def login_redirect_url(candidate: str | None, *, error: str = "") -> str:
    params = {"next": safe_login_target(candidate)}
    if error:
        params["error"] = error
    return "/login?" + urlencode(params)
