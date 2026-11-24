"""The pages the scripted browser shows: a small shop, its cart, a sign-in form, an article.

They stand for the sites an agent visits, so the app's live view has real pixels to draw and its
cursor real elements to point at. ``browser_stub.render_scenes`` renders each one in a page of the
check's own Chromium and keeps the JPEG and the boxes of the elements named here, so an action the
stub replays lands exactly on the button the picture shows.

Invented names and ``example.com`` addresses only: nothing here looks like a real company's site.
"""

from __future__ import annotations

from urllib.parse import quote

BASE_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: "Inter", "Liberation Sans", "DejaVu Sans", Arial, sans-serif; color: #1f2328; background: #faf7f2; }
a { color: inherit; text-decoration: none; }
.top { display: flex; align-items: center; gap: 28px; height: 64px; padding: 0 48px; background: #fff; border-bottom: 1px solid #ece6db; }
.brand { display: flex; align-items: center; gap: 10px; font-weight: 800; font-size: 20px; letter-spacing: -.02em; color: #7a4a14; }
.brand i { width: 30px; height: 30px; border-radius: 8px; background: linear-gradient(135deg, #e7b75f, #b8741f); display: inline-block; }
.nav { display: flex; gap: 22px; font-size: 15px; color: #5b5145; }
.search { flex: 1; max-width: 360px; margin-left: auto; height: 38px; border: 1px solid #ddd3c4; border-radius: 19px; padding: 0 16px; font-size: 14px; color: #8b8175; display: flex; align-items: center; background: #fdfbf8; }
.cart { display: flex; align-items: center; gap: 8px; height: 38px; padding: 0 16px; border-radius: 19px; background: #1f2328; color: #fff; font-size: 14px; font-weight: 600; }
.crumbs { padding: 18px 48px 0; font-size: 13px; color: #8b8175; }
"""

WHEAT = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='%23b8741f'/>"
    "<path d='M16 7v19M16 12c-3-1-5-3-5-5 3 0 5 2 5 5zm0 0c3-1 5-3 5-5-3 0-5 2-5 5zm0 6c-3-1-5-3-5-5 3 0 5 2 5 5zm0 0c3-1 5-3 5-5-3 0-5 2-5 5z' "
    "stroke='%23fff' stroke-width='1.8' fill='none' stroke-linecap='round'/></svg>"
)
SHOP_ICON = "data:image/svg+xml," + WHEAT
KEY_ICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#2f6fe0'/>"
    "<circle cx='13' cy='14' r='5' stroke='#fff' stroke-width='2.4' fill='none'/><path d='M17 17l7 7M21 21l2-2' stroke='#fff' stroke-width='2.4' stroke-linecap='round'/></svg>"
)
DOCS_ICON = "data:image/svg+xml," + quote(
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#1e9e56'/>"
    "<path d='M10 9h12M10 14h12M10 19h8' stroke='#fff' stroke-width='2.4' stroke-linecap='round'/></svg>"
)

SHOP = f"""<!doctype html><html><head><style>{BASE_CSS}
.product {{ display: grid; grid-template-columns: 520px 1fr; gap: 56px; padding: 22px 48px 40px; }}
.photo {{ height: 460px; border-radius: 18px; background: radial-gradient(circle at 35% 30%, #fff7e6 0, #f2d9a8 38%, #d9a55a 70%, #b8741f 100%); position: relative; overflow: hidden; }}
.photo .sack {{ position: absolute; left: 150px; top: 90px; width: 220px; height: 290px; border-radius: 40px 40px 26px 26px; background: linear-gradient(180deg, #f4ecdd, #e2d3b8); box-shadow: 0 30px 50px rgba(90, 50, 10, .35); }}
.photo .sack b {{ position: absolute; left: 30px; right: 30px; top: 110px; height: 70px; border-radius: 10px; background: #7a4a14; color: #fff3dc; font-size: 22px; display: flex; align-items: center; justify-content: center; letter-spacing: .08em; }}
.photo .tag {{ position: absolute; left: 20px; top: 20px; background: #fff; border-radius: 12px; padding: 6px 12px; font-size: 13px; font-weight: 700; color: #7a4a14; }}
h1 {{ font-size: 34px; line-height: 1.15; margin: 8px 0 10px; letter-spacing: -.02em; }}
.rating {{ color: #b8741f; font-size: 15px; margin-bottom: 18px; }} .rating span {{ color: #8b8175; margin-left: 8px; }}
.price {{ font-size: 32px; font-weight: 800; margin: 6px 0 4px; }} .unit {{ color: #8b8175; font-size: 14px; }}
.opts {{ display: flex; gap: 10px; margin: 22px 0; }}
.opt {{ padding: 10px 16px; border: 1.5px solid #ddd3c4; border-radius: 12px; font-size: 14px; background: #fff; }}
.opt.on {{ border-color: #1f2328; font-weight: 700; }}
.buy {{ display: flex; gap: 12px; align-items: center; margin-top: 8px; }}
.qty {{ width: 110px; height: 52px; border: 1.5px solid #ddd3c4; border-radius: 14px; display: flex; align-items: center; justify-content: space-around; font-size: 18px; background: #fff; }}
#add {{ height: 52px; padding: 0 34px; border: 0; border-radius: 14px; background: #b8741f; color: #fff; font-size: 17px; font-weight: 700; }}
.note {{ margin-top: 26px; font-size: 15px; line-height: 1.6; color: #5b5145; max-width: 520px; }}
.ship {{ margin-top: 18px; display: flex; gap: 18px; font-size: 13px; color: #5b5145; }}
.ship div {{ padding: 10px 14px; background: #fff; border-radius: 12px; border: 1px solid #ece6db; }}
</style></head><body>
<div class="top"><div class="brand"><i></i>Northwind Mill</div><div class="nav"><a>Flour</a><a>Grains</a><a>Baking</a><a>Recipes</a></div>
<div class="search" id="search">rye flour</div><div class="cart" id="cart">Cart · 0</div></div>
<div class="crumbs">Flour › Rye › Stone-ground</div>
<div class="product"><div class="photo"><span class="tag">Fresh this week</span><div class="sack"><b>RYE · 5 KG</b></div></div>
<div><div class="unit">Northwind Mill · stone-ground</div><h1>Whole rye flour,<br>5 kg sack</h1>
<div class="rating">★★★★★<span>4.8 · 312 reviews</span></div>
<div class="price">€14.90</div><div class="unit">€2.98 per kg · VAT included</div>
<div class="opts"><div class="opt">1 kg</div><div class="opt on" id="size">5 kg</div><div class="opt">25 kg</div></div>
<div class="buy"><div class="qty">−&nbsp;&nbsp;1&nbsp;&nbsp;+</div><button id="add">Add to cart</button></div>
<div class="note">Milled on stone from rye grown within forty kilometres of the mill. Dark, sweet and a little sour — the flour for a proper sourdough loaf.</div>
<div class="ship"><div>Ships tomorrow</div><div>Free over €40</div></div></div></div>
</body></html>"""

CART = f"""<!doctype html><html><head><style>{BASE_CSS}
.wrap {{ display: grid; grid-template-columns: 1fr 380px; gap: 40px; padding: 26px 48px; }}
h1 {{ font-size: 30px; margin: 0 0 18px; letter-spacing: -.02em; }}
.line {{ display: flex; gap: 20px; align-items: center; padding: 18px; background: #fff; border-radius: 16px; border: 1px solid #ece6db; }}
.thumb {{ width: 92px; height: 92px; border-radius: 12px; background: radial-gradient(circle at 35% 30%, #fff7e6, #d9a55a 70%, #b8741f); }}
.line b {{ font-size: 17px; }} .line .sub {{ color: #8b8175; font-size: 14px; margin-top: 4px; }}
.line .p {{ margin-left: auto; font-size: 18px; font-weight: 700; }}
.sum {{ background: #fff; border-radius: 16px; border: 1px solid #ece6db; padding: 22px; font-size: 15px; }}
.sum div {{ display: flex; justify-content: space-between; margin-bottom: 12px; color: #5b5145; }}
.sum .total {{ font-size: 20px; font-weight: 800; color: #1f2328; border-top: 1px solid #ece6db; padding-top: 14px; }}
#checkout {{ width: 100%; height: 52px; border: 0; border-radius: 14px; background: #1f2328; color: #fff; font-size: 17px; font-weight: 700; margin-top: 8px; }}
</style></head><body>
<div class="top"><div class="brand"><i></i>Northwind Mill</div><div class="nav"><a>Flour</a><a>Grains</a><a>Baking</a><a>Recipes</a></div>
<div class="search">Search the mill</div><div class="cart">Cart · 1</div></div>
<div class="wrap"><div><h1>Your cart</h1><div class="line"><div class="thumb"></div><div><b>Whole rye flour, 5 kg sack</b><div class="sub">Stone-ground · qty 1</div></div><div class="p">€14.90</div></div></div>
<div class="sum"><div><span>Subtotal</span><span>€14.90</span></div><div><span>Delivery</span><span>€4.50</span></div>
<div class="total"><span>Total</span><span>€19.40</span></div><button id="checkout">Checkout</button></div></div>
</body></html>"""

SIGNIN = """<!doctype html><html><head><style>
* { box-sizing: border-box; }
body { margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center; font-family: "Inter", "Liberation Sans", "DejaVu Sans", Arial, sans-serif; background: linear-gradient(160deg, #eef3fb, #dfe8f7); color: #1f2328; }
.card { width: 420px; background: #fff; border-radius: 20px; padding: 36px; box-shadow: 0 30px 60px rgba(40, 70, 130, .18); }
.logo { width: 44px; height: 44px; border-radius: 12px; background: #2f6fe0; margin-bottom: 18px; }
h1 { font-size: 26px; margin: 0 0 6px; letter-spacing: -.02em; } p { margin: 0 0 24px; color: #5b6475; font-size: 15px; }
label { display: block; font-size: 13px; font-weight: 600; margin: 14px 0 6px; color: #3b4455; }
.field { height: 46px; border: 1.5px solid #d3dae6; border-radius: 12px; padding: 0 14px; display: flex; align-items: center; font-size: 15px; color: #1f2328; }
.field.pw { letter-spacing: .3em; color: #9aa3b2; }
#signin { width: 100%; height: 48px; margin-top: 22px; border: 0; border-radius: 12px; background: #2f6fe0; color: #fff; font-size: 16px; font-weight: 700; }
.alt { text-align: center; margin-top: 16px; font-size: 13px; color: #5b6475; }
</style></head><body><div class="card"><div class="logo"></div><h1>Sign in to Example ID</h1><p>One account for the mill's wholesale portal.</p>
<label>Email</label><div class="field" id="email">orders@bakery.example</div>
<label>Password</label><div class="field pw" id="password">••••••••</div>
<button id="signin">Sign in</button><div class="alt">Use a passkey instead</div></div></body></html>"""

ARTICLE = f"""<!doctype html><html><head><style>{BASE_CSS}
body {{ background: #fff; }}
.doc {{ max-width: 760px; margin: 0 auto; padding: 36px 24px; }}
h1 {{ font-size: 38px; letter-spacing: -.02em; margin: 0 0 12px; }}
.meta {{ color: #8b8175; font-size: 14px; margin-bottom: 26px; }}
p {{ font-size: 17px; line-height: 1.7; color: #33302b; }}
h2 {{ font-size: 24px; margin-top: 30px; }}
</style></head><body>
<div class="top"><div class="brand"><i></i>Northwind Mill</div><div class="nav"><a>Flour</a><a>Grains</a><a>Baking</a><a>Recipes</a></div><div class="search">Search the mill</div></div>
<div class="doc"><h1>A rye sourdough in three days</h1><div class="meta">Recipes · 12 minute read</div>
<p>Rye has almost no gluten to speak of, so a rye loaf is held together by its starches and the acid of a long fermentation. That is why the starter matters more here than in any wheat bread, and why the dough is a paste rather than something you knead.</p>
<h2 id="day1">Day one: the starter</h2>
<p>Mix 50 g of whole rye flour with 50 g of water at 26 °C and leave it covered. By evening it should smell of apples and have risen by half. Feed it again before bed with the same amounts.</p>
<p>A starter kept at a steady warmth is more sour; one kept cool is milder and slower. Neither is wrong.</p></div>
</body></html>"""

QR_CODE = (
    # The code of an invented sign-in link, 45 modules square with its quiet zone (segno, level M),
    # kept as drawn so the checks need no QR library.
    '<svg viewBox="0 0 45 45" class="segno"><path class="qrline" stroke="#000" d="M4 4.5h7m3 0h1m1 0h1m4 0h2m1 0h1m1 0h2m1 0h2m3 0h7m-37 1h1m5 0h1m3 0h4m1 0h3m1 0h1m1 0h1m3 0h3m2 0h1m5 0h1m-37 1h1m1 0h3m1 0h1m1 0h2m1 0h1m2 0h1m3 0h5m5 0h1m1 0h1m1 0h3m1 0h1m-37 1h1m1 0h3m1 0h1m1 0h6m1 0h1m1 0h3m2 0h6m2 0h1m1 0h3m1 0h1m-37 1h1m1 0h3m1 0h1m1 0h3m1 0h1m1 0h1m4 0h3m1 0h1m2 0h1m3 0h1m1 0h3m1 0h1m-37 1h1m5 0h1m1 0h2m1 0h1m3 0h1m2 0h1m1 0h1m1 0h1m2 0h4m1 0h1m5 0h1m-37 1h7m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h7m-29 1h2m1 0h4m1 0h1m2 0h5m2 0h3m-29 1h1m1 0h5m3 0h1m3 0h1m3 0h3m1 0h1m2 0h1m1 0h1m2 0h5m-34 1h2m5 0h4m3 0h3m5 0h2m3 0h1m2 0h1m3 0h1m-36 1h1m1 0h7m2 0h2m1 0h1m5 0h1m1 0h1m3 0h6m2 0h3m-37 1h1m2 0h1m1 0h1m1 0h2m2 0h1m2 0h1m1 0h1m4 0h1m5 0h4m5 0h1m-37 1h1m2 0h1m2 0h2m4 0h1m3 0h3m5 0h4m1 0h2m1 0h5m-36 1h1m1 0h1m1 0h1m3 0h3m5 0h1m1 0h5m4 0h2m1 0h1m3 0h1m-36 1h5m1 0h1m4 0h1m1 0h5m2 0h3m2 0h3m2 0h4m2 0h1m-37 1h2m1 0h2m3 0h1m3 0h1m1 0h1m2 0h2m2 0h3m3 0h6m3 0h1m-36 1h1m1 0h1m2 0h2m1 0h2m1 0h2m1 0h2m2 0h1m2 0h5m1 0h1m1 0h1m1 0h1m1 0h3m-37 1h1m1 0h1m1 0h1m5 0h4m1 0h1m1 0h2m1 0h1m2 0h1m5 0h1m1 0h1m3 0h1m-36 1h5m1 0h3m1 0h2m2 0h3m1 0h2m2 0h1m3 0h3m3 0h1m2 0h2m-37 1h3m2 0h1m1 0h4m2 0h3m2 0h7m1 0h3m1 0h2m1 0h1m2 0h1m-37 1h3m1 0h1m1 0h2m4 0h4m1 0h1m1 0h1m2 0h1m1 0h4m1 0h2m1 0h1m1 0h1m-35 1h6m2 0h1m3 0h1m3 0h1m1 0h6m2 0h1m1 0h1m4 0h1m-34 1h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h2m2 0h1m1 0h1m3 0h1m1 0h2m2 0h4m1 0h2m-37 1h5m4 0h1m4 0h1m4 0h1m1 0h1m5 0h1m1 0h4m2 0h2m-37 1h1m2 0h1m2 0h2m1 0h3m1 0h3m2 0h1m1 0h2m1 0h5m1 0h4m1 0h1m1 0h1m-37 1h3m1 0h2m2 0h1m4 0h1m1 0h2m4 0h1m1 0h1m4 0h1m4 0h1m-34 1h1m3 0h3m1 0h2m1 0h2m4 0h1m2 0h1m4 0h1m1 0h1m2 0h1m1 0h2m1 0h2m-37 1h1m1 0h1m7 0h1m1 0h1m3 0h1m1 0h1m2 0h2m1 0h1m1 0h1m2 0h2m2 0h1m-34 1h1m3 0h3m1 0h1m2 0h1m1 0h1m2 0h3m1 0h1m1 0h11m1 0h3m-29 1h1m1 0h3m1 0h1m2 0h1m1 0h3m1 0h1m2 0h1m1 0h1m3 0h2m1 0h1m-36 1h7m2 0h1m4 0h4m2 0h2m4 0h1m1 0h1m1 0h1m1 0h1m1 0h1m1 0h1m-37 1h1m5 0h1m1 0h2m2 0h4m1 0h3m1 0h1m1 0h1m2 0h1m1 0h1m3 0h2m1 0h2m-37 1h1m1 0h3m1 0h1m1 0h2m1 0h1m1 0h4m4 0h1m1 0h10m1 0h1m-35 1h1m1 0h3m1 0h1m1 0h2m1 0h2m1 0h2m2 0h1m1 0h2m1 0h1m2 0h1m2 0h2m1 0h1m3 0h1m-37 1h1m1 0h3m1 0h1m1 0h3m1 0h2m1 0h5m1 0h2m1 0h1m1 0h1m1 0h1m4 0h1m1 0h2m-37 1h1m5 0h1m3 0h3m2 0h1m3 0h5m4 0h1m1 0h1m1 0h1m3 0h1m-37 1h7m1 0h2m1 0h2m2 0h1m1 0h1m1 0h2m1 0h1m1 0h2m1 0h1m1 0h1m2 0h5"/></svg>'
)

QR = f"""<!doctype html><html><head><style>{BASE_CSS}
.qr-page {{ display: grid; grid-template-columns: 1fr 1fr; gap: 56px; padding: 56px 48px; }}
h1 {{ font-size: 30px; margin: 0 0 12px; letter-spacing: -.02em; }}
p {{ font-size: 15px; line-height: 1.6; color: #5b5145; max-width: 460px; }}
#qr {{ width: 45px; height: 45px; background: #fff; }}
#qr svg {{ display: block; width: 100%; height: 100%; }}
</style></head><body>
<div class="top"><div class="brand"><i></i>Example ID</div></div>
<div class="qr-page"><div><h1>Sign in with your phone</h1>
<p>Open the Example app on your phone, choose Scan, and point the camera at the code. The code renews every minute.</p></div>
<div><div id="qr">{QR_CODE}</div><p>Or sign in with a password instead.</p></div></div>
</body></html>"""

# name → (address, title, favicon, HTML, {element name: CSS selector})
SCENES: dict[str, tuple[str, str, str, str, dict[str, str]]] = {
    "shop": ("https://shop.example.com/rye-flour-5kg", "Whole rye flour, 5 kg — Northwind Mill", SHOP_ICON, SHOP, {"add": "#add", "search": "#search", "size": "#size", "cart": "#cart"}),
    "cart": ("https://shop.example.com/cart", "Your cart — Northwind Mill", SHOP_ICON, CART, {"checkout": "#checkout"}),
    "signin": ("https://accounts.example.com/signin", "Sign in — Example ID", KEY_ICON, SIGNIN, {"email": "#email", "password": "#password", "signin": "#signin"}),
    "article": ("https://shop.example.com/recipes/rye-sourdough", "A rye sourdough in three days", DOCS_ICON, ARTICLE, {"day1": "#day1"}),
    # A sign-in code drawn one CSS pixel a module, as small as some sites draw theirs: the take-over
    # view's zoom and sharp frame are checked against it (check_browser_zoom.py).
    "qr": ("https://accounts.example.com/qr", "Sign in with your phone — Example ID", KEY_ICON, QR, {"qr": "#qr"}),
}
