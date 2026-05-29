import json
import os

from dotenv import load_dotenv
from openai import AzureOpenAI

# 加载 .env 文件中的环境变量
load_dotenv()

# ==========================================
# 1. 初始化 Azure OpenAI Client
# ==========================================
client = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_key=os.getenv("AZURE_OPENAI_API_KEY"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
)

# ==========================================
# 2. 定义 Tools Schema (大模型认知层)
# ==========================================
# 这是把你写好的 Python 函数 Docstring 转化为 OpenAI 认识的 JSON Schema 格式
tools = [
    {
        "type": "function",
        "function": {
            "name": "search_bangumi_subject",
            "description": "搜索 Bangumi 条目（番剧/动画/书籍/音乐/游戏），返回符合指定类型的精简条目列表。当用户想要查找特定题材、名称或分类的作品时调用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，支持日语、中文、英文等多语言（例如：'百合', '科幻', '進撃の巨人'）",
                    },
                    "subject_type": {
                        "type": "integer",
                        "enum": [1, 2, 3, 4, 6],
                        "description": "条目类型 ID，默认为 2（动画）。1=书籍, 2=动画, 3=音乐, 4=游戏, 6=三次元",
                    },
                    "sort": {
                        "type": "string",
                        "enum": ["match", "heat", "rank", "score"],
                        "description": "排序方式。如果用户要求'高分'或'口碑好'，必须使用 'rank' 或 'score'；默认使用 'score'。",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_bangumi_subject_detail",
            "description": "获取 Bangumi 上某个条目的完整详细信息（简介、集数、播出日期、评分、制作人员等）。通常在 search_bangumi_subject 之后使用，或在用户明确知道条目 ID 时直接调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject_id": {
                        "type": "integer",
                        "description": "Bangumi 条目的唯一数字 ID（可从搜索结果中获得）",
                    }
                },
                "required": ["subject_id"],
            },
        },
    },
]


# ==========================================
# 3. 构造测试场景与请求
# ==========================================
def run_naked_tool_test():
    print("🚀 正在启动原生 Tool Calling 基准测试...")

    # 系统提示词：赋予基础人设，并明确工具使用策略
    messages = [
        {
            "role": "system",
            "content": "你是 Bangumi Agentic System，一个专业的追番管家。你可以使用工具来查询番剧信息。请不要编造数据，必须优先使用工具获取真实信息。",
        },
        {
            "role": "user",
            "content": "@BangumiBot 帮我推荐几部评分比较高的百合番，最好带有简介",
        },
    ]

    try:
        # 发起调用
        response = client.chat.completions.create(
            model=os.getenv(
                "AZURE_OPENAI_CHAT_DEPLOYMENT", "gpt-5-mini"
            ),  # 从 .env 读取 deployment 名
            messages=messages,
            tools=tools,
            tool_choice="auto",  # 让大模型自主决定是否调用工具以及调用哪个
            temperature=0.1,  # 工具调度通常需要较低的温度以保证确定性
        )

        # 解析结果
        message = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        print("\n" + "=" * 50)
        print(f"✅ 测试完成！Finish Reason: {finish_reason}")
        print("=" * 50)

        if message.tool_calls:
            print("\n🛠️ 模型决定调用以下工具：")
            for tool_call in message.tool_calls:
                func_name = tool_call.function.name
                func_args = tool_call.function.arguments

                print(f"  -> 目标函数: {func_name}")
                print(
                    f"  -> 生成参数: {json.dumps(json.loads(func_args), indent=2, ensure_ascii=False)}"
                )

        else:
            print("\n⚠️ 模型没有调用工具，而是直接返回了文本回复：")
            print(message.content)

    except Exception as e:
        print(f"\n❌ 测试失败，发生异常: {e}")


if __name__ == "__main__":
    run_naked_tool_test()
