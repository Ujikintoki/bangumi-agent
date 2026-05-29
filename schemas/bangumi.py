"""
Bangumi API 数据模型定义 (Fat Model 策略)

采用"渐进式暴露"与"Fat Model"策略，基于 bangumi_openapi.yaml 中定义的
SlimSubject / Subject 组件提取两个层次的 Pydantic v2 模型。
Fat Model 要求每个模型完整覆盖该场景所需的全部字段，不依赖外部组合，
同时使用 ConfigDict(extra="ignore") 严格丢弃未声明字段，保障解析稳定性。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _extract_tag_names(
    raw_tags: list[dict | str] | None,
    max_count: int = 5,
) -> list[str]:
    """从原始 tags 数组中提取标签名称，限制数量。

    Bangumi API 返回的 tags 格式为 ``[{name: str, count: int}, ...]``，
    此函数从中安全提取 ``name`` 并截断。

    Args:
        raw_tags: 原始标签数据，可能为 ``None``。
        max_count: 最多保留的标签数量。

    Returns:
        标签名称列表。
    """
    if not raw_tags:
        return []

    names: list[str] = []
    for item in raw_tags:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            name = item.get("name")
            if isinstance(name, str):
                names.append(name)
        if len(names) >= max_count:
            break

    return names


class SlimSubjectResponse(BaseModel):
    """精简条目模型（用于列表/搜索）。

    仅暴露 LLM 高频需要的核心字段。
    ``score`` 从 ``rating.score`` 中安全提取，``tags`` 精简为标签名列表。
    """

    model_config = ConfigDict(extra="ignore")

    id: Optional[int] = Field(default=0, description="条目 ID")
    type: Optional[int] = Field(
        default=0,
        description="条目类型（1=书籍, 2=动画, 3=音乐, 4=游戏, 6=三次元）",
    )
    name: Optional[str] = Field(default="", description="条目原文名称")
    name_cn: Optional[str] = Field(default="", description="条目中文名称")
    short_summary: Optional[str] = Field(default="", description="截短后的条目简介")
    score: Optional[float] = Field(
        default=0.0, description="条目评分（从 rating.score 提取）"
    )
    # rank: Optional[int] = Field(default=0, description="条目排名")
    tags: list[str] = Field(
        default_factory=list,
        description="标签名称列表（最多 5 个）",
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_subject(cls, data: dict | object) -> dict | object:
        """预处理输入数据：提取嵌套字段并精简 tags 结构。"""
        if not isinstance(data, dict):
            return data

        # 从 rating.score 中提取评分
        if "rating" in data and isinstance(data["rating"], dict):
            data.setdefault("score", data["rating"].get("score", 0.0))

        # 如果没有 short_summary 则从 summary 回退
        if "short_summary" not in data and "summary" in data:
            data["short_summary"] = data.get("summary", "")

        # 将 tags 从 [{name, count}, ...] 精简为 [str, ...]
        if "tags" in data:
            data["tags"] = _extract_tag_names(data["tags"], max_count=5)

        return data


class CollectionSummary(BaseModel):
    """收藏统计（精简版）。

    从完整的 collection 对象中仅提取 LLM 最关心的三个维度。
    """

    wish: Optional[int] = Field(default=0, description="想要观看的人数")
    doing: Optional[int] = Field(default=0, description="正在观看的人数")
    collect: Optional[int] = Field(default=0, description="已经看完的人数")


class DetailedSubjectResponse(SlimSubjectResponse):
    """详细条目模型（用于单条目详情）。

    在 SlimSubjectResponse 基础上增加条目详情的元数据字段。
    使用 ``Optional[int] = 0`` 防御性处理不同条目类型可能缺失的字段。
    """

    total_episodes: Optional[int] = Field(
        default=0,
        description="数据库中的章节总数（动画/剧集条目特有）",
    )
    eps: Optional[int] = Field(
        default=0,
        description="由旧服务端从 wiki 中解析的集数/话数",
    )
    volumes: Optional[int] = Field(
        default=0,
        description="书籍条目的册数（书籍条目特有）",
    )
    platform: Optional[str] = Field(
        default="",
        description="播出/发售平台（TV, Web, 欧美剧, DLC 等）",
    )
    date: Optional[str] = Field(
        default="",
        description="播出/发售日期（YYYY-MM-DD 格式）",
    )
    collection: Optional[CollectionSummary] = Field(
        default=None,
        description="收藏统计（仅 wish / doing / collect 三个维度）",
    )

    @model_validator(mode="before")
    @classmethod
    def extract_detailed_fields(cls, data: dict | object) -> dict | object:
        """预处理 Subject 原始数据，提取详情专属的嵌套字段。"""
        if not isinstance(data, dict):
            return data

        # 精简 collection 为三个核心维度
        if "collection" in data and isinstance(data["collection"], dict):
            coll = data["collection"]
            data["collection"] = CollectionSummary(
                wish=coll.get("wish", 0),
                doing=coll.get("doing", 0),
                collect=coll.get("collect", 0),
            )

        return data
