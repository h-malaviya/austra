"""Provider-counted answer context assembled from one EvidencePacket."""

import json
import re
from collections.abc import Callable
from typing import Any

from austra.observability import observed_message
from austra.skills import load_skill

MODEL = "claude-sonnet-5"
INPUT_TARGET = 12_000
INPUT_HARD_CAP = 16_000
OUTPUT_RESERVE = 3_000
NUMERIC = re.compile(r"\b\d+(?:[.,]\d+)?\b")
UUID_CITATION = re.compile(r"\[([0-9a-f-]{36})\]")
PACKET_CITATION = re.compile(r"\[(?:S)?([1-9]\d*)\]")
UUID_TOKEN = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)
WORKFLOW_KEY = re.compile(r"\bworkflow_?([0-9a-f]{32})(?:_([a-z][a-z0-9_]*))?\b", re.IGNORECASE)


def unavailable_answer(packet: dict, question: str, *, administrator: bool) -> str:
    reason = packet.get("reason", "")
    if reason == "Historical record totals are unavailable":
        return "I can't verify a historical record total from the available data."
    if reason == "No typed SQL aggregate for this total":
        return "I can't calculate that total reliably from the available records."
    if reason == "Choose a workflow":
        return "Which released workflow do you mean? Include its name so I can check the approved rule."
    if reason == "Approved sources conflict":
        return "The approved sources conflict, so I can't give a reliable answer yet."
    if administrator:
        if reason == "No applicable published blueprint" and re.search(
            r"\b(?:rule|policy|approval|authorize|authorise|permission)\b",
            question,
            re.IGNORECASE,
        ):
            return (
                "I can't find an approved business rule for this yet. A working draft may be "
                "available in Business discovery, but it does not count as an approved rule."
            )
        if reason == "No applicable published blueprint":
            return (
                "I couldn't find approved information that answers this question. "
                "You can review the relevant sources in Business discovery."
            )
        return (
            "I couldn't find enough approved information to answer that. Review sources in "
            "Business discovery and proposed workflows in Workflow reviews."
        )
    return (
        "I couldn't find an approved answer in the information available to your department. "
        "A department manager can review the sources or your access."
    )


def readable_citations(answer: str, packet: dict, cited: set[str]) -> str:
    rows = packet["passages"] + packet["exact"] + packet["semantic"] + packet.get("exact_records", [])
    labels = {}
    for row in rows:
        identifier = str(row["id"])
        if identifier not in cited:
            continue
        name = row.get("display_name") or row.get("stable_key") or row.get("key")
        locator = row.get("locator")
        label = ", ".join(str(part) for part in (name, locator) if part) or "Approved record"
        labels[identifier] = readable_reference(label)
    order = list(dict.fromkeys(UUID_CITATION.findall(answer)))
    numbers = {identifier: index for index, identifier in enumerate(order, 1)}
    body = UUID_CITATION.sub(lambda match: f"[{numbers[match.group(1)]}]", answer)
    body = readable_reference(body)
    sources = "\n".join(f"{numbers[item]}. {labels[item]}" for item in order)
    return f"{body}\n\nSources:\n{sources}"


def readable_reference(text: str) -> str:
    def workflow_label(match: re.Match) -> str:
        step = match.group(2)
        return "Workflow rule" + (" · " + step.replace("_", " ") if step else "")

    return UUID_TOKEN.sub("record", WORKFLOW_KEY.sub(workflow_label, text))


def answer_packet(
    client: Any,
    question: str,
    packet: dict,
    *,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
    conversation_context: dict | None = None,
    on_text: Callable[[str], None] | None = None,
    administrator: bool = True,
) -> dict:
    if packet.get("profile"):
        profile = packet["profile"]
        value = profile["value"]
        return {
            "status": "supported",
            "answer": f"According to the confirmed company profile: {value}",
            "citation_ids": [str(profile["id"])],
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    if packet.get("aggregate", {}).get("measure") == "active_record_count":
        value = packet["aggregate"]["value"]
        return {
            "status": "supported",
            "answer": f"There are {value} active records.",
            "citation_ids": [],
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }
    if packet["status"] != "supported" or not packet["coverage"]:
        return {
            "status": "unknown",
            "answer": unavailable_answer(packet, question, administrator=administrator),
            "citation_ids": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }
    system = (
        load_skill("retrieving-evidence")
        + "\nEach evidence row has a citation label such as [S1] for its exact source ID. "
        "Use only labels from this packet. Cite each factual statement with the row's label. "
        "Use plain sentences without numbered lists or a sources footer; the app adds the footer. "
        "Say unknown for unsupported parts. "
        "Conversation context can clarify the user's wording and style, but it is not "
        "evidence for a factual answer. "
        "Do not compute totals; only quote SQL aggregates supplied in the packet."
    )
    passages = list(packet["passages"])
    while True:
        sources = {}
        evidence = {}
        for field in ("passages", "exact", "semantic", "exact_records"):
            evidence[field] = []
            for row in passages if field == "passages" else packet.get(field, []):
                identifier = str(row["id"])
                number = sources.setdefault(identifier, len(sources) + 1)
                evidence[field].append({**row, "citation": f"[S{number}]"})
        content = json.dumps(
            {
                "question": question,
                "blueprint_id": str(packet["blueprint_id"]),
                **evidence,
                "conversation_context": conversation_context or {},
            },
            default=str,
            ensure_ascii=False,
        )
        messages = [{"role": "user", "content": content}]
        counted = client.messages.count_tokens(
            model=MODEL,
            system=system,
            messages=messages,
        ).input_tokens
        if counted <= INPUT_TARGET:
            break
        if not passages:
            raise ValueError("Exact facts and question exceed the input token target")
        passages.pop()
    if (
        not passages
        and not packet["exact"]
        and not packet["semantic"]
        and not packet.get("exact_records")
    ):
        return {
            "status": "unknown",
            "answer": "The supporting passages exceed the context budget.",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": 0,
        }
    if counted > INPUT_HARD_CAP:
        raise ValueError("Input token hard cap exceeded")
    response = observed_message(
        client,
        "retrieval_answer",
        on_text=on_text,
        model=MODEL,
        max_tokens=OUTPUT_RESERVE,
        system=system,
        messages=messages,
    )
    if response.stop_reason != "end_turn":
        raise ValueError("Answer did not finish")
    answer = "\n".join(block.text for block in response.content if block.type == "text")
    by_number = {str(number): identifier for identifier, number in sources.items()}
    unknown_label = any(label not in by_number for label in PACKET_CITATION.findall(answer))
    answer = PACKET_CITATION.sub(
        lambda m: f"[{by_number[m[1]]}]" if m[1] in by_number else m[0], answer
    )
    cited = set(UUID_CITATION.findall(answer))
    cost = None
    if input_usd_per_million is not None and output_usd_per_million is not None:
        cost = round(
            (
                counted * input_usd_per_million
                + response.usage.output_tokens * output_usd_per_million
            )
            / 1_000_000,
            8,
        )
    if unknown_label or not cited or not cited <= sources.keys():
        return {
            "status": "unknown",
            "answer": "I cannot verify citations for that answer.",
            "failure_reason": "citation",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": response.usage.output_tokens,
            "estimated_cost_usd": cost,
        }
    cited_text = " ".join(
        str(row.get("text", ""))
        + json.dumps(row.get("content", {}), default=str)
        + str(row.get("rationale", ""))
        + str(row.get("external_id", ""))
        for row in passages + packet["exact"] + packet["semantic"] + packet.get("exact_records", [])
        if str(row["id"]) in cited
    )
    answer_without_citations = UUID_CITATION.sub("", answer)
    if set(NUMERIC.findall(answer_without_citations)) - set(NUMERIC.findall(cited_text)):
        return {
            "status": "unknown",
            "answer": "I cannot verify a number in that answer.",
            "failure_reason": "citation",
            "citation_ids": [],
            "input_tokens": counted,
            "output_tokens": response.usage.output_tokens,
            "estimated_cost_usd": cost,
        }
    return {
        "status": "supported",
        "answer": readable_citations(answer, packet, cited),
        "citation_ids": sorted(cited),
        "input_tokens": counted,
        "output_tokens": response.usage.output_tokens,
        "estimated_cost_usd": cost,
    }
