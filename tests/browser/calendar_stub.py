"""The calendar and planner API, answered the way the host answers it, and kept between requests.

An invented week of somebody's work and life, laid around the day the harness runs on: three local
calendars and two connected ones, a weekly meeting, a daily run, a conference across three days, three
events that overlap at lunch, a holiday from a read-only subscription, and tasks due today, overdue,
upcoming, undated and one blocked out on today's afternoon. One connected account has failed its last
sync and holds a conflict, so the screen has something to say about both.

Recurring events are expanded into occurrences here as the host does, on the wall clock of the
settings' time zone, so a weekly meeting stays at ten across a daylight-saving change. A move of
"this event" is kept as an override of that occurrence, a move of "all events" changes the series.

Events may also be structured entries: a type from the registry, typed fields, a body of blocks
(checklists, tables, steps, actions, and kinds this stub does not know, which it keeps untouched), a
status, tags and a group. The fixtures hold an agent's week meal plan as a group of meal events and two
grocery orders, and a fifty-item packing list, so every block kind and the day totals have something real.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

ZONE = "Europe/Amsterdam"
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
STATUSES = ("planned", "done", "skipped", "cancelled")
EVENT_FIELDS = ("calendar_id", "title", "description", "location", "start_at", "end_at", "all_day", "start_date", "end_date", "timezone", "recurrence", "reminders",
                "type", "fields", "blocks", "status", "tags", "group_id", "links")
"""What a create or an update may set on an event; the rest is the host's to keep."""

# A week of meals, one row a day: breakfast, lunch, snack, dinner. A dish is (Russian name, English
# name, kcal, protein in grams, ingredients as (Russian, English), steps as (Russian, English, minutes
# or None)). The days sum inside 1900–2100 kcal except the third, which falls short, and the sixth,
# which a slice of honey cake carries over, so the day totals show the target met and missed both ways.
MENU: list[list[tuple]] = [
    [
        ("Овсянка с бананом и орехами", "Oatmeal with banana and walnuts", 518, 17,
         [("Овсяные хлопья 60 г", "Rolled oats 60 g"), ("Молоко 200 мл", "Milk 200 ml"), ("Банан", "Banana"), ("Грецкие орехи 15 г", "Walnuts 15 g"), ("Мёд 1 ч. л.", "Honey 1 tsp")],
         [("Залить хлопья молоком и довести до кипения", "Bring the oats and milk to the boil", None), ("Варить на слабом огне, помешивая", "Simmer, stirring", 7), ("Добавить банан, орехи и мёд", "Top with banana, walnuts and honey", None)]),
        ("Курица с гречкой и салатом", "Chicken with buckwheat and salad", 720, 52,
         [("Куриное филе 180 г", "Chicken breast 180 g"), ("Гречка 80 г", "Buckwheat 80 g"), ("Огурец", "Cucumber"), ("Помидор", "Tomato"), ("Оливковое масло 1 ст. л.", "Olive oil 1 tbsp")],
         [("Отварить гречку", "Boil the buckwheat", 15), ("Обжарить филе по 6 минут с каждой стороны", "Fry the chicken six minutes a side", 12), ("Нарезать овощи и заправить маслом", "Chop the vegetables and dress with oil", None)]),
        ("Яблоко и миндаль", "Apple and almonds", 210, 6,
         [("Яблоко", "Apple"), ("Миндаль 20 г", "Almonds 20 g"), ("Корица", "Cinnamon")],
         [("Нарезать яблоко дольками", "Slice the apple", None), ("Посыпать корицей, подать с миндалём", "Dust with cinnamon and serve with the almonds", None), ("Убрать остатки в холодильник", "Put what is left in the fridge", None)]),
        ("Лосось с овощами на гриле", "Grilled salmon with vegetables", 580, 40,
         [("Филе лосося 160 г", "Salmon fillet 160 g"), ("Кабачок", "Courgette"), ("Болгарский перец", "Bell pepper"), ("Лимон", "Lemon"), ("Розмарин", "Rosemary")],
         [("Замариновать лосось в лимоне с розмарином", "Marinate the salmon in lemon and rosemary", 15), ("Нарезать овощи крупно", "Cut the vegetables in large pieces", None), ("Запечь всё вместе при 200 °C", "Roast everything together at 200 °C", 18), ("Сбрызнуть лимоном перед подачей", "Squeeze lemon over before serving", None)]),
    ],
    [
        ("Омлет с овощами и тостом", "Vegetable omelette with toast", 460, 28,
         [("Яйца 3 шт", "Eggs, 3"), ("Шпинат 40 г", "Spinach 40 g"), ("Помидор", "Tomato"), ("Цельнозерновой хлеб", "Wholegrain bread")],
         [("Взбить яйца со щепоткой соли", "Whisk the eggs with a pinch of salt", None), ("Обжарить шпинат и помидор", "Fry the spinach and tomato", 3), ("Залить яйцами и готовить под крышкой", "Pour in the eggs and cook covered", 5)]),
        ("Чечевичный суп с хлебом", "Lentil soup with bread", 610, 30,
         [("Красная чечевица 100 г", "Red lentils 100 g"), ("Морковь", "Carrot"), ("Лук", "Onion"), ("Томатная паста 1 ст. л.", "Tomato paste 1 tbsp"), ("Зира", "Cumin"), ("Лаваш", "Flatbread")],
         [("Обжарить лук и морковь", "Fry the onion and carrot", 5), ("Добавить чечевицу, пасту и воду", "Add the lentils, paste and water", None), ("Варить до мягкости", "Simmer until soft", 20), ("Пробить блендером", "Blend smooth", None)]),
        ("Кефир с отрубями", "Kefir with bran", 180, 10,
         [("Кефир 250 мл", "Kefir 250 ml"), ("Овсяные отруби 2 ст. л.", "Oat bran 2 tbsp"), ("Корица", "Cinnamon")],
         [("Смешать кефир с отрубями", "Stir the bran into the kefir", None), ("Дать постоять", "Let it stand", 10), ("Посыпать корицей", "Dust with cinnamon", None)]),
        ("Индейка с булгуром", "Turkey with bulgur", 660, 46,
         [("Филе индейки 170 г", "Turkey fillet 170 g"), ("Булгур 70 г", "Bulgur 70 g"), ("Лук", "Onion"), ("Петрушка", "Parsley")],
         [("Обжарить лук", "Fry the onion", 4), ("Добавить булгур и воду, тушить", "Add the bulgur and water and simmer", 15), ("Обжарить индейку ломтиками", "Fry the turkey in strips", 8), ("Посыпать петрушкой", "Scatter the parsley", None)]),
    ],
    [
        ("Творог с ягодами и мёдом", "Cottage cheese with berries and honey", 420, 32,
         [("Творог 5 % 200 г", "Cottage cheese 200 g"), ("Ягоды 80 г", "Berries 80 g"), ("Мёд 1 ч. л.", "Honey 1 tsp")],
         [("Разморозить ягоды", "Thaw the berries", 10), ("Смешать творог с мёдом", "Mix the cottage cheese with the honey", None), ("Выложить ягоды сверху", "Spoon the berries on top", None)]),
        ("Плов с говядиной", "Beef plov", 780, 38,
         [("Говядина 150 г", "Beef 150 g"), ("Рис девзира 80 г", "Devzira rice 80 g"), ("Морковь", "Carrot"), ("Лук", "Onion"), ("Чеснок", "Garlic"), ("Зира", "Cumin")],
         [("Обжарить мясо до корочки", "Brown the beef", 8), ("Добавить лук и морковь соломкой", "Add the onion and carrot in strips", 6), ("Засыпать рис, залить водой, вложить чеснок", "Add the rice, cover with water, push in the garlic", None), ("Томить под крышкой", "Steam under the lid", 30)]),
        ("Хумус с морковными палочками", "Hummus with carrot sticks", 230, 8,
         [("Хумус 80 г", "Hummus 80 g"), ("Морковь", "Carrot"), ("Сельдерей", "Celery")],
         [("Нарезать морковь и сельдерей палочками", "Cut the carrot and celery in sticks", None), ("Выложить хумус в миску", "Spoon the hummus into a bowl", None), ("Подать вместе", "Serve together", None)]),
        ("Салат с тунцом", "Tuna salad", 400, 35,
         [("Тунец в собственном соку", "Tuna in its own juice"), ("Яйцо", "Egg"), ("Листья салата", "Lettuce"), ("Огурец", "Cucumber"), ("Йогурт 2 ст. л.", "Yoghurt 2 tbsp")],
         [("Сварить яйцо вкрутую", "Hard-boil the egg", 9), ("Нарезать овощи", "Chop the vegetables", None), ("Смешать с тунцом и заправить йогуртом", "Mix with the tuna and dress with yoghurt", None)]),
    ],
    [
        ("Гречневая каша с молоком", "Buckwheat porridge with milk", 430, 15,
         [("Гречка 60 г", "Buckwheat 60 g"), ("Молоко 200 мл", "Milk 200 ml"), ("Сливочное масло 5 г", "Butter 5 g")],
         [("Промыть гречку", "Rinse the buckwheat", None), ("Сварить в воде", "Boil it in water", 15), ("Залить горячим молоком, добавить масло", "Pour over hot milk and add the butter", None)]),
        ("Лагман", "Lagman noodles", 690, 34,
         [("Говядина 120 г", "Beef 120 g"), ("Лапша для лагмана 100 г", "Lagman noodles 100 g"), ("Болгарский перец", "Bell pepper"), ("Редька", "Radish"), ("Чеснок", "Garlic"), ("Помидор", "Tomato")],
         [("Обжарить мясо", "Fry the beef", 6), ("Добавить овощи и тушить", "Add the vegetables and stew", 15), ("Отварить лапшу", "Boil the noodles", 8), ("Залить лапшу подливой", "Pour the sauce over the noodles", None)]),
        ("Протеиновый батончик", "Protein bar", 200, 20,
         [("Батончик", "Bar"), ("Вода 300 мл", "Water 300 ml"), ("Зелёный чай", "Green tea")],
         [("Заварить чай", "Brew the tea", 3), ("Съесть батончик", "Eat the bar", None), ("Запить водой", "Drink the water", None)]),
        ("Рагу с фасолью и курицей", "Bean and chicken stew", 650, 44,
         [("Куриное бедро 150 г", "Chicken thigh 150 g"), ("Фасоль 100 г", "Beans 100 g"), ("Кабачок", "Courgette"), ("Томаты в собственном соку", "Tinned tomatoes"), ("Лук", "Onion")],
         [("Обжарить курицу и лук", "Fry the chicken and onion", 8), ("Добавить овощи и томаты", "Add the vegetables and tomatoes", None), ("Тушить с фасолью", "Stew with the beans", 25)]),
    ],
    [
        ("Сырники со сметаной", "Syrniki with sour cream", 560, 30,
         [("Творог 9 % 200 г", "Cottage cheese 200 g"), ("Яйцо", "Egg"), ("Рисовая мука 2 ст. л.", "Rice flour 2 tbsp"), ("Сметана 15 % 50 г", "Sour cream 50 g")],
         [("Смешать творог, яйцо и муку", "Mix the cottage cheese, egg and flour", None), ("Сформовать шесть сырников", "Shape six patties", None), ("Обжарить на слабом огне", "Fry over a low heat", 10), ("Подать со сметаной", "Serve with the sour cream", None)]),
        ("Треска с рисом и брокколи", "Cod with rice and broccoli", 650, 45,
         [("Филе трески 200 г", "Cod fillet 200 g"), ("Рис басмати 70 г", "Basmati rice 70 g"), ("Брокколи 150 г", "Broccoli 150 g"), ("Лимон", "Lemon")],
         [("Отварить рис", "Boil the rice", 15), ("Запечь треску с лимоном", "Bake the cod with lemon", 15), ("Приготовить брокколи на пару", "Steam the broccoli", 6)]),
        ("Банан с арахисовой пастой", "Banana with peanut butter", 260, 7,
         [("Банан", "Banana"), ("Арахисовая паста 1 ст. л.", "Peanut butter 1 tbsp"), ("Щепотка соли", "A pinch of salt")],
         [("Разрезать банан вдоль", "Halve the banana lengthways", None), ("Намазать пастой", "Spread with the peanut butter", None), ("Посолить", "Salt lightly", None)]),
        ("Куриные котлеты с пюре", "Chicken cutlets with mashed potatoes", 620, 42,
         [("Куриный фарш 200 г", "Minced chicken 200 g"), ("Картофель 250 г", "Potatoes 250 g"), ("Молоко 50 мл", "Milk 50 ml"), ("Лук", "Onion"), ("Укроп", "Dill")],
         [("Отварить картофель", "Boil the potatoes", 20), ("Смешать фарш с луком и сформовать котлеты", "Mix the mince with the onion and shape the cutlets", None), ("Запечь котлеты", "Bake the cutlets", 20), ("Размять картофель с молоком", "Mash the potatoes with the milk", None)]),
    ],
    [
        ("Яйца пашот на тосте с авокадо", "Poached eggs on avocado toast", 510, 22,
         [("Яйца 2 шт", "Eggs, 2"), ("Авокадо", "Avocado"), ("Хлеб на закваске", "Sourdough bread"), ("Лимонный сок", "Lemon juice"), ("Перец чили", "Chilli flakes")],
         [("Размять авокадо с лимоном", "Mash the avocado with lemon", None), ("Сварить яйца пашот", "Poach the eggs", 4), ("Собрать тосты", "Build the toast", None)]),
        ("Бешбармак", "Beshbarmak", 820, 48,
         [("Говядина 180 г", "Beef 180 g"), ("Тесто для бешбармака 100 г", "Beshbarmak dough 100 g"), ("Лук", "Onion"), ("Бульон 300 мл", "Broth 300 ml")],
         [("Сварить мясо", "Boil the beef", 90), ("Отварить тесто в бульоне", "Cook the dough sheets in the broth", 5), ("Нарезать лук кольцами и обдать бульоном", "Slice the onion in rings and scald with broth", None), ("Выложить мясо на тесто", "Lay the beef over the dough", None)]),
        ("Кусочек медовика", "A slice of honey cake", 420, 6,
         [("Медовик 120 г", "Honey cake 120 g"), ("Чай", "Tea"), ("Лимон", "Lemon")],
         [("Достать торт заранее", "Take the cake out ahead", 20), ("Заварить чай", "Brew the tea", None), ("Есть не спеша", "Enjoy it slowly", None)]),
        ("Стейк с картофелем", "Steak with potatoes", 760, 55,
         [("Стейк рибай 200 г", "Ribeye steak 200 g"), ("Молодой картофель 200 г", "New potatoes 200 g"), ("Чеснок", "Garlic"), ("Тимьян", "Thyme"), ("Сливочное масло 10 г", "Butter 10 g")],
         [("Запечь картофель", "Roast the potatoes", 30), ("Обжарить стейк по 3 минуты с каждой стороны", "Sear the steak three minutes a side", 6), ("Дать мясу отдохнуть", "Rest the meat", 5), ("Подать с картофелем", "Serve with the potatoes", None)]),
    ],
    [
        ("Гранола с йогуртом", "Granola with yoghurt", 480, 18,
         [("Гранола 50 г", "Granola 50 g"), ("Греческий йогурт 150 г", "Greek yoghurt 150 g"), ("Киви", "Kiwi")],
         [("Выложить йогурт", "Spoon out the yoghurt", None), ("Добавить гранолу", "Add the granola", None), ("Нарезать киви сверху", "Slice the kiwi on top", None)]),
        ("Паста с индейкой", "Turkey pasta", 700, 44,
         [("Паста 80 г", "Pasta 80 g"), ("Фарш индейки 150 г", "Minced turkey 150 g"), ("Томатный соус", "Tomato sauce"), ("Пармезан 15 г", "Parmesan 15 g"), ("Базилик", "Basil")],
         [("Отварить пасту", "Boil the pasta", 10), ("Обжарить фарш", "Brown the mince", 8), ("Смешать с соусом и пастой", "Toss with the sauce and pasta", None), ("Посыпать пармезаном", "Grate the parmesan over", None)]),
        ("Греческий йогурт с чиа", "Greek yoghurt with chia", 150, 15,
         [("Греческий йогурт 150 г", "Greek yoghurt 150 g"), ("Семена чиа 1 ч. л.", "Chia seeds 1 tsp"), ("Корица", "Cinnamon")],
         [("Выложить йогурт", "Spoon out the yoghurt", None), ("Посыпать чиа", "Scatter the chia", None), ("Дать постоять", "Let it stand", 5)]),
        ("Тушёная говядина с овощами", "Braised beef with vegetables", 690, 50,
         [("Говядина 180 г", "Beef 180 g"), ("Картофель", "Potatoes"), ("Морковь", "Carrot"), ("Лук", "Onion"), ("Лавровый лист", "Bay leaf")],
         [("Обжарить говядину", "Brown the beef", 8), ("Добавить овощи и воду", "Add the vegetables and water", None), ("Тушить под крышкой", "Braise covered", 60)]),
    ],
]
TREAT = (5, "snack")
"""The one slot the menu marks as a treat rather than a snack: the sixth day's honey cake."""

# The two grocery orders: (Russian name, English name, quantity in Russian units, price in tenge). The
# first covers the menu's first three days, the second the rest.
BASKET_FIRST: list[tuple[str, str, str, int]] = [
    ("Гречка Eurasia Foods 1 кг", "Eurasia Foods buckwheat 1 kg", "1 шт", 435), ("Овсяные хлопья Ясно Солнышко 500 г", "Rolled oats 500 g", "1 шт", 690),
    ("Молоко Lactel 2,5 % 1 л", "Lactel milk 2.5% 1 l", "2 шт", 1180), ("Бананы", "Bananas", "1 кг", 890), ("Грецкие орехи 200 г", "Walnuts 200 g", "1 шт", 1650),
    ("Мёд горный 250 г", "Mountain honey 250 g", "1 шт", 1890), ("Куриное филе охлаждённое", "Chilled chicken breast", "1 кг", 2390), ("Огурцы", "Cucumbers", "1 кг", 790),
    ("Помидоры", "Tomatoes", "1 кг", 1190), ("Масло оливковое Borges 500 мл", "Borges olive oil 500 ml", "1 шт", 3490), ("Яблоки Голден", "Golden apples", "1 кг", 650),
    ("Миндаль 150 г", "Almonds 150 g", "1 шт", 1490), ("Филе лосося 300 г", "Salmon fillet 300 g", "1 шт", 5990), ("Кабачки", "Courgettes", "1 кг", 590),
    ("Перец болгарский", "Bell peppers", "500 г", 890), ("Лимоны", "Lemons", "3 шт", 540), ("Яйца С1, 10 шт", "Eggs, 10", "1 уп", 790), ("Шпинат 125 г", "Spinach 125 g", "1 шт", 990),
    ("Хлеб цельнозерновой", "Wholegrain bread", "1 шт", 480), ("Чечевица красная 800 г", "Red lentils 800 g", "1 шт", 790), ("Морковь", "Carrots", "1 кг", 290),
    ("Лук репчатый", "Onions", "1 кг", 250), ("Томатная паста Помидорка 270 г", "Tomato paste 270 g", "1 шт", 560), ("Зира 50 г", "Cumin 50 g", "1 шт", 390),
    ("Лаваш тонкий", "Thin flatbread", "2 шт", 360), ("Кефир Food Master 1 % 900 мл", "Food Master kefir 1% 900 ml", "2 шт", 1060), ("Отруби овсяные 200 г", "Oat bran 200 g", "1 шт", 420),
    ("Филе индейки", "Turkey fillet", "600 г", 2890), ("Булгур 500 г", "Bulgur 500 g", "1 шт", 720), ("Петрушка", "Parsley", "1 шт", 190),
    ("Творог Простоквашино 5 % 350 г", "Cottage cheese 5% 350 g", "1 шт", 1090), ("Ягоды замороженные 300 г", "Frozen berries 300 g", "1 шт", 1290), ("Говядина мякоть", "Beef", "1 кг", 4290),
    ("Рис девзира 1 кг", "Devzira rice 1 kg", "1 шт", 1390), ("Чеснок", "Garlic", "3 шт", 270), ("Хумус классический 200 г", "Hummus 200 g", "1 шт", 990),
    ("Сельдерей черешковый", "Celery", "1 шт", 590), ("Тунец в собственном соку 185 г", "Tuna in its own juice 185 g", "2 шт", 1580), ("Салат айсберг", "Iceberg lettuce", "1 шт", 690),
    ("Йогурт натуральный 300 г", "Plain yoghurt 300 g", "1 шт", 590), ("Розмарин свежий", "Fresh rosemary", "1 шт", 390), ("Вода Tassay 1,5 л", "Tassay water 1.5 l", "4 шт", 960),
    ("Корица молотая", "Ground cinnamon", "1 шт", 350), ("Соль морская", "Sea salt", "1 шт", 210),
]
BASKET_SECOND: list[tuple[str, str, str, int]] = [
    ("Лапша для лагмана 500 г", "Lagman noodles 500 g", "1 шт", 650), ("Редька", "Radish", "1 шт", 240), ("Фасоль красная консервированная", "Tinned red beans", "2 шт", 980),
    ("Куриные бёдра", "Chicken thighs", "1 кг", 1990), ("Томаты в собственном соку 400 г", "Tinned tomatoes 400 g", "2 шт", 1180), ("Творог 9 % 350 г", "Cottage cheese 9% 350 g", "1 шт", 1190),
    ("Мука рисовая 500 г", "Rice flour 500 g", "1 шт", 690), ("Сметана 15 % 300 г", "Sour cream 15% 300 g", "1 шт", 620), ("Филе трески 400 г", "Cod fillet 400 g", "1 шт", 3290),
    ("Рис басмати 900 г", "Basmati rice 900 g", "1 шт", 1490), ("Брокколи замороженная 400 г", "Frozen broccoli 400 g", "1 шт", 990), ("Арахисовая паста 340 г", "Peanut butter 340 g", "1 шт", 1590),
    ("Картофель", "Potatoes", "2 кг", 560), ("Куриный фарш 500 г", "Minced chicken 500 g", "1 шт", 1490), ("Авокадо", "Avocado", "2 шт", 1180),
    ("Хлеб на закваске", "Sourdough bread", "1 шт", 890), ("Тесто для бешбармака 500 г", "Beshbarmak dough 500 g", "1 шт", 790), ("Торт Медовик 500 г", "Honey cake 500 g", "1 шт", 2990),
    ("Стейк рибай", "Ribeye steak", "400 г", 6490), ("Гранола 400 г", "Granola 400 g", "1 шт", 1890), ("Киви", "Kiwi", "4 шт", 760),
]
UNITS = {" шт": " pc", " кг": " kg", " г": " g", " уп": " pack"}
"""The basket's Russian quantity units in English, so one list serves both languages."""

PACKING: list[tuple[str, str]] = [
    ("Паспорт", "Passport"), ("Билеты на самолёт", "Plane tickets"), ("Страховка", "Travel insurance"), ("Банковские карты", "Bank cards"), ("Наличные евро", "Cash in euros"),
    ("Зарядка для телефона", "Phone charger"), ("Зарядка для ноутбука", "Laptop charger"), ("Ноутбук", "Laptop"), ("Переходник для розетки", "Plug adapter"), ("Пауэрбанк", "Power bank"),
    ("Наушники", "Headphones"), ("Бейдж конференции", "Conference badge"), ("Визитки", "Business cards"), ("Слайды на флешке", "Slides on a USB stick"), ("Блокнот и ручка", "Notebook and pen"),
    ("Футболки ×4", "T-shirts ×4"), ("Рубашка для доклада", "Shirt for the talk"), ("Джинсы", "Jeans"), ("Лёгкая куртка", "Light jacket"), ("Свитер", "Jumper"),
    ("Нижнее бельё ×4", "Underwear ×4"), ("Носки ×4", "Socks ×4"), ("Пижама", "Pyjamas"), ("Кроссовки", "Trainers"), ("Удобная обувь для прогулок", "Walking shoes"),
    ("Солнцезащитные очки", "Sunglasses"), ("Зонт", "Umbrella"), ("Зубная щётка", "Toothbrush"), ("Зубная паста", "Toothpaste"), ("Дезодорант", "Deodorant"),
    ("Шампунь в дорожной упаковке", "Travel shampoo"), ("Бритва", "Razor"), ("Солнцезащитный крем", "Sunscreen"), ("Расчёска", "Comb"), ("Лекарства", "Medicines"),
    ("Пластыри", "Plasters"), ("Беруши", "Earplugs"), ("Маска для сна", "Sleep mask"), ("Бутылка для воды", "Water bottle"), ("Перекус в дорогу", "Snacks for the journey"),
    ("Книга", "A book"), ("Подушка для шеи", "Neck pillow"), ("Рюкзак", "Backpack"), ("Пакет для жидкостей", "Bag for liquids"), ("Распечатка брони отеля", "Hotel booking printout"),
    ("Адрес отеля в офлайн-картах", "Hotel address in offline maps"), ("Скачать карты Лиссабона", "Download maps of Lisbon"), ("Полить цветы перед отъездом", "Water the plants before leaving"),
    ("Вынести мусор", "Take out the rubbish"), ("Выключить бойлер", "Switch off the boiler"),
]
PACKED = {0, 1, 2, 5, 7, 15, 27}
"""The packing items already ticked, so the list opens part done."""

# What the history says about each block operation, in both languages; ``{name}`` is the item, row or block.
SUMMARIES: dict[str, tuple[str, str]] = {
    "checked": ("Отмечено: {name}", "Ticked {name}"), "unchecked": ("Снята отметка: {name}", "Unticked {name}"),
    "add_item": ("Добавлен пункт: {name}", "Added {name}"), "update_item": ("Изменён пункт: {name}", "Changed {name}"), "remove_item": ("Удалён пункт: {name}", "Removed {name}"),
    "add_row": ("Добавлена строка: {name}", "Added the row {name}"), "update_cell": ("Изменена строка: {name}", "Changed the row {name}"), "remove_row": ("Удалена строка: {name}", "Removed the row {name}"),
    "reorder": ("Изменён порядок: {name}", "Reordered {name}"), "add_block": ("Добавлен блок: {name}", "Added the block {name}"), "update_block": ("Изменён блок: {name}", "Changed the block {name}"),
    "remove_block": ("Удалён блок: {name}", "Removed the block {name}"), "reorder_blocks": ("Изменён порядок блоков", "Reordered the blocks"),
    "status": ("Статус: {name}", "Status: {name}"), "run": ("Агенту отправлено: {name}", "Sent to the agent: {name}"), "created": ("Создано: {name}", "Created: {name}"),
    "shifted": ("Перенесено на {name} дн.", "Moved by {name} days"),
}


STATUS_WORDS = {"planned": ("запланировано", "planned"), "done": ("выполнено", "done"), "skipped": ("пропущено", "skipped"), "cancelled": ("отменено", "cancelled")}


class Refusal(Exception):
    """A request the host turns down, with the status and the detail it answers."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def iso(moment: datetime) -> str:
    return moment.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def number(value: object) -> str:
    """A field as a chip shows it: 518, not 518.0."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class CalendarStub:
    """``answer(method, path, query, body)`` for every ``/api/calendar*`` and ``/api/planner*`` route."""

    def __init__(self, *, zone: str = ZONE, today: date | None = None, lang: str = "en", plan: bool = True) -> None:
        self.zone = ZoneInfo(zone)
        self.today = today or datetime.now(self.zone).date()
        self.lang = lang
        self.writes: list[tuple[str, str, dict]] = []
        """Every write the screen sent, ``(method, path, body)``, for a check to read back."""
        ru = lang == "ru"
        self.settings: dict = {"week_start": 1, "work_start": "09:00", "work_end": "18:00", "default_view": "week", "default_duration": 60, "default_reminders": [10], "timezone": zone, "show_weekends": True}

        def sync(status: str, error: str = "", minutes: int = 6, conflicts: int = 0) -> dict:
            at = None if status == "never" else iso(datetime.now(self.zone) - timedelta(minutes=minutes))
            return {"last_sync_at": at, "error": error, "status": status, "conflicts": conflicts, "next_sync_at": iso(datetime.now(self.zone) + timedelta(minutes=5)), "failures": 1 if error else 0}

        self.calendars: list[dict] = [
            {"id": "cal-personal", "name": "Personal", "color": "#4f8ff7", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 0, "default_reminders": [10], "sync": None},
            {"id": "cal-work", "name": "Работа" if ru else "Work", "color": "#14b8a6", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 1, "default_reminders": [10], "sync": None},
            {"id": "cal-family", "name": "Семья" if ru else "Family", "color": "#e3608f", "kind": "local", "account_id": None, "visible": True, "writable": True, "position": 2, "default_reminders": [60], "sync": None},
            {"id": "cal-team", "name": "Команда (Google)" if ru else "Team (Google)", "color": "#f2782f", "kind": "google", "account_id": "acc-google", "visible": True, "writable": True, "position": 3, "default_reminders": [10], "sync": sync("ok")},
            {"id": "cal-yandex", "name": "Яндекс" if ru else "Yandex", "color": "#9b6ef3", "kind": "caldav", "account_id": "acc-yandex", "visible": True, "writable": True, "position": 4, "default_reminders": [], "sync": sync("error", "the server refused the change: 412 Precondition Failed", 95, conflicts=1)},
            {"id": "cal-holidays", "name": "Праздники" if ru else "Public holidays", "color": "#34a853", "kind": "ics", "account_id": "acc-ics", "visible": True, "writable": False, "position": 5, "default_reminders": [], "sync": sync("ok", minutes=240)},
        ]
        self.accounts: list[dict] = [
            {"id": "acc-google", "provider": "google", "name": "Команда (Google)" if ru else "Team (Google)", "remote_calendar_id": "primary", "calendar_id": "cal-team", "last_sync_at": self.calendars[3]["sync"]["last_sync_at"], "sync_error": "", "status": "ok", "conflicts": 0, "failures": 0, "next_sync_at": None},
            # Yandex is stored as CalDAV with its server filled in, as the host stores its presets.
            {"id": "acc-yandex", "provider": "caldav", "name": "Яндекс" if ru else "Yandex", "remote_calendar_id": "primary", "server_url": "https://caldav.yandex.ru", "username": "someone", "calendar_id": "cal-yandex", "last_sync_at": self.calendars[4]["sync"]["last_sync_at"],
             "sync_error": "the server refused the change: 412 Precondition Failed", "status": "error", "conflicts": 1, "failures": 1, "next_sync_at": None},
            # A subscription shows only the feed's host: its address often carries a private token.
            {"id": "acc-ics", "provider": "ics", "name": "Праздники" if ru else "Public holidays", "remote_calendar_id": "primary", "host": "calendar.example.org", "calendar_id": "cal-holidays", "last_sync_at": self.calendars[5]["sync"]["last_sync_at"], "sync_error": "", "status": "ok", "conflicts": 0, "failures": 0, "next_sync_at": None},
        ]
        d = self.today

        def at(day: date, hour: int, minute: int = 0) -> str:
            return iso(datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.zone))

        def event(id_: str, calendar: str, title: str, start: str, end: str, **over: object) -> dict:
            row = {"id": id_, "calendar_id": calendar, "title": title, "description": "", "location": "", "start_at": start, "end_at": end, "all_day": False, "start_date": None, "end_date": None,
                   "timezone": zone, "color_override": None, "recurrence": "", "reminders": [10], "version": 1, "pending_sync": False, "conflict": False,
                   "type": None, "fields": {}, "blocks": [], "status": "planned", "tags": [], "group_id": None, "links": []}
            row.update(over)
            return row

        def all_day(id_: str, calendar: str, title: str, first: date, days: int, **over: object) -> dict:
            end = first + timedelta(days=days)
            return event(id_, calendar, title, f"{first}T00:00:00Z", f"{end}T00:00:00Z", all_day=True, start_date=str(first), end_date=str(end), reminders=[], **over)

        monday = d - timedelta(days=d.weekday())
        words = {
            "sync": ("Еженедельная планёрка", "Weekly team sync"), "run": ("Утренняя пробежка", "Morning run"), "conf": ("Конференция в Лиссабоне", "Conference in Lisbon"),
            "lunch": ("Обед с Анной", "Lunch with Anna"), "review": ("Ревью дизайна", "Design review"), "bank": ("Звонок в банк", "Call with the bank"),
            "dentist": ("Стоматолог", "Dentist"), "cinema": ("Кино", "Cinema"), "holiday": ("Праздничный день", "Public holiday"), "planning": ("Квартальное планирование", "Quarterly planning"),
            "yoga": ("Йога", "Yoga class"), "focus": ("Глубокая работа", "Focus time"), "dinner": ("Ужин с родителями", "Dinner with parents"), "flight": ("Перелёт в Лиссабон", "Flight to Lisbon"),
            "clinic": ("ул. Садовая, 12", "12 Garden Street"), "room": ("Переговорная «Север»", "North meeting room"),
        }
        w = {k: v[0] if ru else v[1] for k, v in words.items()}
        # A plain event whose description is Markdown: the view renders it, and it has no type or blocks.
        dentist_notes = ("**Плановый осмотр и чистка**\n\n- Взять полис ДМС\n- Не есть за два часа до приёма\n- Спросить про снимок зуба мудрости\n\n[Перенести запись](https://example.org/clinic)" if ru
                         else "**Check-up and cleaning**\n\n- Bring the insurance card\n- No food two hours before\n- Ask about the wisdom tooth X-ray\n\n[Reschedule](https://example.org/clinic)")
        self.events: list[dict] = [
            event("ev-sync", "cal-work", w["sync"], at(monday - timedelta(days=21), 10), at(monday - timedelta(days=21), 10, 45), recurrence="FREQ=WEEKLY;BYDAY=MO,TH", location=w["room"], description="Agenda in the team notes."),
            event("ev-run", "cal-personal", w["run"], at(d - timedelta(days=30), 7, 30), at(d - timedelta(days=30), 8, 0), recurrence="FREQ=DAILY", reminders=[]),
            all_day("ev-conf", "cal-work", w["conf"], d + timedelta(days=2), 3, location="Lisbon"),
            event("ev-lunch", "cal-family", w["lunch"], at(d, 13), at(d, 14)),
            event("ev-review", "cal-team", w["review"], at(d, 13, 30), at(d, 14, 30), location=w["room"]),
            event("ev-bank", "cal-personal", w["bank"], at(d, 13, 45), at(d, 14, 15)),
            event("ev-focus", "cal-work", w["focus"], at(d, 10, 0), at(d, 12, 0), pending_sync=False),
            event("ev-dentist", "cal-personal", w["dentist"], at(d + timedelta(days=1), 16), at(d + timedelta(days=1), 17, 30), location=w["clinic"], reminders=[60, 1440], description=dentist_notes),
            event("ev-cinema", "cal-family", w["cinema"], at(d - timedelta(days=1), 19), at(d - timedelta(days=1), 21, 30)),
            all_day("ev-holiday", "cal-holidays", w["holiday"], d + timedelta(days=6), 1),
            event("ev-planning", "cal-yandex", w["planning"], at(d + timedelta(days=1), 11), at(d + timedelta(days=1), 12, 30), pending_sync=True, conflict=True),
            event("ev-yoga", "cal-personal", w["yoga"], at(d - timedelta(days=2), 18, 30), at(d - timedelta(days=2), 19, 30)),
            event("ev-dinner", "cal-family", w["dinner"], at(d + timedelta(days=3), 19), at(d + timedelta(days=3), 21)),
            event("ev-flight", "cal-work", w["flight"], at(d + timedelta(days=1), 20, 15), at(d + timedelta(days=1), 23, 40)),
        ]
        self.overrides: dict[tuple[str, str], dict] = {}
        self.exdates: set[tuple[str, str]] = set()
        self.lists: list[dict] = [
            {"id": "list-inbox", "name": "Входящие" if ru else "Inbox", "color": "#7d8798", "is_default": True, "position": 0},
            {"id": "list-home", "name": "Дом" if ru else "Home", "color": "#e0a526", "is_default": False, "position": 1},
        ]
        task_words = {
            "bill": ("Оплатить счёт за электричество", "Pay the electricity bill"), "invoice": ("Отправить счёт в Acme", "Send the invoice to Acme"),
            "flights": ("Забронировать перелёт на конференцию", "Book flights for the conference"), "doc": ("Прочитать дизайн-документ", "Read the design doc"),
            "report": ("Написать квартальный отчёт", "Write the quarterly report"), "grandma": ("Позвонить бабушке", "Call grandma"),
            "photos": ("Сделать фото на паспорт", "Get passport photos"), "plants": ("Полить цветы", "Water the plants"),
        }
        tw = {k: v[0] if ru else v[1] for k, v in task_words.items()}

        def task(id_: str, title: str, **over: object) -> dict:
            row = {"id": id_, "list_id": "list-inbox", "title": title, "notes": "", "due_date": None, "due_time": None, "scheduled_start": None, "scheduled_end": None, "duration": None,
                   "priority": 0, "done_at": None, "reminders": [], "recurrence": "", "position": 0, "version": 1}
            row.update(over)
            return row

        self.tasks: list[dict] = [
            task("task-bill", tw["bill"], due_date=str(d), priority=3, list_id="list-home"),
            task("task-invoice", tw["invoice"], due_date=str(d - timedelta(days=2)), priority=2),
            task("task-flights", tw["flights"], due_date=str(d + timedelta(days=1)), priority=1),
            task("task-doc", tw["doc"], duration=45),
            task("task-report", tw["report"], due_date=str(d), scheduled_start=at(d, 15), scheduled_end=at(d, 16, 30), duration=90, priority=2),
            task("task-grandma", tw["grandma"], due_date=str(d + timedelta(days=3)), list_id="list-home"),
            task("task-photos", tw["photos"], due_date=str(d), done_at=iso(datetime.now(self.zone) - timedelta(hours=2))),
            task("task-plants", tw["plants"], list_id="list-home", duration=15),
        ]
        self.seq = 0
        planner = "agent:sess-meal-planner"
        meal_names = ["Завтрак", "Обед", "Перекус", "Ужин", "Вкусняшка"] if ru else ["Breakfast", "Lunch", "Snack", "Dinner", "Treat"]
        basket_columns = [
            {"key": "name", "label": "Товар" if ru else "Item", "type": "text"}, {"key": "qty", "label": "Кол-во" if ru else "Qty", "type": "text"},
            {"key": "price", "label": "Цена" if ru else "Price", "type": "money", "unit": "₸", "align": "right", "aggregate": "sum"},
            {"key": "got", "label": "Куплено" if ru else "Got", "type": "checkbox", "aggregate": "count"},
        ]
        # The registry: four starter types the host seeds as ordinary rows, and the one an agent made on
        # the fly while planning meals, exactly as the type definition in the spec describes it.
        self.types: list[dict] = [
            {"id": "todo-list", "name": "Список дел" if ru else "To-do list", "icon": "check", "color": "#4f8ff7", "version": 1, "owner": "user", "fields_schema": {},
             "default_blocks": [{"kind": "checklist", "title": "Пункты" if ru else "Items", "show_progress": True, "items": []}], "display": {"badge": "progress:checklist"},
             "aggregates": [], "status_flow": ["planned", "done"], "quick_actions": ["mark_done"]},
            {"id": "shopping", "name": "Покупки" if ru else "Shopping", "icon": "cart", "color": "#e0a526", "version": 1, "owner": "user",
             "fields_schema": {"store": {"type": "string"}, "total": {"type": "money", "unit": "₸"}},
             "default_blocks": [{"kind": "table", "title": "Корзина" if ru else "Basket", "columns": basket_columns, "rows": []}],
             "display": {"chip": "{store}", "subtitle": "{title}", "badge": "progress:table"},
             "aggregates": [{"field": "total", "op": "sum", "per": "day", "label": "на покупки" if ru else "on shopping"}], "status_flow": ["planned", "done", "skipped"], "quick_actions": ["mark_done", "skip"]},
            {"id": "appointment", "name": "Встреча" if ru else "Appointment", "icon": "clock", "color": "#9b6ef3", "version": 1, "owner": "user", "fields_schema": {"place": {"type": "string"}},
             "default_blocks": [{"kind": "markdown", "title": "Заметки" if ru else "Notes", "text": ""}], "display": {}, "aggregates": [], "status_flow": ["planned", "done", "cancelled"], "quick_actions": ["mark_done"]},
            {"id": "routine", "name": "Привычка" if ru else "Routine", "icon": "loop", "color": "#14b8a6", "version": 1, "owner": "user", "fields_schema": {"minutes": {"type": "integer", "unit": "мин" if ru else "min"}},
             "default_blocks": [{"kind": "steps", "title": "Шаги" if ru else "Steps", "items": []}], "display": {"badge": "progress:steps"},
             "aggregates": [{"field": "minutes", "op": "sum", "per": "day", "label": "минут в день" if ru else "minutes a day"}], "status_flow": ["planned", "done", "skipped"], "quick_actions": ["mark_done", "skip"]},
            {"id": "meal", "name": "Приём пищи" if ru else "Meal", "icon": "utensils", "color": "#2bb673", "version": 3, "owner": planner,
             "fields_schema": {"kcal": {"type": "number", "unit": "ккал" if ru else "kcal"}, "protein_g": {"type": "number", "unit": "г" if ru else "g"}, "meal": {"type": "enum", "values": meal_names}},
             "default_blocks": [{"kind": "fields", "source": "fields"}, {"kind": "checklist", "title": "Продукты" if ru else "Ingredients"}, {"kind": "steps", "title": "Рецепт" if ru else "Recipe"}],
             "display": {"chip": "{meal} · {kcal} ккал" if ru else "{meal} · {kcal} kcal", "subtitle": "{title}", "badge": "progress:checklist"},
             "aggregates": [{"field": "kcal", "op": "sum", "per": "day", "label": "ккал за день" if ru else "kcal a day", "target": [1900, 2100]}],
             "status_flow": ["planned", "done", "skipped"], "quick_actions": ["mark_done", "skip"]},
        ]
        self.groups: list[dict] = []
        self.history: dict[str, list[dict]] = {}
        """Each event's journal, oldest first; the route answers it newest first."""
        # The week's meal plan and its orders, a 50-item packing list: what the structured entries'
        # check reads. A check of the planner's own mechanics asks for the week without it, so a meal
        # at eight does not stand where it drags out a new event.
        if plan:
            self._seed_plan(d, ru, planner, meal_names, basket_columns, event, all_day, at)

    def _seed_plan(self, d, ru, planner, meal_names, basket_columns, event, all_day, at) -> None:  # type: ignore[no-untyped-def]
        """The plan's fixtures, laid around ``d``, the day the harness runs on."""
        self.groups = [
            {"id": "grp-menu", "name": "Меню на неделю" if ru else "Week meal plan", "icon": "🍽", "color": "#2bb673",
             "description": ("План питания на семь дней: **1900–2100 ккал** в день, белка не меньше 100 г.\n\n- Завтрак, обед, перекус и ужин каждый день\n- Два заказа продуктов: сегодня и через три дня\n- Один день с десертом, это нормально"
                             if ru else "Seven days of meals: **1900–2100 kcal** a day and at least 100 g of protein.\n\n- Breakfast, lunch, a snack and dinner every day\n- Two grocery orders: today and in three days\n- One day has a dessert, and that is fine"),
             "blocks": [], "created_by": planner, "archived": False, "hidden": False},
        ]
        slots = (("breakfast", 8, 0, 30), ("lunch", 13, 0, 45), ("snack", 16, 30, 15), ("dinner", 19, 0, 45))
        for index, menu in enumerate(MENU):
            day = d + timedelta(days=index)
            for kind, ((slot, hour, minute, minutes), dish) in enumerate(zip(slots, menu, strict=True)):
                name_ru, name_en, kcal, protein, ingredients, steps = dish
                today_breakfast = index == 0 and slot == "breakfast"
                begins = datetime(day.year, day.month, day.day, hour, minute, tzinfo=self.zone)
                blocks = [
                    {"id": "b-fields", "kind": "fields", "source": "fields"},
                    {"id": "b-ingredients", "kind": "checklist", "title": "Продукты" if ru else "Ingredients", "show_progress": True,
                     "items": [{"id": f"i{n + 1}", "text": pair[0] if ru else pair[1], "checked": today_breakfast and n < 3} for n, pair in enumerate(ingredients)]},
                    {"id": "b-recipe", "kind": "steps", "title": "Рецепт" if ru else "Recipe",
                     "items": [{"id": f"s{n + 1}", "text": step[0] if ru else step[1], "done": False, **({"duration_min": step[2]} if step[2] else {})} for n, step in enumerate(steps)]},
                ]
                over: dict = {}
                if slot == "lunch":
                    over["tags"] = ["питание"] if ru else ["food"]
                if index == 0 and slot == "dinner":
                    over["description"] = ("Лосось берите **охлаждённый**, не замороженный: так он не пустит воду. Маринад из [заметок](https://example.org/recipes/salmon)."
                                           if ru else "Buy the salmon **chilled**, not frozen, so it does not weep in the oven. The marinade is in [the notes](https://example.org/recipes/salmon).")
                self.events.append(event(f"meal-{index}-{slot}", "cal-personal", name_ru if ru else name_en, iso(begins), iso(begins + timedelta(minutes=minutes)), reminders=[],
                                         type="meal", group_id="grp-menu", status="done" if today_breakfast else "planned", blocks=blocks,
                                         fields={"kcal": kcal, "protein_g": protein, "meal": meal_names[4 if (index, slot) == TREAT else kind]}, **over))

        def basket(rows: list[tuple[str, str, str, int]], ticked: bool) -> list[dict]:
            out = []
            for n, (name_ru, name_en, qty, price) in enumerate(rows):
                if not ru:
                    for word, english in UNITS.items():
                        qty = qty.replace(word, english)
                # About a third ticked on the order that is out today; none on the one still to come.
                out.append({"id": f"r{n + 1}", "cells": {"name": name_ru if ru else name_en, "qty": qty, "price": price, "got": ticked and n % 3 == 0}})
            return out

        def order(id_: str, number: int, day: date, store: str, rows: list[tuple[str, str, str, int]], ticked: bool, extra: list[dict]) -> dict:
            blocks = [
                {"id": "b-basket", "kind": "table", "title": "Корзина" if ru else "Basket", "columns": copy.deepcopy(basket_columns), "rows": basket(rows, ticked)},
                {"id": "b-delivery", "kind": "fields", "items": [{"label": "Доставка+сбор" if ru else "Delivery and fee", "value": 949, "unit": "₸"}]},
                {"id": "b-open", "kind": "action", "label": "Открыть магазин" if ru else "Open the shop", "action": "url", "url": "https://example.org/shop"},
                {"id": "b-rebuild", "kind": "action", "label": "Пересобрать корзину" if ru else "Rebuild the basket", "action": "agent", "session_id": "sess-meal-planner",
                 "prompt": "Пересобери корзину без отмеченного" if ru else "Rebuild the basket without the ticked items"},
                {"id": "b-note", "kind": "markdown", "text": "Курьер оставит у двери. **Молоко**: проверить срок годности." if ru else "The courier leaves it at the door. Check the **milk**'s date."},
                *extra,
            ]
            total = sum(row[3] for row in rows) + 949
            return event(id_, "cal-personal", f"Заказ Wolt №{number}" if ru else f"Grocery order #{number}", at(day, 10), at(day, 10, 30), reminders=[30],
                         type="shopping", group_id="grp-menu", fields={"store": store, "total": total}, blocks=blocks)

        # A block of a kind this stub does not know: it has to survive every read and write untouched.
        future = {"id": "b-future", "kind": "poll", "title": "Когда удобнее доставка?" if ru else "When should it come?", "options": ["10:00", "12:00", "18:00"]}
        self.events.append(order("shop-1", 1, d, "Small Turan №11" if ru else "Small, Turan 11", BASKET_FIRST, True, [future]))
        self.events.append(order("shop-2", 2, d + timedelta(days=3), "Magnum Кабанбай" if ru else "Magnum, Kabanbay", BASKET_SECOND, False, []))
        self.events[-2]["links"] = [{"kind": "event", "ref": "shop-2", "label": "Заказ Wolt №2" if ru else "Grocery order #2"}]
        self.events.append(all_day("trip-pack", "cal-personal", "Собрать вещи в поездку" if ru else "Pack for the trip", d + timedelta(days=2), 1, type="todo-list", tags=["поездка"] if ru else ["trip"],
                                   blocks=[{"id": "b-pack", "kind": "checklist", "title": "Вещи" if ru else "Things", "show_progress": True,
                                            "items": [{"id": f"p{n + 1}", "text": pair[0] if ru else pair[1], "checked": n in PACKED} for n, pair in enumerate(PACKING)]}]))
        now = datetime.now(self.zone)
        evening = datetime.combine(d - timedelta(days=1), time(21, 40), tzinfo=self.zone)
        for id_, count in (("shop-1", len(BASKET_FIRST)), ("shop-2", len(BASKET_SECOND))):
            self._record(id_, "created", self._say("created", f"корзина, позиций: {count}" if ru else f"a basket of {count} items"), actor=planner, at=evening)
        for minutes, n in ((95, 1), (93, 4), (90, 7), (61, 10), (58, 13)):
            self._record("shop-1", "set_checked", self._say("checked", BASKET_FIRST[n - 1][0 if ru else 1]), block_id="b-basket", item_id=f"r{n}", at=now - timedelta(minutes=minutes))
        self._record("meal-0-breakfast", "created", self._say("created", meal_names[0]), actor=planner, at=evening)
        for minutes, n in ((150, 1), (149, 2), (148, 3)):
            self._record("meal-0-breakfast", "set_checked", self._say("checked", MENU[0][0][4][n - 1][0 if ru else 1]), block_id="b-ingredients", item_id=f"i{n}", at=now - timedelta(minutes=minutes))
        self._record("meal-0-breakfast", "status", self._say("status", self._status_word("done")), at=now - timedelta(minutes=120))

    # ── helpers ──

    def _id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-new{self.seq}"

    def _calendar(self, calendar_id: str) -> dict | None:
        return next((c for c in self.calendars if c["id"] == calendar_id), None)

    def _view(self, row: dict, occurrence_start: str | None = None, start: str | None = None, end: str | None = None) -> dict:
        calendar = self._calendar(row["calendar_id"]) or {"color": "#4f8ff7", "writable": True, "name": "", "account_id": None}
        recurring = bool(row["recurrence"])
        view = {k: v for k, v in row.items() if k not in ("id", "color_override")}
        view.update(
            id=f"{row['id']}:{occurrence_start}" if recurring else row["id"], event_id=row["id"], color=row["color_override"] or calendar["color"], recurring=recurring,
            occurrence_start=occurrence_start if recurring else None, writable=bool(calendar["writable"]), calendar_name=calendar["name"], account_id=calendar["account_id"], exception=False,
        )
        if start:
            view["start_at"], view["end_at"] = start, end
        if recurring and occurrence_start and (row["id"], occurrence_start) in self.overrides:
            changed = dict(self.overrides[(row["id"], occurrence_start)])
            if "color_override" in changed:
                changed["color"] = changed.pop("color_override") or view["color"]
            view.update(changed, exception=True)
        if view.get("all_day"):
            view["start_date"], view["end_date"] = view["start_at"][:10], view["end_at"][:10]
        view.update(progress=self._progress(view), chip=self._chip(view))
        return view

    def _rule(self, text: str) -> dict:
        fields = dict(part.split("=", 1) for part in text.replace("RRULE:", "").split(";") if "=" in part)
        return {"freq": fields.get("FREQ", "DAILY"), "interval": int(fields.get("INTERVAL", "1")), "byday": [b for b in fields.get("BYDAY", "").split(",") if b],
                "count": int(fields["COUNT"]) if "COUNT" in fields else None, "until": fields.get("UNTIL", "")[:8] or None, "bymonthday": int(fields["BYMONTHDAY"]) if "BYMONTHDAY" in fields else None}

    def _expand(self, row: dict, start: datetime, end: datetime) -> list[dict]:
        if not row["recurrence"]:
            if parse(row["start_at"]) < end and parse(row["end_at"]) > start:
                return [self._view(row)]
            return []
        rule = self._rule(row["recurrence"])
        first = parse(row["start_at"]).astimezone(self.zone)
        length = parse(row["end_at"]) - parse(row["start_at"])
        out: list[dict] = []
        day = first.date()
        made = 0
        until = datetime.strptime(rule["until"], "%Y%m%d").date() if rule["until"] else None
        while True:
            if until and day > until:
                break
            if parse(iso(datetime(day.year, day.month, day.day, tzinfo=self.zone))) >= end:
                break
            weeks = (day - (first.date() - timedelta(days=first.weekday()))).days // 7
            hit = False
            if rule["freq"] == "DAILY":
                hit = (day - first.date()).days % rule["interval"] == 0
            elif rule["freq"] == "WEEKLY":
                days = rule["byday"] or [BYDAY[first.weekday()]]
                hit = BYDAY[day.weekday()] in days and weeks % rule["interval"] == 0
            elif rule["freq"] == "MONTHLY":
                hit = day.day == (rule["bymonthday"] or first.day)
            elif rule["freq"] == "YEARLY":
                hit = (day.month, day.day) == (first.month, first.day)
            if hit:
                made += 1
                if rule["count"] and made > rule["count"]:
                    break
                local = datetime(day.year, day.month, day.day, first.hour, first.minute, tzinfo=self.zone)
                occurrence = iso(local)
                if (row["id"], occurrence) not in self.exdates:
                    view = self._view(row, occurrence, occurrence, iso(local + length))
                    if parse(view["start_at"]) < end and parse(view["end_at"]) > start:
                        out.append(view)
            day += timedelta(days=1)
            if (day - first.date()).days > 4000:
                break
        return out

    def occurrences(self, start: datetime, end: datetime, named: list[str] | None = None, groups: list[str] | None = None) -> list[dict]:
        shown = {c["id"] for c in self.calendars if c["visible"]} | set(named or [])
        # A hidden group's events leave the views like a hidden calendar's, unless asked for by name.
        hidden = {g["id"] for g in self.groups if g.get("hidden")} - set(groups or [])
        found = [o for row in self.events if row["calendar_id"] in shown and row.get("group_id") not in hidden for o in self._expand(row, start, end)]
        return sorted(found, key=lambda o: (o["start_at"], o["title"]))

    def _tasks(self, query: dict[str, list[str]]) -> list[dict]:
        view = query.get("view", [""])[0]
        today = str(datetime.now(self.zone).date())
        tomorrow = str(datetime.now(self.zone).date() + timedelta(days=1))
        if "start" in query and "end" in query:
            start, end = parse(query["start"][0]), parse(query["end"][0])
            first, last = start.astimezone(self.zone).date().isoformat(), (end.astimezone(self.zone) - timedelta(seconds=1)).date().isoformat()
            return [t for t in self.tasks if (t["scheduled_start"] and parse(t["scheduled_start"]) < end and parse(t["scheduled_end"]) > start) or (t["due_date"] and first <= t["due_date"] <= last)]
        if view == "inbox":
            return [t for t in self.tasks if t["list_id"] == "list-inbox" and not t["done_at"]]
        if view == "today":
            # Overdue is a view of its own; today is what is due or blocked out today.
            return [t for t in self.tasks if t["due_date"] == today or (t["scheduled_start"] and parse(t["scheduled_start"]).astimezone(self.zone).date().isoformat() == today)]
        if view == "upcoming":
            return sorted([t for t in self.tasks if t["due_date"] and t["due_date"] >= tomorrow and not t["done_at"]], key=lambda t: t["due_date"])
        if view == "overdue":
            return [t for t in self.tasks if t["due_date"] and t["due_date"] < today and not t["done_at"]]
        if view == "done":
            return [t for t in self.tasks if t["done_at"]]
        return list(self.tasks)

    def _task(self, row: dict) -> dict:
        return {**row, "done": bool(row["done_at"])}

    # ── structured entries ──

    def _say(self, what: str, name: object = "") -> str:
        words = SUMMARIES[what]
        return (words[0] if self.lang == "ru" else words[1]).format(name=name)

    def _status_word(self, status: str) -> str:
        return STATUS_WORDS.get(status, (status, status))[0 if self.lang == "ru" else 1]

    def _record(self, event_id: str, action: str, summary: str, *, actor: str = "operator", block_id: str | None = None, item_id: str | None = None, at: datetime | None = None) -> None:
        entry: dict = {"at": iso(at or datetime.now(self.zone)), "actor": actor, "action": action, "summary": summary}
        if block_id:
            entry["block_id"] = block_id
        if item_id:
            entry["item_id"] = item_id
        self.history.setdefault(event_id, []).append(entry)

    def _type(self, type_id: str | None) -> dict | None:
        return next((t for t in self.types if t["id"] == type_id), None) if type_id else None

    def _group(self, group_id: str) -> dict | None:
        return next((g for g in self.groups if g["id"] == group_id), None)

    @staticmethod
    def _checkbox_column(block: dict) -> str | None:
        return next((c["key"] for c in block.get("columns") or [] if c.get("type") == "checkbox"), None)

    def _tally(self, block: dict) -> tuple[int, int] | None:
        """How far a block is done: checklist items ticked, steps done, or a table's first checkbox
        column; ``None`` for a kind that has nothing to tick."""
        kind = block.get("kind")
        if kind in ("checklist", "steps"):
            key = "checked" if kind == "checklist" else "done"
            items = block.get("items") or []
            return sum(1 for item in items if item.get(key)), len(items)
        if kind == "table":
            column = self._checkbox_column(block)
            if column is None:
                return None
            rows = block.get("rows") or []
            return sum(1 for row in rows if (row.get("cells") or {}).get(column)), len(rows)
        return None

    def _summary(self, block: dict) -> dict:
        done, total = self._tally(block) or (None, None)
        return {"id": block.get("id"), "kind": block.get("kind"), "title": block.get("title"), "done": done, "total": total}

    def _progress(self, view: dict) -> dict | None:
        """What the chip's badge counts: the block kind the type's ``display.badge`` names, else the
        first checklist, else the first table with a checkbox column. An empty block counts as none,
        so a new event does not show 0/0."""
        badge = ((self._type(view.get("type")) or {}).get("display") or {}).get("badge") or ""
        order = ([badge.split(":", 1)[1]] if badge.startswith("progress:") else []) + ["checklist", "table"]
        for kind in order:
            for block in view.get("blocks") or []:
                tally = self._tally(block) if block.get("kind") == kind else None
                if tally and tally[1]:
                    return {"done": tally[0], "total": tally[1]}
        return None

    def _chip(self, view: dict) -> str | None:
        template = ((self._type(view.get("type")) or {}).get("display") or {}).get("chip")
        if not template:
            return None
        fields = view.get("fields") or {}

        def fill(match: re.Match) -> str:
            name = match.group(1)
            value = fields[name] if name in fields else view.get(name, "") if name == "title" else ""
            return number(value)

        return re.sub(r"\{(\w+)\}", fill, template)

    def _identify(self, blocks: list) -> list:
        """Gives a block, item or row that came without an id one, so the next patch can address it;
        everything else in a block, including a kind this stub does not know, is kept as sent."""
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block.setdefault("id", self._id("blk"))
            if block.get("kind") in ("checklist", "steps"):
                for item in block.get("items") or []:
                    item.setdefault("id", self._id("item"))
            if block.get("kind") == "table":
                for row in block.get("rows") or []:
                    row.setdefault("id", self._id("row"))
        return blocks

    def _full(self, row: dict) -> dict:
        """The one event as the single read answers it: blocks, progress and chip included."""
        return {**self._view(row), "id": row["id"]}

    def _filtered(self, found: list[dict], q: dict[str, list[str]]) -> list[dict]:
        def wanted(name: str) -> list[str]:
            return [v for v in q.get(name, [""])[0].split(",") if v]

        types, groups, tags, statuses = wanted("type"), wanted("group_id"), wanted("tag"), wanted("status")
        return [o for o in found if (not types or o.get("type") in types) and (not groups or o.get("group_id") in groups)
                and (not tags or set(tags) & set(o.get("tags") or [])) and (not statuses or o.get("status", "planned") in statuses)]

    def _listed(self, start: datetime, end: datetime, q: dict[str, list[str]]) -> list[dict]:
        named = q.get("calendars", [""])[0].split(",") if q.get("calendars") else None
        groups = [v for v in q.get("group_id", [""])[0].split(",") if v]
        return self._filtered(self.occurrences(start, end, named, groups), q)

    def _block(self, blocks: list[dict], block_id: object) -> dict:
        block = next((b for b in blocks if isinstance(b, dict) and b.get("id") == block_id), None)
        if block is None:
            raise Refusal(404, f"no block {block_id} in this event")
        return block

    @staticmethod
    def _entry(entries: list[dict], entry_id: object, what: str) -> dict:
        found = next((e for e in entries if e.get("id") == entry_id), None)
        if found is None:
            raise Refusal(404, f"no {what} {entry_id} in this block")
        return found

    @staticmethod
    def _row_name(block: dict, row: dict) -> str:
        column = next((c["key"] for c in block.get("columns") or [] if c.get("type") == "text"), None)
        return str((row.get("cells") or {}).get(column) or row.get("id")) if column else str(row.get("id"))

    def _apply(self, blocks: list[dict], op: dict) -> tuple[str, str | None, str | None]:
        """One block operation on the event's blocks, in place; answers the history's summary, block
        and item. Raises ``Refusal`` for an operation or an id the event does not have."""
        name = op.get("op")
        if name == "reorder_blocks":
            order = {block_id: n for n, block_id in enumerate(op.get("ids") or [])}
            blocks.sort(key=lambda b: order.get(b.get("id"), len(order)))
            return self._say("reorder_blocks"), None, None
        if name == "add_block":
            block = self._identify([copy.deepcopy(op.get("block") or {})])[0]
            blocks.insert(op["index"] if isinstance(op.get("index"), int) else len(blocks), block)
            return self._say("add_block", block.get("title") or block.get("kind")), block["id"], None
        block = self._block(blocks, op.get("block_id"))
        title = block.get("title") or block.get("kind")
        if name == "update_block":
            block.update({k: v for k, v in (op.get("patch") or {}).items() if k != "id"})
            return self._say("update_block", block.get("title") or block.get("kind")), block["id"], None
        if name == "remove_block":
            blocks.remove(block)
            return self._say("remove_block", title), block["id"], None
        if name == "reorder":
            key = "rows" if block.get("kind") == "table" else "items"
            order = {entry_id: n for n, entry_id in enumerate(op.get("ids") or [])}
            block[key] = sorted(block.get(key) or [], key=lambda e: order.get(e.get("id"), len(order)))
            return self._say("reorder", title), block["id"], None
        if name == "set_checked":
            checked = bool(op.get("checked"))
            if "column" in op:
                row = self._entry(block.get("rows") or [], op.get("row_id"), "row")
                row.setdefault("cells", {})[op["column"]] = checked
                return self._say("checked" if checked else "unchecked", self._row_name(block, row)), block["id"], row["id"]
            item = self._entry(block.get("items") or [], op.get("item_id"), "item")
            item["done" if block.get("kind") == "steps" else "checked"] = checked
            return self._say("checked" if checked else "unchecked", item.get("text", item["id"])), block["id"], item["id"]
        if name == "add_item":
            item = copy.deepcopy(op.get("item") or {})
            item.setdefault("id", self._id("item"))
            item.setdefault("done" if block.get("kind") == "steps" else "checked", False)
            items = block.setdefault("items", [])
            items.insert(op["index"] if isinstance(op.get("index"), int) else len(items), item)
            return self._say("add_item", item.get("text", item["id"])), block["id"], item["id"]
        if name == "update_item":
            item = self._entry(block.get("items") or [], op.get("item_id"), "item")
            item.update({k: v for k, v in (op.get("patch") or {}).items() if k != "id"})
            return self._say("update_item", item.get("text", item["id"])), block["id"], item["id"]
        if name == "remove_item":
            item = self._entry(block.get("items") or [], op.get("item_id"), "item")
            block["items"].remove(item)
            return self._say("remove_item", item.get("text", item["id"])), block["id"], item["id"]
        if name == "add_row":
            row = copy.deepcopy(op.get("row") or {})
            row.setdefault("id", self._id("row"))
            row.setdefault("cells", {})
            rows = block.setdefault("rows", [])
            rows.insert(op["index"] if isinstance(op.get("index"), int) else len(rows), row)
            return self._say("add_row", self._row_name(block, row)), block["id"], row["id"]
        if name == "update_cell":
            row = self._entry(block.get("rows") or [], op.get("row_id"), "row")
            row.setdefault("cells", {})[op.get("column")] = op.get("value")
            return self._say("update_cell", self._row_name(block, row)), block["id"], row["id"]
        if name == "remove_row":
            row = self._entry(block.get("rows") or [], op.get("row_id"), "row")
            block["rows"].remove(row)
            return self._say("remove_row", self._row_name(block, row)), block["id"], row["id"]
        raise Refusal(400, f"unknown block operation {name}")

    def _local_days(self, row: dict) -> tuple[str, str]:
        """The first and last local day an event covers, the last inclusive."""
        if row.get("all_day") and row.get("start_date"):
            return row["start_date"], str(date.fromisoformat(row["end_date"]) - timedelta(days=1))
        first = parse(row["start_at"]).astimezone(self.zone).date()
        last = (parse(row["end_at"]) - timedelta(seconds=1)).astimezone(self.zone).date()
        return str(first), str(max(first, last))

    def _group_view(self, group: dict) -> dict:
        members = [e for e in self.events if e.get("group_id") == group["id"]]
        spans = [self._local_days(e) for e in members]
        return {**group, "event_count": len(members), "progress": {"done": sum(1 for e in members if e.get("status") == "done"), "total": len(members)},
                "range": {"from": min(s[0] for s in spans), "to": max(s[1] for s in spans)} if spans else None}

    def _shift(self, row: dict, days: int) -> None:
        """Moves an event by whole days on the wall clock of the zone, so a meal at eight stays at
        eight across a daylight-saving change."""
        if row.get("all_day") and row.get("start_date"):
            first, end = date.fromisoformat(row["start_date"]) + timedelta(days=days), date.fromisoformat(row["end_date"]) + timedelta(days=days)
            row.update(start_date=str(first), end_date=str(end), start_at=f"{first}T00:00:00Z", end_at=f"{end}T00:00:00Z")
            return
        for key in ("start_at", "end_at"):
            local = parse(row[key]).astimezone(self.zone)
            row[key] = iso(datetime.combine(local.date() + timedelta(days=days), local.time(), tzinfo=self.zone))

    def _reset(self, blocks: list) -> list:
        """A copy's blocks start over: nothing ticked, no step done, no basket row bought."""
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("kind") in ("checklist", "steps"):
                for item in block.get("items") or []:
                    item["done" if block.get("kind") == "steps" else "checked"] = False
            if block.get("kind") == "table":
                for column in [c["key"] for c in block.get("columns") or [] if c.get("type") == "checkbox"]:
                    for row in block.get("rows") or []:
                        row.setdefault("cells", {})[column] = False
        return blocks

    def _aggregates(self, q: dict[str, list[str]]) -> dict:
        """Each day's totals by the types' ``aggregates``, over what the calendar shows. A skipped or
        cancelled event is left out: a meal not eaten has no calories."""
        first, last = date.fromisoformat(q["from"][0]), date.fromisoformat(q["to"][0])
        start = datetime(first.year, first.month, first.day, tzinfo=self.zone)
        end = datetime(last.year, last.month, last.day, tzinfo=self.zone)
        values: dict[tuple[str, str, int], list[float]] = {}
        for o in self._listed(start, end, q):
            kind = self._type(o.get("type"))
            if kind is None or o.get("status") in ("skipped", "cancelled"):
                continue
            day = o["start_date"] if o.get("all_day") and o.get("start_date") else str(parse(o["start_at"]).astimezone(self.zone).date())
            if not str(first) <= day < str(last):
                continue
            for n, aggregate in enumerate(kind.get("aggregates") or []):
                if aggregate.get("per", "day") != "day":
                    continue
                value = (o.get("fields") or {}).get(aggregate["field"])
                if aggregate.get("op") == "count":
                    values.setdefault((day, kind["id"], n), []).append(1)
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    values.setdefault((day, kind["id"], n), []).append(value)
        days = []
        for day in sorted({key[0] for key in values}):
            items = []
            for kind in self.types:
                for n, aggregate in enumerate(kind.get("aggregates") or []):
                    found = values.get((day, kind["id"], n))
                    if not found:
                        continue
                    op = aggregate.get("op", "sum")
                    value = {"sum": sum(found), "count": len(found), "min": min(found), "max": max(found)}.get(op, sum(found) / len(found))
                    value = int(value) if float(value).is_integer() else round(value, 1)
                    item = {"type": kind["id"], "field": aggregate["field"], "op": op, "label": aggregate.get("label", aggregate["field"]), "value": value}
                    unit = ((kind.get("fields_schema") or {}).get(aggregate["field"]) or {}).get("unit")
                    if unit:
                        item["unit"] = unit
                    if aggregate.get("target"):
                        low, high = aggregate["target"]
                        item["target"] = [low, high]
                        item["ok"] = low <= value <= high
                    items.append(item)
            days.append({"date": day, "items": items})
        return {"days": days}

    def _structured(self, method: str, path: str, q: dict[str, list[str]], body: dict) -> tuple[int, object] | None:
        """The registry of types, the day totals and the groups; ``None`` for any other path."""
        parts = path.split("/")
        if path == "/api/calendar/types":
            return 200, {"types": self.types}
        if path.startswith("/api/calendar/types/") and len(parts) == 5:
            kind = self._type(parts[4])
            if method == "PUT":
                fields = {k: v for k, v in body.items() if k not in ("id", "version")}
                if kind is None:
                    kind = {"id": parts[4], "version": 0, "owner": "user", "fields_schema": {}, "default_blocks": [], "display": {}, "aggregates": [], "status_flow": list(STATUSES[:3]), "quick_actions": []}
                    self.types.append(kind)
                kind.update(fields)
                kind["version"] += 1
                return 200, kind
            if kind is None:
                return 404, {"detail": "no such type"}
            if method == "DELETE":
                # The events stay as they were, fields and blocks included; only the type is gone.
                self.types.remove(kind)
                for row in self.events:
                    if row.get("type") == kind["id"]:
                        row["type"] = None
                return 200, {"ok": True}
            return 200, kind
        if path == "/api/calendar/aggregates":
            return 200, self._aggregates(q)
        if path == "/api/calendar/groups":
            if method == "POST":
                group = {"id": self._id("grp"), "name": "", "icon": "", "color": "#7d8798", "description": "", "blocks": [], "created_by": "user", "archived": False, "hidden": False}
                group.update({k: v for k, v in body.items() if k in ("name", "icon", "color", "description", "blocks", "created_by", "archived", "hidden")})
                self.groups.append(group)
                return 201, self._group_view(group)
            return 200, {"groups": [self._group_view(g) for g in self.groups]}
        if not path.startswith("/api/calendar/groups/"):
            return None
        group = self._group(parts[4])
        if group is None:
            return 404, {"detail": "no such group"}
        members = [e for e in self.events if e.get("group_id") == group["id"]]
        if len(parts) == 6 and parts[5] == "shift" and method == "POST":
            days = int(body.get("days") or 0)
            for row in members:
                self._shift(row, days)
                row["version"] += 1
                self._record(row["id"], "shifted", self._say("shifted", days))
            return 200, self._group_view(group)
        if len(parts) == 6 and parts[5] == "duplicate" and method == "POST":
            offset = int(body.get("offset_days") or 0)
            copied = {**copy.deepcopy(group), "id": self._id("grp"), "archived": False, "hidden": False}
            self.groups.append(copied)
            for row in members:
                twin = copy.deepcopy(row)
                twin.update(id=self._id("ev"), group_id=copied["id"], status="planned", version=1, pending_sync=False, conflict=False, blocks=self._reset(twin.get("blocks") or []))
                self._shift(twin, offset)
                self.events.append(twin)
            return 201, self._group_view(copied)
        if len(parts) != 5:
            return None
        if method == "PATCH":
            group.update({k: v for k, v in body.items() if k in ("name", "icon", "color", "description", "archived", "hidden")})
        if method == "DELETE":
            self.groups.remove(group)
            if q.get("events", ["keep"])[0] == "delete":
                self.events = [e for e in self.events if e.get("group_id") != group["id"]]
            else:
                for row in members:
                    row["group_id"] = None
            return 200, {"ok": True}
        return 200, self._group_view(group)

    # ── the routes ──

    def answer(self, method: str, path: str, query: str, body: dict | None) -> tuple[int, object] | None:
        if not (path.startswith("/api/calendar") or path.startswith("/api/planner")):
            return None
        q = parse_qs(query)
        body = body or {}
        if method != "GET":
            self.writes.append((method, path + (f"?{query}" if query else ""), copy.deepcopy(body)))
        parts = path.split("/")
        if path == "/api/calendar/settings":
            if method == "PUT":
                self.settings.update(body)
            return 200, self.settings
        if path == "/api/calendar/calendars":
            if method == "POST":
                row = {"id": self._id("cal"), "name": body.get("name", ""), "color": body.get("color", "#4f8ff7"), "kind": "local", "account_id": None, "visible": True, "writable": True, "position": len(self.calendars), "default_reminders": [10], "sync": None}
                self.calendars.append(row)
                return 201, row
            return 200, sorted(self.calendars, key=lambda c: c["position"])
        if path.startswith("/api/calendar/calendars/"):
            row = self._calendar(parts[4])
            if row is None:
                return 404, {"detail": "no such calendar"}
            if method == "PATCH":
                row.update({k: v for k, v in body.items() if k in ("name", "color", "visible", "position", "default_reminders")})
                return 200, row
            if method == "DELETE":
                if row["kind"] != "local" or sum(1 for c in self.calendars if c["kind"] == "local") <= 1:
                    return 409, {"detail": "the last local calendar cannot be deleted"}
                self.calendars.remove(row)
                self.events = [e for e in self.events if e["calendar_id"] != row["id"]]
                return 200, {"ok": True}
        if path == "/api/calendar/events":
            if method == "GET":
                found = self._listed(parse(q["start"][0]), parse(q["end"][0]), q)
                if q.get("include_blocks", ["false"])[0].lower() not in ("1", "true", "yes"):
                    # The light list: what a chip and a badge need, without the bodies of the blocks.
                    for o in found:
                        o["block_summary"] = [self._summary(b) for b in o.pop("blocks", None) or [] if isinstance(b, dict)]
                return 200, found
            if method == "POST":
                row = {"id": self._id("ev"), "description": "", "location": "", "all_day": False, "start_date": None, "end_date": None, "timezone": self.settings["timezone"], "color_override": body.get("color"),
                       "recurrence": "", "reminders": [], "version": 1, "pending_sync": False, "conflict": None, "type": None, "fields": {}, "blocks": [], "status": "planned", "tags": [], "group_id": None, "links": []}
                row.update(copy.deepcopy({k: v for k, v in body.items() if k in EVENT_FIELDS}))
                kind = self._type(row["type"])
                if kind and "blocks" not in body:
                    # A new event of a type starts from the type's template, as the host fills it.
                    row["blocks"] = copy.deepcopy(kind.get("default_blocks") or [])
                self._identify(row["blocks"])
                self.events.append(row)
                self._record(row["id"], "created", self._say("created", row.get("title", "")))
                return 201, self._view(row)
        if path == "/api/calendar/search":
            words = q.get("q", [""])[0].lower()
            now = datetime.now(self.zone)
            window = self.occurrences(now - timedelta(days=60), now + timedelta(days=120))
            seen: dict[str, dict] = {}
            for o in window:
                if words not in f"{o['title']} {o['description']} {o['location']}".lower():
                    continue
                # One result per series: the occurrence nearest to now.
                best = seen.get(o["event_id"])
                if best is None or abs(parse(o["start_at"]) - now) < abs(parse(best["start_at"]) - now):
                    seen[o["event_id"]] = o
            found = sorted(seen.values(), key=lambda o: abs(parse(o["start_at"]) - now))
            return 200, found[: int(q.get("limit", ["20"])[0])]
        if path.startswith("/api/calendar/events/"):
            event_id = parts[4]
            if ":" in event_id:
                # An occurrence's own id stands for its series and its start.
                event_id, started = event_id.split(":", 1)
                body.setdefault("occurrence_start", started)
                q.setdefault("occurrence_start", [started])
            row = next((e for e in self.events if e["id"] == event_id), None)
            if row is None:
                return 404, {"detail": "no such event"}
            if len(parts) == 6 and parts[5] == "resolve":
                row["conflict"] = False
                row["pending_sync"] = False
                calendar = self._calendar(row["calendar_id"]) or {}
                for account in self.accounts:
                    if account["id"] == calendar.get("account_id"):
                        account.update(conflicts=0, status="ok", sync_error="", failures=0, last_sync_at=iso(datetime.now(self.zone)))
                if calendar.get("sync"):
                    calendar["sync"] = {**calendar["sync"], "last_sync_at": iso(datetime.now(self.zone)), "error": "", "status": "ok", "conflicts": 0, "failures": 0}
                return 200, self._view(row)
            if len(parts) == 6 and parts[5] == "blocks" and method == "PATCH":
                ops = [op for op in body.get("ops") or [] if isinstance(op, dict)]
                # A tick is last write wins and needs no version; anything that changes the shape does.
                if body.get("version") is not None and body["version"] != row["version"] and any(op.get("op") != "set_checked" for op in ops):
                    return 409, {"detail": "The event changed since it was read"}
                blocks = copy.deepcopy(row["blocks"])
                done: list[tuple[str, str, str | None, str | None]] = []
                try:
                    for op in ops:
                        done.append((str(op.get("op")), *self._apply(blocks, op)))
                except Refusal as refused:
                    # Nothing of a refused patch is kept, not even the operations before the one refused.
                    return refused.status, {"detail": refused.detail}
                row["blocks"] = blocks
                row["version"] += 1
                for action, summary, block_id, item_id in done:
                    self._record(row["id"], action, summary, block_id=block_id, item_id=item_id)
                return 200, self._full(row)
            if len(parts) == 6 and parts[5] == "status" and method == "POST":
                if body.get("status") not in STATUSES:
                    return 400, {"detail": f"the status is one of {', '.join(STATUSES)}"}
                row["status"] = body["status"]
                row["version"] += 1
                self._record(row["id"], "status", self._say("status", self._status_word(row["status"])))
                return 200, self._full(row)
            if len(parts) == 6 and parts[5] == "history":
                # Reversed before the sort: the operations of one patch share a second, and the last of them is the newest.
                return 200, {"entries": sorted(reversed(self.history.get(row["id"], [])), key=lambda e: e["at"], reverse=True)}
            if len(parts) == 8 and parts[5] == "blocks" and parts[7] == "run" and method == "POST":
                block = next((b for b in row["blocks"] if isinstance(b, dict) and b.get("id") == parts[6]), None)
                if block is None:
                    return 404, {"detail": f"no block {parts[6]} in this event"}
                if block.get("action") != "agent" or not block.get("session_id"):
                    return 400, {"detail": "this block does not ask an agent"}
                self._record(row["id"], "run", self._say("run", block.get("prompt", "")), block_id=block["id"])
                return 200, {"ok": True, "session_id": block["session_id"]}
            if method == "GET":
                return 200, self._full(row)
            if method == "PUT":
                if body.get("version") != row["version"]:
                    return 409, {"detail": "the event changed since it was opened"}
                fields = copy.deepcopy({k: v for k, v in body.items() if k in EVENT_FIELDS})
                if isinstance(fields.get("blocks"), list):
                    self._identify(fields["blocks"])
                if "color" in body:
                    fields["color_override"] = body["color"]
                occurrence = body.get("occurrence_start")
                if body.get("scope") == "this" and row["recurrence"] and occurrence:
                    key = (row["id"], iso(parse(occurrence)))
                    self.overrides[key] = {**self.overrides.get(key, {}), **{k: v for k, v in fields.items() if k != "recurrence"}}
                else:
                    if row["recurrence"] and occurrence and "start_at" in fields and "end_at" in fields and not row["all_day"]:
                        # Sent as one occurrence's new times: the series moves by as much, with the new length.
                        shift = parse(fields["start_at"]) - parse(occurrence)
                        length = parse(fields["end_at"]) - parse(fields["start_at"])
                        begins = parse(row["start_at"]) + shift
                        fields["start_at"], fields["end_at"] = iso(begins), iso(begins + length)
                    row.update(fields)
                row["version"] += 1
                return 200, self._view(row)
            if method == "DELETE":
                if q.get("scope", ["all"])[0] == "this" and row["recurrence"]:
                    self.exdates.add((row["id"], iso(parse(q["occurrence_start"][0]))))
                else:
                    self.events.remove(row)
                return 200, {"ok": True}
        if path == "/api/calendar/accounts":
            if method == "POST":
                provider = body.get("provider", "caldav")
                credentials = dict(body.get("credentials") or {})
                presets = {"yandex": "https://caldav.yandex.ru", "icloud": "https://caldav.icloud.com"}
                if provider in presets:
                    provider, credentials["server_url"] = "caldav", presets[provider]
                if provider not in ("google", "outlook", "caldav") or (provider == "caldav" and not all(credentials.get(k) for k in ("server_url", "username", "password"))):
                    return 400, {"detail": "missing calendar credentials"}
                calendar_id = self._id("cal")
                row = {"id": self._id("acc"), "provider": provider, "name": body.get("name", ""), "remote_calendar_id": body.get("remote_calendar_id", "primary"), "server_url": credentials.get("server_url", ""), "username": credentials.get("username", ""),
                       "calendar_id": calendar_id, "last_sync_at": None, "sync_error": "", "status": "never", "conflicts": 0, "failures": 0, "next_sync_at": None}
                self.accounts.append(row)
                self.calendars.append({"id": calendar_id, "name": row["name"], "color": "#7d8798", "kind": "caldav", "account_id": row["id"], "visible": True, "writable": True, "position": len(self.calendars), "default_reminders": [], "sync": {"last_sync_at": None, "error": "", "status": "never"}})
                return 201, row
            return 200, self.accounts
        if path == "/api/calendar/subscriptions" and method == "POST":
            calendar_id = self._id("cal")
            host = body.get("url", "").split("://", 1)[-1].split("/", 1)[0]
            row = {"id": self._id("acc"), "provider": "ics", "name": body.get("name", ""), "remote_calendar_id": "primary", "host": host, "calendar_id": calendar_id, "last_sync_at": None, "sync_error": "", "status": "never", "conflicts": 0, "failures": 0, "next_sync_at": None}
            self.accounts.append(row)
            self.calendars.append({"id": calendar_id, "name": row["name"], "color": body.get("color", "#34a853"), "kind": "ics", "account_id": row["id"], "visible": True, "writable": False, "position": len(self.calendars), "default_reminders": [], "sync": {"last_sync_at": None, "error": "", "status": "never"}})
            return 201, row
        if path == "/api/calendar/oauth/start" and method == "POST":
            # Somewhere the browser can go without leaving the harness: the app itself.
            return 200, {"url": "/app/calendar?linked=1"}
        if path.startswith("/api/calendar/accounts/"):
            account = next((a for a in self.accounts if a["id"] == parts[4]), None)
            if account is None:
                return 404, {"detail": "no such account"}
            if len(parts) == 6 and parts[5] == "sync":
                if account.get("conflicts"):
                    return 502, {"detail": "calendar synchronization failed: an event changed in both places"}
                account.update(status="ok", sync_error="", last_sync_at=iso(datetime.now(self.zone)))
                return 200, account
            if method == "DELETE":
                self.accounts.remove(account)
                gone = {c["id"] for c in self.calendars if c["account_id"] == account["id"]}
                self.calendars = [c for c in self.calendars if c["id"] not in gone]
                self.events = [e for e in self.events if e["calendar_id"] not in gone]
                return 200, {"ok": True}
        structured = self._structured(method, path, q, body)
        if structured is not None:
            return structured
        if path == "/api/planner/lists":
            if method == "POST":
                row = {"id": self._id("list"), "name": body.get("name", ""), "color": body.get("color", "#7d8798"), "is_default": False, "position": len(self.lists)}
                self.lists.append(row)
                return 201, row
            return 200, self.lists
        if path == "/api/planner/tasks":
            if method == "POST":
                row = {"id": self._id("task"), "list_id": "list-inbox", "title": "", "notes": "", "due_date": None, "due_time": None, "scheduled_start": None, "scheduled_end": None, "duration": None,
                       "priority": 0, "done_at": None, "reminders": [], "recurrence": "", "position": len(self.tasks), "version": 1}
                row.update({k: v for k, v in body.items() if k in row and k not in ("id", "version", "done_at")})
                row["list_id"] = row["list_id"] or "list-inbox"
                self.tasks.append(row)
                return 201, self._task(row)
            return 200, [self._task(t) for t in self._tasks(q)]
        if path.startswith("/api/planner/tasks/"):
            row = next((t for t in self.tasks if t["id"] == parts[4]), None)
            if row is None:
                return 404, {"detail": "no such task"}
            if len(parts) == 6 and parts[5] == "complete":
                row["done_at"] = iso(datetime.now(self.zone)) if body.get("done", True) else None
                row["version"] += 1
                return 200, self._task(row)
            if method == "PUT":
                if body.get("version") != row["version"]:
                    return 409, {"detail": "the task changed since it was opened"}
                row.update({k: v for k, v in body.items() if k in row and k not in ("id", "version", "done_at")})
                row["version"] += 1
                return 200, self._task(row)
            if method == "DELETE":
                self.tasks.remove(row)
                return 200, {"ok": True}
        return 404, {"detail": f"the calendar stub has no {method} {path}"}

    def fulfil(self, route) -> bool:  # type: ignore[no-untyped-def]
        """Answers a Playwright route when it is one of these; ``False`` otherwise."""
        request = route.request
        url = request.url
        path = url.split("?", 1)[0]
        path = path[path.index("/api/"):] if "/api/" in path else path
        query = url.split("?", 1)[1] if "?" in url else ""
        body = None
        if request.method in ("POST", "PUT", "PATCH") and request.post_data:
            try:
                body = json.loads(request.post_data)
            except ValueError:
                body = None
        found = self.answer(request.method, path, query, body)
        if found is None:
            return False
        status, payload = found
        route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        return True
