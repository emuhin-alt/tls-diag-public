"""Read-only loader: T-Invest API -> Apps Script web app (Google Sheet).

Only read methods are whitelisted. Nothing but counts is printed:
the repository is public and Actions logs are public.
"""
import json, os, ssl, sys, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

API = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
READ_ONLY = {
    "UsersService/GetAccounts",
    "OperationsService/GetPortfolio",
    "OperationsService/GetOperationsByCursor",
    "InstrumentsService/GetBondCoupons",
}

TOKEN = os.environ["TBANK_TOKEN"].strip()
TARGET = os.environ["APPS_SCRIPT_URL"].strip()
SECRET = os.environ["SYNC_SECRET"].strip()
CA_FILE = os.environ["TBANK_CA_FILE"]

ctx = ssl.create_default_context(cafile=CA_FILE)
ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT  # the Russian CA chain lacks some X.509 extensions


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def api(method, body, tries=4):
    if method not in READ_ONLY:
        raise RuntimeError("method is not whitelisted: " + method)
    req = urllib.request.Request(
        API + method, data=json.dumps(body).encode(), method="POST",
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(int(e.headers.get("x-ratelimit-reset", 0) or 0) + 2 ** attempt * 2)
                continue
            raise RuntimeError(f"{method}: HTTP {e.code}") from None  # never echo response/headers
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"{method}: network error {type(e).__name__}") from None


def num(q):
    return int((q or {}).get("units", 0) or 0) + (q or {}).get("nano", 0) / 1e9


def key(method, body, cursor=""):
    return "|".join([method, body.get("instrumentId", ""), body.get("accountId", ""), cursor or ""])


def main():
    print("start", flush=True)
    now = datetime.now(timezone.utc)
    out, bonds = {}, {}
    m = "UsersService/GetAccounts"
    accounts = api(m, {})
    out[key(m, {})] = accounts
    open_accounts = [a for a in accounts.get("accounts", [])
                     if a.get("status") == "ACCOUNT_STATUS_OPEN"
                     and a.get("type") in ("ACCOUNT_TYPE_TINKOFF", "ACCOUNT_TYPE_TINKOFF_IIS")]
    if not open_accounts:
        raise RuntimeError("no open brokerage/IIS accounts returned")
    start = iso(datetime(2018, 1, 1, tzinfo=timezone.utc))
    end = iso(now + timedelta(days=1))
    n_ops = 0
    for a in open_accounts:
        body = {"accountId": a["id"], "currency": "RUB"}
        p = api("OperationsService/GetPortfolio", body)
        out[key("OperationsService/GetPortfolio", body)] = p
        for pos in p.get("positions", []):
            if str(pos.get("instrumentType", "")).lower() == "bond" and num(pos.get("quantity")) > 0 and pos.get("instrumentUid"):
                bonds[pos["instrumentUid"]] = True
        cursor = ""
        for _ in range(100):
            q = {"accountId": a["id"], "from": start, "to": end, "limit": 500, "state": "OPERATION_STATE_EXECUTED"}
            if cursor:
                q["cursor"] = cursor
            batch = api("OperationsService/GetOperationsByCursor", q)
            out[key("OperationsService/GetOperationsByCursor", q, cursor)] = batch
            n_ops += len(batch.get("items", []))
            if not batch.get("hasNext"):
                break
            nxt = batch.get("nextCursor")
            if not nxt or nxt == cursor:
                raise RuntimeError("bad operations cursor")
            cursor = nxt
        else:
            raise RuntimeError("operations history page limit exceeded")
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for uid in bonds:
        b = {"instrumentId": uid, "from": iso(day0), "to": iso(day0.replace(year=day0.year + 1))}
        out[key("InstrumentsService/GetBondCoupons", b)] = api("InstrumentsService/GetBondCoupons", b)
    print(f"fetched: accounts={len(open_accounts)} operations={n_ops} bonds={len(bonds)} responses={len(out)}")

    payload = json.dumps({"secret": SECRET, "generatedAt": iso(now), "responses": out}).encode()
    req = urllib.request.Request(TARGET, data=payload, method="POST", headers={"Content-Type": "text/plain"})
    with urllib.request.urlopen(req, timeout=300) as r:   # Google's public CA, default trust store
        res = json.loads(r.read())
    if not res.get("ok"):
        raise RuntimeError("sheet rejected the data: " + str(res.get("error", ""))[:200])
    print("sheet updated, responses applied:", res.get("responses"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        msg = str(e)[:300]
        print("FAILED:", msg)
        print(f"::error title=sync failed::{msg} (secret_len={len(SECRET)}, token_len={len(TOKEN)})")
        sys.exit(1)
