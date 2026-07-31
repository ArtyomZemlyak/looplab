"""Authenticated current/history projections for event-sourced operator comments."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Response

from looplab.events.comment_projection import (
    CommentCursorError, comments_page, history_page, project_comments)
from looplab.serve.run_commands import run_generation_token


def _cursor_error(exc: CommentCursorError) -> HTTPException:
    return HTTPException(409 if exc.stale else 400, {
        "code": "comment_cursor_stale" if exc.stale else "invalid_comment_cursor",
        "message": str(exc),
        "remediation": "refresh comments from the first page",
    })


def _stable_events(srv, rd):
    """Read one generation and reject a reset/replacement that wins during projection."""
    events = srv.events(rd)
    generation = run_generation_token(events)
    if not generation:
        raise HTTPException(409, {
            "code": "run_generation_unavailable",
            "message": "the run has no durable generation identity",
            "remediation": "wait for run_started, then refresh comments",
        })
    return events, generation


def _assert_still_current(srv, rd, generation: str) -> None:
    """Reject a reset/replacement that wins the race against an in-flight projection.

    The token is derived from the FIRST durable event, so it does NOT change when a log is repaired or
    rewritten in place. That raised a real question: could a RETAINED comment anchor serve page one from
    the old body and page two from the replacement? Checked against every rewrite this code actually
    performs, it cannot — each is already rejected one layer down:
      * `looplab repair-log` truncates to the last valid boundary, dropping the TAIL. The newest
        comments live there, so a page-one anchor goes with it and `comments_page` rejects the vanished
        anchor rather than paging into the repaired body.
      * node/run deletion (`tools/machine_runs_tools.py`) drops records WITHOUT renumbering, so the
        survivors leave a seq gap and `EventStore.read_all` fails closed AT that gap. Everything newer —
        including the anchor — is outside the recoverable prefix, so the same rejection applies.
    The residual is an in-place edit that preserves BOTH dense seq numbering and the anchor row. No path
    here produces that (it needs events.jsonl edited underneath the server), and it is the same
    middle-of-prefix case `eventstore.prefix_anchor_from_handle` already documents its head/tail windows
    cannot catch. Binding cursors to a byte revision here would advertise an integrity guarantee the
    event store itself declines to make, so this stays a generation check.
    `tests/test_collaboration.py::test_same_generation_log_rewrites_cannot_split_a_comment_page`
    locks in both rejections, so weakening either one fails loudly instead of silently mixing pages.
    """
    if srv.commands.run_generation(rd) != generation:
        raise HTTPException(409, {
            "code": "run_generation_changed",
            "message": "the run was reset while comments were being projected",
            "remediation": "refresh comments for the replacement run generation",
        })


def build_router(srv) -> APIRouter:
    router = APIRouter()

    @router.get("/api/runs/{run_id}/comments")
    def list_comments(run_id: str, response: Response,
                      limit: int = Query(100, ge=1, le=100),
                      cursor: Optional[str] = None,
                      node_id: Optional[int] = Query(None, ge=0),
                      node_generation: Optional[int] = Query(None, ge=0),
                      include_resolved: bool = True):
        rd = srv.run_dir(run_id)
        if (node_id is None) != (node_generation is None):
            raise HTTPException(400, {
                "code": "comment_filter_invalid",
                "message": "node_id and node_generation must be supplied together",
                "remediation": "select an exact experiment lifecycle or remove both filters",
            })
        events, generation = _stable_events(srv, rd)
        comments, _history = project_comments(events)
        try:
            payload = comments_page(
                comments, generation=generation, limit=limit, cursor=cursor,
                node_id=node_id, node_generation=node_generation,
                include_resolved=include_resolved)
        except CommentCursorError as exc:
            raise _cursor_error(exc) from exc
        _assert_still_current(srv, rd, generation)
        response.headers["Cache-Control"] = "no-store"
        return payload

    @router.get("/api/runs/{run_id}/comments/{comment_id}/history")
    def comment_history(run_id: str, comment_id: str, response: Response,
                        limit: int = Query(100, ge=1, le=100),
                        cursor: Optional[str] = None):
        rd = srv.run_dir(run_id)
        events, generation = _stable_events(srv, rd)
        comments, histories = project_comments(events, include_history=True)
        if comment_id not in comments:
            raise HTTPException(404, {
                "code": "comment_not_found",
                "message": "the comment does not exist in this run generation",
                "remediation": "refresh the collaboration panel",
            })
        try:
            payload = history_page(
                comment_id, histories.get(comment_id, []), generation=generation,
                limit=limit, cursor=cursor)
        except CommentCursorError as exc:
            raise _cursor_error(exc) from exc
        _assert_still_current(srv, rd, generation)
        response.headers["Cache-Control"] = "no-store"
        return payload

    return router
