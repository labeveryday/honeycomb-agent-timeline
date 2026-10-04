"""Run ten sample conversations, so Honeycomb's Agent Timeline looks like production traffic."""
import argparse
import asyncio
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from lab import agents
from lab.agents import build_agents
from lab.server import FIXTURES, start_lab
from lab.telemetry import configure
from main import pick_model, run_turn

SYMPTOM = FIXTURES["symptom"]
# (name, retry version, the messages someone sends in order). Each message is its own trace. Each conversation
# gets a fresh lab, so the deployment API starts busy; in broken ones, a later "try again" succeeds.
CONVERSATIONS = [
    ("full-incident", "fixed", [SYMPTOM, "Which release should we roll back, and what exactly would change?",
                                "Is anything else in r42 risky if we roll it back?", "Write the rollback steps for the on-call engineer.",
                                "What should we tell the support team in the meantime?", "Check whether the portal is healthy right now.",
                                "Draft a status page update.", "What should we alert on so we catch this sooner next time?",
                                "Summarize the timeline of this incident so far.", "Write three action items for the postmortem."]),
    ("portal-down", "broken", [SYMPTOM, "Try the deployment history again.", "So what should we roll back?",
                               "How confident are you?", "What would prove it?", "Check the portal one more time."]),
    ("what-changed", "fixed", ["What changed in the 14:00 deployment?", "Could that change break the portal?",
                               "Is anything else in that deployment risky?", "Who should review the rollback?",
                               "Write a one-line summary for the change log."]),
    ("network-check", "fixed", ["Do portal.lab and inventory.lab resolve, and are their ports open?", "What about port 443?",
                                "Is the portal itself up?", "What is the portal calling upstream?", "So is this a network problem or not?"]),
    ("inventory-api", "fixed", ["Which inventory API versions are answering right now?", "Is v2 healthy enough to point the portal at it?",
                                "What happens to anything still calling v1?", "Which evidence supports that?"]),
    ("logs", "broken", ["When did the inventory errors start?", "Does the timing line up with a deployment?",
                        "Try the deployment history again.", "Summarize it in two sentences."]),
    ("portal-health", "fixed", ["Is the portal healthy right now?", "Why not?", "What's the fastest safe fix?"]),
    ("status-update", "fixed", [SYMPTOM, "Draft a status page update.", "Make it shorter and less technical."]),
    ("what-changed", "broken", ["What changed in the 14:00 deployment?", "Try the deployment history again.",
                                "Could that change break the portal?"]),
    ("portal-down", "fixed", [SYMPTOM, "Summarize this for the incident channel in three bullets."]),
]


async def run_conversation(model, stamp, number, name, retries, messages, limit):
    conversation = f"sample-{stamp}-{number:02d}-{name}-{retries}"
    async with limit:
        coordinator, history, tool_calls, tokens = build_agents(model), None, 0, 0
        try:
            with start_lab() as (base, _):
                for message in messages:
                    result = await run_turn(coordinator, message, base, retries, conversation, history)
                    history = result.all_messages()
                    tool_calls, tokens = tool_calls + result.usage.tool_calls, tokens + result.usage.total_tokens
            print(f"{conversation}: {len(messages)} messages, so {len(messages)} traces, {tool_calls} tool calls, {tokens:,} tokens")
        except Exception as exc:  # noqa: BLE001  (one failed conversation shouldn't stop the rest)
            print(f"{conversation}: stopped, {type(exc).__name__}: {str(exc)[:200]}")


async def run_all(model, stamp):
    limit = asyncio.Semaphore(3)  # a few at once, so they overlap like real traffic without tripping API rate limits
    await asyncio.gather(*(run_conversation(model, stamp, number, *conversation, limit)
                           for number, conversation in enumerate(CONVERSATIONS, 1)))


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true", help="Scripted plumbing test, no AI reasoning or API costs")
    args = parser.parse_args()
    model = pick_model(parser, args.offline)
    agents.SHOW_STEPS = False  # parallel conversations would interleave; Honeycomb has the detail
    stamp = f"{datetime.now().astimezone():%Y%m%d-%H%M}{'-offline' if args.offline else ''}"
    folder = Path("artifacts") / f"samples-{stamp}"
    provider = configure(folder / "spans.jsonl")
    total = sum(len(messages) for _, _, messages in CONVERSATIONS)
    print(f"Running {len(CONVERSATIONS)} conversations ({total} messages), three at a time. "
          "A live run takes about 5 to 8 minutes and uses paid API calls.")
    try:
        asyncio.run(run_all(model, stamp))
        print(f"Artifacts: {folder}")
    finally:
        provider.force_flush(timeout_millis=10000)
        provider.shutdown()


if __name__ == "__main__":
    main()
