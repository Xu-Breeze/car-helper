from langchain.tools import ToolRuntime

from src.memory import AgentContext, build_memory_context
from src.memory.middleware import build_system_prompt
from src.prompts.system_prompt import SYSTEM_PROMPT
from src.memory.tools import (
    _REMEMBER_INTENT_PATTERNS,
    _matches_intent,
    forget_user_preference,
    remember_user_preference,
)
from src.storage import LocalStore


def test_long_term_phrasings_are_recognized_as_remember_intent():
    # “长期”后可以不跟“记住”，直接接偏好动词（长期只考虑 / 长期偏好 / 长期都选）
    for text in ("长期只考虑增程式", "长期偏好SUV", "长期都选纯电"):
        assert _matches_intent(text, _REMEMBER_INTENT_PATTERNS), text


def test_temporary_constraints_are_not_read_as_long_term_intent():
    for text in ("这次预算20万，帮我看看有什么车", "给父母选车，要舒服一点的"):
        assert not _matches_intent(text, _REMEMBER_INTENT_PATTERNS), text


def test_common_long_term_phrasings_are_recognized_as_remember_intent():
    # “以后/今后”后可直接接意愿动词，不要求中间有副词（以后只买 / 以后买 / 今后就选）；
    # “一直”后允许夹程度副词，且词表要认“倾向”。
    for text in (
        "以后只买增程式",
        "以后只考虑增程式",
        "以后买增程式",
        "以后打算买增程式",
        "今后就选纯电动",
        "一直倾向插混",
        "我一直比较倾向插混",
    ):
        assert _matches_intent(text, _REMEMBER_INTENT_PATTERNS), text


def test_time_only_phrases_are_not_latched_as_long_term_intent():
    # 只提时间、没有意愿动词的句子不得被当成长期偏好写进记忆。
    for text in ("以后再说吧", "以后有时间再聊", "这次先看看增程式"):
        assert not _matches_intent(text, _REMEMBER_INTENT_PATTERNS), text


def create_runtime(store, profile_id, message):
    return ToolRuntime(
        state={"messages": [{"role": "user", "content": message}]},
        context=AgentContext(profile_id, "session_123", "", store),
        config={},
        stream_writer=lambda _: None,
        tool_call_id="tool_call_1",
        store=None,
    )


def test_memory_context_is_bounded_to_structured_facts():
    context = build_memory_context(
        [
            {
                "category": "energy_preference",
                "key": "preferred_energy",
                "value": "纯电动",
            }
        ]
    )
    prompt = build_system_prompt(context)

    assert "energy_preference/preferred_energy: 纯电动" in prompt
    assert "本轮明确表达的要求优先" in prompt


def test_memory_tool_schema_hides_runtime_context():
    remember_schema = remember_user_preference.tool_call_schema.model_json_schema()
    forget_schema = forget_user_preference.tool_call_schema.model_json_schema()

    assert set(remember_schema["properties"]) == {"category", "key", "value"}
    assert set(forget_schema["properties"]) == {"category", "key"}


async def test_memory_tools_use_injected_profile_and_thread(tmp_path):
    store = LocalStore(tmp_path / "car_helper.db")
    store.initialize()
    profile_id = store.get_default_profile()["profile_id"]
    store.upsert_conversation(profile_id, "session_123", "请记住我只考虑纯电车")
    runtime = create_runtime(store, profile_id, "请记住我以后都只考虑纯电车")

    remembered = await remember_user_preference.coroutine(
        category="energy_preference",
        key="preferred_energy",
        value="纯电动",
        runtime=runtime,
    )
    forget_runtime = create_runtime(store, profile_id, "请忘记我只考虑纯电车这项偏好")
    forgotten = await forget_user_preference.coroutine(
        category="energy_preference",
        key="preferred_energy",
        runtime=forget_runtime,
    )

    assert remembered["状态"] == "已保存"
    assert forgotten["状态"] == "已删除"
    assert store.list_memories(profile_id) == []


async def test_temporary_purchase_constraints_cannot_be_persisted(tmp_path):
    store = LocalStore(tmp_path / "car_helper.db")
    store.initialize()
    profile_id = store.get_default_profile()["profile_id"]
    store.upsert_conversation(profile_id, "session_123", "这次预算20万，给父母买车")

    for message, category, key, value in (
        ("这次预算20万", "budget_preference", "preferred_budget", "20万元"),
        ("给父母买车，想要SUV", "family_context", "buyer_context", "给父母买车"),
    ):
        result = await remember_user_preference.coroutine(
            category=category,
            key=key,
            value=value,
            runtime=create_runtime(store, profile_id, message),
        )
        assert result.startswith("长期记忆未保存")

    assert store.list_memories(profile_id) == []


async def test_memory_cannot_be_forgotten_without_explicit_user_intent(tmp_path):
    store = LocalStore(tmp_path / "car_helper.db")
    store.initialize()
    profile_id = store.get_default_profile()["profile_id"]
    store.upsert_conversation(profile_id, "session_123", "请记住我以后都只考虑纯电车")
    store.remember(
        profile_id,
        category="energy_preference",
        key="preferred_energy",
        value="纯电动",
        source_thread_id="session_123",
    )

    result = await forget_user_preference.coroutine(
        category="energy_preference",
        key="preferred_energy",
        runtime=create_runtime(store, profile_id, "这次可以看看混动车"),
    )

    assert result.startswith("长期记忆未删除")
    assert len(store.list_memories(profile_id)) == 1


def test_empty_memory_still_renders_the_memory_section():
    # 记忆为空时区块不能整体消失：否则模型看不到“当前没有任何记忆”这一事实，
    # 就会依据历史对话里的“已经记住”作答，用户删掉记忆后不再重新保存。
    context = build_memory_context([])

    assert "当前用户已确认的长期记忆" in context
    assert "当前没有任何长期记忆" in context


def test_system_prompt_always_carries_the_memory_section():
    empty_context_prompt = build_system_prompt(build_memory_context([]))

    assert empty_context_prompt != SYSTEM_PROMPT
    assert "当前没有任何长期记忆" in empty_context_prompt
    # 连空字符串（上下文缺失的兜底路径）也不得退化成裸提示词。
    assert "当前没有任何长期记忆" in build_system_prompt("")


def test_system_prompt_makes_the_memory_section_authoritative():
    assert "唯一依据" in SYSTEM_PROMPT
    assert "不作为依据" in SYSTEM_PROMPT


def test_deleted_memory_leaves_an_explicit_empty_snapshot(tmp_path):
    store = LocalStore(tmp_path / "car_helper.db")
    store.initialize()
    profile_id = store.get_default_profile()["profile_id"]
    store.upsert_conversation(profile_id, "session_123", "请记住我长期只考虑增程式")
    memory = store.remember(
        profile_id,
        category="energy_preference",
        key="preferred_energy",
        value="增程式",
        source_thread_id="session_123",
    )

    saved_turn = build_system_prompt(build_memory_context(store.list_memories(profile_id)))
    assert "preferred_energy: 增程式" in saved_turn

    store.forget(profile_id, memory_id=memory["memory_id"])
    next_turn = build_system_prompt(build_memory_context(store.list_memories(profile_id)))

    assert "preferred_energy: 增程式" not in next_turn
    # 关键：删掉之后这一轮仍要明确告知模型“当前没有任何长期记忆”，
    # 模型才有依据重新调用保存工具，而不是沿用历史里的“已经记住”。
    assert "当前没有任何长期记忆" in next_turn
