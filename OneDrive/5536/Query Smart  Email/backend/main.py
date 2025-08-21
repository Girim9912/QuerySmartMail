# main.py
import os, smtplib, imaplib, email, ssl, re
from email.message import EmailMessage
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Body, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --------- ENV VARS ----------
SMTP_HOST = os.getenv("SMTP_HOST", "smtp-relay.brevo.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("BREVO_SMTP_USER")      # your Brevo login (email)
SMTP_PASS = os.getenv("BREVO_SMTP_PASS")      # your Brevo SMTP key

IMAP_HOST = os.getenv("IMAP_HOST", "outlook.office365.com")  # or imap-mail.outlook.com
IMAP_USER = os.getenv("IMAP_USER")            # your Hotmail address (where Cloudflare forwards)
IMAP_PASS = os.getenv("IMAP_PASS")            # your Hotmail password (or App Password if 2FA)
FROM_ADDR = os.getenv("FROM_ADDR", "info@querysmartaillc.com")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")        # choose any strong random string

if not (SMTP_USER and SMTP_PASS and IMAP_USER and IMAP_PASS and ADMIN_TOKEN):
    print("WARNING: Some required env vars are missing. Set BREVO_SMTP_USER, BREVO_SMTP_PASS, IMAP_USER, IMAP_PASS, ADMIN_TOKEN.")

app = FastAPI(title="QuerySmart Email Backend")

# CORS (allow your site)
FRONTEND_ORIGIN = os.getenv("FRONTEND_ORIGIN", "https://querysmartaillc.com")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "https://girim9912.github.io"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def require_admin_token(token: Optional[str]):
    if not token or token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

class SendEmailIn(BaseModel):
    to: List[str]
    subject: str
    html: Optional[str] = None
    text: Optional[str] = None
    cc: Optional[List[str]] = None
    bcc: Optional[List[str]] = None

@app.post("/api/email/send")
def send_email(
    payload: SendEmailIn,
    x_admin_token: Optional[str] = Header(default=None)
):
    require_admin_token(x_admin_token)

    if not payload.text and not payload.html:
        raise HTTPException(400, "Provide text or html body")

    msg = EmailMessage()
    msg["From"] = FROM_ADDR
    msg["To"] = ", ".join(payload.to)
    if payload.cc: msg["Cc"] = ", ".join(payload.cc)
    if payload.bcc: msg["Bcc"] = ", ".join(payload.bcc)
    msg["Subject"] = payload.subject

    if payload.html and payload.text:
        msg.set_content(payload.text)
        msg.add_alternative(payload.html, subtype="html")
    elif payload.html:
        msg.set_content(re.sub("<[^<]+?>", "", payload.html))
        msg.add_alternative(payload.html, subtype="html")
    else:
        msg.set_content(payload.text)

    # TIP: BCC yourself so “Sent” appears in your Hotmail inbox
    if payload.bcc is None:
        msg["Bcc"] = FROM_ADDR

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls(context=ssl.create_default_context())
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"SMTP send failed: {e}")

def _imap_login():
    M = imaplib.IMAP4_SSL(IMAP_HOST)
    M.login(IMAP_USER, IMAP_PASS)
    return M

def _fetch_headers(M, mailbox="INBOX", limit=50, from_filter=None):
    M.select(mailbox)
    search_criteria = '(ALL)'
    if from_filter:
        search_criteria = f'(FROM "{from_filter}")'
    typ, data = M.search(None, search_criteria)
    if typ != "OK":
        return []

    ids = data[0].split()
    ids = ids[-limit:]  # last N
    messages = []
    for uid in reversed(ids):
        typ, msg_data = M.fetch(uid, "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])")
        if typ != "OK" or not msg_data or msg_data[0] is None:
            continue
        raw = msg_data[0][1]
        try:
            hdr = email.message_from_bytes(raw)
        except:
            continue
        subject = hdr.get("Subject", "(no subject)")
        sender = hdr.get("From", "")
        date = hdr.get("Date", "")
        mid = hdr.get("Message-ID", "")
        messages.append({
            "uid": uid.decode(),
            "subject": subject,
            "from": sender,
            "date": date,
            "message_id": mid
        })
    return messages

@app.get("/api/email/inbox")
def inbox(
    folder: str = Query(default="inbox", pattern="^(inbox|sent)$"),
    limit: int = 50,
    x_admin_token: Optional[str] = Header(default=None)
):
    require_admin_token(x_admin_token)
    try:
        M = _imap_login()
        # If folder=sent, we approximate by filtering INBOX messages from our own address (because SMTP is Brevo).
        if folder == "sent":
            messages = _fetch_headers(M, mailbox="INBOX", limit=limit, from_filter=FROM_ADDR)
        else:
            messages = _fetch_headers(M, mailbox="INBOX", limit=limit)
        M.logout()
        return {"ok": True, "messages": messages}
    except Exception as e:
        raise HTTPException(500, f"IMAP fetch failed: {e}")

@app.get("/api/email/message")
def get_message(
    id: str = Query(..., description="IMAP UID"),
    x_admin_token: Optional[str] = Header(default=None)
):
    require_admin_token(x_admin_token)
    try:
        M = _imap_login()
        M.select("INBOX")
        typ, msg_data = M.fetch(id.encode(), "(RFC822)")
        M.logout()
        if typ != "OK" or not msg_data or msg_data[0] is None:
            raise HTTPException(404, "Message not found")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        # Extract text/html parts
        body_text, body_html = "", ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                if ctype == "text/plain":
                    body_text += part.get_payload(decode=True).decode(errors="ignore")
                elif ctype == "text/html":
                    body_html += part.get_payload(decode=True).decode(errors="ignore")
        else:
            ctype = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                if ctype == "text/plain":
                    body_text = payload.decode(errors="ignore")
                elif ctype == "text/html":
                    body_html = payload.decode(errors="ignore")

        return {
            "ok": True,
            "subject": msg.get("Subject", ""),
            "from": msg.get("From", ""),
            "date": msg.get("Date", ""),
            "text": body_text.strip(),
            "html": body_html.strip()
        }
    except Exception as e:
        raise HTTPException(500, f"IMAP read failed: {e}")
