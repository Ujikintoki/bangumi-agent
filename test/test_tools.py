import asyncio

from tools.bgm_tools import get_bangumi_subject_detail, search_bangumi_subject


async def main():
    print("--- 验收测试 1: 搜索工具 (必须返回纯字符串，且只有动画) ---")
    # 模拟大模型的调用方式：只传关键词
    search_result_str = await search_bangumi_subject(keyword="魔法少女小圆")

    print(f"返回类型: {type(search_result_str)}")  # 预期: <class 'str'>
    print(
        f"返回内容截取: {search_result_str[:300]}...\n"
    )  # 预期: 一个 JSON 字符串，里面看不到 OST 和手游

    print("--- 验收测试 2: 详情工具 ---")
    # 找一个具体的动画 ID 测试
    detail_result_str = await get_bangumi_subject_detail(subject_id=9717)
    print(f"返回类型: {type(detail_result_str)}")
    print(f"返回内容截取: {detail_result_str[:300]}...")


if __name__ == "__main__":
    asyncio.run(main())
