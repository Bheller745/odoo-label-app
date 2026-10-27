"""920D Custom — Product Label Printing App

Search a product, preview a label (color bar for its product group, name,
description, photo, UPC barcode) at one of two physical sizes, and print it
via the browser's normal print dialog to a Primera LX500 (a standard
Windows-driver color label printer -- no ZPL/raw protocol needed, the
browser just needs a page sized exactly to the label).

Required environment variables:
    ODOO_URL       e.g. https://the920d-custom.odoo.com
    ODOO_DB        e.g. the920d-custom
    ODOO_LOGIN     e.g. bheller@920dcustom.com
    ODOO_API_KEY   generated via Settings -> Users -> profile -> Account
                   Security -> New API Key (RPC scope)
"""
import base64
import io
import os
import xmlrpc.client

import barcode
from barcode.writer import ImageWriter
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

ODOO_URL = os.environ["ODOO_URL"]
ODOO_DB = os.environ["ODOO_DB"]
ODOO_LOGIN = os.environ["ODOO_LOGIN"]
ODOO_API_KEY = os.environ["ODOO_API_KEY"]

_common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
_models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
_uid = None


def odoo(model, method, args, kwargs=None):
    global _uid
    if _uid is None:
        _uid = _common.authenticate(ODOO_DB, ODOO_LOGIN, ODOO_API_KEY, {})
        if not _uid:
            raise RuntimeError("Odoo authentication failed -- check ODOO_LOGIN / ODOO_API_KEY")
    return _models.execute_kw(ODOO_DB, _uid, ODOO_API_KEY, model, method, args, kwargs or {})


# Distinct color per real product group, assigned fresh (not a port of
# Airtable's old per-category sticker background images -- see plan).
# Accounting-scaffolding categories and anything uncategorized fall back to
# the neutral default below. Easy to tweak -- just edit the hex values.
CATEGORY_COLORS = {
    "72 Deluxe Telecaster® Loaded Pickguards": "#E63946",
    "72 Thinline® Loaded Pickguards": "#F4A261",
    "Assemblies": "#2A9D8F",
    "Bass Loaded Pickguards": "#264653",
    "Combo Kits": "#8338EC",
    "Components": "#D4A017",
    "Control Plates": "#6A4C93",
    "DIY Kits": "#06D6A0",
    "HSS Loaded Pickguards": "#EF476F",
    "Jazz Bass Loaded Pickguards": "#118AB2",
    "Offset Loaded Pickguards": "#FF6B35",
    "Parts": "#5C677D",
    "Pickups": "#073B4C",
    "Pre-Wired Pickguards": "#E76F51",
    "Rogue® Lap Steel Pickguards": "#43AA8B",
    "Strat Wired Pickguards": "#577590",
    "Stratocaster® Loaded Pickguards": "#C1121F",
    "Telecaster® Loaded Pickguards": "#F77F00",
    "Wiring Harnesses": "#3A86FF",
}
DEFAULT_COLOR = "#6B7280"  # MISC, uncategorized, and accounting scaffolding (Goods/Expenses/Services/Deliveries)

LABEL_SIZES = {
    "small": {"label": "Small", "width_in": 3.0, "height_in": 2.5},
    "large": {"label": "Large", "width_in": 4.0, "height_in": 2.0},
}

DESCRIPTION_MAX_CHARS = 160


def truncate(text, max_chars=DESCRIPTION_MAX_CHARS):
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


@app.route("/")
def index():
    return render_template("index.html", label_sizes=LABEL_SIZES)


@app.route("/api/products/search")
def search_products():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    domain = ["|", "|", ("name", "ilike", q), ("default_code", "ilike", q), ("barcode", "=", q)]
    products = odoo("product.template", "search_read", [domain],
                     {"fields": ["id", "name", "default_code"], "limit": 20})
    return jsonify(products)


@app.route("/api/products/<int:product_id>/label-data")
def label_data(product_id):
    recs = odoo("product.template", "read", [[product_id]],
                {"fields": ["name", "default_code", "description_sale", "categ_id", "barcode", "image_1920"]})
    if not recs:
        return jsonify({"error": "not found"}), 404
    p = recs[0]
    category_name = p["categ_id"][1] if p["categ_id"] else None
    return jsonify({
        "id": p["id"],
        "name": p["name"],
        "sku": p["default_code"] or "",
        "description": truncate(p["description_sale"]),
        "category": category_name,
        "color": CATEGORY_COLORS.get(category_name, DEFAULT_COLOR),
        "barcode": p["barcode"] or None,
        "has_photo": bool(p["image_1920"]),
    })


def variant_to_template_ids(variant_ids):
    """sale.order.line / mrp.production hold product.product (variant) ids;
    our label endpoints key off product.template ids -- resolve the lot in
    one call rather than one lookup per line."""
    if not variant_ids:
        return {}
    variants = odoo("product.product", "read", [variant_ids], {"fields": ["product_tmpl_id", "default_code"]})
    return {v["id"]: v for v in variants}


@app.route("/api/orders/search")
def search_orders():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    orders = odoo("sale.order", "search_read", [[("name", "ilike", q)]],
                  {"fields": ["id", "name", "partner_id", "state", "date_order"], "limit": 20})
    for o_ in orders:
        o_["partner_name"] = o_["partner_id"][1] if o_["partner_id"] else None
        del o_["partner_id"]
    return jsonify(orders)


@app.route("/api/orders/<int:order_id>/lines")
def order_lines(order_id):
    orders = odoo("sale.order", "read", [[order_id]], {"fields": ["name", "order_line"]})
    if not orders:
        return jsonify({"error": "not found"}), 404
    order = orders[0]

    lines = odoo("sale.order.line", "read", [order["order_line"]],
                 {"fields": ["product_id", "product_uom_qty", "name"]})
    lines = [l for l in lines if l["product_id"]]  # skip section/note lines, which have no product
    variants = variant_to_template_ids([l["product_id"][0] for l in lines])

    items = []
    for l in lines:
        variant = variants.get(l["product_id"][0])
        if not variant:
            continue
        items.append({
            "product_tmpl_id": variant["product_tmpl_id"][0],
            "sku": variant["default_code"] or "",
            "name": l["product_id"][1],
            "qty": l["product_uom_qty"],
        })
    return jsonify({"order_name": order["name"], "items": items})


@app.route("/api/mo/lookup")
def mo_lookup():
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"error": "code is required"}), 400

    mos = odoo("mrp.production", "search_read", [[("name", "=", code)]],
               {"fields": ["name", "product_id", "product_qty", "state"], "limit": 1})
    if not mos:
        mos = odoo("mrp.production", "search_read", [[("name", "ilike", code)]],
                   {"fields": ["name", "product_id", "product_qty", "state"], "limit": 1})
    if not mos:
        return jsonify({"error": f"No Manufacturing Order found matching '{code}'"}), 404

    mo = mos[0]
    variant = variant_to_template_ids([mo["product_id"][0]]).get(mo["product_id"][0])
    if not variant:
        return jsonify({"error": "MO's product could not be resolved"}), 404

    return jsonify({
        "mo_name": mo["name"],
        "mo_state": mo["state"],
        "mo_qty": mo["product_qty"],
        "product_tmpl_id": variant["product_tmpl_id"][0],
        "sku": variant["default_code"] or "",
        "name": mo["product_id"][1],
    })


@app.route("/api/products/<int:product_id>/photo")
def photo(product_id):
    recs = odoo("product.template", "read", [[product_id]], {"fields": ["image_1920"]})
    if not recs or not recs[0]["image_1920"]:
        return Response(status=404)
    data = base64.b64decode(recs[0]["image_1920"])
    return Response(data, mimetype="image/png")


@app.route("/api/products/<int:product_id>/barcode.png")
def barcode_png(product_id):
    recs = odoo("product.template", "read", [[product_id]], {"fields": ["barcode"]})
    value = recs[0]["barcode"] if recs else None
    if not value:
        return Response(status=404)

    value = value.strip()
    barcode_type = "upca" if len(value) == 12 else "ean13" if len(value) == 13 else None
    if not barcode_type:
        return Response(status=404)

    try:
        writer = ImageWriter()
        writer.set_options({"write_text": False, "quiet_zone": 1.5})
        code = barcode.get(barcode_type, value, writer=writer)
        buf = io.BytesIO()
        code.write(buf)
        return Response(buf.getvalue(), mimetype="image/png")
    except Exception:
        return Response(status=404)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.environ.get("PORT", 5000)))
