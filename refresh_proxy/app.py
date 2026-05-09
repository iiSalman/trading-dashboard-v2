"""Tiny Flask proxy: the public GitHub Pages dashboard cannot hold a GitHub
token, so it POSTs here. We hold GH_TOKEN as a Render env var, validate the
request origin, and forward to GitHub's workflow_dispatch endpoint."""
import os
import time
import threading
from collections import deque

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

GH_TOKEN     = os.environ.get('GH_TOKEN', '').strip()
GH_REPO      = os.environ.get('GH_REPO', 'iiSalman/trading-dashboard-v2')
GH_WORKFLOW  = os.environ.get('GH_WORKFLOW', 'refresh.yml')
GH_BRANCH    = os.environ.get('GH_BRANCH', 'main')

ALLOWED_ORIGINS = {
    'https://iisalman.github.io',
    'http://localhost:8000',
    'http://localhost:5050',
    'http://127.0.0.1:8000',
}

_recent = deque(maxlen=20)
_lock = threading.Lock()
RATE_WINDOW = 30  # seconds between dispatches per origin


def _cors_headers(origin):
    if origin in ALLOWED_ORIGINS:
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
            'Access-Control-Max-Age': '600',
        }
    return {}


def _rate_limited(origin):
    now = time.time()
    with _lock:
        for o, ts in list(_recent):
            if now - ts > RATE_WINDOW * 4:
                _recent.popleft()
        for o, ts in _recent:
            if o == origin and (now - ts) < RATE_WINDOW:
                return int(RATE_WINDOW - (now - ts))
        _recent.append((origin, now))
    return 0


@app.route('/refresh', methods=['POST', 'OPTIONS'])
def refresh():
    origin = request.headers.get('Origin', '')
    headers = _cors_headers(origin)

    if request.method == 'OPTIONS':
        return ('', 204, headers)

    if origin not in ALLOWED_ORIGINS:
        return (jsonify({'error': 'origin not allowed'}), 403, headers)

    wait = _rate_limited(origin)
    if wait:
        return (jsonify({'error': 'rate limited', 'retry_in_seconds': wait}), 429, headers)

    if not GH_TOKEN:
        return (jsonify({'error': 'GH_TOKEN not configured on server'}), 500, headers)

    r = requests.post(
        f'https://api.github.com/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches',
        headers={
            'Authorization': f'Bearer {GH_TOKEN}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'trading-dashboard-refresher',
        },
        json={'ref': GH_BRANCH},
        timeout=15,
    )

    if r.status_code == 204:
        return (jsonify({
            'status': 'dispatched',
            'workflow': GH_WORKFLOW,
            'expected_eta_seconds': 35,
        }), 200, headers)
    return (
        jsonify({'error': f'GitHub returned {r.status_code}', 'detail': r.text[:300]}),
        502, headers,
    )


@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'repo': GH_REPO,
        'workflow': GH_WORKFLOW,
        'branch': GH_BRANCH,
        'token_configured': bool(GH_TOKEN),
    })


@app.route('/')
def root():
    return jsonify({
        'service': 'trading-dashboard refresh proxy',
        'endpoints': ['POST /refresh', 'GET /health'],
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5050)))
