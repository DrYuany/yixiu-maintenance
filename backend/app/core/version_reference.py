from __future__ import annotations

import re

from app.schemas.agent import WindowVersion


def chronological_versions(versions: list[WindowVersion]) -> list[WindowVersion]:
    return sorted(
        versions,
        key=lambda item: (item.generatedAt, item.id),
    )


def resolve_version_reference(
    message: str,
    versions: list[WindowVersion],
    selected_version: WindowVersion | None = None,
) -> tuple[WindowVersion | None, str | None]:
    if not versions:
        return None, None
    text = "".join(message.split())
    ordered = chronological_versions(versions)

    version_id_match = re.search(r"ver_[a-f0-9]{8}", text, flags=re.IGNORECASE)
    if version_id_match:
        version_id = version_id_match.group(0).lower()
        return next((item for item in ordered if item.id.lower() == version_id), None), "id"

    if any(token in text for token in ("最新版", "最新方案", "最后一版", "最后一个方案")):
        return ordered[-1], "latest"

    if any(token in text for token in ("上一版", "上一个方案", "前一版")):
        if selected_version is None or selected_version.id not in {item.id for item in ordered}:
            return None, "previous"
        index = next(i for i, item in enumerate(ordered) if item.id == selected_version.id)
        return (ordered[index - 1] if index > 0 else None), "previous"

    patterns = (
        r"方案\s*0*(\d+)",
        r"版本\s*0*(\d+)",
        r"第\s*0*(\d+)\s*(?:版|个方案|个版本)",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        ordinal = int(match.group(1))
        if 1 <= ordinal <= len(ordered):
            return ordered[ordinal - 1], "ordinal"
        return None, "ordinal"
    return None, None


def is_version_switch_request(message: str) -> bool:
    text = "".join(message.split())
    return any(
        token in text
        for token in (
            "切换到方案",
            "切到方案",
            "改用方案",
            "用方案",
            "看方案",
            "聊聊方案",
            "换成方案",
        )
    )
