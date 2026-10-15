"""The operator's style profile: learned from their own messages only, a few at a time, on the back of
the next-message guess, and read beside a request — never inside a system prompt."""

from __future__ import annotations

from typing import Any

from protocore.contracts.types import Message, MessageRole, TextBlock

from daedalus.config import AssistConfig, Settings
from daedalus.host import predict, user_style
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database
from tests.support.models import model_config


def _user(text: str, origin: str = "operator") -> Message:
    return Message(role=MessageRole.user, content_blocks=[TextBlock(text=text)], metadata={"daedalus.origin": origin})


def _agent(text: str) -> Message:
    return Message(role=MessageRole.assistant, content_blocks=[TextBlock(text=text)])


async def _manager(settings: Settings, db: Database, answers: list[str]) -> tuple[SessionManager, list[dict[str, Any]]]:
    manager = SessionManager(settings, model_config(assist=AssistConfig()), db=db)  # type: ignore[arg-type]
    await manager.start()
    asked: list[dict[str, Any]] = []

    async def small_model(prompt: str, preset: str, *, purpose: str, session_id: str | None = None, max_tokens: int = 200,
                          temperature: float = 0.0, instructions: str = "") -> str:
        asked.append({"prompt": prompt, "instructions": instructions, "max_tokens": max_tokens})
        return answers.pop(0)

    manager.small_model = small_model  # type: ignore[method-assign]
    return manager, asked


async def test_only_the_operators_own_words_teach_it(settings: Settings, db: Database) -> None:
    manager, _ = await _manager(settings, db, [])
    try:
        chat = (await manager.create_session("chat")).session.id
        coach = (await manager.create_session("coach", metadata={"prompt_coach_of": chat})).session.id
        await manager.sessions.append_transcript(chat, [
            _user("keep it short, no bullet lists"), _agent("ok"), _user("go on", "autopilot"), _user("[schedule] check", "schedule"),
            _user("x" * 3000),
        ])
        await manager.sessions.append_transcript(coach, [_user("framed draft for the coach")])
        texts, through = await user_style.pending(manager.db, 0)
        assert texts[0] == "keep it short, no bullet lists" and len(texts) == 2 and len(texts[1]) == user_style.MESSAGE_CHARS
        assert through > 0
        # Newest eight only; everything read moves the watermark.
        await manager.sessions.append_transcript(chat, [_user(f"message {i}") for i in range(12)])
        texts, through = await user_style.pending(manager.db, through)
        assert texts == [f"message {i}" for i in range(4, 12)]
        assert await user_style.pending(manager.db, through) == ([], through)
    finally:
        await manager.close()


async def test_the_guess_carries_the_update_and_the_profile_after(settings: Settings, db: Database) -> None:
    answers = [
        "Run the tests\nSTYLE:\nWrites in Russian, short imperative requests; dislikes bullet lists.",
        "Ship it",
    ]
    manager, asked = await _manager(settings, db, answers)
    try:
        guesses = predict.Predictor(manager)
        sid = (await manager.create_session("s")).session.id
        await manager.sessions.append_transcript(sid, [_user("сделай короче, без списков"), _agent("Готово.")])
        assert await guesses.suggest(sid) == {"text": "Run the tests"}
        profile = await user_style.load(manager.db)
        assert profile.summary.startswith("Writes in Russian") and profile.learned == 1
        assert user_style.STYLE_INSTRUCTIONS in asked[0]["instructions"] and "сделай короче" in asked[0]["prompt"]
        assert asked[0]["max_tokens"] > predict.GUESS_TOKENS
        # Nothing new from the operator: a plain guess, with the profile beside the conversation and
        # never in the instructions, which stay the same words on every call.
        await manager.sessions.append_transcript(sid, [_agent("Also updated the docs.")])
        assert await guesses.suggest(sid) == {"text": "Ship it"}
        assert asked[1]["instructions"] == predict.PROMPT and asked[1]["max_tokens"] == predict.GUESS_TOKENS
        assert user_style.CONTEXT_HEAD in asked[1]["prompt"] and "Writes in Russian" in asked[1]["prompt"]
        # The operator took the guess with Tab and sent it unchanged: those are not their words to learn.
        await manager.sessions.append_transcript(sid, [_user("Ship it"), _agent("Shipped.")])
        answers.append("Thanks")
        assert await guesses.suggest(sid) == {"text": "Thanks"}
        assert asked[2]["instructions"] == predict.PROMPT
        # A reset forgets it and does not read the old messages again.
        assert (await user_style.reset(manager.db)).summary == ""
        assert (await user_style.pending(manager.db, (await user_style.load(manager.db)).through_seq))[0] == []
    finally:
        await manager.close()


def test_a_long_profile_is_cut_at_a_line() -> None:
    text = ("a line of the profile\n" * 200).strip()
    clipped = user_style.clip(text)
    assert len(clipped) <= user_style.MAX_CHARS and clipped.endswith("profile")
    head, learned = user_style.split_answer("guess\nSTYLE:\n```\nprofile\n```")
    assert head.strip() == "guess" and learned == "profile"
    assert user_style.split_answer("just a guess") == ("just a guess", None)


async def test_the_coach_reads_the_profile_in_its_turn_context(settings: Settings, db: Database) -> None:
    manager, _ = await _manager(settings, db, [])
    try:
        await user_style.save(manager.db, "Prefers terse prompts.", 0, 1)
        chat = await manager.create_session("chat")
        coach = await manager.create_session("coach", metadata={"prompt_coach_of": chat.session.id})
        opening = _user("help me write a prompt")
        coached = await manager._with_turn_context(coach, opening)
        plain = await manager._with_turn_context(chat, opening)
        assert "Prefers terse prompts." in coached.content_blocks[0].text  # type: ignore[union-attr]
        assert "Prefers terse prompts." not in plain.content_blocks[0].text  # type: ignore[union-attr]
    finally:
        await manager.close()
