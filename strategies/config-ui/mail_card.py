"""One plain, email-safe card layout for every SRSI email (inline styles and tables only, so Gmail keeps it).

card(label=..., tone="red"|"green"|"amber"|"grey", title=..., subtitle=..., big=..., big_tone=...,
     rows=[(name, value), ...], table=(headers, [[cells], ...]), note=..., image_cid=...)
Cells and values are plain text (escaped). A cell can be (text, tone) to colour it.
"""
from html import escape

TONES = {"red": "#c62828", "green": "#1b7f3b", "amber": "#a15c00", "grey": "#5f6673", "ink": "#1f2329"}
FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
LINE = "1px solid #eceef1"


def money(v):
    return ("−" if v < 0 else "+" if v > 0 else "") + f"₹{abs(v):,.2f}"


def tone_of(v):
    return "red" if v < 0 else "green" if v > 0 else "grey"


def _cell(value, align="left", head=False):
    text, tone = value if isinstance(value, tuple) else (value, None)
    color = TONES.get(tone, TONES["grey"] if head else TONES["ink"])
    style = (f"padding:7px 6px;border-top:{LINE};font-size:{11 if head else 13}px;color:{color};text-align:{align};"
             f"{'font-weight:600;letter-spacing:.03em;' if head else 'font-variant-numeric:tabular-nums;'}white-space:nowrap")
    return f'<td style="{style}">{escape(str(text))}</td>'


def card(label, tone="grey", title="", subtitle="", big=None, big_tone=None, rows=(), table=None, note="", image_cid=None, width=440):
    parts = [f'<div style="font-size:12px;color:{TONES[tone]};font-weight:600;letter-spacing:.04em">{escape(label)}</div>']
    if title:
        parts.append(f'<div style="font-size:20px;font-weight:600;color:{TONES["ink"]};margin-top:4px">{escape(title)}</div>')
    if subtitle:
        parts.append(f'<div style="font-size:13px;color:{TONES["grey"]};margin-top:2px">{escape(subtitle)}</div>')
    head = f'<tr><td style="padding:20px 22px 6px">{"".join(parts)}</td></tr>'
    body = ""
    if big is not None:
        body += (f'<div style="font-size:26px;font-weight:600;color:{TONES[big_tone or "ink"]};margin:2px 0 12px;'
                 f'font-variant-numeric:tabular-nums">{escape(big)}</div>')
    if rows:
        cell = f'style="padding:9px 0;border-top:{LINE};font-size:14px;color:{TONES["grey"]}"'
        body += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0">'
        for name, value in rows:
            text, vt = value if isinstance(value, tuple) else (value, None)
            body += (f'<tr><td {cell}>{escape(str(name))}</td><td style="padding:9px 0;border-top:{LINE};font-size:14px;'
                     f'color:{TONES.get(vt, TONES["ink"])};text-align:right;font-variant-numeric:tabular-nums">{escape(str(text))}</td></tr>')
        body += "</table>"
    if table:
        headers, data = table
        body += '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px">'
        body += "<tr>" + "".join(_cell(h, "right" if i >= len(headers) - 1 else "left", True) for i, h in enumerate(headers)) + "</tr>"
        for r in data:
            body += "<tr>" + "".join(_cell(c, "right" if i >= len(r) - 1 else "left") for i, c in enumerate(r)) + "</tr>"
        body += "</table>"
    if image_cid:
        body += f'<img src="cid:{escape(image_cid)}" alt="chart" width="{width - 44}" style="display:block;width:100%;max-width:{width - 44}px;margin-top:14px;border:0">'
    mid = f'<tr><td style="padding:10px 22px 6px">{body}</td></tr>' if body else ""
    foot = (f'<tr><td style="padding:12px 22px 18px;font-size:13px;color:{TONES["grey"]};border-top:{LINE}">{escape(note)}</td></tr>'
            if note else '<tr><td style="padding:6px"></td></tr>')
    return (f'<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f4f5f7;font-family:{FONT}">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:{width}px;background:#ffffff;'
            f'border:1px solid #e3e6ea;border-radius:10px">{head}{mid}{foot}</table>'
            f'<div style="font-size:11px;color:#9aa0a8;margin-top:10px">SRSI · OpenAlgo</div></td></tr></table></body></html>')
