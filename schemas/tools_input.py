"""
契约层：所有 Bangumi 工具的 Pydantic v2 输入 Schema。

本模块为 LLM（大语言模型）与 Tool 函数之间的"类型契约"。
每一个 BaseModel 定义了该 Tool 所需的全部参数、硬性边界约束以及语义描述，
确保 LLM 在调用工具时不会越界或捏造不存在的枚举值。
"""

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SearchBangumiInput(BaseModel):
    """
    【名字→ID 搜索工具】统一的名字到 ID 映射入口。

    当用户用自然语言提到番剧、角色或人物名称时，LLM 应优先调用此 Tool
    获取对应的 Bangumi ID，再将 ID 传递给其他功能 Tool 进行深度查询。
    支持精确/模糊匹配，返回结果按相关度排序。
    """

    keyword: str = Field(
        ...,
        description="搜索关键词，支持中文名、日文名或部分名称的模糊匹配。"
        "例如：'平家物语'、'攻壳机动队'、'花泽香菜'",
    )
    entity_type: Literal["subject", "character", "person"] = Field(
        default="subject",
        description="搜索的实体类型：subject=番剧/书籍/音乐/游戏条目，"
        "character=虚拟角色，person=现实人物（声优、导演等）",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=10,
        description="返回结果的最大条数。单条结果时 LLM 可直接使用；多条时需要 LLM 根据 name/nameCN/type "
        "推断用户意图选择最匹配的一项",
    )
    subject_type: Optional[int] = Field(
        default=None,
        description="【仅 entity_type=subject 时生效】条目类型过滤：1=书籍, 2=动画, 3=音乐, 4=游戏, 6=真人。"
        "留空则不限制类型",
        ge=1,
        le=6,
    )
    nsfw: Optional[bool] = Field(
        default=None,
        description="【仅 entity_type=character 时生效】是否包含 NSFW（不适合工作场合）角色。"
        "留空由 API 默认行为决定",
    )


class GetBlogInput(BaseModel):
    """
    【日志分析工具】获取 Bangumi 日志正文、评论及关联条目的聚合工具。

    一次调用返回三个维度的数据——
    正文（日志内容）、评论反应（社区观点）、关联作品（上下文），
    让 LLM 能对一篇日志做完整的语义分析，而非三次独立调用。
    此 Tool 需要有效的 Access Token。
    """

    entry_id: int = Field(
        ...,
        description="Bangumi 日志条目 ID，可从 search_bangumi 返回结果或 URL 中获得。"
        "例如 URL /blog/{entry_id} 中的数字部分",
    )
    include_comments: bool = Field(
        default=True,
        description="是否同时拉取该日志的评论区内容（最近 30 条，每条截断 200 字）",
    )
    include_subjects: bool = Field(
        default=True,
        description="是否同时拉取该日志关联的条目信息（番剧/书籍等），帮助 LLM 理解日志讨论的作品上下文",
    )


class GetCalendarInput(BaseModel):
    """
    【番组表工具】获取 Bangumi 每日放送排期。

    API 按星期几分组返回一周的放送数据，本 Tool 内置"今天星期几"的过滤逻辑，
    默认只返回当日番组，也可按需获取整周或指定日期的放送安排。
    """

    weekday: Literal[
        "today", "mon", "tue", "wed", "thu", "fri", "sat", "sun", "all"
    ] = Field(
        default="today",
        description="目标星期：today=今天（系统日期自动推断），mon~sun=指定星期几，all=整周全部数据",
    )
    limit_per_day: int = Field(
        default=10,
        ge=1,
        le=50,
        description="每天最多返回的番剧条目数量",
    )


class GetEpisodeDiscussionInput(BaseModel):
    """
    【单集讨论工具】获取某一集剧集的详情与社区吐槽箱。

    这是情感分析和舆情监控的核心数据源。LLM 拿到单集元数据（集数、标题、简介）
    加上用户吐槽后，可以提取情感倾向、高频关键词、争议焦点等洞察。
    """

    episode_id: int = Field(
        ...,
        description="单集 ID，可通过 get_subject_discussion 的 episodes 列表获得，"
        "或从 search_bangumi 定位条目后按集数查找",
    )
    comments_limit: int = Field(
        default=30,
        ge=1,
        le=200,
        description="吐槽箱评论的最大拉取条数。越多评论越能反映社区整体情绪，但也会增加 Token 消耗",
    )


class GetUserProfileInput(BaseModel):
    """
    【用户画像工具】获取 Bangumi 用户的多维度画像数据。

    一次调用返回五维数据：用户基本信息 + 条目收藏 + 角色收藏 + 人物收藏 + 日志列表。
    LLM 可据此分析用户的评分偏好、类型倾向、角色审美及内容产出风格。
    部分子功能（如博客列表）需要有效的 Access Token。
    """

    username: str = Field(
        ...,
        description="Bangumi 用户名（唯一标识），例如 UID 或自定义用户名，可从用户主页 URL 中获得",
    )
    collections_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="收藏条目拉取的最大数量，用于评分分析和类型分布统计",
    )
    include_blogs: bool = Field(
        default=True,
        description="是否拉取该用户的日志列表，用于分析其内容产出风格和关注领域。需要 Access Token",
    )
    include_characters: bool = Field(
        default=False,
        description="是否拉取该用户收藏的虚拟角色列表，用于角色偏好聚类分析",
    )
    include_persons: bool = Field(
        default=False,
        description="是否拉取该用户收藏的现实人物列表（声优、导演等），用于人物偏好分析",
    )


class GetSubjectDiscussionInput(BaseModel):
    """
    【条目讨论全景工具】全面了解一部作品的社区评价和讨论。

    四个维度的数据各有侧重——
    comments 反映口碑温度，reviews 提供深度观点，topics 展示讨论热点，
    episodes 帮助 LLM 定位关键集数。LLM 可按需选择拉取哪些维度的数据。
    """

    subject_id: int = Field(
        ...,
        description="Bangumi 条目 ID，可通过 search_bangumi 搜索番剧名称获得",
    )
    data_types: list[Literal["comments", "reviews", "topics", "episodes"]] = Field(
        default=["comments", "reviews"],
        description="需要拉取的数据维度列表：comments=吐槽箱（短评+评分），"
        "reviews=长篇评测（深度分析），topics=讨论帖（社区热点），episodes=剧集列表（帮助定位单集）",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=50,
        description="每个数据维度最多拉取的条数",
    )


class GetTrendingInput(BaseModel):
    """
    【热门趋势工具】回答"最近什么火"的数据源。

    两个维度——作品热度（哪些番剧热度飙升）和讨论热度（哪些话题被激烈讨论），
    帮助 LLM 了解 Bangumi 社区当下的关注焦点。
    """

    category: Literal["subjects", "topics", "both"] = Field(
        default="both",
        description="热门维度：subjects=热门条目排行，topics=热门讨论帖排行，both=两者都拉取",
    )
    subject_type: Optional[Literal["anime", "book", "music", "game", "real"]] = Field(
        default=None,
        description="【仅 category 含 subjects 时生效】按条目类型过滤热门结果。"
        "anime=动画, book=书籍, music=音乐, game=游戏, real=真人。留空则不限制类型",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=30,
        description="每个维度返回的最大条数",
    )


class GetEntityCommentsInput(BaseModel):
    """
    【角色/人物评论工具】获取虚拟角色或现实人物的社区评论。

    角色和人物的评论接口结构完全一致，统一为一个 Tool，通过 entity_type 区分。
    LLM 可据此分析特定角色/人物在社区中的讨论热度和舆论倾向。
    """

    entity_type: Literal["character", "person"] = Field(
        ...,
        description="实体类型：character=虚拟角色（如'阿尔托莉雅'），person=现实人物（如'花泽香菜'、'新房昭之'）",
    )
    entity_id: int = Field(
        ...,
        description="角色或人物的 Bangumi ID，可通过 search_bangumi 以对应的 entity_type 搜索名称获得",
        ge=1,
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="拉取的评论最大条数。每条评论正文截断 200 字，保留用户昵称、时间、反应数和回复数",
    )


class GetSubjectDetailInput(BaseModel):
    """
    【条目详情工具】获取单个条目的完整详细信息。

    在 search_bangumi_subject 定位到目标条目后调用，获取评分、集数、
    简介、标签等完整数据。也可用于已知 subject_id 的直接查询。
    """

    subject_id: int = Field(
        ...,
        description="Bangumi 条目 ID（即 subject_id），可通过 search_bangumi_subject 搜索名称获得",
        ge=1,
    )


class GetCharacterDetailInput(BaseModel):
    """
    【角色详情工具】获取 Bangumi 虚拟角色的完整详细信息。

    在 search_bangumi_subject(entity_type="character") 定位到目标角色后调用，
    获取角色简介、背景故事、出演作品、收藏数等完整数据。
    与 get_subject_characters（列出条目下所有角色）不同，此工具关注单个角色的深度信息。

    典型场景：
    - "阿尔托莉雅这个角色的背景故事是什么？"
    - "帮我看看编号 12345 这个角色的详细信息"
    - "这个角色在 Bangumi 上有多受欢迎？"
    """

    character_id: int = Field(
        ...,
        description="Bangumi 角色 ID，可通过 search_bangumi_subject(keyword=角色名, entity_type='character') 搜索获得",
        ge=1,
    )


class GetPersonDetailInput(BaseModel):
    """
    【人物详情工具】获取 Bangumi 现实人物（声优、导演、作者等）的完整详细信息。

    在 search_bangumi_subject(entity_type="person") 定位到目标人物后调用，
    获取人物简介、职业标签、代表作列表、收藏数等完整数据。

    典型场景：
    - "花泽香菜配过哪些代表作？"
    - "新房昭之的个人简介和代表作有哪些？"
    - "帮我看看这位声优的详细资料"
    """

    person_id: int = Field(
        ...,
        description="Bangumi 人物 ID，可通过 search_bangumi_subject(keyword=人物名, entity_type='person') 搜索获得",
        ge=1,
    )


class GetSubjectCharactersInput(BaseModel):
    """
    【条目角色工具】获取一部作品的全部登场角色及其声优/演员信息。

    返回角色列表，包含角色名、出演类型（主角/配角/客串）、
    饰演者（声优/演员）名称和 ID。这是回答"主角是谁？""声优是谁？"
    的核心数据源。
    """

    subject_id: int = Field(
        ...,
        description="Bangumi 条目 ID，可通过 search_bangumi_subject 搜索名称获得",
        ge=1,
    )


class LocalSearchInput(BaseModel):
    """
    【本地语义搜索工具】基于 RAG 向量检索的离线搜索引擎。

    适用于 API 关键词搜索无法覆盖的模糊意图——
    如"类似命运石之门的烧脑番"、"80年代评分最高的机战番"、
    或跨实体关联查询"配过最多主角的声优"。
    """

    query: str = Field(
        ...,
        description="自然语言查询，越具体越好。例如：'80年代评分最高的机战番'",
    )
    entity_type: Literal["subject", "character", "person", "all"] = Field(
        default="all",
        description="实体类型过滤：subject=番剧/书籍/音乐/游戏，character=虚拟角色，"
        "person=现实人物（声优/导演等），all=跨域全量检索",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="返回结果数上限",
    )
    nsfw: bool = Field(
        default=False,
        description="是否包含 R18 内容，默认 False（安全护栏）",
    )


class UserTimelineInput(BaseModel):
    """
    【用户时光机工具】获取指定 Bangumi 用户的动态时间线。

    拉取用户最近的收藏、评分、吐槽等动态，帮助分析用户的追番偏好。
    **需要系统配置有效的 Bangumi Access Token。**
    """

    username: str = Field(
        ...,
        description="Bangumi 用户名（个人主页 URL 中的用户名部分），如 'deepseek_jiang'",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=50,
        description="返回动态条数上限",
    )
