"""``native`` Gmail upstream: read-only tools implemented on the GA Gmail REST API.

Every request is a GET made with the calling user's own access token, which Google issued
for ``gmail.readonly`` only, so Google itself rejects any write even if this code had a
bug. IDs are validated before they are placed in a URL path.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx2
from fastmcp.tools.base import ToolResult
from mcp_types import TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_ro_proxy.auth.google import GMAIL_READONLY
from mcp_ro_proxy.config import GmailSettings
from mcp_ro_proxy.upstreams.base import Principal, UpstreamError, UpstreamTool

_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"
_LIST_HEADERS = ("Subject", "From", "To", "Date")
_MESSAGE_HEADERS = ("subject", "from", "to", "cc", "date")


class AccessTokenSource(Protocol):
    async def get(self, principal: Principal) -> str: ...

    def invalidate(self, sub: str) -> None: ...


# --- tool arguments ------------------------------------------------------------------


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchThreadsArgs(_Args):
    query: str = Field(
        default="",
        max_length=2000,
        description=(
            "Gmail search query, same syntax as the Gmail search box, for example "
            "'from:alice@example.com newer_than:7d' or 'label:invoices is:unread'. "
            "Empty returns the most recent threads."
        ),
    )
    max_results: int = Field(default=10, ge=1, le=50, description="Threads per page (1-50).")
    page_token: str | None = Field(
        default=None, max_length=512, description="next_page_token from a previous call."
    )
    include_spam_trash: bool = Field(default=False, description="Also search Spam and Trash.")


class GetThreadArgs(_Args):
    thread_id: str = Field(pattern=_ID_PATTERN, description="Thread id from search_threads.")
    include_bodies: bool = Field(
        default=True, description="Include each message's text body (false: headers only)."
    )
    max_body_chars: int | None = Field(
        default=None, ge=1, description="Truncate each body to this many characters."
    )


class GetMessageArgs(_Args):
    message_id: str = Field(pattern=_ID_PATTERN, description="Message id from get_thread.")
    max_body_chars: int | None = Field(
        default=None, ge=1, description="Truncate the body to this many characters."
    )


class ListLabelsArgs(_Args):
    pass


class ListDraftsArgs(_Args):
    max_results: int = Field(default=10, ge=1, le=50, description="Drafts per page (1-50).")
    page_token: str | None = Field(
        default=None, max_length=512, description="next_page_token from a previous call."
    )


# --- message parsing -----------------------------------------------------------------


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


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = unescape("".join(parser.parts))
    lines = (re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _decode_body(data: str | None) -> str:
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


@dataclass(frozen=True)
class Body:
    text: str
    source: str | None
    truncated: bool


def extract_body(payload: dict[str, Any], max_chars: int) -> Body:
    plain: list[str] = []
    html: list[str] = []
    for part in _walk(payload):
        if part.get("filename"):
            continue
        mime = str(part.get("mimeType", ""))
        data = cast(dict[str, Any], part.get("body") or {}).get("data")
        if mime == "text/plain":
            plain.append(_decode_body(data))
        elif mime == "text/html":
            html.append(_decode_body(data))
    if any(p.strip() for p in plain):
        text, source = "\n".join(plain).strip(), "text/plain"
    elif any(h.strip() for h in html):
        text, source = html_to_text("\n".join(html)), "text/html"
    else:
        text, source = "", None
    truncated = len(text) > max_chars
    return Body(text=text[:max_chars], source=source, truncated=truncated)


def attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for part in _walk(payload):
        filename = part.get("filename")
        if filename:
            body = cast(dict[str, Any], part.get("body") or {})
            found.append(
                {
                    "filename": str(filename),
                    "mime_type": str(part.get("mimeType", "")),
                    "size": int(body.get("size", 0)),
                }
            )
    return found


def summarize_message(
    message: dict[str, Any], *, include_body: bool, max_chars: int
) -> dict[str, Any]:
    payload = cast(dict[str, Any], message.get("payload") or {})
    out: dict[str, Any] = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds", []),
        "snippet": unescape(str(message.get("snippet", ""))),
        **_headers(payload, _MESSAGE_HEADERS),
        "attachments": attachments(payload),
    }
    if include_body:
        body = extract_body(payload, max_chars)
        out["body"] = {"text": body.text, "source": body.source, "truncated": body.truncated}
    return out


# --- Gmail REST client (GET only) ----------------------------------------------------


class GmailApi:
    def __init__(self, http: httpx2.AsyncClient, settings: GmailSettings, token: str) -> None:
        self._http = http
        self._settings = settings
        self._token = token

    async def get(self, path: str, params: Sequence[tuple[str, str]] = ()) -> dict[str, Any]:
        url = f"{self._settings.api_base_url}/gmail/v1/users/me/{path}"
        try:
            resp = await self._http.get(
                url,
                params=list(params),
                headers={"Authorization": f"Bearer {self._token}", "Accept": "application/json"},
                timeout=self._settings.request_timeout_seconds,
            )
        except httpx2.HTTPError as exc:
            raise UpstreamError("Could not reach the Gmail API.") from exc
        if resp.status_code == 200:
            return cast(dict[str, Any], resp.json())
        if resp.status_code == 401:
            raise _UnauthorizedError
        if resp.status_code == 404:
            raise UpstreamError("Gmail could not find that item.")
        if resp.status_code == 403:
            raise UpstreamError("Gmail refused the request (403).")
        if resp.status_code == 400:
            raise UpstreamError("Gmail rejected the request as invalid (400).")
        if resp.status_code == 429:
            raise UpstreamError("Gmail rate limit reached; try again shortly.")
        raise UpstreamError(f"Gmail API error ({resp.status_code}); try again later.")


class _UnauthorizedError(Exception):
    pass


def _id(value: str) -> str:
    return quote(value, safe="")


# --- the upstream --------------------------------------------------------------------

Handler = Callable[["GmailNativeUpstream", GmailApi, Any], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class NativeTool:
    name: str
    title: str
    description: str
    args: type[_Args]
    handler: Handler

    def definition(self) -> UpstreamTool:
        return UpstreamTool(
            name=self.name,
            description=self.description,
            input_schema=self.args.model_json_schema(),
            title=self.title,
        )


async def _search_threads(
    up: GmailNativeUpstream, api: GmailApi, args: SearchThreadsArgs
) -> dict[str, Any]:
    params: list[tuple[str, str]] = [("maxResults", str(args.max_results))]
    if args.query:
        params.append(("q", args.query))
    if args.page_token:
        params.append(("pageToken", args.page_token))
    if args.include_spam_trash:
        params.append(("includeSpamTrash", "true"))
    listing = await api.get("threads", params)
    ids = [str(t["id"]) for t in cast(list[dict[str, Any]], listing.get("threads") or [])]
    meta_params = [("format", "metadata"), *(("metadataHeaders", h) for h in _LIST_HEADERS)]
    threads = await up.gather(api.get(f"threads/{_id(i)}", meta_params) for i in ids)
    out: list[dict[str, Any]] = []
    for thread in threads:
        messages = cast(list[dict[str, Any]], thread.get("messages") or [])
        first = messages[0] if messages else {}
        last = messages[-1] if messages else {}
        headers = _headers(cast(dict[str, Any], first.get("payload") or {}), _LIST_HEADERS)
        last_headers = _headers(cast(dict[str, Any], last.get("payload") or {}), ("Date",))
        labels = sorted({str(lbl) for m in messages for lbl in m.get("labelIds", [])})
        out.append(
            {
                "id": thread.get("id"),
                "subject": headers.get("subject", ""),
                "from": headers.get("from", ""),
                "to": headers.get("to", ""),
                "last_message_date": last_headers.get("date", headers.get("date", "")),
                "message_count": len(messages),
                "label_ids": labels,
                "snippet": unescape(str(last.get("snippet", ""))),
            }
        )
    return {
        "threads": out,
        "next_page_token": listing.get("nextPageToken"),
        "result_size_estimate": listing.get("resultSizeEstimate"),
    }


async def _get_thread(
    up: GmailNativeUpstream, api: GmailApi, args: GetThreadArgs
) -> dict[str, Any]:
    fmt = "full" if args.include_bodies else "metadata"
    params: list[tuple[str, str]] = [("format", fmt)]
    if not args.include_bodies:
        params.extend(("metadataHeaders", h) for h in _MESSAGE_HEADERS)
    thread = await api.get(f"threads/{_id(args.thread_id)}", params)
    max_chars = up.body_limit(args.max_body_chars)
    messages = [
        summarize_message(m, include_body=args.include_bodies, max_chars=max_chars)
        for m in cast(list[dict[str, Any]], thread.get("messages") or [])
    ]
    return {"id": thread.get("id"), "messages": messages}


async def _get_message(
    up: GmailNativeUpstream, api: GmailApi, args: GetMessageArgs
) -> dict[str, Any]:
    message = await api.get(f"messages/{_id(args.message_id)}", [("format", "full")])
    return summarize_message(
        message, include_body=True, max_chars=up.body_limit(args.max_body_chars)
    )


async def _list_labels(
    up: GmailNativeUpstream, api: GmailApi, args: ListLabelsArgs
) -> dict[str, Any]:
    del up, args
    listing = await api.get("labels")
    labels = [
        {"id": lbl.get("id"), "name": lbl.get("name"), "type": lbl.get("type")}
        for lbl in cast(list[dict[str, Any]], listing.get("labels") or [])
    ]
    return {"labels": sorted(labels, key=lambda lbl: (str(lbl["type"]), str(lbl["name"])))}


async def _list_drafts(
    up: GmailNativeUpstream, api: GmailApi, args: ListDraftsArgs
) -> dict[str, Any]:
    params: list[tuple[str, str]] = [("maxResults", str(args.max_results))]
    if args.page_token:
        params.append(("pageToken", args.page_token))
    listing = await api.get("drafts", params)
    ids = [str(d["id"]) for d in cast(list[dict[str, Any]], listing.get("drafts") or [])]
    meta_params = [("format", "metadata"), *(("metadataHeaders", h) for h in _LIST_HEADERS)]
    drafts = await up.gather(api.get(f"drafts/{_id(i)}", meta_params) for i in ids)
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


NATIVE_TOOLS: tuple[NativeTool, ...] = (
    NativeTool(
        name="search_threads",
        title="Search Gmail threads",
        description=(
            "Search the signed-in user's Gmail and return matching threads with subject, "
            "sender, date, labels and a snippet. Read-only."
        ),
        args=SearchThreadsArgs,
        handler=cast(Handler, _search_threads),
    ),
    NativeTool(
        name="get_thread",
        title="Read a Gmail thread",
        description=(
            "Return every message in a Gmail thread with headers, labels, attachment names "
            "and the text body. Read-only."
        ),
        args=GetThreadArgs,
        handler=cast(Handler, _get_thread),
    ),
    NativeTool(
        name="get_message",
        title="Read a Gmail message",
        description=(
            "Return one Gmail message with headers, labels, attachment names and the text "
            "body. Read-only."
        ),
        args=GetMessageArgs,
        handler=cast(Handler, _get_message),
    ),
    NativeTool(
        name="list_labels",
        title="List Gmail labels",
        description="List the signed-in user's Gmail labels (system and user labels). Read-only.",
        args=ListLabelsArgs,
        handler=cast(Handler, _list_labels),
    ),
    NativeTool(
        name="list_drafts",
        title="List Gmail drafts",
        description=(
            "List the signed-in user's Gmail drafts with recipient, subject and snippet. "
            "Read-only: drafts cannot be created, changed or sent."
        ),
        args=ListDraftsArgs,
        handler=cast(Handler, _list_drafts),
    ),
)


class GmailNativeUpstream:
    def __init__(
        self,
        name: str,
        settings: GmailSettings,
        *,
        tokens: AccessTokenSource,
        http: httpx2.AsyncClient,
    ) -> None:
        self._name = name
        self._settings = settings
        self._tokens = tokens
        self._http = http
        self._tools = {t.name: t for t in NATIVE_TOOLS}
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    @property
    def name(self) -> str:
        return self._name

    @property
    def google_scopes(self) -> frozenset[str]:
        return frozenset({GMAIL_READONLY})

    def body_limit(self, requested: int | None) -> int:
        cap = self._settings.max_body_chars
        return min(requested, cap) if requested else cap

    async def gather(self, calls: Iterable[Awaitable[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Run Gmail reads concurrently (bounded); the first failure cancels the rest."""

        async def limited(call: Awaitable[dict[str, Any]]) -> dict[str, Any]:
            async with self._semaphore:
                return await call

        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(limited(c)) for c in calls]
        except ExceptionGroup as eg:
            raise eg.exceptions[0] from None
        return [t.result() for t in tasks]

    async def list_tools(self, principal: Principal | None) -> Sequence[UpstreamTool]:
        del principal
        return [t.definition() for t in self._tools.values()]

    async def get_tool(self, principal: Principal | None, name: str) -> UpstreamTool | None:
        del principal
        tool = self._tools.get(name)
        return tool.definition() if tool else None

    async def call_tool(
        self, principal: Principal | None, name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            raise UpstreamError(f"Unknown tool: {name}")
        if principal is None:
            raise UpstreamError("Gmail tools need an authenticated user.")
        try:
            args = tool.args.model_validate(arguments)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(p) for p in e["loc"]) or "arguments" for e in exc.errors()}
            )
            raise UpstreamError(f"Invalid arguments: {', '.join(fields)}") from exc

        result = await self._run(tool, args, principal)
        text = json.dumps(result, ensure_ascii=False)
        return ToolResult(content=[TextContent(type="text", text=text)])

    async def _run(self, tool: NativeTool, args: _Args, principal: Principal) -> dict[str, Any]:
        try:
            return await tool.handler(
                self, GmailApi(self._http, self._settings, await self._tokens.get(principal)), args
            )
        except _UnauthorizedError:
            # The cached access token was revoked or expired early: refresh once.
            self._tokens.invalidate(principal.sub)
        try:
            return await tool.handler(
                self, GmailApi(self._http, self._settings, await self._tokens.get(principal)), args
            )
        except _UnauthorizedError:
            self._tokens.invalidate(principal.sub)
            raise UpstreamError("Gmail rejected the stored Google access.") from None

    async def aclose(self) -> None:
        return None
