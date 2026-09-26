from dataclasses import dataclass

from src.storage import LocalStore


@dataclass(frozen=True)
class AgentContext:
    profile_id: str
    thread_id: str
    memory_context: str
    local_store: LocalStore


MEMORY_SECTION_TITLE = "# 当前用户已确认的长期记忆"
EMPTY_MEMORY_NOTE = "（当前没有任何长期记忆。）"


def build_memory_context(memories: list[dict]):
    """渲染本轮注入的长期记忆区块。

    记忆为空时同样返回一个显式的空态区块。区块整体消失会让模型看不到
    “当前没有任何长期记忆”这一事实，于是依据历史对话里的“已经记住”作答，
    导致用户删掉记忆后再要求保存时不再调用工具。
    """
    lines = [MEMORY_SECTION_TITLE]
    if memories:
        for memory in memories:
            lines.append(f"- {memory['category']}/{memory['key']}: {memory['value']}")
    else:
        lines.append(EMPTY_MEMORY_NOTE)
    lines.extend(
        [
            "",
            "本区块是本轮唯一可信的长期记忆快照；历史对话中关于“已经记住”“已保存”的说法，"
            "一律以本区块为准。",
            "使用规则：当前用户本轮明确表达的要求优先于长期记忆；仅在相关时使用这些偏好，"
            "不要主动复述整份记忆。",
        ]
    )
    return "\n".join(lines)
