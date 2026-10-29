"""轮次执行状态；连接状态和回答质量不属于这台状态机。"""
ACTIVE = frozenset({'accepted', 'running', 'cancelling'})
TERMINAL = frozenset({'completed', 'failed', 'cancelled', 'interrupted'})
TRANSITIONS = {
    'accepted': {'running', 'cancelling', 'cancelled', 'interrupted', 'failed'},
    'running': {'cancelling', 'completed', 'failed', 'cancelled', 'interrupted'},
    'cancelling': {'cancelled', 'completed', 'failed', 'interrupted'},
}
