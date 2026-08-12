"""Ticket content survives the tool unchanged, whatever language it is in.

This is the guard for the rule that balances the English-only source rule: the
tool's own words are English, and the user's words are never touched. A tool
that quietly anglicises a team's tickets is worse than one that refuses to run.

Everything asserted here is character-for-character equality against the
original strings, including full-width punctuation and a deliberately
decomposed Unicode sequence that must not be silently normalised.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from click.testing import CliRunner

from lazyfish.cli import cli
from lazyfish.trackers.base import Attachment, Comment

from .conftest import FakeTracker, make_ticket, write_plan

TITLE = "登录后密码重置链接立即失效"
DESCRIPTION = (
    "用户报告：邮件里的重置链接点开就提示已过期。\n"
    "怀疑是 src/auth/reset_token.py 里的 RESET_TOKEN_TTL 配置有误。\n"
    "日本語のチケットも同様に扱われること。"
)
COMMENT_ONE = "已在预发布环境复现，日志见附件。"
COMMENT_TWO = "パスワードのリセット処理を確認しました。"
ATTACHMENT_NAME = "错误日志.log"
ATTACHMENT_BODY = "エラー: トークンの有効期限切れ\n错误：令牌已过期\n"

# 'é' written as 'e' + U+0301. Any well-meaning normalisation would turn this
# into a single codepoint, and the user would get back text they did not write.
DECOMPOSED = "cafe\u0301 mu\u0308nu\u0308"  # e + combining acute, u + diaeresis


def prepare(runner: CliRunner, tracker: FakeTracker) -> tuple[Path, str]:
    tracker.tickets = [
        make_ticket(
            key="PROJ-77",
            title=TITLE,
            description=DESCRIPTION + "\n" + DECOMPOSED,
            comments=(
                Comment("张伟", "2026-01-06T10:00:00.000+0000", COMMENT_ONE),
                Comment("山田太郎", "2026-01-06T12:00:00.000+0000", COMMENT_TWO),
            ),
            attachments=(
                Attachment(
                    filename=ATTACHMENT_NAME,
                    mime_type="text/plain",
                    size_bytes=len(ATTACHMENT_BODY.encode("utf-8")),
                    url="https://example.atlassian.net/attachment/9",
                ),
            ),
        )
    ]
    tracker.payloads = {ATTACHMENT_NAME: ATTACHMENT_BODY.encode("utf-8")}

    result = runner.invoke(cli, ["prep"])
    assert result.exit_code == 0, result.stdout + result.stderr
    worktree = Path(result.stdout.strip().splitlines()[-1][3:])
    return worktree, result.stdout


def test_ticket_json_holds_the_original_characters(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC15, first half."""
    worktree, _ = prepare(runner, tracker)
    path = worktree / "artifacts" / "PROJ-77" / "ticket.json"

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["title"] == TITLE
    assert payload["description"].startswith(DESCRIPTION)
    assert payload["comments"][0]["body"] == COMMENT_ONE
    assert payload["comments"][1]["body"] == COMMENT_TWO
    assert payload["comments"][0]["author"] == "张伟"
    assert payload["attachments"][0]["filename"] == ATTACHMENT_NAME

    # Stored as real UTF-8, not as \\uXXXX escapes: the file is meant to be read.
    assert TITLE.encode("utf-8") in path.read_bytes()


def test_context_file_reproduces_the_ticket_verbatim(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """AC15, second half."""
    worktree, _ = prepare(runner, tracker)
    context = (worktree / "CLAUDE.md").read_text(encoding="utf-8")

    assert TITLE in context
    for line in DESCRIPTION.splitlines():
        assert line in context
    assert COMMENT_ONE in context
    assert COMMENT_TWO in context
    assert "张伟" in context
    assert ATTACHMENT_NAME in context
    assert ATTACHMENT_BODY.strip() in context


def test_attachment_filename_and_content_survive(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    worktree, _ = prepare(runner, tracker)
    stored = worktree / "artifacts" / "PROJ-77" / "attachments" / ATTACHMENT_NAME
    assert stored.exists()
    assert stored.name == ATTACHMENT_NAME
    assert stored.read_text(encoding="utf-8") == ATTACHMENT_BODY


def test_terminal_output_is_not_mangled(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    _, stdout = prepare(runner, tracker)
    assert TITLE in stdout
    assert "�" not in stdout  # no replacement characters
    assert "\\u" not in stdout


def test_unicode_normalisation_is_not_applied(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """The tool must not "tidy up" text it did not write."""
    worktree, _ = prepare(runner, tracker)
    payload = json.loads(
        (worktree / "artifacts" / "PROJ-77" / "ticket.json").read_text(encoding="utf-8")
    )
    assert DECOMPOSED in payload["description"]
    assert not unicodedata.is_normalized("NFC", payload["description"])

    context = (worktree / "CLAUDE.md").read_text(encoding="utf-8")
    assert DECOMPOSED in context


def test_a_plan_written_in_another_language_is_accepted(
    runner: CliRunner, configured: dict[str, Path], tracker: FakeTracker
) -> None:
    """Schema keys are English; the values are the team's business."""
    worktree, _ = prepare(runner, tracker)
    write_plan(
        worktree,
        ticket="PROJ-77",
        understanding="重置令牌的有效期被设置为一小时，应为一天。",
        assumptions=["没有其他模块从配置里读取这个 TTL。"],
        acceptance_criteria=["现在生成的链接在 23 小时后仍然有效。"],
    )

    result = runner.invoke(cli, ["show"])
    assert result.exit_code == 0
    assert "重置令牌的有效期被设置为一小时，应为一天。" in result.stdout
    assert "Validation: passes" in result.stdout

    result = runner.invoke(cli, ["accept"], input="n\n漏掉了限流逻辑\n")
    assert result.exit_code == 0

    from lazyfish.db import Database

    database = Database(configured["data"] / "lazyfish.db")
    database.initialise()
    try:
        task = database.list_tasks()[0]
    finally:
        database.close()
    assert task.notes == "漏掉了限流逻辑"
    assert task.plan_accepted is False
