"""The `gmail_api` upstream: read-only Gmail tools on the GA Gmail REST API.

Every request is a GET made with the signed-in person's own Google token, which holds only
`gmail.readonly`, so Google itself refuses any change even if this code had a bug. Ids are
checked before they go into a URL path. The tool names match Google's Gmail MCP server, so an
allowlist works the same with either upstream.
"""

import asyncio
import base64
import binascii
import re
from collections.abc import Coroutine, Iterable, Sequence
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Any, cast
from urllib.parse import quote

import httpx2
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token
from mcp_types import ToolAnnotations
from pydantic import Field

_ID = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
_LIST_HEADERS = ("Subject", "From", "To", "Date")
_MESSAGE_HEADERS = ("subject", "from", "to", "cc", "date")
# Longest a message body may be, whatever the agent asks for; the response size cap is separate.
MAX_BODY_CHARS = 20_000
# Parallel Gmail requests per tool call (search and list_drafts fetch each result).
MAX_CONCURRENCY = 8
TIMEOUT_S = 30
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


# --- message parsing ------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "head", "title"})
    _BREAK = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    text = unescape("".join(parser.parts))
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode(data: str | None) -> str:
    if not data:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in cast(list[dict[str, Any]], part.get("parts") or []):
        parts.extend(_walk(child))
    return parts


def _headers(payload: dict[str, Any], wanted: Sequence[str]) -> dict[str, str]:
    wanted_lower = {w.lower() for w in wanted}
    found: dict[str, str] = {}
    for header in cast(list[dict[str, Any]], payload.get("headers") or []):
        name = str(header.get("name", "")).lower()
        if name in wanted_lower and name not in found:
            found[name] = str(header.get("value", ""))
    return found


def extract_body(payload: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """The message's text: its text/plain parts, or else its HTML turned into text."""
    plain: list[str] = []
    markup: list[str] = []
    for part in _walk(payload):
        if part.get("filename"):
            continue
        data = cast(dict[str, Any], part.get("body") or {}).get("data")
        if part.get("mimeType") == "text/plain":
            plain.append(_decode(data))
        elif part.get("mimeType") == "text/html":
            markup.append(_decode(data))
    if any(p.strip() for p in plain):
        text, source = "\n".join(plain).strip(), "text/plain"
    elif any(h.strip() for h in markup):
        text, source = html_to_text("\n".join(markup)), "text/html"
    else:
        text, source = "", None
    return {"text": text[:max_chars], "source": source, "truncated": len(text) > max_chars}


def attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for part in _walk(payload):
        if part.get("filename"):
            body = cast(dict[str, Any], part.get("body") or {})
            found.append(
                {
                    "filename": str(part["filename"]),
                    "mime_type": str(part.get("mimeType", "")),
                    "size": int(body.get("size", 0)),
                }
            )
    return found


def summarize_message(message: dict[str, Any], *, body: bool, max_chars: int) -> dict[str, Any]:
    payload = cast(dict[str, Any], message.get("payload") or {})
    out: dict[str, Any] = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds", []),
        "snippet": unescape(str(message.get("snippet", ""))),
        **_headers(payload, _MESSAGE_HEADERS),
        "attachments": attachments(payload),
    }
    if body:
        out["body"] = extract_body(payload, max_chars)
    return out


def _body_limit(requested: int | None) -> int:
    return min(requested, MAX_BODY_CHARS) if requested else MAX_BODY_CHARS


# --- Gmail REST client (GET only) -----------------------------------------------------------


class GmailApi:
    def __init__(self, base_url: str, http: httpx2.AsyncClient):
        self._base = f"{base_url.rstrip('/')}/gmail/v1/users/me"
        self._http = http
        self._limit = asyncio.Semaphore(MAX_CONCURRENCY)

    async def get(self, path: str, params: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
        token = get_access_token()
        if token is None:
            raise ToolError("Not signed in.")
        try:
            async with self._limit:
                response = await self._http.get(
                    f"{self._base}/{path}",
                    params=list(params),
                    headers={"Authorization": f"Bearer {token.token}"},
                )
        except httpx2.HTTPError:
            raise ToolError("Couldn't reach the Gmail API. Try again later.") from None
        if response.status_code == 200:
            return cast(dict[str, Any], response.json())
        raise ToolError(
            {
                401: "Gmail refused the stored Google access; sign in again.",
                403: "Gmail refused the request (403).",
                404: "Gmail couldn't find that item.",
                400: "Gmail rejected the request as invalid (400).",
                429: "Gmail's rate limit was reached; try again shortly.",
            }.get(
                response.status_code, f"Gmail API error ({response.status_code}); try again later."
            )
        )

    async def gather(
        self, calls: Iterable[Coroutine[Any, Any, dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Run Gmail reads concurrently; the first failure cancels the rest."""
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(c) for c in calls]
        except ExceptionGroup as eg:
            raise eg.exceptions[0] from None
        return [t.result() for t in tasks]


def _id(value: str) -> str:
    return quote(value, safe="")


# --- the tools ------------------------------------------------------------------------------


def add_gmail_tools(server: FastMCP[Any], base_url: str) -> None:  # noqa: PLR0915 - one def per tool
    """Register the read-only Gmail tools on `server`, calling the Gmail API at `base_url`."""

    def api(http: httpx2.AsyncClient) -> GmailApi:
        return GmailApi(base_url, http)

    def client() -> httpx2.AsyncClient:
        return httpx2.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False)

    @server.tool(title="Search Gmail threads", annotations=READ_ONLY)
    async def search_threads(
        query: Annotated[
            str,
            Field(
                max_length=2000,
                description="Gmail search query, same syntax as the Gmail search box, for "
                "example 'from:alice@example.com newer_than:7d' or 'label:invoices is:unread'. "
                "Empty returns the most recent threads.",
            ),
        ] = "",
        max_results: Annotated[int, Field(ge=1, le=50, description="Threads per page.")] = 10,
        page_token: Annotated[
            str | None, Field(max_length=512, description="next_page_token from a previous call.")
        ] = None,
        include_spam_trash: Annotated[
            bool, Field(description="Also search Spam and Trash.")
        ] = False,
    ) -> dict[str, Any]:
        """Search the signed-in person's Gmail and return matching threads with subject, sender,
        date, labels and a snippet. Read-only."""
        params: list[tuple[str, str]] = [("maxResults", str(max_results))]
        if query:
            params.append(("q", query))
        if page_token:
            params.append(("pageToken", page_token))
        if include_spam_trash:
            params.append(("includeSpamTrash", "true"))
        async with client() as http:
            gmail = api(http)
            listing = await gmail.get("threads", params)
            ids = [str(t["id"]) for t in cast(list[dict[str, Any]], listing.get("threads") or [])]
            meta = [("format", "metadata"), *(("metadataHeaders", h) for h in _LIST_HEADERS)]
            threads = await gmail.gather(gmail.get(f"threads/{_id(i)}", meta) for i in ids)
        out: list[dict[str, Any]] = []
        for thread in threads:
            messages = cast(list[dict[str, Any]], thread.get("messages") or [])
            first = messages[0] if messages else {}
            last = messages[-1] if messages else {}
            headers = _headers(cast(dict[str, Any], first.get("payload") or {}), _LIST_HEADERS)
            last_date = _headers(cast(dict[str, Any], last.get("payload") or {}), ("Date",))
            out.append(
                {
                    "id": thread.get("id"),
                    "subject": headers.get("subject", ""),
                    "from": headers.get("from", ""),
                    "to": headers.get("to", ""),
                    "last_message_date": last_date.get("date", headers.get("date", "")),
                    "message_count": len(messages),
                    "label_ids": sorted(
                        {str(lb) for m in messages for lb in m.get("labelIds", [])}
                    ),
                    "snippet": unescape(str(last.get("snippet", ""))),
                }
            )
        return {
            "threads": out,
            "next_page_token": listing.get("nextPageToken"),
            "result_size_estimate": listing.get("resultSizeEstimate"),
        }

    @server.tool(title="Read a Gmail thread", annotations=READ_ONLY)
    async def get_thread(
        thread_id: Annotated[str, _ID, Field(description="Thread id from search_threads.")],
        include_bodies: Annotated[
            bool, Field(description="Include each message's text body (false: headers only).")
        ] = True,
        max_body_chars: Annotated[
            int | None, Field(ge=1, description="Cut each body to this many characters.")
        ] = None,
    ) -> dict[str, Any]:
        """Return every message in a Gmail thread with headers, labels, attachment names and the
        text body. Read-only."""
        params: list[tuple[str, str]] = [("format", "full" if include_bodies else "metadata")]
        if not include_bodies:
            params.extend(("metadataHeaders", h) for h in _MESSAGE_HEADERS)
        async with client() as http:
            thread = await api(http).get(f"threads/{_id(thread_id)}", params)
        limit = _body_limit(max_body_chars)
        return {
            "id": thread.get("id"),
            "messages": [
                summarize_message(m, body=include_bodies, max_chars=limit)
                for m in cast(list[dict[str, Any]], thread.get("messages") or [])
            ],
        }

    @server.tool(title="Read a Gmail message", annotations=READ_ONLY)
    async def get_message(
        message_id: Annotated[str, _ID, Field(description="Message id from get_thread.")],
        max_body_chars: Annotated[
            int | None, Field(ge=1, description="Cut the body to this many characters.")
        ] = None,
    ) -> dict[str, Any]:
        """Return one Gmail message with headers, labels, attachment names and the text body.
        Read-only."""
        async with client() as http:
            message = await api(http).get(f"messages/{_id(message_id)}", [("format", "full")])
        return summarize_message(message, body=True, max_chars=_body_limit(max_body_chars))

    @server.tool(title="List Gmail labels", annotations=READ_ONLY)
    async def list_labels() -> dict[str, Any]:
        """List the signed-in person's Gmail labels (system and user labels). Read-only."""
        async with client() as http:
            listing = await api(http).get("labels")
        labels = [
            {"id": lb.get("id"), "name": lb.get("name"), "type": lb.get("type")}
            for lb in cast(list[dict[str, Any]], listing.get("labels") or [])
        ]
        return {"labels": sorted(labels, key=lambda lb: (str(lb["type"]), str(lb["name"])))}

    @server.tool(title="List Gmail drafts", annotations=READ_ONLY)
    async def list_drafts(
        max_results: Annotated[int, Field(ge=1, le=50, description="Drafts per page.")] = 10,
        page_token: Annotated[
            str | None, Field(max_length=512, description="next_page_token from a previous call.")
        ] = None,
    ) -> dict[str, Any]:
        """List the signed-in person's Gmail drafts with recipient, subject and snippet.
        Read-only: drafts can't be created, changed or sent."""
        params: list[tuple[str, str]] = [("maxResults", str(max_results))]
        if page_token:
            params.append(("pageToken", page_token))
        async with client() as http:
            gmail = api(http)
            listing = await gmail.get("drafts", params)
            ids = [str(d["id"]) for d in cast(list[dict[str, Any]], listing.get("drafts") or [])]
            drafts = await gmail.gather(
                gmail.get(f"drafts/{_id(i)}", [("format", "metadata")]) for i in ids
            )
        out: list[dict[str, Any]] = []
        for draft in drafts:
            message = cast(dict[str, Any], draft.get("message") or {})
            headers = _headers(cast(dict[str, Any], message.get("payload") or {}), _LIST_HEADERS)
            out.append(
                {
                    "id": draft.get("id"),
                    "message_id": message.get("id"),
                    "thread_id": message.get("threadId"),
                    "subject": headers.get("subject", ""),
                    "to": headers.get("to", ""),
                    "date": headers.get("date", ""),
                    "snippet": unescape(str(message.get("snippet", ""))),
                }
            )
        return {"drafts": out, "next_page_token": listing.get("nextPageToken")}

    @server.tool(title="Read a Gmail draft", annotations=READ_ONLY)
    async def get_draft(
        draft_id: Annotated[str, _ID, Field(description="Draft id from list_drafts.")],
        max_body_chars: Annotated[
            int | None, Field(ge=1, description="Cut the body to this many characters.")
        ] = None,
    ) -> dict[str, Any]:
        """Return one Gmail draft with recipients, subject and text body. Read-only: drafts can't
        be created, changed or sent."""
        async with client() as http:
            draft = await api(http).get(f"drafts/{_id(draft_id)}", [("format", "full")])
        message = cast(dict[str, Any], draft.get("message") or {})
        return {
            "id": draft.get("id"),
            "message": summarize_message(message, body=True, max_chars=_body_limit(max_body_chars)),
        }

    del search_threads, get_thread, get_message, list_labels, list_drafts, get_draft
