#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = ["pythaiidcard>=0.3.0", "websockets>=14"]
# ///
"""thaiid-agent — read a Thai national ID card from a browser page.

A web page cannot touch a PC/SC reader on its own: WebUSB blocks smart-card
interface class 0x0B, and navigator.smartCard needs an Isolated Web App and is
ChromeOS-only. So this owns the reader and answers on a loopback WebSocket.

It holds no credentials and writes nothing to disk. It reads a card and returns
JSON; whatever page you point at it decides what to do with that.

    thaiid-agent             # then open http://127.0.0.1:8765
    thaiid-agent --dev       # also accept file:// pages (Origin: null)
    thaiid-agent --selftest  # no hardware, no network

The built-in page is served from this same port, so it is same-origin with the
WebSocket and Chrome's Local Network Access permission never applies. A page you
host elsewhere must be named in THAIID_ORIGINS and will need that permission —
and cannot be a cross-origin iframe at all, because embedders do not delegate
local-network-access (gristlabs/grist-core#2550).

Prerequisites by platform. PC/SC is built into macOS and Windows; only Linux
needs anything installed:

  macOS    nothing — plug the reader in.
  Windows  nothing — the "Smart Card" service (SCardSvr) ships with Windows.
  Linux    sudo apt install pcscd libpcsclite1 libpcsclite-dev
           sudo systemctl enable --now pcscd

Running from source needs those dev headers on Linux, because pyscard publishes
wheels for macOS and Windows only. The released binaries need nothing beyond
the OS prerequisites above.

Protocol, JSON both ways:
  -> {"cmd":"read","photo":true,"laser":true,"nhso":true}
                                  <- {"ok":true,"card":{...,"photo":"data:...","laser_id":"...",
                                       "nhso":{...}}}
     laser and nhso are off by default. Each sits on its own applet and can be
     absent on a card, so a failure comes back as laser_error / nhso_error and
     the rest of the scan still succeeds. NHSO is health data (PDPA s.26).
  -> {"cmd":"status"}             <- {"ok":true,"readers":[{"index":0,"name":"...","card":true}]}
  <- {"event":"card","present":true}   pushed on insert/removal
"""

import argparse
import asyncio
import base64
import json
import os
import sys

HOST = os.environ.get("THAIID_HOST", "127.0.0.1")  # loopback only, never 0.0.0.0
PORT = int(os.environ.get("THAIID_PORT", "8765"))
POLL_SECONDS = 1.0


def allowed_origins(dev: bool = False, port: int = PORT) -> list[str]:
    """Origin allowlist. Without it, ANY page you visit could quietly connect and
    read a card left sitting in the reader.

    The page this agent serves itself is same-origin, so it is allowed by
    default. Anything else — your own hosted page, a Grist widget — must be
    named in THAIID_ORIGINS."""
    origins = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    origins += [o for o in os.environ.get("THAIID_ORIGINS", "").split(",") if o]
    if dev:
        origins.append("null")  # file:// pages
    return origins


def ui_html() -> bytes | None:
    """The reader page, served from this same port.

    That is the whole trick for standalone use: a page on http://127.0.0.1
    talking to ws://127.0.0.1 is local-to-local, so Chrome's Local Network
    Access permission never comes into it. Only a page served from somewhere
    else has to ask."""
    here = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "reader.html"), "rb") as f:
            return f.read()
    except OSError:
        return None


def card_payload(card, photo: bool) -> dict:
    """ThaiIDCard -> the JSON the widget consumes. Photo stays in memory as a data URL."""
    a = card.address_info
    n = card.thai_name
    out = {
        "cid": card.cid,
        "thai_fullname": card.thai_fullname,
        "english_fullname": card.english_fullname,
        "first_name": n.first_name,
        "last_name": n.last_name,
        "date_of_birth": card.date_of_birth.isoformat(),
        "age": card.age,
        "gender": "ชาย" if card.gender == "1" else "หญิง",
        "address": card.address,
        "subdistrict": a.subdistrict,
        "district": a.district,
        "province": a.province,
        "issue_date": card.issue_date.isoformat(),
        "expire_date": card.expire_date.isoformat(),
        "is_expired": card.is_expired,
    }
    if photo and card.photo:
        out["photo"] = "data:image/jpeg;base64," + base64.b64encode(card.photo).decode()
    return out


# --- hardware, all blocking; callers push it to a thread -----------------------

def nhso_payload(n) -> dict:
    """สปสช. entitlement. This is health data — PDPA s.26 sensitive — so it is
    only read when asked for, and the caller is expected to say why."""
    return {
        "main_inscl": n.main_inscl,
        "sub_inscl": n.sub_inscl,
        "main_hospital": n.main_hospital_name,
        "sub_hospital": n.sub_hospital_name,
        "paid_type": n.paid_type,
        "issue_date": n.issue_date.isoformat(),
        "expire_date": n.expire_date.isoformat(),
        "update_date": n.update_date.isoformat(),
        "change_hospital_amount": n.change_hospital_amount,
        "is_expired": n.is_expired,
    }


def _optional(fn):
    """The extras sit on their own applets and are absent or unreadable on some
    cards — an unregistered card raises on NHSO's strict date parsing. Report
    the failure rather than losing an otherwise good scan."""
    try:
        return fn(), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _read_card(photo: bool, laser: bool = False, nhso: bool = False) -> dict:
    from pythaiidcard import ThaiIDCardReader

    reader = ThaiIDCardReader()
    with reader.card_session():
        out = card_payload(reader.read_card(include_photo=photo), photo)
        # Order matters: read_nhso_data selects the NHSO applet, read_laser_id
        # selects the card applet back again. Personal data is already read.
        if nhso:
            out["nhso"], out["nhso_error"] = _optional(lambda: nhso_payload(reader.read_nhso_data()))
        if laser:
            out["laser_id"], out["laser_error"] = _optional(reader.read_laser_id)
        return out


def _reader_status() -> list[dict]:
    from pythaiidcard import ThaiIDCardReader
    from pythaiidcard.exceptions import NoReaderFoundError

    try:
        return [
            {"index": r.index, "name": r.name, "card": r.connected}
            for r in ThaiIDCardReader.list_readers()
        ]
    except NoReaderFoundError:
        return []


async def dispatch(msg: dict, read=None, status=None) -> dict:
    """One request -> one response. Injectable so the selftest needs no reader."""
    read, status = read or _read_card, status or _reader_status
    cmd = msg.get("cmd")
    if cmd == "status":
        return {"ok": True, "readers": await asyncio.to_thread(status)}
    if cmd == "read":
        try:
            card = await asyncio.to_thread(
                read,
                bool(msg.get("photo", True)),
                bool(msg.get("laser", False)),
                bool(msg.get("nhso", False)),
            )
            return {"ok": True, "card": card}
        except Exception as e:
            return {"ok": False, "error": type(e).__name__, "detail": str(e)}
    return {"ok": False, "error": "UnknownCommand", "detail": str(cmd)}


# --- server -------------------------------------------------------------------

CLIENTS: set = set()
READ_LOCK = asyncio.Lock()


async def handler(ws):
    CLIENTS.add(ws)
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send(json.dumps({"ok": False, "error": "BadJSON"}))
                continue
            # one reader, one card at a time
            async with READ_LOCK:
                reply = await dispatch(msg)
            await ws.send(json.dumps(reply, ensure_ascii=False))
    finally:
        CLIENTS.discard(ws)


async def presence_loop():
    """Push card insert/removal so the widget can scan the moment a card goes in."""
    was = None
    while True:
        # ponytail: polls by connecting for the ATR; fine at 1s, swap for
        # SCardGetStatusChange if this ever needs to be instant or battery-cheap.
        try:
            present = any(r["card"] for r in await asyncio.to_thread(_reader_status))
        except Exception:
            present = False
        if present != was and CLIENTS:
            note = json.dumps({"event": "card", "present": present})
            await asyncio.gather(*(c.send(note) for c in list(CLIENTS)), return_exceptions=True)
        was = present
        await asyncio.sleep(POLL_SECONDS)


def http_or_ws(connection, request):
    """One port does both jobs.

    A plain browser GET gets the reader page. A WebSocket handshake is logged
    (a page connects from the origin that SERVES it, so an allowlist mismatch is
    the likeliest failure and this prints the exact string to allow) and then
    handed on — enforcement stays with the origins= allowlist, and returning
    None is what lets it decide."""
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    if (request.headers.get("Upgrade") or "").lower() == "websocket":
        print(f"  handshake Origin: {request.headers.get('Origin')!r}", flush=True)
        return None

    if request.path.split("?")[0] not in ("/", "/index.html", "/reader.html"):
        return connection.respond(404, "not found\n")

    body = ui_html()
    if body is None:
        return connection.respond(500, "reader.html is missing next to the agent\n")
    return Response(
        200,
        "OK",
        Headers(
            {
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": str(len(body)),
                # It reads ID cards; do not let anything cache or embed it.
                "Cache-Control": "no-store",
                "X-Frame-Options": "DENY",
            }
        ),
        body,
    )


async def serve(dev: bool) -> None:
    from websockets.asyncio.server import serve as ws_serve

    origins = allowed_origins(dev)
    print(f"thaiid-agent  open http://{HOST}:{PORT}  (ws on the same port)", flush=True)
    print(f"  origins allowed: {origins}", flush=True)
    async with ws_serve(handler, HOST, PORT, origins=origins, process_request=http_or_ws):
        await presence_loop()


def selftest() -> None:
    class N:
        first_name, last_name = "สมหญิง", "ใจดี"

    class A:
        subdistrict, district, province = "ในเมือง", "เมือง", "ขอนแก่น"
        address = "99 หมู่ที่3 ตำบลในเมือง อำเภอเมือง จังหวัดขอนแก่น"

    import datetime

    class C:
        cid = "1103700123456"
        thai_fullname, english_fullname = "นางสาวสมหญิง ใจดี", "Miss Somying Jaidee"
        thai_name, address_info = N(), A()
        date_of_birth = datetime.date(1992, 3, 4)
        issue_date, expire_date = datetime.date(2020, 1, 1), datetime.date(2028, 1, 1)
        age, gender, is_expired = 34, "2", False
        address = A.address
        photo = b"\xff\xd8jpegbytes"

    p = card_payload(C(), photo=True)
    assert p["cid"] == "1103700123456"
    assert p["gender"] == "หญิง"
    assert p["province"] == "ขอนแก่น"
    assert p["photo"].startswith("data:image/jpeg;base64,")
    assert base64.b64decode(p["photo"].split(",")[1]) == C.photo
    assert "photo" not in card_payload(C(), photo=False)

    # the allowlist is the only thing standing between a random tab and your ID card
    assert allowed_origins(port=9) == ["http://127.0.0.1:9", "http://localhost:9"]
    assert "null" not in allowed_origins()
    assert "null" in allowed_origins(dev=True)
    os.environ["THAIID_ORIGINS"] = "https://example.test"
    assert "https://example.test" in allowed_origins()
    del os.environ["THAIID_ORIGINS"]

    # an unregistered card raises inside the NHSO date parser; the scan survives
    ok, err = _optional(lambda: 1 / 0)
    assert ok is None and err.startswith("ZeroDivisionError"), (ok, err)
    assert _optional(lambda: "JT1234567890") == ("JT1234567890", None)

    import datetime as _dt

    class _N:
        main_inscl, sub_inscl = "UCS", "อ"
        main_hospital_name, sub_hospital_name = "รพ.ตัวอย่าง", ""
        paid_type, change_hospital_amount = "03", "0"
        issue_date = update_date = _dt.date(2020, 1, 1)
        expire_date = _dt.date(2030, 1, 1)
        is_expired = False

    n = nhso_payload(_N())
    assert n["main_hospital"] == "รพ.ตัวอย่าง"
    assert n["expire_date"] == "2030-01-01"   # ISO out, BE conversion already done

    fake_card = lambda photo, laser=False, nhso=False: {
        "cid": "x", "photo": photo, "laser": laser, "nhso": nhso}
    fake_status = lambda: [{"index": 0, "name": "ACS", "card": True}]

    async def checks():
        r = await dispatch({"cmd": "read", "photo": False}, fake_card, fake_status)
        assert r["card"] == {"cid": "x", "photo": False, "laser": False, "nhso": False}, r
        # the extras are opt-in and must be passed through
        r = await dispatch({"cmd": "read", "laser": True, "nhso": True}, fake_card, fake_status)
        assert r["card"]["laser"] is True and r["card"]["nhso"] is True, r
        r = await dispatch({"cmd": "status"}, fake_card, fake_status)
        assert r["readers"][0]["card"] is True
        r = await dispatch({"cmd": "nope"}, fake_card, fake_status)
        assert r["ok"] is False and r["error"] == "UnknownCommand"

        class NoCardDetectedError(Exception):
            pass

        def boom(photo, laser=False, nhso=False):
            raise NoCardDetectedError("no card")

        r = await dispatch({"cmd": "read"}, boom, fake_status)
        assert r["ok"] is False and r["error"] == "NoCardDetectedError", r

    asyncio.run(checks())

    try:
        import websockets  # noqa: F401
    except ImportError:
        print("selftest ok (logic only — install websockets to also test the socket)")
        return
    asyncio.run(_socket_checks())
    print("selftest ok (incl. socket + origin allowlist)")


async def _socket_checks() -> None:
    """Round-trip over a real socket, and prove a random tab cannot connect."""
    import websockets
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve as ws_serve

    global _read_card, _reader_status
    _read_card = lambda photo, laser=False, nhso=False: {"cid": "1103700123456", "photo": photo}
    _reader_status = lambda: [{"index": 0, "name": "FakeReader", "card": True}]
    good = "https://cards.example.test"

    async with ws_serve(handler, "127.0.0.1", 0, origins=[good],
                        process_request=http_or_ws) as server:
        port = server.sockets[0].getsockname()[1]
        url = f"ws://127.0.0.1:{port}"

        # The same port must also hand out the page — that is what makes the
        # standalone case same-origin, and free of the permission prompt.
        import urllib.error
        import urllib.request

        def fetch(path):
            # In a thread: urlopen is blocking, and the server answering it is
            # on this very event loop, so calling it inline deadlocks.
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                    return r.status, r.headers.get("Content-Type", ""), r.read()
            except urllib.error.HTTPError as e:
                return e.code, "", b""

        status, ctype, page = await asyncio.to_thread(fetch, "/")
        assert status == 200 and b"<html" in page.lower(), status
        assert ctype.startswith("text/html"), ctype
        status, _, _ = await asyncio.to_thread(fetch, "/nope")
        assert status == 404, status

        async with connect(url, additional_headers={"Origin": good}) as ws:
            await ws.send(json.dumps({"cmd": "read", "photo": False}))
            r = json.loads(await ws.recv())
            assert r["ok"] and r["card"]["cid"] == "1103700123456", r
            await ws.send(json.dumps({"cmd": "status"}))
            assert json.loads(await ws.recv())["readers"][0]["name"] == "FakeReader"
            await ws.send("not json at all")
            assert json.loads(await ws.recv())["error"] == "BadJSON"

        # the whole point of the allowlist: evil.example must be refused
        try:
            async with connect(url, additional_headers={"Origin": "https://evil.example"}):
                raise AssertionError("evil origin was allowed to connect")
        except websockets.exceptions.InvalidStatus as e:
            assert e.response.status_code == 403, e


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dev", action="store_true", help="also accept file:// pages (Origin: null)")
    p.add_argument("--selftest", action="store_true", help="run the built-in checks")
    args = p.parse_args()

    if args.selftest:
        selftest()
        return 0
    try:
        asyncio.run(serve(args.dev))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
