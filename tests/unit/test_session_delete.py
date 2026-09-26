import asyncio

import pytest

from src.agent import delete_conversation
from src.storage import create_local_store


class FakeCheckpointer:
    def __init__(self):
        self.deleted = []

    async def adelete_thread(self, thread_id):
        self.deleted.append(thread_id)


@pytest.fixture
def store(tmp_path):
    local_store = create_local_store(tmp_path / "car_helper.db")
    profile_id = local_store.get_default_profile()["profile_id"]
    local_store.upsert_conversation(profile_id, "session_first", "第一次提问")
    local_store.remember(
        profile_id,
        category="budget_preference",
        key="budget_wan",
        value="20万以内",
        source_thread_id="session_first",
    )
    return local_store, profile_id


def answer_with(monkeypatch, *answers):
    replies = iter(answers)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))


def run_delete(local_store, checkpointer, profile_id, current_session_id):
    return asyncio.run(
        delete_conversation(local_store, checkpointer, profile_id, current_session_id)
    )


def test_delete_current_session_switches_to_a_fresh_one(store, monkeypatch, capsys):
    local_store, profile_id = store
    checkpointer = FakeCheckpointer()
    answer_with(monkeypatch, "1", "y")

    result = run_delete(local_store, checkpointer, profile_id, "session_first")

    assert checkpointer.deleted == ["session_first"]
    assert local_store.list_conversations(profile_id) == []
    assert result != "session_first"
    assert "已删除会话" in capsys.readouterr().out


def test_delete_keeps_long_term_memories(store, monkeypatch):
    local_store, profile_id = store
    answer_with(monkeypatch, "1", "y")

    run_delete(local_store, FakeCheckpointer(), profile_id, "session_other")

    memories = local_store.list_memories(profile_id)
    assert len(memories) == 1
    assert memories[0]["value"] == "20万以内"
    # The source conversation is gone, so the link is severed but the
    # remembered preference survives.
    assert memories[0]["source_thread_id"] is None


def test_cancelled_confirmation_deletes_nothing(store, monkeypatch, capsys):
    local_store, profile_id = store
    checkpointer = FakeCheckpointer()
    answer_with(monkeypatch, "1", "n")

    result = run_delete(local_store, checkpointer, profile_id, "session_first")

    assert result == "session_first"
    assert checkpointer.deleted == []
    assert len(local_store.list_conversations(profile_id)) == 1
    assert "已取消" in capsys.readouterr().out


def test_empty_history_reports_and_returns(tmp_path, monkeypatch, capsys):
    local_store = create_local_store(tmp_path / "empty.db")
    profile_id = local_store.get_default_profile()["profile_id"]

    result = run_delete(local_store, FakeCheckpointer(), profile_id, "session_first")

    assert result == "session_first"
    assert "暂无会话历史" in capsys.readouterr().out
