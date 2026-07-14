from __future__ import annotations

import re

# Structural tags we use to frame user-visible content. User-controlled text must not be
# able to forge or close these, or it could break out and inject instructions.
RESERVED_TAGS = ("slack_thread", "user_request", "channel_memory", "integrations")

# Match any tag-like form of a reserved name, not just the exact `<tag>`/`</tag>`:
# whitespace (`</user_request >`), attributes (`<user_request foo="1">`), and mixed
# case all count. `\b` keeps distinct names like `<user_requests>` untouched.
_RESERVED_TAG_RE = re.compile(
    r"<\s*/?\s*(?:" + "|".join(RESERVED_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)


def neutralize_delimiters(text: str) -> str:
    """Escape any reserved framing tags appearing in untrusted text so the model reads them literally."""
    return _RESERVED_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text)
