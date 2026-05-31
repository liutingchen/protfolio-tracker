"""Send email via Resend (HTTPS API) or SMTP, with a dev/console fallback.

Provider order:
  1. Resend HTTP API  — set RESEND_API_KEY (recommended on Railway/Render/Fly,
     which block outbound SMTP ports 25/465/587). Sends over HTTPS (443).
  2. SMTP             — set SMTP_HOST etc. (works on hosts that allow SMTP).
  3. Console (dev)    — neither configured: print the email + link to the log.

Env vars:
    RESEND_API_KEY  Resend API key (re_...)
    EMAIL_FROM      From address. Resend needs a verified domain, OR use
                    'onboarding@resend.dev' to send to your own account email.
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_SSL
"""
import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage


def email_configured():
    return bool(os.environ.get("RESEND_API_KEY") or os.environ.get("SMTP_HOST"))


def _default_sender():
    return (os.environ.get("EMAIL_FROM") or os.environ.get("SMTP_USER")
            or "onboarding@resend.dev")


def _send_resend(to, subject, text, html):
    key = os.environ.get("RESEND_API_KEY")
    payload = {
        "from": _default_sender(), "to": [to],
        "subject": subject, "text": text,
    }
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.status not in (200, 201):
            raise RuntimeError(f"Resend HTTP {r.status}: {r.read()[:200]}")
    return True


def _send_smtp(to, subject, text, html):
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")

    msg = EmailMessage()
    msg["From"] = _default_sender()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    ctx = ssl.create_default_context()
    if os.environ.get("SMTP_SSL") == "1" or port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if user:
                s.login(user, password)
            s.send_message(msg)
    return True


def send_email(to, subject, text, html=None):
    if os.environ.get("RESEND_API_KEY"):
        return _send_resend(to, subject, text, html)
    if os.environ.get("SMTP_HOST"):
        return _send_smtp(to, subject, text, html)
    print("\n" + "=" * 64)
    print(f"[DEV EMAIL — no mail provider configured]\nTo:      {to}\nSubject: {subject}\n")
    print(text)
    print("=" * 64 + "\n", flush=True)
    return True


def _template(title, intro, button_label, link, footer):
    text = f"{intro}\n\n{link}\n\n{footer}"
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:480px;margin:24px auto;color:#1a1a1a">
  <h2 style="margin:0 0 12px">{title}</h2>
  <p style="margin:0 0 18px;color:#444;line-height:1.5">{intro}</p>
  <p style="margin:0 0 20px">
    <a href="{link}" style="background:#f0b90b;color:#000;text-decoration:none;padding:11px 22px;
       border-radius:8px;font-weight:600;display:inline-block">{button_label}</a>
  </p>
  <p style="margin:0 0 6px;color:#888;font-size:12px">或把下面的链接复制到浏览器打开：</p>
  <p style="margin:0 0 18px;word-break:break-all;font-size:12px"><a href="{link}">{link}</a></p>
  <p style="margin:0;color:#999;font-size:12px">{footer}</p>
</div>"""
    return text, html


def send_password_reset(to, link):
    text, html = _template(
        "重置密码",
        "我们收到了重置你 Portfolio Tracker 密码的请求。点击下面的按钮设置新密码。",
        "重置密码", link,
        "如果这不是你本人操作，忽略此邮件即可，密码不会改变。链接 1 小时内有效。")
    return send_email(to, "重置你的 Portfolio Tracker 密码", text, html)
