"""Conversation retrieval, project recency, and the single-agent transition in the browser."""
from __future__ import annotations

import json
import os
import sys
from urllib.parse import parse_qs, urlsplit

from api_stub import DEFAULT_APP, expect_app, folders
from playwright.sync_api import expect, sync_playwright
from screenshots import S1, UNHANDLED, stub

BASE = os.environ.get('APP_URL', DEFAULT_APP)
CHROMIUM = os.environ.get('CHROMIUM', '/usr/local/bin/chromium')


def run() -> int:
    projects = [
        dict(id='garden', name='Garden', total=1, members=1, last_message_at='2026-09-19T00:00:00Z'),
        dict(id='voice', name='Voice', total=1, members=1, last_message_at='2026-09-17T00:00:00Z', system='voice'),
        dict(id='empty', name='Empty', total=0, members=0, last_message_at=''),
    ]
    for p in projects:
        p.update(folders=folders('/projects/' + p['id']), created_at='2026-01-01T00:00:00Z', settings={'snapshots': False}, active=0, loops=0)
    agents = [dict(id=S1, title='Planting plan', project_id='garden', project='Garden', model='Local model', status='waiting', created_at='2026-01-01T00:00:00Z', last_message_at=projects[0]['last_message_at'], run_id=None, metadata={}),
              dict(id='spoken', title='A spoken question', project_id='voice', project='Voice', model='Local model', status='idle', created_at='2026-01-01T00:00:00Z', last_message_at=projects[1]['last_message_at'], run_id=None, metadata={})]
    requests = []

    def route(answer):
        url = urlsplit(answer.request.url)
        if url.path == '/api/sessions':
            return answer.fulfill(content_type='application/json', body=json.dumps(dict(sessions=agents, projects=projects)))
        if url.path == '/api/projects':
            return answer.fulfill(content_type='application/json', body=json.dumps([{**p, 'sessions': []} for p in projects]))
        if url.path == '/api/sessions/search':
            q = parse_qs(url.query)['q'][0]
            requests.append(q)
            result = dict(sessions=[{**agents[0], 'match': {'score': 1, 'snippet': 'Grow tomatoes on the balcony'}}], projects=[projects[0]], semantic=q != 'exact', reason='ready' if q != 'exact' else 'off', partial=False, indexing=False)
            if q == 'no results':
                result.update(sessions=[], projects=[])
            return answer.fulfill(content_type='application/json', body=json.dumps(result))
        return stub(answer)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=CHROMIUM)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        page.route('**/api/**', route)
        page.goto(f'{BASE}/agents?token=t&lang=en')
        expect(page.locator('.folder')).to_have_count(3)
        assert page.locator('.folder').evaluate_all('(nodes) => nodes.map(n => n.dataset.project)') == ['garden', 'voice', 'empty']
        garden = page.locator('[data-project="garden"]')
        expect(garden).to_have_class('folder single  ')
        expect(garden).to_contain_text('Local model')
        expect(garden).to_contain_text('Needs you')
        assert garden.locator('.erow-time').get_attribute('title')
        # The path is available in project settings; mobile navigation spends no row on it. Opening a
        # project of one agent would show that path and nothing else, so a phone has no chevron for it.
        expect(garden.locator('.folder-expand')).to_be_hidden()
        expect(garden.locator('.folder-root')).to_have_count(0)
        # A single-agent project uses the session menu instead of a second overflow beside it.
        expect(garden.locator('.folder-actions')).to_have_count(0)
        garden.get_by_role('button', name='Planting plan: More').click()
        page.get_by_role('menuitem', name='Settings for Garden').click()
        expect(page.get_by_role('dialog', name='Garden', exact=True)).to_be_visible()
        page.get_by_role('dialog').get_by_role('button', name='Close', exact=True).click()
        page.locator('[data-project="empty"] .folder-head').click()
        expect(page.locator('[data-project="empty"] .folder-add')).to_be_visible()
        search = page.get_by_role('searchbox', name='Search conversations')
        # The phone's composer stands above the list and steps aside while the search has the reader,
        # so what a search finds is under the field rather than in the lower third of the screen.
        composer = page.locator('.start-composer')
        expect(composer).to_be_visible()
        search.focus()
        expect(composer).to_be_hidden()
        search.fill('vegetables outside')
        expect(page.locator('.search-passage')).to_have_text('Grow tomatoes on the balcony')
        expect(page.locator('.erow')).to_have_count(1)
        expect(page.locator('.search-notice')).to_contain_text('Matching words and meaning')
        search.fill('exact')
        expect(page.locator('.search-notice')).to_contain_text('Exact search only')
        expect(page.locator('.search-notice a')).to_have_attribute('href', '/app/settings/components')
        search.fill('no results')
        expect(page.locator('.search-notice')).to_contain_text('Matching words and meaning')
        expect(page.locator('.erow')).to_have_count(0)
        search.fill('')
        expect(page.locator('.folder')).to_have_count(3)
        expect(composer).to_be_hidden()
        search.blur()
        expect(composer).to_be_visible()
        agents.append({**agents[0], 'id': 'second', 'title': 'Watering schedule', 'last_message_at': '2026-09-19T01:00:00Z'})
        projects[0].update(total=2, members=2, last_message_at=agents[-1]['last_message_at'])
        expect(garden.locator('.erow')).to_have_count(2, timeout=10000)
        assert 'single' not in garden.get_attribute('class').split()
        expect(garden.locator('.erow-title').first).to_have_text('Watering schedule')
        # A row pressed while the empty field still has focus: the composer returns only after the
        # press, so the list does not slide under the pointer and the click opens the row it was on.
        search.focus()
        expect(composer).to_be_hidden()
        garden.get_by_text('Planting plan', exact=True).click()
        expect(page).to_have_url(f'{BASE}/agents/{S1}')
        expect(page.locator('.chat-scroll')).to_be_visible()
        assert requests == ['vegetables outside', 'exact', 'no results']
        browser.close()
    print('project recency, hybrid row, transition, conversation search and fallback: passed')
    return UNHANDLED.report()


if __name__ == '__main__':
    expect_app(BASE)
    sys.exit(run())
