"""Feedback loop: "wrong department?" links in e-mails -> corrections -> evaluation dataset.

* `GET  /api/v1/feedback/{ticket_id}?token=...`  - small HTML form (link from the e-mail),
* `POST /api/v1/feedback/{ticket_id}?token=...`  - store the verdict (HTML form or JSON),
* `GET  /api/v1/feedback/export`                 - corrections as YAML cases for `dataset.yaml`.

Links are signed (HMAC), so recipients need no account and nobody can forge feedback.
"""

import html
from typing import Annotated

import yaml
from fastapi import APIRouter, HTTPException, Path, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from ai_router.errors import TicketNotFoundError
from ai_router.observability import metrics
from ai_router.storage.tickets import TicketRecord
from ai_router.web.dependencies import ContainerDep
from ai_router.web.schemas import FeedbackRequest, FeedbackResponse

router = APIRouter(prefix="/api/v1/feedback", tags=["feedback"])

TicketId = Annotated[str, Path(pattern=r"^[0-9a-f]{32}$")]
Token = Annotated[
    str, Query(min_length=16, max_length=64, description="Signed token from the e-mail.")
]

_PAGE = """<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>body{{font-family:sans-serif;max-width:640px;margin:2rem auto;padding:0 1rem;
line-height:1.5}}
label{{display:block;margin:.3rem 0}} textarea{{width:100%}} button{{padding:.5rem 1rem}}
.muted{{color:#666}}</style></head><body>{body}</body></html>"""


async def _ticket_for_feedback(container: ContainerDep, ticket_id: str, token: str) -> TicketRecord:
    signer = container.tickets.signer
    ticket = await container.tickets.get(ticket_id)
    if (
        signer is None
        or ticket is None
        or ticket.result is None
        or not signer.verify(ticket_id, token)
    ):
        raise TicketNotFoundError()  # invalid tokens look exactly like unknown tickets
    return ticket


@router.get(
    "/export",
    summary="Export department corrections as evaluation cases (YAML)",
    response_class=PlainTextResponse,
)
async def export_feedback(request: Request, container: ContainerDep) -> PlainTextResponse:
    container.authenticator.require_admin(request)
    cases = [
        {
            "id": f"fb-{record.ticket_id[:8]}",
            "message": record.message,
            "expected": record.correct_department,
            "tags": ["feedback", "real"],
            "routed_as": record.routed_department,
        }
        for record in await container.feedback.all()
    ]
    body = yaml.safe_dump(cases, allow_unicode=True, sort_keys=False) if cases else "[]\n"
    return PlainTextResponse(body, media_type="application/yaml")


@router.get(
    "/{ticket_id}", summary="Feedback form (link from the e-mail)", response_class=HTMLResponse
)
async def feedback_form(ticket_id: TicketId, token: Token, container: ContainerDep) -> HTMLResponse:
    ticket = await _ticket_for_feedback(container, ticket_id, token)
    assert ticket.result is not None  # noqa: S101 - checked above
    current = ticket.result["department"]
    options = "".join(
        f'<label><input type="radio" name="correct_department" value="{html.escape(d.address)}"'
        f"{' checked' if d.address == current else ''}> {html.escape(d.name_pl)} "
        f'<span class="muted">({html.escape(d.address)})</span></label>'
        for d in container.policy.departments
    )
    body = (
        f"<h1>Zgłoszenie {html.escape(ticket.ticket_code)}</h1>"
        f"<p>AI Router przekazał je do <strong>{html.escape(current)}</strong>.</p>"
        f'<p class="muted">Uzasadnienie modelu: {html.escape(ticket.result.get("reason", ""))}</p>'
        f'<form method="post" action="/api/v1/feedback/{ticket_id}?token={html.escape(token)}">'
        f"<p>Który dział powinien się tym zająć?</p>{options}"
        '<p><label>Komentarz (opcjonalnie):<textarea name="comment" rows="3" maxlength="1000">'
        '</textarea></label></p><button type="submit">Wyślij</button></form>'
    )
    return HTMLResponse(_PAGE.format(title="Informacja zwrotna - AI Router", body=body))


@router.post(
    "/{ticket_id}",
    summary="Store feedback (HTML form or JSON)",
    response_model=FeedbackResponse,
    responses={200: {"content": {"text/html": {}}}},
)
async def submit_feedback(
    ticket_id: TicketId, token: Token, request: Request, container: ContainerDep
) -> Response:
    ticket = await _ticket_for_feedback(container, ticket_id, token)
    assert ticket.result is not None  # noqa: S101
    is_form = request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    )
    if is_form:
        form = await request.form()
        feedback = FeedbackRequest(
            correct_department=str(form.get("correct_department", "")),
            comment=str(form.get("comment") or "") or None,
        )
    else:
        feedback = FeedbackRequest.model_validate(await request.json())
    correct = container.policy.get(feedback.correct_department)
    if correct is None:
        raise HTTPException(422, "Unknown department.")
    routed = str(ticket.result["department"])
    verdict = "confirmed" if correct.address == routed else "corrected"
    await container.feedback.add(
        ticket_id=ticket_id,
        message=ticket.message or "",
        routed_department=routed,
        correct_department=correct.address,
        comment=feedback.comment,
    )
    metrics.FEEDBACK.labels(verdict).inc()
    if is_form:
        body = (
            "<h1>Dziękujemy!</h1><p>Informacja zwrotna została zapisana i pomoże ulepszyć "
            "automatyczne przypisywanie zgłoszeń.</p>"
        )
        return HTMLResponse(_PAGE.format(title="Dziękujemy - AI Router", body=body))
    return Response(
        FeedbackResponse(ticket_id=ticket_id, verdict=verdict).model_dump_json(),
        media_type="application/json",
    )
