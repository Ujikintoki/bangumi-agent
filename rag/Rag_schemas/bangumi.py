"""
Bangumi API 数据模型定义 (Fat Model 策略)

采用"渐进式暴露"与"Fat Model"策略，覆盖 v0 公开 API 与 p1 private API
的响应契约。Fat Model 要求每个模型完整覆盖该场景所需的全部字段，
不依赖外部组合，同时使用 ConfigDict(extra="ignore") 严格丢弃未声明字段。

============================================================================
  模型分层:
  - v0 API: SlimSubjectResponse / DetailedSubjectResponse (搜索与详情)
  - p1 API: P1SubjectResponse / P1CharacterResponse / P1PersonResponse
            (RAG 摄入管道的原始数据源)
  - 关联边: CastItem / WorkItem (角色出演 / 人物代表作列表项)
============================================================================
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _extract_tag_names(
    raw_tags: list[dict | str] | None,
    max_count: int = 10,
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
    rank: Optional[int] = Field(
        default=0, description="条目排名（从 rating.rank 提取）"
    )
    tags: list[str] = Field(
        default_factory=list,
        description="标签名称列表（最多 10 个）（暂时，可能更改）",
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_subject(cls, data: dict | object) -> dict | object:
        """预处理输入数据：提取嵌套字段并精简 tags 结构。"""
        if not isinstance(data, dict):
            return data

        # 从 rating 中提取评分和排名
        if "rating" in data and isinstance(data["rating"], dict):
            data.setdefault("score", data["rating"].get("score", 0.0))
            data.setdefault("rank", data["rating"].get("rank", 0))

        # 如果没有 short_summary 则从 summary 回退
        if "short_summary" not in data and "summary" in data:
            data["short_summary"] = data.get("summary", "")

        # 将 tags 从 [{name, count}, ...] 精简为 [str, ...]
        if "tags" in data:
            data["tags"] = _extract_tag_names(data["tags"], max_count=10)

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


# ============================================================================
# p1 API 响应模型 — Character / Person / Subject（RAG 摄入数据源）
# ============================================================================


class P1SubjectResponse(BaseModel):
    """p1 API ``/subjects/{subjectID}`` 响应模型。

    相比 v0 API 的 ``DetailedSubjectResponse``，p1 API 额外暴露 airtime、
    platform 结构体、metaTags 及 rating 分布等更细粒度的元数据。
    """

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="条目 ID")
    name: str = Field(default="", description="条目原文名称")
    nameCN: str = Field(default="", description="条目中文名称")
    summary: str = Field(default="", description="条目完整简介")
    type: int = Field(
        default=0,
        description="条目类型（1=书籍, 2=动画, 3=音乐, 4=游戏, 6=三次元）",
    )
    nsfw: bool = Field(default=False, description="是否 R18 内容")
    eps: int = Field(default=0, description="由旧服务端从 wiki 中解析的集数/话数")
    rating: dict = Field(
        default_factory=dict,
        description="评分详情，含 score(float) / total(int) / count(list[int])",
    )
    airtime: dict = Field(
        default_factory=dict,
        description="播出时间，含 date(str) / month(int) / weekday(int) / year(int)",
    )
    platform: dict = Field(
        default_factory=dict,
        description="播出平台，含 alias / type / typeCN 等",
    )
    metaTags: list[str] = Field(
        default_factory=list, description="元标签（wiki 分类标签）"
    )


class P1CharacterResponse(BaseModel):
    """p1 API ``/characters/{characterID}`` 响应模型。"""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="角色 ID")
    name: str = Field(default="", description="角色原文名称")
    nameCN: str = Field(default="", description="角色中文名称")
    summary: str = Field(default="", description="角色简介")
    role: int = Field(default=0, description="角色类型编号")
    collects: int = Field(default=0, description="收藏数")
    comment: int = Field(default=0, description="评论数")
    nsfw: bool = Field(default=False, description="是否 R18 内容")


class P1PersonResponse(BaseModel):
    """p1 API ``/persons/{personID}`` 响应模型。"""

    model_config = ConfigDict(extra="ignore")

    id: int = Field(description="人物 ID")
    name: str = Field(default="", description="人物原文名称")
    nameCN: str = Field(default="", description="人物中文名称")
    summary: str = Field(default="", description="人物简介")
    career: list[str] = Field(
        default_factory=list, description="职业标签列表，如 ['seiyu', 'actor']"
    )
    type: int = Field(default=0, description="人物类型编号")
    collects: int = Field(default=0, description="收藏数")
    comment: int = Field(default=0, description="评论数")
    nsfw: bool = Field(default=False, description="是否 R18 内容")


class CastItem(BaseModel):
    """p1 API ``/characters/{characterID}/casts`` 返回的单条记录。

    对应角色在某部作品中的出场信息。
    """

    model_config = ConfigDict(extra="ignore")

    subject_id: int = Field(description="作品原始数字 ID")
    subject_name: str = Field(default="", description="作品名称")
    person_id: Optional[int] = Field(default=None, description="饰演者原始数字 ID")
    person_name: Optional[str] = Field(default=None, description="饰演者名称")
    type: int = Field(default=0, description="角色出场类型: 1=主角, 2=配角, 3=客串")


class WorkItem(BaseModel):
    """p1 API ``/persons/{personID}/casts`` 返回的单条记录。

    对应人物在某部作品中参与的信息。
    """

    model_config = ConfigDict(extra="ignore")

    subject_id: int = Field(description="作品原始数字 ID")
    subject_name: str = Field(default="", description="作品名称")
    character_id: Optional[int] = Field(default=None, description="关联角色原始数字 ID")
    character_name: Optional[str] = Field(default=None, description="关联角色名称")
    type: int = Field(default=0, description="角色出场类型: 1=主角, 2=配角, 3=客串")
