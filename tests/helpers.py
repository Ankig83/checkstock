import uuid


def post(client, url, payload=None, key=None):
    headers = {"Idempotency-Key": key or str(uuid.uuid4())}
    if payload is None:
        return client.post(url, headers=headers)
    return client.post(url, json=payload, headers=headers)


def patch(client, url, payload, key=None):
    headers = {"Idempotency-Key": key or str(uuid.uuid4())}
    return client.patch(url, json=payload, headers=headers)


def reset(client):
    response = client.post("/api/demo/reset")
    assert response.status_code == 200, response.text
    return client.get("/api/stock").json()


def balance(stock, warehouse_id, product_id):
    for row in stock["balances"]:
        if row["warehouse_id"] == warehouse_id and row["product_id"] == product_id:
            return row
    raise AssertionError(f"нет остатка warehouse={warehouse_id} product={product_id}")


def ids(stock):
    products = {item["sku"]: item["id"] for item in stock["products"]}
    warehouses = {item["name"]: item for item in stock["warehouses"]}
    return products, warehouses


def assert_books(client):
    """Сверяет приход из истории с суммой мест, где товар числится. Резерв внутрь физического остатка не прибавляется."""

    stock = client.get("/api/stock").json()
    operations = client.get("/api/operations").json()["operations"]
    assert stock["reconciliation"]["ok"] is True
    for product in stock["products"]:
        product_id = product["id"]
        sku = product["sku"]
        on_hand = sum(row["on_hand"] for row in stock["balances"] if row["product_id"] == product_id)
        reserved = sum(row["reserved_fbs"] for row in stock["balances"] if row["product_id"] == product_id)
        transit = (
            sum(row["qty"] for row in stock["transfers"] if row["product_id"] == product_id)
            + sum(row["qty"] for row in stock["fbo_shipments"] if row["product_id"] == product_id)
            + sum(row["qty"] for row in stock["fbs_shipments"] if row["product_id"] == product_id)
        )
        market = sum(row["qty"] for row in stock["marketplace"] if row["product_id"] == product_id)
        received = sum(row["qty"] or 0 for row in operations if row["type"] == "receipt" and row["sku"] == sku)
        assert reserved <= on_hand
        assert received == on_hand + transit + market, (sku, received, on_hand, transit, market)
        for row in stock["balances"]:
            if row["product_id"] == product_id:
                assert row["available"] == row["on_hand"] - row["reserved_fbs"]
        report = next(item for item in stock["reconciliation"]["products"] if item["product_id"] == product_id)
        assert report["balanced"] is True
        assert report["received"] == received
        assert report["on_hand"] == on_hand
        assert report["in_transit"] == transit
        assert report["marketplace"] == market
