from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx

from douyin_live_recorder.data import USER_AGENT

PUSH_PATTERN = re.compile(
    r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*\.push)\s*\("
)
ASSIGNMENT_PATTERN = re.compile(
    r"((?:window\.)?[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*=\s*"
)
JSON_PARSE_PATTERN = re.compile(r"JSON\.parse\s*\(")
STREAM_URL_PATTERN = re.compile(
    r'https?(?:\\+u002[fF]|\\/|/)[^"\'\s<>]+?\.(?:flv|m3u8)(?:\?[^"\'\s<>]*)?',
    re.IGNORECASE,
)
RELEVANT_KEY_PATTERN = re.compile(
    r"room|live|stream|user|anchor|owner|status|pk|link|hls|flv|codec|quality|"
    r"resolution|bitrate|fps|title|nickname",
    re.IGNORECASE,
)


class PageCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self.metadata: list[dict[str, str | None]] = []
        self.title = ""
        self._script_attributes: dict[str, str | None] | None = None
        self._script_content: list[str] = []
        self._in_title = False

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script":
            self._script_attributes = attributes
            self._script_content = []
        elif tag == "meta":
            self.metadata.append(attributes)
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_attributes is not None:
            self.scripts.append(
                (self._script_attributes, "".join(self._script_content))
            )
            self._script_attributes = None
            self._script_content = []
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._script_attributes is not None:
            self._script_content.append(data)
        elif self._in_title:
            self.title += data


def decode_json(source: str) -> Any | None:
    source = source.lstrip()
    if not source or source[0] not in '[{"-0123456789tfn':
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(source)
    except json.JSONDecodeError:
        return None
    return value


def nested_json_values(value: Any, path: str) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    stack = [(path, value)]
    while stack:
        current_path, current = stack.pop()
        if isinstance(current, dict):
            stack.extend(
                (f"{current_path}.{key}", child)
                for key, child in current.items()
            )
        elif isinstance(current, list):
            stack.extend(
                (f"{current_path}[{index}]", child)
                for index, child in enumerate(current)
            )
        elif isinstance(current, str):
            candidates = [(current_path, current)]
            if ":" in current:
                prefix, remainder = current.split(":", 1)
                candidates.append((f"{current_path} after {prefix!r} prefix", remainder))
            for source, candidate in candidates:
                decoded = decode_json(candidate)
                if isinstance(decoded, (dict, list)):
                    found.append((source, decoded))
    return found


def canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(value)


def collect_payloads(
    response_text: str,
    scripts: list[tuple[dict[str, str | None], str]],
) -> list[tuple[str, Any]]:
    pending: list[tuple[str, Any]] = []
    whole_response = decode_json(response_text)
    if whole_response is not None:
        pending.append(("response body", whole_response))

    for index, (attributes, content) in enumerate(scripts):
        script_name = attributes.get("id") or attributes.get("src") or str(index)
        whole_script = decode_json(content)
        if whole_script is not None:
            pending.append((f"script[{script_name}] body", whole_script))

        for match in PUSH_PATTERN.finditer(content):
            value = decode_json(content[match.end() :])
            if value is not None:
                pending.append((f"script[{script_name}] {match.group(1)}", value))

        for match in JSON_PARSE_PATTERN.finditer(content):
            value = decode_json(content[match.end() :])
            if value is not None:
                pending.append((f"script[{script_name}] JSON.parse", value))

        for match in ASSIGNMENT_PATTERN.finditer(content):
            value = decode_json(content[match.end() :])
            if isinstance(value, (dict, list)):
                pending.append(
                    (f"script[{script_name}] assignment {match.group(1)}", value)
                )

    result: list[tuple[str, Any]] = []
    seen: set[bytes] = set()
    offset = 0
    while offset < len(pending):
        source, value = pending[offset]
        offset += 1
        identity = hashlib.sha256(canonical(value).encode()).digest()
        if identity in seen:
            continue
        seen.add(identity)
        result.append((source, value))
        pending.extend(nested_json_values(value, source))
    return sorted(result, key=lambda item: item[0])


def relevant_fields(payloads: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    result: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for source, value in payloads:
        stack = [(source, value)]
        while stack:
            path, current = stack.pop()
            if isinstance(current, dict):
                for key, child in current.items():
                    child_path = f"{path}.{key}"
                    if RELEVANT_KEY_PATTERN.search(str(key)):
                        if isinstance(child, dict):
                            summary: Any = f"object with {len(child)} fields"
                        elif isinstance(child, list):
                            summary = f"array with {len(child)} items"
                        else:
                            summary = child
                        identity = f"{child_path}={summary!r}"
                        if identity not in seen:
                            seen.add(identity)
                            result.append((child_path, summary))
                    stack.append((child_path, child))
            elif isinstance(current, list):
                stack.extend(
                    (f"{path}[{index}]", child)
                    for index, child in enumerate(current)
                )
    return sorted(result, key=lambda item: item[0])


def normalize_stream_url(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\\+u002[fF]", "/", value)
    value = re.sub(r"\\+u0026", "&", value)
    return value.replace(r"\/", "/")


def print_section(title: str) -> None:
    print(f"\n{'=' * 24} {title} {'=' * 24}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect all structured data exposed by a Douyin live page"
    )
    parser.add_argument("user_id", help="Douyin live ID or account handle")
    args = parser.parse_args()

    user_id = args.user_id.strip()
    if not user_id or len(user_id) > 100:
        parser.error("user_id must contain 1 to 100 characters")

    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://live.douyin.com/",
    }

    requested_url = f"https://live.douyin.com/{quote(user_id, safe='')}"
    try:
        with httpx.Client(headers=headers, timeout=30.0, follow_redirects=True) as client:
            response = client.get(requested_url)
    except httpx.HTTPError as error:
        print(f"Request failed: {error}", file=sys.stderr)
        return 1

    response_text = response.text
    collector = PageCollector()
    collector.feed(response_text)
    collector.close()
    payloads = collect_payloads(response_text, collector.scripts)

    print_section("REQUEST")
    print(f"user_id:       {user_id}")
    print(f"requested_url: {requested_url}")
    print(f"final_url:     {response.url}")
    print(f"status:        {response.status_code}")
    print(f"content_type:  {response.headers.get('content-type', '')}")
    print(f"bytes:         {len(response.content)}")
    print(f"encoding:      {response.encoding}")

    print_section("RESPONSE HEADERS")
    for key, value in sorted(response.headers.multi_items()):
        print(f"{key}: {value}")

    print_section("HTML METADATA")
    print(f"title: {collector.title.strip()}")
    print(f"script_count: {len(collector.scripts)}")
    print(f"meta_count: {len(collector.metadata)}")
    for index, metadata in enumerate(collector.metadata):
        print(f"meta[{index}]: {json.dumps(metadata, ensure_ascii=False)}")

    stream_urls = sorted(
        {normalize_stream_url(value) for value in STREAM_URL_PATTERN.findall(response_text)}
    )
    print_section("STREAM URLS")
    hls_urls = [url for url in stream_urls if ".m3u8" in url.casefold()]
    flv_urls = [url for url in stream_urls if ".flv" in url.casefold()]
    print(f"HLS: {len(hls_urls)}")
    for url in hls_urls:
        print(f"  {url}")
    print(f"FLV: {len(flv_urls)}")
    for url in flv_urls:
        print(f"  {url}")

    print_section("RELEVANT FIELD INDEX")
    fields = relevant_fields(payloads)
    print(f"fields: {len(fields)}")
    for path, value in fields:
        rendered = json.dumps(value, ensure_ascii=False, default=str)
        print(f"{path} = {rendered}")

    print_section("ALL DECODED PAYLOADS")
    print(f"payloads: {len(payloads)}")
    for index, (source, value) in enumerate(payloads, start=1):
        print(f"\n--- payload {index}: {source} ---")
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))

    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
