from concurrent.futures import ThreadPoolExecutor

from app.db import SessionLocal
from app.errors import InventoryError
from app.inventory import reserve_fbs
from tests.helpers import assert_books, balance, ids, patch, post, reset


def test_demo_seed_matches_the_brief(client):
    stock = reset(client)
    assert stock["store"]["name"] == "Магазин"
    warehouses = {row["name"]: row for row in stock["warehouses"]}
    assert set(warehouses) == {"Север", "Юг"}
    products = {row["sku"]: row["id"] for row in stock["products"]}
    expected = {
        ("SKU-101", "Север"): 10,
        ("SKU-101", "Юг"): 6,
        ("SKU-102", "Север"): 5,
        ("SKU-102", "Юг"): 0,
        ("SKU-103", "Север"): 0,
        ("SKU-103", "Юг"): 4,
    }
    for (sku, name), qty in expected.items():
        row = balance(stock, warehouses[name]["id"], products[sku])
        assert row["on_hand"] == qty
        assert row["reserved_fbs"] == 0
        assert row["available"] == qty
    assert_books(client)


def test_fbs_reserve_release_and_ship(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    sku = products["SKU-101"]

    reserved = post(client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": sku, "qty": 4})
    assert reserved.status_code == 200, reserved.text
    reservation_id = reserved.json()["reservation_id"]
    stock = client.get("/api/stock").json()
    row = balance(stock, north, sku)
    assert row == {"warehouse_id": north, "product_id": sku, "on_hand": 10, "reserved_fbs": 4, "available": 6}

    cancelled = post(client, f"/api/fbs/reservations/{reservation_id}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["available"] == 10
    assert client.get("/api/stock").json()["reservations"] == []

    reserved = post(client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": sku, "qty": 10})
    reservation_id = reserved.json()["reservation_id"]
    shipped = post(client, f"/api/fbs/reservations/{reservation_id}/ship")
    assert shipped.status_code == 200, shipped.text
    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["on_hand"] == 0
    assert balance(stock, north, sku)["reserved_fbs"] == 0
    assert sum(item["qty"] for item in stock["fbs_shipments"]) == 10

    again = post(client, f"/api/fbs/reservations/{reservation_id}/cancel")
    assert again.status_code == 409
    assert again.json()["code"] == "reservation_closed"
    assert_books(client)


def test_transfer_between_ff_and_second_receive_is_rejected(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    south = warehouses["Юг"]["id"]
    sku = products["SKU-101"]

    sent = post(
        client,
        "/api/transfers",
        {"from_warehouse_id": north, "to_warehouse_id": south, "product_id": sku, "qty": 4},
    )
    assert sent.status_code == 200, sent.text
    shipment_id = sent.json()["shipment_id"]
    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["on_hand"] == 6
    assert balance(stock, south, sku)["on_hand"] == 6
    assert stock["transfers"][0]["qty"] == 4

    first = post(client, f"/api/transfers/{shipment_id}/receive", key="receive-once")
    replay = post(client, f"/api/transfers/{shipment_id}/receive", key="receive-once")
    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    assert first.json()["idempotent_replay"] is False
    assert replay.json()["idempotent_replay"] is True
    stock = client.get("/api/stock").json()
    assert balance(stock, south, sku)["on_hand"] == 10
    assert stock["transfers"] == []

    second = post(client, f"/api/transfers/{shipment_id}/receive", key="receive-twice")
    assert second.status_code == 409
    assert second.json()["code"] == "already_received"
    stock = client.get("/api/stock").json()
    assert balance(stock, south, sku)["on_hand"] == 10
    assert_books(client)


def test_fbo_stays_in_transit_until_marketplace_accepts(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    sku = products["SKU-102"]

    sent = post(client, "/api/fbo/shipments", {"warehouse_id": north, "product_id": sku, "qty": 2})
    assert sent.status_code == 200, sent.text
    shipment_id = sent.json()["shipment_id"]
    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["on_hand"] == 3
    assert stock["fbo_shipments"][0]["qty"] == 2
    assert all(item["qty"] == 0 for item in stock["marketplace"])

    accepted = post(client, f"/api/fbo/shipments/{shipment_id}/receive")
    assert accepted.status_code == 200, accepted.text
    stock = client.get("/api/stock").json()
    assert stock["fbo_shipments"] == []
    assert sum(item["qty"] for item in stock["marketplace"] if item["product_id"] == sku) == 2
    assert_books(client)


def test_transfer_cannot_take_reserved_units(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    south = warehouses["Юг"]["id"]
    sku = products["SKU-101"]
    post(client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": sku, "qty": 8})

    denied = post(
        client,
        "/api/transfers",
        {"from_warehouse_id": north, "to_warehouse_id": south, "product_id": sku, "qty": 3},
    )
    assert denied.status_code == 409
    assert denied.json()["code"] == "insufficient_at_warehouse"
    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["on_hand"] == 10
    assert balance(stock, north, sku)["reserved_fbs"] == 8
    assert stock["transfers"] == []

    allowed = post(
        client,
        "/api/transfers",
        {"from_warehouse_id": north, "to_warehouse_id": south, "product_id": sku, "qty": 2},
    )
    assert allowed.status_code == 200, allowed.text
    stock = client.get("/api/stock").json()
    row = balance(stock, north, sku)
    assert row["on_hand"] == 8
    assert row["reserved_fbs"] == 8
    assert row["available"] == 0
    assert_books(client)


def test_shortage_on_one_ff_when_total_is_enough(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    sku = products["SKU-101"]
    post(client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": sku, "qty": 8})

    denied = post(client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": sku, "qty": 5})
    assert denied.status_code == 409
    body = denied.json()
    assert body["code"] == "insufficient_at_warehouse"
    assert body["details"]["available_here"] == 2
    assert body["details"]["requested"] == 5
    elsewhere = {item["name"]: item["available"] for item in body["details"]["available_elsewhere"]}
    assert elsewhere == {"Юг": 6}
    assert "ничего не списал" in body["message"]

    stock = client.get("/api/stock").json()
    assert balance(stock, north, sku)["on_hand"] == 10
    assert balance(stock, north, sku)["reserved_fbs"] == 8
    assert balance(stock, warehouses["Юг"]["id"], sku)["on_hand"] == 6
    assert_books(client)


def test_shortage_when_total_is_not_enough(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    denied = post(
        client,
        "/api/fbo/shipments",
        {"warehouse_id": warehouses["Север"]["id"], "product_id": products["SKU-102"], "qty": 6},
    )
    assert denied.status_code == 409
    body = denied.json()
    assert body["code"] == "insufficient_at_warehouse"
    assert body["details"]["available_here"] == 5
    assert body["details"]["available_elsewhere"] == []
    stock = client.get("/api/stock").json()
    assert balance(stock, warehouses["Север"]["id"], products["SKU-102"])["on_hand"] == 5
    assert_books(client)


def test_replay_does_not_double_post_and_foreign_body_is_rejected(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    payload = {"warehouse_id": warehouses["Юг"]["id"], "product_id": products["SKU-102"], "qty": 1}
    before = len(client.get("/api/operations").json()["operations"])

    first = post(client, "/api/receipts", payload, key="same-receipt")
    second = post(client, "/api/receipts", payload, key="same-receipt")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["idempotent_replay"] is True
    assert first.json()["operation_id"] == second.json()["operation_id"]
    stock = client.get("/api/stock").json()
    assert balance(stock, warehouses["Юг"]["id"], products["SKU-102"])["on_hand"] == 1
    assert len(client.get("/api/operations").json()["operations"]) == before + 1

    conflict = post(client, "/api/receipts", {**payload, "qty": 2}, key="same-receipt")
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    stock = client.get("/api/stock").json()
    assert balance(stock, warehouses["Юг"]["id"], products["SKU-102"])["on_hand"] == 1
    assert_books(client)


def test_archive_preserves_balance_and_history(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    south = warehouses["Юг"]["id"]
    sku = products["SKU-103"]
    assert balance(stock, south, sku)["on_hand"] == 4

    archived = post(client, f"/api/warehouses/{south}/archive")
    assert archived.status_code == 200, archived.text
    stock = client.get("/api/stock").json()
    south_row = next(row for row in stock["warehouses"] if row["id"] == south)
    assert south_row["status"] == "archived"
    assert balance(stock, south, sku)["on_hand"] == 4
    history = client.get("/api/operations").json()["operations"]
    assert any(row["type"] == "receipt" and row["sku"] == "SKU-103" and "Юг" in row["destination"] for row in history)
    assert any(row["type"] == "warehouse_archive" and "Юг" in row["origin"] for row in history)

    denied = post(client, "/api/receipts", {"warehouse_id": south, "product_id": sku, "qty": 1})
    assert denied.status_code == 409
    assert denied.json()["code"] == "warehouse_archived"
    stock = client.get("/api/stock").json()
    assert balance(stock, south, sku)["on_hand"] == 4
    assert_books(client)


def test_archived_warehouse_keeps_reservation_closable(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    south = warehouses["Юг"]["id"]
    sku = products["SKU-101"]
    reserved = post(client, "/api/fbs/reservations", {"warehouse_id": south, "product_id": sku, "qty": 2})
    reservation_id = reserved.json()["reservation_id"]
    assert post(client, f"/api/warehouses/{south}/archive").status_code == 200

    stock = client.get("/api/stock").json()
    assert balance(stock, south, sku)["on_hand"] == 6
    assert balance(stock, south, sku)["reserved_fbs"] == 2

    shipped = post(client, f"/api/fbs/reservations/{reservation_id}/ship")
    assert shipped.status_code == 200, shipped.text
    stock = client.get("/api/stock").json()
    assert balance(stock, south, sku)["on_hand"] == 4
    assert balance(stock, south, sku)["reserved_fbs"] == 0
    assert sum(item["qty"] for item in stock["fbs_shipments"] if item["product_id"] == sku) == 2
    assert_books(client)


def test_archived_name_can_be_reused(client):
    stock = reset(client)
    _, warehouses = ids(stock)
    south = warehouses["Юг"]["id"]
    assert post(client, f"/api/warehouses/{south}/archive").status_code == 200
    created = post(client, "/api/warehouses", {"name": "Юг"})
    assert created.status_code == 200, created.text
    stock = client.get("/api/stock").json()
    souths = [row for row in stock["warehouses"] if row["name"] == "Юг"]
    assert len(souths) == 2
    assert {row["status"] for row in souths} == {"active", "archived"}
    renamed = patch(client, f"/api/warehouses/{created.json()['warehouse_id']}", {"name": "Южный"})
    assert renamed.status_code == 200, renamed.text
    stock = client.get("/api/stock").json()
    assert any(row["name"] == "Южный" and row["status"] == "active" for row in stock["warehouses"])
    assert_books(client)


def test_rejections_do_not_move_stock(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    missing_key = client.post(
        "/api/receipts",
        json={"warehouse_id": warehouses["Север"]["id"], "product_id": products["SKU-101"], "qty": 1},
    )
    assert missing_key.status_code == 400
    assert missing_key.json()["code"] == "idempotency_key_required"

    zero = post(
        client,
        "/api/receipts",
        {"warehouse_id": warehouses["Север"]["id"], "product_id": products["SKU-101"], "qty": 0},
    )
    assert zero.status_code == 422

    same = post(
        client,
        "/api/transfers",
        {
            "from_warehouse_id": warehouses["Север"]["id"],
            "to_warehouse_id": warehouses["Север"]["id"],
            "product_id": products["SKU-101"],
            "qty": 1,
        },
    )
    assert same.status_code == 409
    assert same.json()["code"] == "same_warehouse"

    missing = post(client, "/api/receipts", {"warehouse_id": warehouses["Север"]["id"], "product_id": 99999, "qty": 1})
    assert missing.status_code == 404
    assert_books(client)


def test_mixed_scenario_keeps_the_books_balanced(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    south = warehouses["Юг"]["id"]
    organizer = products["SKU-101"]
    container = products["SKU-102"]

    reservation_id = post(
        client, "/api/fbs/reservations", {"warehouse_id": north, "product_id": organizer, "qty": 4}
    ).json()["reservation_id"]
    shipment_id = post(
        client,
        "/api/transfers",
        {"from_warehouse_id": north, "to_warehouse_id": south, "product_id": organizer, "qty": 3},
    ).json()["shipment_id"]
    assert post(client, f"/api/transfers/{shipment_id}/receive").status_code == 200
    fbo_id = post(client, "/api/fbo/shipments", {"warehouse_id": south, "product_id": organizer, "qty": 2}).json()[
        "shipment_id"
    ]
    assert post(client, f"/api/fbo/shipments/{fbo_id}/receive").status_code == 200
    assert post(client, f"/api/fbs/reservations/{reservation_id}/ship").status_code == 200
    assert post(client, "/api/receipts", {"warehouse_id": south, "product_id": container, "qty": 1}).status_code == 200

    stock = client.get("/api/stock").json()
    assert balance(stock, north, organizer) == {
        "warehouse_id": north,
        "product_id": organizer,
        "on_hand": 3,
        "reserved_fbs": 0,
        "available": 3,
    }
    assert balance(stock, south, organizer)["on_hand"] == 7
    assert sum(item["qty"] for item in stock["fbs_shipments"] if item["product_id"] == organizer) == 4
    assert sum(item["qty"] for item in stock["marketplace"] if item["product_id"] == organizer) == 2
    assert balance(stock, south, container)["on_hand"] == 1
    assert_books(client)


def test_concurrent_reserves_cannot_oversell(client):
    stock = reset(client)
    products, warehouses = ids(stock)
    north = warehouses["Север"]["id"]
    sku = products["SKU-102"]

    def once(key):
        with SessionLocal() as session:
            try:
                reserve_fbs(session, key, {"warehouse_id": north, "product_id": sku, "qty": 5})
                return "ok"
            except InventoryError as error:
                return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(once, ["parallel-a", "parallel-b"]))

    assert sorted(results) == ["insufficient_at_warehouse", "ok"]
    stock = client.get("/api/stock").json()
    row = balance(stock, north, sku)
    assert row["on_hand"] == 5
    assert row["reserved_fbs"] == 5
    assert row["available"] == 0
    assert_books(client)
