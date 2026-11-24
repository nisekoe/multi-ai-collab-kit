"""Open a Codex draft from the Windows desktop card, without submitting a turn."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote, urlencode

from codex_picker_adapter import default_workspace_allowed

MAX_PROMPT_URL_LENGTH = 6000
MAX_LINK_LENGTH = 7500


def prompt_fits(prompt: str) -> bool:
    try:
        return bool(len(prompt) <= MAX_PROMPT_URL_LENGTH and prompt.strip()
                    and len(quote(prompt, safe="")) <= MAX_PROMPT_URL_LENGTH)
    except UnicodeError:
        return False


def draft_link(cwd: str, prompt: str) -> str:
    work = Path(cwd)
    if not work.is_absolute() or cwd.startswith(("\\\\", "//")):
        raise ValueError("新窗口必须使用本机绝对目录")
    if not default_workspace_allowed(("local", ""), str(work)):
        raise ValueError("禁止在受保护目录新开协作窗口")
    work = work.resolve(strict=True)
    if str(work).startswith(("\\\\", "//")) or not work.is_dir():
        raise ValueError("新窗口必须使用本机现有目录")
    if not default_workspace_allowed(("local", ""), str(work)):
        raise ValueError("禁止在受保护目录新开协作窗口")
    if not prompt_fits(prompt):
        raise ValueError("前台草稿为空或太长")
    url = "codex://threads/new?" + urlencode({"path": str(work), "prompt": prompt}, quote_via=quote)
    if len(url) > MAX_LINK_LENGTH:
        raise ValueError("前台草稿链接太长")
    return url


def request_draft(cwd: str, prompt: str) -> None:
    if os.name != "nt":
        raise OSError("前台草稿需要 Windows 桌面宿主")
    os.startfile(draft_link(cwd, prompt))
