"""
Phase 5 L2/L3 记忆管理器

L2 长记忆的统一入口：召回（recall） + 记住（remember）。
L3 公共记忆留有 Phase 6 桩。

设计原则：
    - 异步写（remember），同步读（recall）：写入用 fire-and-forget，召回在推理前同步完成
    - 优雅降级：任何故障不阻塞主流程，静默回退为无记忆模式
    - 非侵入：L1 滑动窗口（agent/memory.py）不变，L2 作为附加层叠加
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from core.config import get_settings

logger = logging.getLogger("bgm-agent.memory_manager")

# ═══════════════════════════════════════════════════════════════════════════
# 摘要 Prompt
# ═══════════════════════════════════════════════════════════════════════════

SUMMARIZE_PROMPT = """你是 Bangumi 助手的记忆编码器。请将以下对话历史压缩为一段不超过200字的摘要。

摘要应包含：
1. 用户的核心问题或需求
2. 你给出的关键回答、推荐的作品
3. 用户表现出的偏好信号（喜欢/不喜欢什么、评分倾向等）
4. 涉及的关键实体（作品名、角色名、声优名等）

对话历史：
{conversation_history}

请直接输出摘要文本，不要包含"摘要："等前缀。使用简体中文。"""


# ═══════════════════════════════════════════════════════════════════════════
# MemoryManager
# ═══════════════════════════════════════════════════════════════════════════


class MemoryManager:
    """L2/L3 记忆管理器。

    生命周期：
        - 写入：remember_session() → session_memories + user_profiles（异步 fire-and-forget）
        - 读取：recall_for_prompt() → 格式化的记忆文本（同步，注入 System Prompt）

    Attributes:
        _engine: SQLAlchemy Engine，复用 database/engine.py 的全局实例。
        _zhipu_api_key: 智谱 API 密钥。
        _llm_model: 用于摘要生成的 LLM 模型名。
    """

    def __init__(
        self,
        engine: Any,
        zhipu_api_key: str,
        llm_model: str,
    ) -> None:
        """初始化 MemoryManager。

        Args:
            engine: SQLAlchemy Engine 实例。
            zhipu_api_key: 智谱 API 密钥。
            llm_model: 摘要 LLM 模型名（如 gpt-4o, deepseek-chat）。
        """
        self._engine = engine
        self._zhipu_api_key = zhipu_api_key
        self._llm_model = llm_model

    # ═══════════════════════════════════════════════════════════════════
    # 核心 API
    # ═══════════════════════════════════════════════════════════════════

    async def recall_for_prompt(
        self,
        user_id: str,
        query: str,
        max_tokens: int = 500,
    ) -> str:
        """召回用户历史记忆并格式化为 System Prompt 注入文本。

        执行顺序：
            1. 向量化用户查询
            2. 语义检索 session_memories（cosine distance）
            3. 距离阈值过滤
            4. 读取 user_profiles
            5. 格式化注入文本（≤ max_tokens）

        Args:
            user_id: 用户标识。
            query: 用户当前查询文本，用于语义匹配。
            max_tokens: 注入文本的最大 Token 数。

        Returns:
            格式化的记忆文本，无相关记忆时返回空字符串。
        """
        settings = get_settings()

        # Guard: 匿名用户或记忆关闭
        if user_id == "anonymous" or not settings.MEMORY_ENABLED:
            return ""

        sessions: list = []
        profile: Optional[Any] = None

        # ── Step 1-2: embedding + 语义检索 ─────────────────
        query_embedding = await self._embed_text(query)
        if query_embedding is not None:
            try:
                raw_sessions = await self._search_similar_sessions(
                    user_id,
                    query_embedding,
                    limit=settings.MEMORY_RECALL_TOP_K,
                )
                for sm, distance in raw_sessions:
                    if distance <= settings.MEMORY_RECALL_THRESHOLD:
                        sessions.append(sm)
            except RuntimeError as exc:
                logger.warning("语义检索失败 (user=%s): %s", user_id, exc)
        else:
            logger.warning("embedding 失败，回退到时效排序 (user=%s)", user_id)
            # 回退：按创建时间降序取最近 session
            try:
                from database.memory_tables import SessionMemory

                with Session(self._engine) as session:
                    stmt = (
                        select(SessionMemory)
                        .where(SessionMemory.user_id == user_id)
                        .order_by(SessionMemory.created_at.desc())
                        .limit(settings.MEMORY_RECALL_TOP_K)
                    )
                    sessions = list(session.exec(stmt).all())
            except Exception as exc:
                logger.warning("时效排序检索失败 (user=%s): %s", user_id, exc)

        # ── Step 3: 读取用户画像 ─────────────────────────
        try:
            from database.memory_tables import UserProfile

            with Session(self._engine) as session:
                profile = session.exec(
                    select(UserProfile).where(UserProfile.user_id == user_id)
                ).first()
        except Exception as exc:
            logger.warning("画像读取失败 (user=%s): %s", user_id, exc)

        # ── Step 4: 格式化 ──────────────────────────────
        if not sessions and profile is None:
            return ""

        return self._format_memory_context(sessions, profile, max_tokens)

    async def remember_session(
        self,
        session_id: str,
        user_id: str,
        messages: list,
        final_reply: str,
        query_intent: str,
    ) -> None:
        """写入 session 摘要并增量更新用户画像。

        执行顺序：
            1. 截断对话历史到 3000 tokens → LLM 生成摘要
            2. 提取关键实体
            3. 生成 embedding
            4. INSERT session_memories
            5. 更新 user_profiles

        所有异常内部捕获，仅记录 WARNING 日志——永不阻塞用户响应。

        Args:
            session_id: 会话标识。
            user_id: 用户标识。
            messages: Agent Graph 消息列表（含 Human/AI/Tool/System）。
            final_reply: 最终回复文本。
            query_intent: 本轮意图类型（lookup/discovery/chitchat/...）。
        """
        settings = get_settings()

        # Guard: 匿名用户或记忆关闭
        if user_id == "anonymous" or not settings.MEMORY_ENABLED:
            return

        try:
            # ── Step 1: 摘要 ────────────────────────────
            summary = await self._summarize_session(messages, final_reply)
            if not summary:
                logger.warning(
                    "摘要为空，跳过记忆 (session=%s, user=%s)", session_id, user_id
                )
                return

            # ── Step 2: 提取实体 ─────────────────────────
            entities = self._extract_key_entities(summary)

            # ── Step 3: embedding ───────────────────────
            embedding = await self._embed_text(summary)

            # ── Step 4: 元数据 ──────────────────────────
            from langchain_core.messages import SystemMessage, ToolMessage

            message_count = sum(
                1
                for m in messages
                if not isinstance(m, (SystemMessage, ToolMessage))
            )
            tools_used: list[str] = list(
                dict.fromkeys(
                    getattr(m, "name", "")
                    for m in messages
                    if isinstance(m, ToolMessage) and getattr(m, "name", "")
                )
            )

            # ── Step 5: INSERT session_memories ─────────
            try:
                from database.memory_tables import SessionMemory

                with Session(self._engine) as session:
                    sm = SessionMemory(
                        session_id=session_id,
                        user_id=user_id,
                        summary_text=summary,
                        embedding=embedding,  # None 也可接受
                        key_entities=entities,
                        intent_distribution={query_intent: 1} if query_intent else {},
                        tools_used=tools_used,
                        message_count=message_count,
                    )
                    session.add(sm)
                    session.commit()
            except SQLAlchemyError as exc:
                logger.warning(
                    "session_memory 写入失败 (session=%s, user=%s): %s",
                    session_id,
                    user_id,
                    exc,
                )
                return

            # ── Step 6: 更新画像 ────────────────────────
            await self._update_user_profile(
                user_id, summary, query_intent, entities
            )

        except Exception:
            logger.exception(
                "remember_session 异常 (user=%s, session=%s)", user_id, session_id
            )

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — embedding
    # ═══════════════════════════════════════════════════════════════════

    async def _embed_text(self, text: str) -> Optional[list[float]]:
        """向量化文本。

        Args:
            text: 待向量化的文本。

        Returns:
            向量列表，或 None（API 失败/sdk 未安装时）。
        """
        from clients.zhipu_client import embed_single

        return await embed_single(text)

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — 语义检索
    # ═══════════════════════════════════════════════════════════════════

    async def _search_similar_sessions(
        self,
        user_id: str,
        query_embedding: list[float],
        limit: int = 5,
    ) -> list[tuple]:
        """pgvector 余弦距离检索相关 session 摘要。

        Args:
            user_id: 用户标识。
            query_embedding: 查询向量。
            limit: 最大返回数。

        Returns:
            ``[(SessionMemory, cosine_distance), ...]`` 按距离升序排列。

        Raises:
            RuntimeError: 数据库查询失败时。
        """
        from database.memory_tables import SessionMemory

        distance_expr = SessionMemory.embedding.cosine_distance(
            query_embedding
        ).label("cosine_dist")

        try:
            with Session(self._engine) as session:
                stmt = (
                    select(SessionMemory, distance_expr)
                    .where(SessionMemory.user_id == user_id)
                    .where(SessionMemory.embedding.isnot(None))
                    .order_by(distance_expr)
                    .limit(limit)
                )
                return list(session.execute(stmt).fetchall())
        except SQLAlchemyError as exc:
            logger.error("语义检索查询失败 (user=%s): %s", user_id, exc)
            raise RuntimeError(f"语义检索查询失败: {exc}") from exc

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — 摘要
    # ═══════════════════════════════════════════════════════════════════

    async def _summarize_session(
        self,
        messages: list,
        final_reply: str,
    ) -> str:
        """使用 LLM 将对话历史压缩为 ~200 字中文摘要。

        Args:
            messages: Agent Graph 消息列表。
            final_reply: 最终回复文本。

        Returns:
            摘要文本。LLM 失败时回退为 final_reply[:200]。
        """
        # 1. 格式化为对话文本并截断
        conversation_text = self._format_conversation_text(messages, final_reply)

        # 2. 调用轻量 LLM
        try:
            from agent.llm import create_llm

            llm = create_llm(
                temperature=0,
                max_tokens=300,
                request_timeout=10,
            )
            prompt = SUMMARIZE_PROMPT.format(conversation_history=conversation_text)
            response = await llm.ainvoke(prompt)
            summary = (
                response.content.strip()
                if hasattr(response, "content")
                else str(response).strip()
            )
            if summary:
                return summary
        except Exception as exc:
            logger.warning("会话摘要 LLM 调用失败: %s", exc)

        # 3. Fallback
        return final_reply[:200] if final_reply else "（摘要生成失败）"

    @staticmethod
    def _format_conversation_text(
        messages: list,
        final_reply: str,
        max_tokens: int = 3000,
    ) -> str:
        """将消息列表转为纯文本对话记录，供 LLM 摘要使用。

        过滤策略：
            - HumanMessage → "用户: {content}"
            - AIMessage（有 content）→ "助手: {content}"
            - ToolMessage → 跳过（数据噪音，语义价值低）
            - SystemMessage → 跳过（人格 prompt，不参与摘要）

        Args:
            messages: Agent Graph 消息列表。
            final_reply: 最终回复文本。
            max_tokens: 最大 Token 数（超限时从头部截断）。

        Returns:
            格式化的对话文本。
        """
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

        lines: list[str] = []
        for m in messages:
            if isinstance(m, HumanMessage):
                content = m.content if hasattr(m, "content") else str(m)
                if content:
                    lines.append(f"用户: {content}")
            elif isinstance(m, AIMessage):
                content = m.content if hasattr(m, "content") else ""
                # 跳过仅有 tool_calls 无 content 的 AIMessage
                if content:
                    lines.append(f"助手: {content}")
            # 跳过 SystemMessage, ToolMessage

        # 追加最终回复
        if final_reply:
            lines.append(f"助手: {final_reply}")

        full_text = "\n".join(lines)

        # Token 截断
        try:
            from agent.memory import count_tokens

            if count_tokens(full_text) <= max_tokens:
                return full_text
            # 从尾部保留 max_tokens（保留最近的交流）
            enc = __import__("tiktoken").get_encoding("cl100k_base")
            tokens = enc.encode(full_text)
            if len(tokens) > max_tokens:
                tokens = tokens[-max_tokens:]
            return enc.decode(tokens)
        except Exception:
            # tiktoken 不可用时，保守截断字符
            return full_text[-(max_tokens * 2):]

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — 实体提取
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _extract_key_entities(summary: str) -> list[dict]:
        """从摘要文本中提取关键实体名。

        当前使用轻量正则匹配引号内的实体名（「」""）。
        Phase 5 初期有意保持简单，后续可升级为 LLM 提取或 NER。

        Args:
            summary: LLM 生成的中文摘要。

        Returns:
            实体列表，格式 ``[{"type": "subject", "name": "高达Seed"}, ...]``。
        """
        entities: list[dict] = []
        seen: set[str] = set()

        # 匹配中文书名号「」
        for match in re.finditer(r"「([^」]{2,30})」", summary):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                entities.append({"type": "subject", "name": name})

        # 匹配中文双引号 "" （非贪婪，避免跨句匹配）
        for match in re.finditer(r"“([^”]{2,30})”", summary):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                entities.append({"type": "subject", "name": name})

        # 匹配英文双引号 ""
        for match in re.finditer(r'"([^"]{2,30})"', summary):
            name = match.group(1)
            if name not in seen:
                seen.add(name)
                entities.append({"type": "subject", "name": name})

        return entities

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — 用户画像
    # ═══════════════════════════════════════════════════════════════════

    async def _update_user_profile(
        self,
        user_id: str,
        session_summary: str,
        intent_dist: str,
        entities: list[dict],
    ) -> None:
        """增量更新用户画像。

        Args:
            user_id: 用户标识。
            session_summary: 本轮 LLM 摘要。
            intent_dist: 本轮意图类型。
            entities: 提取的关键实体列表。
        """
        try:
            from database.memory_tables import UserProfile

            now = datetime.now(timezone.utc)

            with Session(self._engine) as session:
                existing = session.exec(
                    select(UserProfile).where(UserProfile.user_id == user_id)
                ).first()

                if existing is None:
                    # 新用户：创建初始画像
                    profile = UserProfile(user_id=user_id)
                    profile.preferences_json = self._build_initial_preferences(
                        intent_dist, entities
                    )
                    profile.total_sessions = 1
                    profile.avg_session_length = 1.0
                    profile.dominant_intent = intent_dist
                    profile.last_active_at = now
                    profile.updated_at = now
                    session.add(profile)
                else:
                    # 已有画像：增量更新
                    prefs = existing.preferences_json or {}
                    prefs = self._update_genres(prefs, entities)
                    prefs = self._update_affinities(prefs, entities)

                    # 更新 activity_profile
                    activity = prefs.get("activity_profile", {})
                    qtypes: dict = activity.get("query_types", {})
                    intent_name = (
                        intent_dist if isinstance(intent_dist, str) else "unknown"
                    )
                    qtypes[intent_name] = qtypes.get(intent_name, 0) + 1
                    activity["query_types"] = qtypes
                    activity["total_sessions"] = existing.total_sessions + 1
                    prefs["activity_profile"] = activity

                    existing.preferences_json = prefs
                    existing.total_sessions += 1
                    existing.avg_session_length = (
                        0.8 * existing.avg_session_length + 0.2
                    )
                    existing.dominant_intent = max(
                        qtypes, key=lambda k: qtypes[k]
                    )
                    existing.last_active_at = now
                    existing.updated_at = now

                session.commit()

        except Exception as exc:
            logger.warning("画像更新失败 (user=%s): %s", user_id, exc)

    @staticmethod
    def _build_initial_preferences(
        intent_dist: str,
        entities: list[dict],
    ) -> dict:
        """为新用户构建初始偏好画像。

        Args:
            intent_dist: 首轮意图类型。
            entities: 提取的关键实体。

        Returns:
            ``preferences_json`` 结构的 dict。
        """
        intent_name = intent_dist if isinstance(intent_dist, str) else "unknown"
        affinities: dict = {}
        for ent in entities:
            affinities[ent["name"]] = {"name": ent["name"], "interest_score": 0.5}

        return {
            "favorite_genres": [],
            "entity_affinities": affinities,
            "activity_profile": {
                "query_types": {intent_name: 1},
                "total_sessions": 1,
            },
        }

    @staticmethod
    def _update_genres(prefs: dict, entities: list[dict]) -> dict:
        """增量更新类型频率。

        当前 Phase 5 初期做轻量处理：从实体名推断类型关键词，
        按 count 降序截断 top-10。

        Args:
            prefs: 现有 preferences_json。
            entities: 本轮关键实体。

        Returns:
            更新后的 prefs。
        """
        # 简易类型推断：中文名含「高达」「机战」→ 机战，「EVA」「科幻」→ 科幻
        genre_hints = {
            "高达": "机战", "机战": "机战", "机器人": "机战",
            "科幻": "科幻", "SF": "科幻", "赛博": "科幻",
            "恋爱": "恋爱", "爱情": "恋爱", "校园": "校园",
            "悬疑": "悬疑", "推理": "悬疑", "恐怖": "恐怖",
        }

        genres_list: list[dict] = list(prefs.get("favorite_genres", []))
        genre_map: dict[str, dict] = {
            g["genre"]: g for g in genres_list if "genre" in g
        }

        for ent in entities:
            name = ent.get("name", "")
            for keyword, genre in genre_hints.items():
                if keyword.lower() in name.lower():
                    if genre in genre_map:
                        genre_map[genre]["count"] += 1
                    else:
                        genre_map[genre] = {"genre": genre, "count": 1}
                    break

        # 按 count 降序截断 top-10
        sorted_genres = sorted(
            genre_map.values(), key=lambda g: g.get("count", 0), reverse=True
        )
        prefs["favorite_genres"] = sorted_genres[:10]
        return prefs

    @staticmethod
    def _update_affinities(prefs: dict, entities: list[dict]) -> dict:
        """增量更新实体亲和度（指数移动平均）。

        新实体: interest_score = 0.5
        旧实体: interest_score = 0.9 * old + 0.1
        截断 top-20。

        Args:
            prefs: 现有 preferences_json。
            entities: 本轮关键实体。

        Returns:
            更新后的 prefs。
        """
        affinities: dict = prefs.get("entity_affinities", {})

        for ent in entities:
            name = ent.get("name", "")
            if not name:
                continue
            if name in affinities:
                old_score = affinities[name].get("interest_score", 0.5)
                affinities[name]["interest_score"] = min(1.0, 0.9 * old_score + 0.1)
            else:
                affinities[name] = {
                    "name": name,
                    "type": ent.get("type", "subject"),
                    "interest_score": 0.5,
                }

        # 按 interest_score 降序截断 top-20
        sorted_affs = sorted(
            affinities.items(),
            key=lambda kv: kv[1].get("interest_score", 0),
            reverse=True,
        )
        prefs["entity_affinities"] = dict(sorted_affs[:20])
        return prefs

    # ═══════════════════════════════════════════════════════════════════
    # 内部方法 — 格式化注入文本
    # ═══════════════════════════════════════════════════════════════════

    def _format_memory_context(
        self,
        sessions: list,
        profile,
        max_tokens: int = 500,
    ) -> str:
        """将召回的 session 摘要 + 用户画像格式化为 System Prompt 注入文本。

        Args:
            sessions: SessionMemory 列表（已按相关度排序）。
            profile: UserProfile 实例或 None。
            max_tokens: 最大 Token 数。

        Returns:
            格式化的记忆文本，空列表且无画像时返回空字符串。
        """
        settings = get_settings()
        parts: list[str] = []

        # ── Session 摘要 ─────────────────────────────
        if sessions:
            parts.append("## 用户历史\n")
            parts.append("你之前和该用户有过以下相关对话：\n")
            now = datetime.now(timezone.utc)
            for sm in sessions:
                created = sm.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_ago = (now - created).days
                if days_ago == 0:
                    time_str = "今天"
                elif days_ago == 1:
                    time_str = "昨天"
                elif days_ago < 7:
                    time_str = f"{days_ago}天前"
                elif days_ago < 30:
                    time_str = f"{days_ago // 7}周前"
                else:
                    time_str = f"{days_ago // 30}个月前"
                parts.append(f"- [{time_str}] {sm.summary_text[:150]}")

        # ── 用户画像（冷启动保护）─────────────────────
        if profile is not None and (
            (profile.total_sessions or 0) >= settings.MEMORY_MIN_SESSIONS_FOR_PROFILE
        ):
            profile_text = self._format_profile_summary(profile)
            if profile_text:
                parts.append(f"\n**用户偏好摘要**：{profile_text}")

        if not parts:
            return ""

        parts.append("\n请结合以上历史信息回答当前问题。如果历史和当前问题无关，可以忽略。")
        result = "\n".join(parts)

        # ── Token 截断 ──────────────────────────────
        try:
            from agent.memory import count_tokens

            if count_tokens(result) <= max_tokens:
                return result

            enc = __import__("tiktoken").get_encoding("cl100k_base")
            tokens = enc.encode(result)
            return enc.decode(tokens[:max_tokens])
        except Exception:
            return result[: max_tokens * 4]

    @staticmethod
    def _format_profile_summary(profile) -> str:
        """格式化用户画像摘要为简短文本。

        Args:
            profile: UserProfile 实例。

        Returns:
            如 "喜欢科幻/机战类型，偏好80-90年代作品"，或空字符串。
        """
        prefs = profile.preferences_json or {}
        if not prefs:
            return ""

        # Phase 5.3: L3 画像是补充信息——权重低于 L1/L2
        # 输出精简，不做统计数字展示，避免模型过度依赖画像
        parts: list[str] = []

        # 偏好类型 top-2（精简——给方向感即可）
        genres = prefs.get("favorite_genres", [])
        if genres:
            top_genres = [
                g["genre"] for g in genres[:2] if g.get("genre")
            ]
            if top_genres:
                parts.append(f"偏好{'/'.join(top_genres)}类作品")

        # 实体亲和 top-2（精简）
        affinities = prefs.get("entity_affinities", {})
        if affinities:
            top_entities = sorted(
                affinities.values(),
                key=lambda e: e.get("interest_score", 0),
                reverse=True,
            )[:2]
            entity_names = [
                e["name"] for e in top_entities if e.get("name")
            ]
            if entity_names:
                parts.append(f"关注{'/'.join(entity_names)}")

        return "，".join(parts) if parts else ""

    # ═══════════════════════════════════════════════════════════════════
    # Phase 6 桩
    # ═══════════════════════════════════════════════════════════════════

    async def remember_public(self, **kwargs: Any) -> None:
        """[Phase 6] 写入公共记忆。当前为 no-op。"""
        pass

    async def recall_public(self, query: str) -> list:
        """[Phase 6] 召回公共记忆。当前返回空列表。"""
        return []


# ═══════════════════════════════════════════════════════════════════════════
# 模块级单例
# ═══════════════════════════════════════════════════════════════════════════

_memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取进程级 MemoryManager 单例。

    首次调用时从 Settings 读取配置并初始化，后续调用返回缓存实例。

    Returns:
        MemoryManager: 全局唯一的记忆管理器实例。
    """
    global _memory_manager
    if _memory_manager is None:
        settings = get_settings()
        from database.engine import engine as db_engine

        _memory_manager = MemoryManager(
            engine=db_engine,
            zhipu_api_key=settings.ZHIPU_API_KEY,
            llm_model=settings.LLM_MODEL,
        )
    return _memory_manager
