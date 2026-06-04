"""BangumiClient 手动冒烟测试（p1 API）。

验证搜索、详情、防御性错误处理三条关键路径。
运行：python test/test_client.py
"""

import asyncio

from clients import BangumiClient
from schemas.tools_input import SearchBangumiInput


async def main():
    """测试不需要 Token 的公共接口。"""
    client = BangumiClient()

    print("--- 测试 1: 搜索接口 ---")
    search_res = await client.search(
        SearchBangumiInput(keyword="魔法少女小圆", entity_type="subject", limit=3)
    )
    if "_error" in search_res:
        print(f"搜索失败: {search_res['_error']}")
    else:
        results = search_res.get("results", [])
        print(f"搜索结果数: {len(results)}")
        for r in results[:3]:
            print(f"  - {r.get('name', '?')} (id={r.get('id', 0)})")

    print("\n--- 测试 2: 详情接口 ---")
    detail_res = await client.get_subject_detail(subject_id=9717)
    if "_error" in detail_res:
        print(f"详情失败: {detail_res['_error']}")
    else:
        print(
            f"详情抓取结果: {detail_res.get('name', '?')}, "
            f"评分: {detail_res.get('score', 0)}, "
            f"总集数: {detail_res.get('eps', 0)}"
        )

    print("\n--- 测试 3: 防御性测试 (假ID) ---")
    error_res = await client.get_subject_detail(subject_id=999999999)
    if "_error" in error_res:
        print(f"预期返回错误: {error_res['_error']}")
    else:
        print(f"意外成功: {error_res}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
