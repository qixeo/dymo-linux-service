# dymo-linux-service

A stand-in for the **DYMO Connect web service** on Linux.

DYMO only ships DYMO Connect (and the local web service that lets a web page
print to a LabelWriter) for Windows and macOS. This script speaks the same
HTTPS API on the same port, `https://127.0.0.1:41951/DYMO/DLS/Printing/…`, so
a web app that prints "through DYMO Connect" works on a Linux workstation
without any change to the app.

How it works:

- the browser finds the service the way it finds DYMO Connect (it scans
  ports 41951–41960 on `127.0.0.1`);
- the service renders the label XML the app sends (the `.dymo` format DYMO
  Connect saves) with Pillow: text objects, address objects and QR codes;
- the rendered label goes to the LabelWriter through CUPS (`lp`), on the page
  size matching the label, using the open-source DYMO CUPS driver.

One Python file, standard library plus Pillow and `qrcode`.

## Install

Tested path: Ubuntu / Debian, Chrome or Chromium, a USB LabelWriter.

**Run:**

```bash
curl -fsSL https://raw.githubusercontent.com/qixeo/dymo-linux-service/main/install.sh | bash
```

Or, from a checkout, `./install.sh`. Either way it installs the apt packages,
copies the service to `~/.local/share/dymo-linux-service`, creates and trusts
the certificate, and starts the systemd user service. Then add the LabelWriter
as a printer if you haven't, and restart the browser.

**Manual steps**:

```bash
# 1. CUPS, the DYMO driver, Python deps, and certutil (to trust the certificate in Chrome)
sudo apt install cups printer-driver-dymo python3-pil python3-qrcode libnss3-tools

# 2. add the LabelWriter as a printer (Settings → Printers, or http://localhost:631).
#    It should show up in:
lpstat -v

# 3. get the service
git clone <this repo> ~/dymo-linux-service
cd ~/dymo-linux-service

# 4. check that it sees the printer and can render a label
./dymo-linux-service.py printers
./dymo-linux-service.py render examples/work-order.dymo /tmp/label.png   # then look at /tmp/label.png

# 5. install: creates the certificate, trusts it in Chrome, installs and starts a
#    systemd user service that runs whenever you are logged in
./dymo-linux-service.py install
```

Then **restart the browser** (so it picks up the trusted certificate) and open
<https://127.0.0.1:41951/DYMO/DLS/Printing/StatusConnected>. It should show
`true` with no certificate warning. In the app, "Print Label" now lists the
LabelWriter.

The first time the app talks to the service, Chrome asks whether the site may
connect to devices on your local network. Choose **Allow**. (If you dismissed
it: click the icon left of the address bar → Site settings → Local network
access → Allow, then reload.)

### Firefox

Firefox keeps its own certificate store. Import the certificate once:
Settings → Privacy & Security → Certificates → View Certificates →
Authorities → Import → `~/.config/dymo-linux-service/cert.pem` → tick "Trust
this CA to identify websites". Or, simpler, open the StatusConnected link
above and accept the warning permanently.

## Commands

| Command                                          | What it does                                                            |
| ------------------------------------------------ | ----------------------------------------------------------------------- |
| `dymo-linux-service.py` / `serve [--port N]`     | run in the foreground (what the systemd unit runs)                      |
| `install [--port N]`                             | certificate + Chrome trust + systemd user unit, started and enabled     |
| `trust [--regenerate]`                           | (re)import the certificate into every Chrome/Chromium NSS store found   |
| `printers`                                       | the printers the service offers, with their CUPS state and USB presence |
| `render LABEL.dymo OUT.png [--dpi N]`            | render a label file to a PNG to check the layout                        |
| `print LABEL.dymo [--printer NAME] [--copies N]` | print a label file through CUPS                                         |

Service management after install:

```bash
systemctl --user status dymo-linux-service
systemctl --user restart dymo-linux-service
journalctl --user -u dymo-linux-service -f
```

## Configuration

Environment variables; the systemd unit reads them from
`~/.config/dymo-linux-service/env` (restart the service after editing).

| Variable              | Default             | Meaning                                                                                                                                                                  |
| --------------------- | ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `DYMO_LINUX_PORT`     | `41951`             | listen port; DYMO clients scan 41951–41960                                                                                                                               |
| `DYMO_LINUX_PRINTERS` | `dymo`              | which CUPS queues to offer: `dymo` (name, model or device URI mentions DYMO), `all`, or a comma-separated list of queue names                                            |
| `DYMO_LINUX_MEDIA`    | auto                | CUPS media name to print on. By default the queue's `PageSize` choice closest to the label is used (`w162h90` for the 30334 2¼"×1¼" label), falling back to `Custom.WxH` |
| `DYMO_LINUX_ROTATE`   | `0`                 | extra rotation in degrees (`0`/`90`/`180`/`270`) applied before printing, on top of the 90° a Landscape label gets                                                       |
| `DYMO_LINUX_DPI`      | `300`               | print resolution (LabelWriters are 300 dpi)                                                                                                                              |
| `DYMO_LINUX_FONT`     | fontconfig's choice | font family used for every text object, e.g. `Liberation Sans`                                                                                                           |
| `DYMO_LINUX_DRY_RUN`  |                     | `1`: keep the PDF and log the `lp` command instead of printing                                                                                                           |

Fonts: labels designed in DYMO Connect say "Helvetica". Linux maps that
through fontconfig, usually to Nimbus Sans or Liberation Sans, which have the
same metrics. Install `fonts-liberation` if the text looks off.

## Troubleshooting

**The app says it can't reach DYMO Connect.**
`systemctl --user status dymo-linux-service`, then open the StatusConnected
link above. If the browser shows a certificate warning, run
`./dymo-linux-service.py trust` and restart the browser (or accept the
warning). If the page doesn't load at all, the service isn't running; run
`./dymo-linux-service.py serve` in a terminal to see why.

**No printers listed.** `./dymo-linux-service.py printers`. If CUPS knows the
printer but it isn't offered, its queue name, model and URI don't mention
DYMO: set `DYMO_LINUX_PRINTERS=all` or name the queue.

**Printer shows "offline".** The CUPS queue is disabled or paused
(`cupsenable NAME`), or the USB device with that serial isn't plugged in.

**Label prints on the wrong size / clipped.** `lpoptions -p NAME -l | grep
PageSize` lists what the driver knows; pin one with `DYMO_LINUX_MEDIA`. Make
sure the roll in the printer matches the label the template was designed for.

**Label prints sideways or upside down.** `DYMO_LINUX_ROTATE=180` (or 90/270).

**Text too big or small.** DYMO's "AlwaysFit" / "ShrinkToFit" sizing is
reproduced by measuring the text in the box; a different font than DYMO's
Helvetica can shift it slightly. Try `DYMO_LINUX_FONT="Liberation Sans"`.

## Limitations

- Supports **TextObject**, **AddressObject** and **QRCodeObject**. Barcodes,
  images and shapes are skipped (logged as a warning).
- Only DYMO Connect's `.dymo` format (`<DesktopLabel>`), not the older DYMO
  Label v8 `.label` format.
- Text layout approximates DYMO Connect's: one font per line, block-level
  fit-to-box scaling, no rotation, no vertical text.
- Printer "connected" means the CUPS queue is enabled and, for USB printers,
  the device is present. It doesn't check paper or cover state.
- Verified against the real DYMO Connect service's response formats and
  rendering on macOS, and dry-run through CUPS page-size matching. Printing on
  a physical LabelWriter from Linux still needs its first real test: if the
  label comes out shifted, clipped or rotated, `DYMO_LINUX_MEDIA` and
  `DYMO_LINUX_ROTATE` are the knobs.

## The API, for reference

Everything is under `https://127.0.0.1:41951/DYMO/DLS/Printing/`. Responses
carry CORS headers for any origin, including Chrome's private/local network
access preflight.

| Endpoint              | Body                                                                          | Reply                                                                                                  |
| --------------------- | ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `GET StatusConnected` |                                                                               | `true`                                                                                                 |
| `GET GetPrinters`     |                                                                               | `<Printers><LabelWriterPrinter><Name>…</Name><ModelName>…</ModelName><IsConnected>True</IsConnected>…` |
| `POST RenderLabel`    | form: `labelXml`, `renderParamsXml`, `printerName`                            | JSON string of base64 PNG                                                                              |
| `POST PrintLabel`     | form: `printerName`, `labelXml`, `printParamsXml` (`<Copies>`), `labelSetXml` | `true`                                                                                                 |

`Name` is the CUPS queue name; send it back as `printerName`. Errors are a
`500` with a one-line message.
