from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from novel_agent.config import default_max_steps
from novel_agent.repository import NovelCorpus
from novel_agent.service import execute_agent, load_corpus
from novel_agent.tracing import run_metrics


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _load_corpus(path: str) -> NovelCorpus:
    loaded = load_corpus(path)
    corpus = loaded.corpus
    print(
        f"已加载：{Path(path).name} | 编码={corpus.document.source.encoding} | "
        f"sections={len(corpus.document.sections)} | chunks={len(corpus.document.chunks)} | "
        f"耗时={loaded.elapsed_seconds:.2f}s",
        file=sys.stderr,
    )
    return corpus


def command_inspect(args: argparse.Namespace) -> int:
    corpus = _load_corpus(args.novel)
    summary = corpus.structure_summary()
    sections = summary.pop("sections")
    summary["sample_sections"] = sections[: args.limit_sections]
    summary["omitted_section_count"] = max(0, len(sections) - args.limit_sections)
    _print_json(summary)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(corpus.structure_summary(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"完整结构报告：{output.resolve()}", file=sys.stderr)
    return 0


def command_search(args: argparse.Namespace) -> int:
    corpus = _load_corpus(args.novel)
    _print_json([hit.model_dump() for hit in corpus.search(args.keyword, args.top_k)])
    return 0


def _compact_update(node: str, update: dict[str, Any]) -> str:
    parts = [f"node={node}"]
    if "step_count" in update:
        parts.append(f"step={update['step_count']}")
    if update.get("current_task_id"):
        parts.append(f"task={update['current_task_id']}")
    if "evidence" in update:
        parts.append(f"evidence={len(update['evidence'])}")
    if "plan" in update:
        parts.append(f"plan={len(update['plan'])}")
    if update.get("termination_reason"):
        parts.append(f"termination={update['termination_reason']}")
    messages = update.get("messages") or []
    if messages:
        message = messages[-1]
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            parts.append("tools=" + ",".join(call.get("name", "unknown") for call in calls))
    return "[Trace] " + " | ".join(parts)


def _compact_diagnostic(diagnostic: dict[str, Any]) -> str:
    code = diagnostic.get("diagnostic_code")
    node = diagnostic.get("source_node", "unknown")
    schema = diagnostic.get("schema", "unknown")
    if code == "structured_output_retry":
        return (
            f"[Retry] node={node} | schema={schema} | "
            f"retry={diagnostic.get('retry_number')}/{diagnostic.get('max_retries')} | "
            f"reason={diagnostic.get('failure_reason')}"
        )
    if code == "content_json_fallback":
        return f"[Fallback] node={node} | schema={schema} | source=content_json"
    return f"[StructuredOutputError] node={node} | schema={schema}"


def run_agent(
    corpus: NovelCorpus,
    question: str,
    *,
    max_steps: int,
    thread_id: str,
    trace_path: Path,
) -> dict[str, Any]:
    result = execute_agent(
        corpus,
        question,
        max_steps=max_steps,
        thread_id=thread_id,
        trace_path=trace_path,
        on_update=lambda node, update, _state: print(
            _compact_update(node, update), file=sys.stderr
        ),
        on_diagnostic=lambda diagnostic, _state: print(
            _compact_diagnostic(diagnostic), file=sys.stderr
        ),
    )
    return result.state


def command_ask(args: argparse.Namespace) -> int:
    corpus = _load_corpus(args.novel)
    thread_id = args.thread_id or uuid.uuid4().hex[:12]
    trace_path = Path(args.trace or f"output/traces/{thread_id}.jsonl")
    state = run_agent(
        corpus,
        args.question,
        max_steps=args.max_steps,
        thread_id=thread_id,
        trace_path=trace_path,
    )
    print(state.get("final_answer") or "未生成最终答案。")
    if state.get("limitations"):
        print("\n限制：")
        for item in state["limitations"]:
            print(f"- {item}")
    print("\n运行指标：", file=sys.stderr)
    print(json.dumps(run_metrics(state), ensure_ascii=False, indent=2), file=sys.stderr)
    print(f"Trace：{trace_path.resolve()}", file=sys.stderr)
    return 0


def command_evaluate(args: argparse.Namespace) -> int:
    corpus = _load_corpus(args.novel)
    questions = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    if not isinstance(questions, list):
        raise ValueError("评测文件必须是 JSON 数组。")
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for index, item in enumerate(questions, start=1):
        question = str(item["question"])
        case_id = str(item.get("id", f"case-{index:03d}"))
        print(f"\n[Evaluate] {case_id}: {question}", file=sys.stderr)
        state = run_agent(
            corpus,
            question,
            max_steps=args.max_steps,
            thread_id=f"eval-{case_id}-{uuid.uuid4().hex[:8]}",
            trace_path=output_dir / f"{case_id}.jsonl",
        )
        results.append(
            {
                "id": case_id,
                "question": question,
                "answer": state.get("final_answer"),
                "limitations": state.get("limitations", []),
                "metrics": run_metrics(state),
            }
        )
    result_path = output_dir / "results.json"
    result_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"评测结果：{result_path.resolve()}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="证据约束的小说解读 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="检查 TXT 编码、结构识别和切分结果")
    inspect_parser.add_argument("novel", help="小说 TXT 路径")
    inspect_parser.add_argument("--limit-sections", type=int, default=20)
    inspect_parser.add_argument("--output", help="保存完整 JSON 结构报告")
    inspect_parser.set_defaults(func=command_inspect)

    search_parser = subparsers.add_parser("search", help="不调用模型，直接测试本地检索")
    search_parser.add_argument("novel", help="小说 TXT 路径")
    search_parser.add_argument("keyword", help="搜索关键词，多个词使用空格分隔")
    search_parser.add_argument("--top-k", type=int, default=5)
    search_parser.set_defaults(func=command_search)

    ask_parser = subparsers.add_parser("ask", help="运行完整长链路 Agent")
    ask_parser.add_argument("novel", help="小说 TXT 路径")
    ask_parser.add_argument("question", help="需要基于原文调查的问题")
    ask_parser.add_argument("--max-steps", type=int, default=default_max_steps())
    ask_parser.add_argument("--thread-id")
    ask_parser.add_argument("--trace", help="JSONL Trace 输出路径")
    ask_parser.set_defaults(func=command_ask)

    eval_parser = subparsers.add_parser("evaluate", help="批量运行问题并汇总轨迹指标")
    eval_parser.add_argument("novel", help="小说 TXT 路径")
    eval_parser.add_argument("questions", help="包含 id/question 的 JSON 数组")
    eval_parser.add_argument("--max-steps", type=int, default=default_max_steps())
    eval_parser.add_argument("--output-dir", default="output/evaluation")
    eval_parser.set_defaults(func=command_evaluate)
    return parser


def main() -> int:
    _configure_console()
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except (FileNotFoundError, KeyError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
