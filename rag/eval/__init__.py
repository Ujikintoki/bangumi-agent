"""
RAG 检索评测管线

提供自动 Ground Truth 生成、池化人工标注、多元指标计算的完整评测流程。

用法::

    python -m rag.eval.evaluate --build       # 生成 GT + 标注模板
    python -m rag.eval.evaluate --evaluate    # 检索 + 计算指标
"""
