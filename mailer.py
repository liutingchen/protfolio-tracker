"""Send email via SMTP, with a dev/console fallback.

Configure with env vars (any provider that offers SMTP works — Gmail, Resend,
SendGrid, Mailgun, SES, …):
    SMTP_HOST       e.g. smtp.gmail.com
    SMTP_PORT       default 587 (STARTTLS); use 465 for SSL
    SMTP_USER       login user
    SMTP_PASSWORD   login password / app password / API key
    EMAIL_FROM      From address (default = SMTP_USER)
    SMTP_SSL        "1" to force SSL (port 465)

If SMTP_HOST is unset, emails are printed to the server log (dev mode) so the
verify/reset links are still retrievable without configuring a mail server.
"""
import os
import smtplib
import ssl
from email.message import EmailMessage


def email_configured():
    return bool(os.environ.get("SMTP_HOST"))


def send_email(to, subject, text, html=None):
    host = os.environ.get("SMTP_HOST")
    if not host:
        print("\n" + "=" * 64)
        print(f"[DEV EMAIL — SMTP not configured]\nTo:      {to}\nSubject: {subject}\n")
        print(text)
        print("=" * 64 + "\n", flush=True)
        return True

    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    sender = os.environ.get("EMAIL_FROM") or user or "no-reply@localhost"

    msg = EmailMessage()
    msg["From"] = sender
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


def send_verification(to, link):
    text, html = _template(
        "验证你的邮箱",
        "感谢注册 Portfolio Tracker！点击下面的按钮验证邮箱，验证后即可登录。",
        "验证邮箱", link,
        "如果这不是你本人操作，忽略此邮件即可。链接 24 小时内有效。")
    return send_email(to, "验证你的 Portfolio Tracker 邮箱", text, html)


def send_password_reset(to, link):
    text, html = _template(
        "重置密码",
        "我们收到了重置你 Portfolio Tracker 密码的请求。点击下面的按钮设置新密码。",
        "重置密码", link,
        "如果这不是你本人操作，忽略此邮件即可，密码不会改变。链接 1 小时内有效。")
    return send_email(to, "重置你的 Portfolio Tracker 密码", text, html)
