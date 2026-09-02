# thaiid-agent

Read a Thai national ID card from a web page, and print the copy people sign —
the way a bank does it, instead of photocopying the card.

Run the binary, open <http://127.0.0.1:8765>, insert a card.

```
┌─ browser ──────────────┐        ┌─ thaiid-agent ─┐
│ http://127.0.0.1:8765  │──ws────│  same port     │──USB──▶ PC/SC reader
└────────────────────────┘        └────────────────┘
```

## Why an agent at all

A web page cannot talk to a smartcard reader. WebUSB refuses smart-card
interface class `0x0B` outright, and the one API built for the job —
`navigator.smartCard` — requires an Isolated Web App and exists only on
ChromeOS. So something native has to own the reader.

The page is served **from the agent's own port**, which matters more than it
sounds: page and socket are then the same origin, so Chrome's Local Network
Access permission never applies and there is nothing to allow. Point a page
hosted elsewhere at it and you are back to needing that permission.

## Install

Download the binary for your platform from
[Releases](https://github.com/ninyawee/thaiid-agent/releases). They are
unsigned, so the OS will object once:

| | prerequisites | first run | run at login |
|---|---|---|---|
| **macOS** | none — PC/SC is built in | `xattr -d com.apple.quarantine thaiid-agent-macos-*` | `packaging/org.thaiid.agent.plist` |
| **Windows** | none — SCardSvr ships with Windows | SmartScreen → More info → Run anyway | `packaging\install-windows.ps1` |
| **Linux** | `sudo apt install pcscd libpcsclite1`<br>`sudo systemctl enable --now pcscd` | `chmod +x thaiid-agent` | `packaging/thaiid-agent.service` |

Each packaging file carries its own install steps in a comment at the top.

### From source

```sh
uv run thaiid-agent.py            # PEP 723 header pulls the deps
./thaiid-agent.py --selftest      # no hardware, no network
```

On Linux add `libpcsclite-dev` first: pyscard publishes wheels for macOS and
Windows only, so Linux compiles it and needs the headers. The released binaries
do not.

### Build a binary

```sh
pip install pyinstaller pythaiidcard websockets
pyinstaller packaging/thaiid-agent.spec
./dist/thaiid-agent --selftest
```

Build on the OS you are targeting — PyInstaller does not cross-compile. CI does
all four in a matrix and smoke-tests each one.

## What it reads

Personal data and the chip photo on every scan. Two extras are opt-in per read
because each sits on its own applet and can be missing from a card:

- **Laser number** — engraved on the *back*, so a photocopy of the front misses
  it. It is what proves the card is genuine.
- **สปสช. entitlement** — main/sub scheme, hospital, dates. **Health data
  under PDPA s.26**, so it needs consent of its own; the page has a switch for
  it rather than collecting it silently.

Neither is ever printed on the copy. The sheet stands in for a photocopy, and a
photocopy of the front shows neither — printing them would make the replacement
more dangerous than the thing it replaces.

A failure on either comes back as `laser_error` / `nhso_error` and never costs
you an otherwise good scan.

## The printed copy

A4 **สำเนาบัตรประจำตัวประชาชน**: photo, CID, both names, dates in Buddhist era,
registered address, a certification sentence and signature lines for cardholder,
recipient and date.

Fill in **ใช้สำหรับ** before printing. It prints in a box *and* diagonally
across the sheet — that is the part that stops a loose copy being reused for
something else.

## Protocol

```
-> {"cmd":"read","photo":true,"laser":true,"nhso":true}
<- {"ok":true,"card":{"cid":"…","thai_fullname":"…","photo":"data:image/jpeg;base64,…",
                      "laser_id":"…","nhso":{…}}}
-> {"cmd":"status"}   <- {"ok":true,"readers":[{"index":0,"name":"…","card":true}]}
<- {"event":"card","present":true}      pushed on insert/removal
```

Errors are `{"ok":false,"error":"CardConnectionError","detail":"…"}`. Note that
an **empty reader reports `CardConnectionError`**, PC/SC `0x8010000C`, not
`NoCardDetectedError` — that is the commonest case of all, so match on it.

## Security

- Binds **loopback only**. Never `0.0.0.0`.
- **Origin allowlist.** Without it any page you visit could connect and read a
  card you left in the reader. The built-in page is same-origin and allowed;
  anything else must be named in `THAIID_ORIGINS` (comma-separated). Every
  handshake's `Origin` is logged, so a mismatch names its own fix.
- **No credentials, and nothing written to disk.** It reads a card and returns
  JSON. The photo stays in memory as a data URL; the library's own
  `save_photo()` is never called.

### It cannot be used from an embedded iframe

`local-network-access` is a Permissions-Policy feature, and an embedder must
delegate it. Grist's custom-widget iframe, for one, carries
`allow="clipboard-write"` and nothing else, so a socket opened from inside it
hangs forever — no error, no prompt, nothing reaching the agent. Filed upstream
as [grist-core#2550](https://github.com/gristlabs/grist-core/issues/2550). Use a
popup (a top-level context) and `postMessage` the result back.

## Environment

| | |
|---|---|
| `THAIID_ORIGINS` | extra allowed origins, comma-separated |
| `THAIID_PORT` | default `8765` |
| `THAIID_HOST` | default `127.0.0.1` |

## Known upstream bug

`pythaiidcard`'s `Name.from_raw()` splits the raw name on `#`. A real card came
back space-separated with no `#`, so the whole name landed in `prefix` and
`first_name` / `last_name` were empty. Do not depend on those two fields without
checking against a real card — use `thai_fullname`.

## Licence

ISC. Built on [pythaiidcard](https://github.com/ninyawee/pythaiidcard).
