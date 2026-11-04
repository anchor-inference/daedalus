"""The notes the screenshots and the Notes checks open on: a bakery's project notes and its site chat's
own, in English or Russian, with pictures and a PDF made here so no image file lives beside them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from api_stub import preview_png
from notes_stub import NotesStub

P1 = "9f3c2a1b7d40"
S1 = "a1b2c3d4e5f6"

# A one-page PDF, enough for a file card and a download.
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj 2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj 3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

TEXT = {
    "en": {
        "brand": ("Brand & tone", "## Voice\n\nWarm, plain and a little proud of the bread. No exclamation marks on the site.\n\n![storefront.png]({storefront})\n\n- Colours: flour white, crust brown, one green for prices\n- Type: one serif for headings, the system font for the rest\n\nSee the [supplier list]({prices}) for what we may name on the menu."),
        "launch": ("Launch checklist", "- [x] Menu data file\n- [x] Photos sorted\n- [x] Prices checked with the owner\n- [ ] Opening hours on the contact page\n- [ ] Domain points at the new site"),
        "suppliers": ("Suppliers", "| Supplier | Delivers | Day |\n| --- | --- | --- |\n| Mill on the river | Flour | Mon |\n| Hill farm | Eggs, butter | Tue, Fri |\n\n```mermaid\nflowchart LR\n  Mill --> Bakery --> Shop\n```"),
        "shoot": ("Menu photo shoot", "Shot on Tuesday morning, natural light.\n\n![croissants.png]({croissants})\n\n## Keep\n\n1. Croissants on the board\n2. Rye loaf, cut\n\n## Redo\n\n- [ ] Cinnamon buns: too dark"),
        "questions": ("Open questions", "- Does the owner want online orders this season?\n- Which photo goes on the home page?\n\n> The agent will ask before changing prices."),
    },
    "ru": {
        "brand": ("Бренд и тон", "## Голос\n\nТепло, просто и с гордостью за хлеб. Без восклицательных знаков на сайте.\n\n![витрина.png]({storefront})\n\n- Цвета: мука, корочка, один зелёный для цен\n- Шрифт: антиква для заголовков, системный для остального\n\nСм. [список поставщиков]({prices}) — что можно называть в меню."),
        "launch": ("Чек-лист запуска", "- [x] Файл с меню\n- [x] Фото разобраны\n- [x] Цены сверены с владелицей\n- [ ] Часы работы на странице контактов\n- [ ] Домен смотрит на новый сайт"),
        "suppliers": ("Поставщики", "| Поставщик | Возит | День |\n| --- | --- | --- |\n| Мельница у реки | Мука | Пн |\n| Ферма на холме | Яйца, масло | Вт, Пт |\n\n```mermaid\nflowchart LR\n  Мельница --> Пекарня --> Магазин\n```"),
        "shoot": ("Съёмка меню", "Снимали во вторник утром, при дневном свете.\n\n![круассаны.png]({croissants})\n\n## Оставляем\n\n1. Круассаны на доске\n2. Ржаной, в разрезе\n\n## Переснять\n\n- [ ] Булочки с корицей: слишком тёмные"),
        "questions": ("Открытые вопросы", "- Нужны ли владелице онлайн-заказы в этом сезоне?\n- Какое фото на главную?\n\n> Агент спросит, прежде чем менять цены."),
    },
}


def _ago(**delta: float) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).isoformat(timespec="milliseconds")


def seed(notes: NotesStub, lang: str = "en") -> dict[str, Any]:
    """Fill ``notes`` with the bakery's notes; returns the ids a check reaches for."""
    notes.reset()
    words = TEXT["ru" if lang == "ru" else "en"]
    notes.project_of[S1] = {"id": P1, "name": "Bakery site" if lang != "ru" else "Сайт пекарни"}
    storefront = notes.add_file("project", P1, "storefront.png" if lang != "ru" else "витрина.png", preview_png("storefront", 960, 540), "image/png", file_id="f0storefront")
    prices = notes.add_file("project", P1, "suppliers-2026.pdf", PDF, "application/pdf", file_id="f0suppliers0")
    croissants = notes.add_file("chat", S1, "croissants.png" if lang != "ru" else "круассаны.png", preview_png("croissants", 800, 600), "image/png", file_id="f0croissant0")

    def ref(file: dict[str, Any]) -> str:
        return f"files/{file['id']}/{file['name']}".replace(" ", "%20")

    fill = {"storefront": ref(storefront), "prices": ref(prices), "croissants": ref(croissants)}
    title, body = words["suppliers"]
    suppliers = notes.seed("project", P1, title, body, updated_at=_ago(days=2), note_id="n0suppliers0")
    title, body = words["launch"]
    launch = notes.seed("project", P1, title, body, color="green", updated_at=_ago(hours=5), note_id="n0launch0000")
    title, body = words["brand"]
    brand = notes.seed("project", P1, title, body.format(**fill), color="amber", pinned=True, updated_at=_ago(days=1), note_id="n0brand00000")
    title, body = words["questions"]
    questions = notes.seed("chat", S1, title, body, by="agent", updated_at=_ago(minutes=12), note_id="n0questions0")
    title, body = words["shoot"]
    shoot = notes.seed("chat", S1, title, body.format(**fill), color="blue", updated_at=_ago(minutes=40), note_id="n0shoot00000")
    return {"brand": brand, "launch": launch, "suppliers": suppliers, "questions": questions, "shoot": shoot, "storefront": storefront, "prices": prices, "croissants": croissants}
