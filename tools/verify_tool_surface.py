#!/usr/bin/env python3
"""Compare a running editor's MetaSounds tool surface against a known hash.

Asks the editor's MCP server for the toolset schema (describe_toolset, the
same text an agent reads), hashes it, and compares the result with a pinned
value: the copy in this repo by default, or the hash you recorded when you
first reviewed the tools (--expected). A mismatch means the tool names,
schemas or descriptions an agent sees have changed since that hash was taken.
A match says only that: it does not cover what the tools do, the bundled
skill, or anything else registered on the server.

Standard library only. It connects straight to 127.0.0.1, ignoring proxy
settings and refusing redirects, so nothing leaves the machine. It needs
Epic's tool search on (the default), because describe_toolset does not exist
with it off.

    python tools/verify_tool_surface.py --port 8000
    python tools/verify_tool_surface.py --port 8000 --expected <sha256>

Exit codes: 0 match, 1 mismatch, 2 could not read the surface or the pin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path

TOOLSET_NAME = 'metasound_toolset.toolset.MetaSoundsToolset'
PIN_PATH = (Path(__file__).resolve().parent.parent / 'Content' / 'Python'
            / 'metasound_toolset' / 'data' / 'tool_surface_pin.json')


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Treat a redirect as an error: the answer must come from the editor."""

    def redirect_request(self, *args, **kwargs):
        return None


# No ProxyHandler entries: urllib would otherwise send 127.0.0.1 traffic to
# any HTTP_PROXY in the environment, and hash whatever the proxy answered.
_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _RefuseRedirects())


def surface_sha256(schema_text: str) -> str:
    """Hash a toolset schema independently of key and tool ordering.

    Must stay identical to metasound_toolset.surface.surface_sha256.
    """
    schema = json.loads(schema_text)
    schema['tools'] = sorted(schema['tools'], key=lambda tool: tool['name'])
    canonical = json.dumps(schema, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def _post(url: str, payload: dict, session_id: str | None) -> tuple[dict, str]:
    headers = {'Content-Type': 'application/json',
               'Accept': 'application/json, text/event-stream'}
    if session_id:
        headers['Mcp-Session-Id'] = session_id
    request = urllib.request.Request(
        url, json.dumps(payload).encode('utf-8'), headers)
    with _OPENER.open(request, timeout=30) as response:
        body = response.read().decode('utf-8')
        new_session = response.headers.get('Mcp-Session-Id', '')
    if not body.strip():
        return {}, new_session
    if 'data:' in body and not body.lstrip().startswith('{'):
        # Streamable HTTP may answer as server-sent events; the JSON-RPC
        # result is the last data line.
        lines = [line[5:].strip() for line in body.splitlines()
                 if line.startswith('data:')]
        return json.loads(lines[-1]), new_session
    return json.loads(body), new_session


def expected_hash(value: object, source: str) -> str:
    """Normalize a hash to compare with, or raise ValueError.

    Anything but 64 hex digits (after trimming and lowercasing) is not a
    hash at all, and must not read as a mismatch: an empty or garbage pin
    says nothing about whether the tools changed.
    """
    if not isinstance(value, str):
        raise ValueError(f'{source} holds no hash string: {value!r}')
    normalized = value.strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', normalized):
        raise ValueError(f'{source} is not a sha256 hex digest: {value!r}')
    return normalized


def fetch_schema(url: str) -> str:
    """Return the describe_toolset text from the MCP server at url."""
    _, session_id = _post(url, {
        'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
        'params': {'protocolVersion': '2025-03-26', 'capabilities': {},
                   'clientInfo': {'name': 'verify_tool_surface',
                                  'version': '1'}}}, None)
    _post(url, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
          session_id)
    reply, _ = _post(url, {
        'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
        'params': {'name': 'describe_toolset',
                   'arguments': {'toolset_name': TOOLSET_NAME}}}, session_id)
    if 'error' in reply:
        raise RuntimeError(str(reply['error']))
    result = reply['result']
    if result.get('isError'):
        raise RuntimeError(str(result.get('content')))
    return result['content'][0]['text']


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--port', type=int, default=8000,
                        help='MCP server port (default 8000)')
    parser.add_argument('--path', default='/mcp',
                        help='MCP server URL path (default /mcp)')
    parser.add_argument('--expected',
                        help='sha256 to compare with (default: the repo pin)')
    args = parser.parse_args()

    url = f'http://127.0.0.1:{args.port}{args.path}'
    pin = None
    # Anything that goes wrong here must not read as a mismatch, which means
    # the tools changed: a dropped connection, a missing pin or a pin that
    # holds no hash says nothing. The pin is read first, so a bad one is
    # reported without needing the editor at all.
    try:
        if args.expected is None:
            pin = json.loads(PIN_PATH.read_text(encoding='utf-8'))
            expected = expected_hash(pin.get('toolset_sha256'),
                                     f'the pin {PIN_PATH}')
        else:
            expected = expected_hash(args.expected, '--expected')
        live = surface_sha256(fetch_schema(url))
    except Exception as exc:  # noqa: BLE001
        print(f'Could not compare the tool surface at {url}: {exc}',
              file=sys.stderr)
        return 2

    print(f'live     {live}')
    print(f'expected {expected}')
    if live == expected:
        print('MATCH')
        return 0
    print('MISMATCH: the tool surface differs from the pinned one.')
    if pin is not None:
        engine = pin.get('engine') or 'an unrecorded engine version'
        print(f'The repo pin was taken on engine {engine}; a different '
              'engine build can change the schema text on its own.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
