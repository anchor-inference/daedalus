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
    # Garden is a chat's own scratch project, listed as the chat; Empty was made by hand.
    projects[0]['settings'] = {'snapshots': True, 'ephemeral': True}
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
        # A phone's chats are the Chats page: a chat's own scratch project has no section of its own
        # (its chat is filed by day), the Voice folder keeps its section, and an empty project is not drawn
        # (the drawer lists every project). The row's commands are behind its ⋮.
        page = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        page.route('**/api/**', route)
        page.goto(f'{BASE}/agents?view=chats&token=t&lang=en')
        expect(page.locator('.ph-row')).to_have_count(2)
        assert page.locator('.ph-chats section[data-project]').evaluate_all('(nodes) => nodes.map(n => n.dataset.project)') == ['voice']
        # The lone chat's day group depends on today's date, so only its being a day group is asserted.
        titles = page.locator('.ph-sec-t').all_inner_texts()
        assert titles[0] == 'Voice' and len(titles) == 2 and titles[1] in ('Today', 'Yesterday', 'Previous 7 days', 'Previous 30 days', 'Older'), titles
        garden = page.locator(f'[data-session="{S1}"]')
        expect(garden).to_contain_text('Local model')
        expect(garden).to_contain_text('Needs you')
        garden.get_by_role('button', name='Planting plan: More').click()
        page.locator('.ph-actions').get_by_role('button', name='Settings for Garden').click()
        expect(page.get_by_role('dialog', name='Garden', exact=True)).to_be_visible()
        page.get_by_role('dialog').get_by_role('button', name='Close', exact=True).click()
        expect(page.locator('[data-project="empty"]')).to_have_count(0)
        # The search is behind the bar's icon, and the bar becomes the field while it is open.
        page.locator('.ph-top').get_by_role('button', name='Search conversations').click()
        search = page.get_by_role('searchbox', name='Search conversations')
        expect(search).to_be_focused()
        expect(page.locator('.ph-chips')).to_have_count(0)
        search.fill('vegetables outside')
        expect(page.locator('.ph-snip')).to_have_text('Grow tomatoes on the balcony')
        expect(page.locator('.ph-row')).to_have_count(1)
        expect(page.locator('.ph-note')).to_contain_text('Matching words and meaning')
        search.fill('exact')
        expect(page.locator('.ph-note')).to_contain_text('Exact search only')
        expect(page.locator('.ph-note a')).to_have_attribute('href', '/app/settings/components')
        search.fill('no results')
        expect(page.locator('.ph-note')).to_contain_text('Matching words and meaning')
        expect(page.locator('.ph-row')).to_have_count(0)
        expect(page.locator('.ph-empty')).to_contain_text('Nothing matches.')
        page.locator('.ph-top').get_by_role('button', name='Back').click()
        expect(page.locator('.ph-row')).to_have_count(2)
        # A second chat in the project gives it a section of its own, newest first: the server clears
        # the scratch mark when the second top-level chat arrives.
        agents.append({**agents[0], 'id': 'second', 'title': 'Watering schedule', 'last_message_at': '2026-09-19T01:00:00Z'})
        projects[0].update(total=2, members=2, last_message_at=agents[-1]['last_message_at'], settings={'snapshots': True, 'ephemeral': False})
        garden_section = page.locator('section[data-project="garden"]')
        expect(garden_section.locator('.ph-row')).to_have_count(2, timeout=10000)
        expect(garden_section.locator('.ph-row-t').first).to_have_text('Watering schedule')
        garden_section.get_by_text('Planting plan', exact=True).click()
        expect(page).to_have_url(f'{BASE}/agents/{S1}')
        expect(page.locator('.chat-scroll')).to_be_visible()
        assert requests == ['vegetables outside', 'exact', 'no results']
        page.close()

        # The desktop's column searches the same way and says which search answered.
        requests.clear()
        agents.pop()
        projects[0].update(total=1, members=1, last_message_at=agents[0]['last_message_at'])
        page = browser.new_page(viewport={'width': 1280, 'height': 860})
        page.route('**/api/**', route)
        page.goto(f'{BASE}/agents?token=t&lang=en')
        search = page.locator('.sidebar').get_by_role('searchbox', name='Search conversations')
        search.fill('vegetables outside')
        expect(page.locator('.sidebar .search-passage')).to_have_text('Grow tomatoes on the balcony')
        expect(page.locator('.sidebar .search-notice')).to_contain_text('Matching words and meaning')
        search.fill('exact')
        expect(page.locator('.sidebar .search-notice')).to_contain_text('Exact search only')
        expect(page.locator('.sidebar .search-notice a')).to_have_attribute('href', '/app/settings/components')
        assert requests == ['vegetables outside', 'exact']
        browser.close()
    print('chats sections, row commands, transition, conversation search and fallback: passed')
    return UNHANDLED.report()


if __name__ == '__main__':
    expect_app(BASE)
    sys.exit(run())
