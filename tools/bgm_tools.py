"""
AI Agent 工具函数层

将底层 BangumiClient 包装为 LLM 可直接调用的异步工具函数。
每个函数附带详尽的 Google Style 中文 Docstring，帮助大模型
理解工具用途、参数含义及最佳调用时机。
"""

from __future__ import annotations

import json
from typing import Any

from clients.bgm_client import BangumiClient


async def search_bangumi_subject(
    keyword: str,
    subject_type: int = 2,
    sort: str = "score",
) -> str:
    """搜索 Bangumi 条目，返回符合指定类型的精简条目列表（JSON 格式字符串）。

    当用户想要查找动画、书籍、音乐、游戏等条目时调用此工具。
    典型场景：
    - "帮我搜一下《进击的巨人》"
    - "推荐几部评分高的科幻动画"
    - "查一下 2024 年有什么新番"

    本工具会按照 `subject_type` 对搜索结果进行二次过滤，确保返回结果
    的类型与用户需求一致。返回的 JSON 中包含每个条目的 ID、名称、
    中文名、评分、排名、标签和简介，便于后续后续调用
    ``get_bangumi_subject_detail`` 获取完整详情。

    Args:
        keyword: 搜索关键词，支持日语、中文、英文等多种语言。例如
            "進撃の巨人"、"命运石之门"、"Steins;Gate"。
        subject_type: 条目类型 ID，默认为 ``2``（动画）。
            可用的类型：
            - ``1``: 书籍（漫画、小说、画集等）
            - ``2``: 动画（TV、OVA、剧场版等）
            - ``3``: 音乐（专辑、单曲等）
            - ``4``: 游戏（主机、PC、桌游等）
            - ``6``: 三次元（日剧、欧美剧、电影、综艺等）
        sort: 排序方式，默认为 ``"score"``（按评分从高到低）。
            可选值：
            - ``"match"``: 按匹配程度排序（MeiliSearch 默认）
            - ``"heat"``: 按收藏人数排序（热度）
            - ``"rank"``: 按排名由高到低排序
            - ``"score"``: 按评分从高到低排序

    Returns:
        JSON 格式字符串。成功时为一个包含 ``SlimSubjectResponse``
        对象的数组，每个对象包含 ``id``、``type``、``name``、
        ``name_cn``、``score``、``rank``、``tags`` 等字段；
        失败时返回 ``{"error": "错误描述", "status_code": ...}``。
    """
    async with BangumiClient() as client:
        result: list[Any] | dict[str, Any] = await client.search_subjects(
            keyword=keyword,
            sort=sort,
            limit=30,
        )

    # ── 错误分支：Client 返回了错误字典 ──
    if isinstance(result, dict):
        return json.dumps(result, ensure_ascii=False)

    # ── 按 subject_type 过滤，确保返回结果类型一致 ──
    filtered = [item for item in result if item.type == subject_type]

    # ── 序列化为 JSON 字符串 ──
    return json.dumps(
        [item.model_dump() for item in filtered],
        ensure_ascii=False,
    )


async def get_bangumi_subject_detail(subject_id: int) -> str:
    """获取 Bangumi 单个条目的完整详细信息（JSON 格式字符串）。

    当用户需要了解某个条目的完整信息时调用此工具，通常在
    ``search_bangumi_subject`` 之后使用。在用户已明确知道条目 ID
    （如从搜索结果中看到）时也可直接调用。

    典型场景：
    - "帮我看看编号 12345 这个番的详情"
    - "这部动画有多少集？什么时候播出的？"
    - "查一下这个条目的评分和收藏情况"

    返回的 JSON 中包含该条目的：
    - 基本信息和评分（name、name_cn、score、rank）
    - 播出/发售日期（date）
    - 平台信息（platform，如 TV / Web / Movie 等）
    - 章节总数（total_episodes，动画/剧集特有）
    - 集数/册数（eps / volumes）
    - 简介（short_summary）
    - 标签（tags）
    - 收藏统计（collection.wish / doing / collect）

    Args:
        subject_id: 条目 ID，即 Bangumi 条目详情页 URL 中的
            数字编号。例如 ``https://bgm.tv/subject/8`` 对应的
            ``subject_id`` 为 ``8``。

    Returns:
        JSON 格式字符串。成功时为一个包含完整条目信息的
        ``DetailedSubjectResponse`` 对象；失败时返回
        ``{"error": "错误描述", "status_code": ...}``。
    """
    async with BangumiClient() as client:
        result: Any | dict[str, Any] = await client.get_subject(
            subject_id=subject_id,
        )

    # ── 错误分支 ──
    if isinstance(result, dict) and "error" in result:
        return json.dumps(result, ensure_ascii=False)

    # ── 序列化为 JSON 字符串 ──
    return result.model_dump_json()
