"""Concrete LLM use cases (one class per call).

Each class extends one of the bases in ``core.llm_task`` (Text/Json/Streaming)
and encapsulates: (input type, prompt, parser, output type). Stages just
``await task.run(input)`` — they never see the prompt or the raw response.
"""
