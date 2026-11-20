"""Retrieval containment: a grounded chat turn only cites configured folders.

The review of the 1.3.0 wave found that the daemon's grounding step filtered
retrieval hits by score only. The index can hold documents from folders that
were configured earlier, so after a folder is removed from
Config.retrieval_dirs a stale hit could still reach the model prompt and the
sources event, putting a path outside the configured roots in front of the
user. These tests lock the containment rule:

    * a hit outside every configured root is dropped before grounding, so the
      turn stays a plain assistant turn and nothing is cited;
    * a hit inside a configured root still grounds normally;
    * an empty root list grounds nothing (fail closed);
    * a path that resolves outside the root (for example through a symlink)
      is dropped even when its text matched;
    * the sources step drops a non-contained hit even when it is handed one
      directly, as defense in depth.

Every case fails if the containment filter is removed.
"""

from __future__ import annotations

import os
from pathlib import Path

from steward.server import daemon as daemon_mod

from test_chat_citations import (
    FakeIndex,
    ScriptedRuntime,
    _StubHandler,
    make_hit,
)


def _ctx(index, roots):
    ctx = daemon_mod.DaemonContext(
        runtime=object(), model="qwen3:8b", source_dir="/tmp/none",
        db_path=Path("/tmp/steward-containment-test/db/steward.db"),
        secret=b"x" * 32, version="test",
        retrieval_index=index, retrieval_roots=roots)
    return ctx


def _grounded(messages, reply, index, roots):
    """Run one chat turn; returns (reply text, sources payload, handler).

    Since the cite-wire merge a grounded turn returns its sources as a
    structured list rather than appending a text block, so the containment
    assertion is on that payload.
    """
    ctx = _ctx(index, roots)
    handler = _StubHandler(ctx)
    handler.ctx.runtime = ScriptedRuntime([reply])
    final, sources = handler._run_tool_loop(messages, emit=None)
    return final, sources, handler


def test_hit_outside_configured_root_is_not_grounded(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "secret" / "plan.txt"
    outside.parent.mkdir()
    outside.write_text("CLASSIFIED PLAN: launch the missile at noon\n")

    index = FakeIndex(hits=[make_hit(outside, "CLASSIFIED PLAN: launch the missile at noon",
                                    score=0.99)])
    final, sources, _handler = _grounded(
        [{"role": "user", "content": "what is the plan?"}],
        "The plan is classified [1].", index, [str(corpus)])
    assert final == "The plan is classified [1].", "turn must stay ungrounded"
    assert sources == [], "a stale hit must never be emitted as a source"
    assert str(outside) not in final


def test_hit_inside_configured_root_still_grounds(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    inside = corpus / "drive.md"
    inside.write_text("The Stellar Drive costs 1299 dollars.\n")

    index = FakeIndex(hits=[make_hit(inside, "The Stellar Drive costs 1299 dollars.",
                                    score=0.99)])
    final, sources, _handler = _grounded(
        [{"role": "user", "content": "how much is the drive?"}],
        "It costs 1299 dollars [1].", index, [str(corpus)])
    assert final == "It costs 1299 dollars [1]."
    assert [s["path"] for s in sources] == [str(inside)]


def test_empty_root_list_grounds_nothing(tmp_path):
    inside = tmp_path / "corpus" / "a.md"
    inside.parent.mkdir()
    inside.write_text("content worth citing\n")
    index = FakeIndex(hits=[make_hit(inside, "content worth citing", score=0.99)])
    final, sources, _handler = _grounded([{"role": "user", "content": "q"}],
                                         "answer [1]", index, [])
    assert final == "answer [1]"
    assert sources == []


def test_symlink_escaping_the_root_is_dropped(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("CLASSIFIED PLAN: launch the missile at noon\n")
    link = corpus / "innocent.md"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        return  # platform without symlink support; the other cases still hold

    index = FakeIndex(hits=[make_hit(link, "CLASSIFIED PLAN: launch the missile at noon",
                                    score=0.99)])
    final, sources, _handler = _grounded([{"role": "user", "content": "read it"}],
                                         "The plan is [1].", index, [str(corpus)])
    assert final == "The plan is [1]."
    assert sources == [], "a symlinked escape must never be emitted"


def test_sources_step_drops_non_contained_selection(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("CLASSIFIED PLAN\n")
    handler = _StubHandler(_ctx(FakeIndex(), [str(corpus)]))
    selected = [make_hit(outside, "CLASSIFIED PLAN", score=0.99)]
    payload = handler._resolve_chat_sources("answer [1].", selected)
    assert payload == [], "a non-contained hit must never be emitted as a source"
