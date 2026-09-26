from langchain.agents.middleware import dynamic_prompt

from src.memory.context import build_memory_context
from src.prompts.system_prompt import SYSTEM_PROMPT


def build_system_prompt(memory_context: str):
    # 记忆区块必须始终出现。为空时也给出显式空态，否则模型看不到
    # “当前没有任何长期记忆”这一事实，会依据历史对话里的“已经记住”作答。
    section = memory_context or build_memory_context([])
    return f"{SYSTEM_PROMPT.rstrip()}\n\n{section}\n"


@dynamic_prompt
def memory_aware_prompt(request):
    context = request.runtime.context
    memory_context = context.memory_context if context else ""
    return build_system_prompt(memory_context)
