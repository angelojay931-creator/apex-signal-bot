def px_order(symbol, side, price, qty, qdec):
    try:
        ts = str(int(time.time() * 1000))
        query_params = {"timestamp": ts}
        sorted_query = "timestamp=" + ts
        path = "/api/v1/trade/order"
        path_url = path + "?" + sorted_query
        body = {"symbol":symbol,"side":side,"type":"LIMIT","price":str(round(price,6)),"size":str(round(qty,qdec)),"timeInForce":"GTC"}
        body_str = json.dumps(body, separators=(',', ':'))
        to_sign = "POST" + path_url + body_str
        sig = hmac.new(PX_SEC.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        headers = {"PIONEX-KEY": PX_KEY, "PIONEX-SIGNATURE": sig, "Content-Type": "application/json"}
        r = requests.post("https://api.pionex.com" + path_url, json=body, headers=headers, timeout=10)
        print("SIGN: " + to_sign[:100])
        return r.json()
    except Exception as e:
        return {"error": str(e)}
