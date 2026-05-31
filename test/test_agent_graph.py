"""
Agent 图谱单元测试

验证 LangGraph 的编译、执行与控制流正确性，
不依赖任何 LLM API 或数据库。
"""

from __future__ import annotations

from agent.graph import agent_app, build_graph
from agent.state import AgentState


class TestAgentGraph:
    """Agent 图谱编译与执行测试套件。

    覆盖场景：
        - 正常流转（需工具）：2 轮 ReAct 后自省通过并安全退出。
        - 正常流转（无需工具）：跳过 tool_node，仅 reasoning + critic。
        - 熔断保护：iterations 达上限时强制终止，设置 error_flag。
        - 边界情况：空消息列表、模块级 agent_app 实例可用性。
    """

    def test_graph_compiles_successfully(self) -> None:
        """验证图谱能成功编译且不抛出异常。"""
        graph = build_graph()
        assert graph is not None, "编译后的图谱不应为 None"

    def test_agent_app_available(self) -> None:
        """验证模块级 agent_app 实例已编译且可直接 invoke。"""
        assert agent_app is not None, "agent_app 不应为 None"

    def test_graph_invoke_basic_flow(self) -> None:
        """验证需要工具时的执行流：用户消息含工具意图 → 走 tool_node。

        期望行为：
            1. 初始 iterations=0, critic_status="PENDING", error_flag=False
            2. reasoning_node → iterations=1, needs_tool=True（"找"匹配关键词）
            3. 条件边 needs_tool → tool_node → "Tool execution successful."
            4. critic_node → iterations=1 < 2 → "REVISE"
            5. 条件边 REVISE + 未超限 → 回 reasoning_node
            6. reasoning_node → iterations=2, needs_tool=True
            7. tool_node → "Tool execution successful."
            8. critic_node → iterations=2 >= 2 → "PASS"
            9. 条件边 PASS → END
        """
        graph = build_graph()

        initial_state: AgentState = {
            "messages": ["我想找一部类似《星际牛仔》的高分动画"],
            "iterations": 0,
            "critic_status": "PENDING",
            "error_flag": False,
            "needs_tool": False,
        }

        result = graph.invoke(initial_state)

        # ── 断言: iterations ───────────────────────────────
        assert result["iterations"] == 2, (
            f"预期 iterations=2（经历 2 轮 ReAct 循环），"
            f"实际 iterations={result['iterations']}"
        )

        # ── 断言: critic_status ────────────────────────────
        assert result["critic_status"] == "PASS", (
            f"预期 critic_status='PASS'（第 2 轮后自省通过），"
            f"实际 critic_status={result['critic_status']!r}"
        )

        # ── 断言: error_flag 应为 False（未触发熔断） ──────
        assert result["error_flag"] is False, (
            f"预期 error_flag=False（正常流程不应触发熔断），"
            f"实际 error_flag={result['error_flag']}"
        )

        # ── 断言: needs_tool 最终为 True ───────────────────
        assert result["needs_tool"] is True, (
            f"预期 needs_tool=True（消息含'找'，语义判定需工具），"
            f"实际 needs_tool={result['needs_tool']}"
        )

        # ── 断言: messages 数量 ────────────────────────────
        assert len(result["messages"]) == 5, (
            f"预期 messages 共 5 条（1 用户 + 2 Thinking + 2 Tool），"
            f"实际 {len(result['messages'])} 条"
        )

        # ── 断言: 消息内容 ─────────────────────────────────
        assert result["messages"][0] == "我想找一部类似《星际牛仔》的高分动画"
        assert "Thinking..." in result["messages"][1]
        assert "Tool execution successful." in result["messages"][2]
        assert "Thinking..." in result["messages"][3]
        assert "Tool execution successful." in result["messages"][4]

    def test_graph_invoke_max_iterations_meltdown(self) -> None:
        """验证熔断保护：iterations 达上限时强制终止并设置 error_flag。

        构造初始 iterations=2 的临界状态，进入第 3 轮后
        critic_node 应检测到超限 → 强制 PASS + error_flag=True，
        条件边再补一刀强制 END，绝不抛出 RecursionError。
        """
        graph = build_graph()

        initial_state: AgentState = {
            "messages": ["测试消息"],
            "iterations": 2,
            "critic_status": "PENDING",
            "error_flag": False,
            "needs_tool": False,
        }

        result = graph.invoke(initial_state)

        # reasoning_node → iterations=3
        # critic_node → iterations=3 >= 3 → "PASS" + error_flag=True
        # 条件边 → iterations=3 >= MAX → END（强制）
        assert result["iterations"] == 3, (
            f"预期 iterations=3（临界状态 +1 轮），"
            f"实际 iterations={result['iterations']}"
        )
        assert result["critic_status"] == "PASS"

        # ── 核心断言: error_flag 必须为 True（熔断标记） ───
        assert result["error_flag"] is True, (
            f"预期 error_flag=True（超限熔断已触发），"
            f"实际 error_flag={result['error_flag']}"
        )

    def test_graph_invoke_no_tool_needed(self) -> None:
        """验证无需工具时的执行流：用户说"你好" → 不触发 tool_node。

        这是本架构的核心优化：当 reasoning_node 判定用户意图不需要
        工具调用时，条件边直接跳过 tool_node 进入 critic_node，
        避免对 Bangumi 数据库的无意义查询。

        期望行为：
            1. reasoning_node → iterations=1, needs_tool=False
            2. 条件边 → 跳过 tool_node，直达 critic_node
            3. critic_node → iterations=1 < 2 → "REVISE"
            4. 回到 reasoning_node → iterations=2, needs_tool=False
            5. 跳过 tool_node → critic_node → "PASS" → END
            6. messages 仅含 1 用户 + 2 Thinking，零条 Tool 消息
        """
        graph = build_graph()

        initial_state: AgentState = {
            "messages": ["你好"],
            "iterations": 0,
            "critic_status": "PENDING",
            "error_flag": False,
            "needs_tool": False,
        }

        result = graph.invoke(initial_state)

        assert result["iterations"] == 2
        assert result["critic_status"] == "PASS"
        assert result["error_flag"] is False
        assert result["needs_tool"] is False

        # ── 核心断言: 不应产生任何工具调用消息 ─────────────
        tool_messages = [m for m in result["messages"] if "Tool execution" in str(m)]
        assert len(tool_messages) == 0, (
            f"预期零条 Tool 消息（'你好'不应触发工具调用），"
            f"实际 {len(tool_messages)} 条: {tool_messages}"
        )

        # ── 消息数量: 1 用户 + 2 Thinking = 3 ──────────────
        assert len(result["messages"]) == 3, (
            f"预期 messages 共 3 条（1 用户 + 2 Thinking），"
            f"实际 {len(result['messages'])} 条"
        )
        assert "你好" in str(result["messages"][0])
        assert "Thinking..." in str(result["messages"][1])
        assert "Thinking..." in str(result["messages"][2])

    def test_graph_invoke_no_user_message(self) -> None:
        """验证空消息列表的边界情况。

        即使没有任何用户消息，图谱仍应正常运行并完成 ReAct 循环，
        且因无关键词匹配，needs_tool 始终为 False。
        """
        graph = build_graph()

        initial_state: AgentState = {
            "messages": [],
            "iterations": 0,
            "critic_status": "PENDING",
            "error_flag": False,
            "needs_tool": False,
        }

        result = graph.invoke(initial_state)

        assert result["iterations"] == 2
        assert result["critic_status"] == "PASS"
        assert result["error_flag"] is False
        assert result["needs_tool"] is False
        assert len(result["messages"]) == 2  # 仅 2 条 Thinking，无 Tool
