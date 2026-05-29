import asyncio

from clients.bgm_client import BangumiClient


async def main():
    # 测试不需要 Token 的公共接口
    client = BangumiClient()

    print("--- 测试 1: 搜索接口 ---")
    search_res = await client.search_subjects(
        keyword="魔法少女小圆", sort="score", limit=3
    )
    print(search_res)

    print("\n--- 测试 2: 详情接口 ---")
    if isinstance(search_res, list) and len(search_res) > 0:
        first_id = search_res[0].id
        detail_res = await client.get_subject(subject_id=first_id)
        print(
            f"详情抓取结果: {detail_res.name}, 评分: {detail_res.score}, 总集数: {detail_res.total_episodes}"
        )

    print("\n--- 测试 3: 防御性测试 (假ID) ---")
    error_res = await client.get_subject(subject_id=999999999)
    print(f"预期返回错误字典: {error_res}")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
